import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace as N

import pytest
from telegram.error import TelegramError

from app.bot import main as bot
from app.bot.pipeline import Reply, _try_media_reference
from app.config import get_settings
from app.db.models import MediaItem
from app.media.catalog import item_out
from app.media.contexts import ContextService
from app.media.preferences import PreferenceService
from app.agent.schemas import MediaReply
from tests.conftest import FIXED_NOW
from tests.media_fakes import add_media


class Msg:
    def __init__(self, chat_id=5, text=""):
        self.chat_id = chat_id
        self.text = text
        self.texts = []
        self.photos = []

    async def reply_text(self, text, **kw):
        self.texts.append((text, kw.get("reply_markup")))

    async def reply_photo(self, photo, caption=None, **kw):
        self.photos.append((photo, caption))
        raise TelegramError("cannot fetch poster")

    async def answer(self, *a, **kw):
        pass


def test_text_and_voice_both_enter_the_same_respond_pipeline(monkeypatch, tmp_path):
    calls = []

    async def capture(update, context, text, voice_id=None):
        calls.append((text, voice_id))

    monkeypatch.setattr(bot, "respond", capture)
    text_msg = Msg(text="یک فیلم کمدی پیشنهاد بده")
    asyncio.run(bot.text_message(N(message=text_msg), None))
    assert calls == [("یک فیلم کمدی پیشنهاد بده", None)]

    settings = get_settings()
    monkeypatch.setattr(settings, "audio_dir", str(tmp_path))
    monkeypatch.setattr(bot, "_store_voice", lambda *a, **k: 7)
    monkeypatch.setattr(bot, "_finish_voice", lambda *a, **k: None)
    monkeypatch.setattr(
        bot, "transcribe_ogg", lambda path: N(text="دومی را واچ‌لیست کن", confidence=0.9, usable=True)
    )

    class File:
        async def download_to_drive(self, path):
            Path(path).write_bytes(b"audio")

    class TgBot:
        async def get_file(self, file_id):
            return File()

    reply_texts = []

    async def reply_text(text, **kw):
        reply_texts.append(text)

    msg = N(
        voice=N(duration=5, file_id="f", file_unique_id="u"),
        chat_id=9,
        reply_text=reply_text,
    )
    asyncio.run(bot.voice(N(message=msg, effective_user=N(id=111)), N(bot=TgBot())))
    assert calls[-1] == ("دومی را واچ‌لیست کن", 7)
    assert reply_texts[0].startswith("🎙")


def test_respond_passes_the_chat_id_to_analyse(monkeypatch):
    seen = {}

    def fake_analyse(tg_user, text, **kw):
        seen.update(kw)
        return Reply("clarify", text="سؤال")

    monkeypatch.setattr(bot, "analyse", fake_analyse)
    msg = Msg(chat_id=42)
    bot_context = N(bot=N(send_chat_action=None))
    bot_context.user_data = {}

    async def send_chat_action(*a, **k):
        return None

    bot_context.bot.send_chat_action = send_chat_action
    asyncio.run(bot.respond(N(effective_message=msg, effective_user=N(id=111)), bot_context, "سلام"))
    assert seen["chat_id"] == 42


def test_send_media_falls_back_to_text_when_poster_fails(session, monkeypatch, user):
    with_poster = add_media(session, tmdb_id=400, title_fa="با پوستر")
    without = add_media(session, tmdb_id=401, title_fa="بی پوستر", poster_path=None)
    cards = [(item_out(with_poster), "دلیل واقعی"), (item_out(without), None)]
    monkeypatch.setattr(bot, "_load_media", lambda action, tg_user, chat_id: (cards, None))

    msg = Msg(chat_id=5)
    action = MediaReply(text="دو پیشنهاد:", items=[{"media_id": with_poster.id}])
    asyncio.run(bot.send_media(N(effective_message=msg, effective_user=N(id=111)), N(), action, None))
    assert msg.texts[0][0] == "دو پیشنهاد:"
    assert msg.photos and "با پوستر" in msg.photos[0][1]
    # poster failed -> text fallback for the first, plain text for the second
    assert len(msg.texts) == 3
    assert "با پوستر" in msg.texts[1][0] and "بی پوستر" in msg.texts[2][0]
    assert "دلیل واقعی" in msg.texts[1][0]


def test_media_callbacks_validate_ownership(session, user, monkeypatch):
    item = add_media(session, tmdb_id=410, title_fa="فیلم من")
    ContextService(session).record_list(user.id, 5, [item])
    monkeypatch.setattr(bot, "SessionLocal", lambda: contextlib.nullcontext(session))

    state, text, media_id = bot._media_work(N(id=111), 5, f"md:wl:{item.id}")
    assert state in ("saved", "already") and media_id == item.id
    assert item.id in PreferenceService(session).ids(user.id, "watchlist")

    # another user cannot act on a card that was shown in this user's chat
    other_item = add_media(session, tmdb_id=411, title_fa="فیلم دیگری")
    ContextService(session).record_list(user.id, 5, [other_item])
    state, text, _ = bot._media_work(N(id=222, full_name="Other"), 5, f"md:wl:{other_item.id}")
    assert state == "forbidden" and "پیشنهاد شما نیست" in text


def test_resolve_reference_second_item_and_no_calendar_false_match(session, user, monkeypatch):
    a = add_media(session, tmdb_id=420, title_fa="اول")
    b = add_media(session, tmdb_id=421, title_fa="دوم")
    ContextService(session).record_list(user.id, 5, [a, b])

    reply = _try_media_reference(session, user, 5, "دومی را به واچ‌لیست اضافه کن")
    assert reply is not None and reply.kind == "media"
    assert b.id in PreferenceService(session).ids(user.id, "watchlist")

    assert _try_media_reference(session, user, 5, "فردا ساعت ۲ جلسه بذار") is None
    assert _try_media_reference(session, user, 5, "ساعت ۲ را برای جلسه ذخیره کن") is None
    assert _try_media_reference(session, user, 5, "ایمیل را برای فردا بفرست") is None

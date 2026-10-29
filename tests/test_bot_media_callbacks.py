"""Media card and watchlist callback tests (no network, no real Telegram)."""

import asyncio
import contextlib
from types import SimpleNamespace as N

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import main as bot
from app.media.contexts import ContextService
from app.media.preferences import PreferenceService
from tests.media_fakes import add_media


class FakeMessage:
    def __init__(self, chat_id=5, text="", markup=None):
        self.chat_id = chat_id
        self.text = text
        self.reply_markup = markup
        self.edited_markup = "__unchanged__"

    async def edit_message_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup
        self.edited_markup = reply_markup

    async def edit_message_text(self, text, **kw):
        self.text = text
        self.reply_markup = kw.get("reply_markup")


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        await self.message.edit_message_reply_markup(reply_markup)

    async def edit_message_text(self, text, **kw):
        await self.message.edit_message_text(text, **kw)


def make_update(data, message, user_id=111):
    return N(
        callback_query=FakeQuery(data, message),
        effective_user=N(id=user_id),
        effective_message=message,
    )


def callback_data(markup):
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


def test_media_vote_removes_both_like_and_dislike(session, user, monkeypatch):
    item = add_media(session, tmdb_id=500, title_fa="فیلم تست")
    ContextService(session).record_list(user.id, 5, [item])
    monkeypatch.setattr(bot, "SessionLocal", lambda: contextlib.nullcontext(session))

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍", callback_data=f"md:lk:{item.id}"),
                InlineKeyboardButton("👎", callback_data=f"md:dl:{item.id}"),
            ],
            [InlineKeyboardButton("➕ واچ‌لیست", callback_data=f"md:wl:{item.id}")],
        ]
    )
    msg = FakeMessage(markup=keyboard)
    update = make_update(f"md:lk:{item.id}", msg)

    asyncio.run(bot.on_button(update, N(user_data={})))

    data = callback_data(msg.edited_markup)
    assert f"md:lk:{item.id}" not in data and f"md:dl:{item.id}" not in data
    assert f"md:wl:{item.id}" in data  # بقیهٔ دکمه‌ها دست‌نخورده می‌مانند
    assert item.id in PreferenceService(session).ids(user.id, "liked")


def test_watchlist_remove_button_updates_the_same_message(session, user, monkeypatch):
    item = add_media(session, tmdb_id=501, title_fa="سریال تست", media_type="tv")
    PreferenceService(session).watchlist_add(user.id, item.id)
    monkeypatch.setattr(bot, "SessionLocal", lambda: contextlib.nullcontext(session))

    msg = FakeMessage()
    update = make_update(f"wl:rm:{item.id}", msg)

    asyncio.run(bot.on_button(update, N(user_data={})))

    assert msg.text == "لیست شما خالی است 📭"
    assert PreferenceService(session).watchlist(user.id) == []

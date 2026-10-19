import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.agent.executor import cancel_run, confirm_run
from app.bot import messages
from app.bot.pipeline import Reply, analyse, get_or_create_user
from app.bot.preview import confirmation_keyboard, feedback_keyboard, format_preview
from app.bot.cleanup import cleanup_loop
from app.bot.heartbeat import heartbeat_loop
from app.bot.ratelimit import RateLimiter
from app.config import get_settings
from app.db.models import AgentRun, AuditEvent, RunFeedback, VoiceMessage
from app.db.session import SessionLocal
from app.logging_config import setup_logging
from app.stt.vosk_stt import SttError, transcribe_ogg

log = logging.getLogger("bot")
_limiter: RateLimiter | None = None


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler; stops everyone not on the allowlist."""
    global _limiter
    user = update.effective_user
    if user is not None and user.id in get_settings().allowed_ids:
        if _limiter is None:
            _limiter = RateLimiter(get_settings().rate_limit_per_minute)
        if _limiter.allow(user.id):
            return
        log.warning("Rate limited user %s", user.id)
        if update.callback_query is not None:
            await update.callback_query.answer(messages.RATE_LIMITED, show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(messages.RATE_LIMITED)
        raise ApplicationHandlerStop
    log.warning("Refused user %s", user.id if user else None)
    if update.effective_message is not None:
        await update.effective_message.reply_text(messages.REFUSED)
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.START)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.HELP)


# ---------- agent replies ----------


async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, voice_id: int | None = None):
    """Run the agent on `text` and send either a question or a confirmable preview."""
    msg = update.effective_message
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    editing = context.user_data.pop("editing_run", None)
    reply: Reply = await asyncio.to_thread(
        analyse, update.effective_user, text, voice_message_id=voice_id, editing_run_id=editing
    )
    if reply.kind == "clarify":
        await msg.reply_text(reply.text, reply_markup=feedback_keyboard(reply.run_id))
    elif reply.kind == "blocked":
        await msg.reply_text(messages.BUDGET_STOP)
    elif reply.kind == "failed":
        await msg.reply_text(messages.AGENT_FAILED)
    else:
        from app.util.telegram_text import message_parts

        parts = message_parts(format_preview(reply.action))
        for part in parts[:-1]:
            await msg.reply_text(part)
        await msg.reply_text(parts[-1], reply_markup=confirmation_keyboard(reply.run_id))


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await respond(update, context, update.message.text)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data.startswith("fb:"):
        await _feedback(update, context)
        return
    action, _, raw_id = query.data.partition(":")
    run_id = int(raw_id)
    await query.edit_message_reply_markup(reply_markup=None)  # buttons vanish on the first tap

    if action == "ed":
        context.user_data["editing_run"] = run_id
        await query.message.reply_text(messages.EDIT_PROMPT)
        return

    def work():
        with SessionLocal() as session:
            user = get_or_create_user(session, update.effective_user)
            fn = confirm_run if action == "ok" else cancel_run
            return fn(session, user, run_id)

    if action == "ok":
        await query.message.reply_text(messages.CONFIRMING)
    result = await asyncio.to_thread(work)
    await query.message.reply_text(result.message, reply_markup=feedback_keyboard(run_id))


def _save_feedback(tg_user, run_id: int, rating: int) -> bool:
    with SessionLocal() as session:
        user = get_or_create_user(session, tg_user)
        run = session.get(AgentRun, run_id)
        if run is None or run.user_id != user.id:
            return False
        row = session.scalar(
            select(RunFeedback).where(RunFeedback.run_id == run_id, RunFeedback.user_id == user.id)
        )
        if row is None:
            session.add(RunFeedback(run_id=run_id, user_id=user.id, rating=rating))
        else:
            row.rating = rating
        session.commit()
        return True


async def _feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, raw_id, raw_rating = query.data.split(":")
    saved = await asyncio.to_thread(_save_feedback, update.effective_user, int(raw_id), int(raw_rating))
    await query.answer(messages.FEEDBACK_THANKS if saved else messages.ERROR)
    # drop only the thumbs row; keep Confirm/Edit/Cancel if they are still there
    rows = list(query.message.reply_markup.inline_keyboard) if query.message.reply_markup else []
    remaining = [r for r in rows if not any(b.callback_data.startswith("fb:") for b in r)]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(remaining) if remaining else None)


# ---------- voice ----------


def _store_voice(tg_user, file_id: str, duration: int | None, path: str) -> int:
    with SessionLocal() as session:
        user = get_or_create_user(session, tg_user)
        voice = VoiceMessage(
            user_id=user.id,
            telegram_file_id=file_id,
            duration_s=duration,
            audio_path=path,
        )
        session.add(voice)
        session.flush()
        session.add(
            AuditEvent(
                event_type="voice_received",
                actor=f"telegram:{tg_user.id}",
                payload_json={"voice_message_id": voice.id, "duration_s": duration},
            )
        )
        session.commit()
        return voice.id


def _finish_voice(voice_id: int, text: str, confidence: float) -> None:
    with SessionLocal() as session:
        row = session.get(VoiceMessage, voice_id)
        row.transcript = text
        row.stt_confidence = confidence
        row.audio_path = None  # audio is deleted right after transcription (SPEC)
        session.commit()


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    tg_voice = msg.voice
    max_s = get_settings().max_voice_seconds
    if (tg_voice.duration or 0) > max_s:
        await msg.reply_text(messages.VOICE_TOO_LONG.format(max=max_s))
        return
    audio_dir = Path(get_settings().audio_dir) / str(update.effective_user.id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / f"{tg_voice.file_unique_id}.ogg"

    try:
        tg_file = await context.bot.get_file(tg_voice.file_id)
        await tg_file.download_to_drive(path)
        voice_id = await asyncio.to_thread(
            _store_voice, update.effective_user, tg_voice.file_id, tg_voice.duration, str(path)
        )
        try:
            result = await asyncio.to_thread(transcribe_ogg, path)
        except SttError:
            log.exception("STT failed for voice %s", voice_id)
            await msg.reply_text(messages.STT_FAILED)
            return
    finally:
        path.unlink(missing_ok=True)

    await asyncio.to_thread(_finish_voice, voice_id, result.text, result.confidence)
    log.info("Voice %s transcribed (confidence %.2f)", voice_id, result.confidence)
    if not result.usable:
        await msg.reply_text(messages.REPEAT)
        return
    await msg.reply_text(messages.TRANSCRIPT.format(text=result.text))
    await respond(update, context, result.text, voice_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(messages.ERROR)


async def _start_cleanup(app: Application) -> None:
    s = get_settings()
    app.bot_data["cleanup_task"] = asyncio.create_task(cleanup_loop(s.audio_dir, s.audio_max_age_s))
    app.bot_data["heartbeat_task"] = asyncio.create_task(heartbeat_loop(s.bot_heartbeat_file))


async def _stop_cleanup(app: Application) -> None:
    for key in ("cleanup_task", "heartbeat_task"):
        task = app.bot_data.get(key)
        if task:
            task.cancel()


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set.")
    if not settings.allowed_ids:
        raise SystemExit("ALLOWED_USER_IDS is not set; refusing to start an open bot.")

    builder = Application.builder().token(settings.telegram_bot_token)
    if settings.telegram_api_base_url:
        base = settings.telegram_api_base_url.rstrip("/")
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
    if settings.telegram_proxy_url:
        builder = builder.proxy(settings.telegram_proxy_url).get_updates_proxy(settings.telegram_proxy_url)
    app = builder.post_init(_start_cleanup).post_shutdown(_stop_cleanup).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^((ok|no|ed):\d+|fb:\d+:-?1)$"))
    app.add_handler(MessageHandler(filters.VOICE, voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    setup_logging(get_settings().log_level)
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

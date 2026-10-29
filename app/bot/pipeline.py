"""Blocking glue between Telegram updates and the agent. Called via asyncio.to_thread."""

import threading
from dataclasses import dataclass

from sqlalchemy import select

from app import budget
from app.agent.agent import process_message
from app.agent.executor import supersede_run
from app.agent.memory import build_context
from app.agent.llm import DeepSeekProvider, LLMError, LLMProvider
from app.agent.schemas import Action, EmailAction, MediaReply, ScheduleAction
from app.config import get_settings
from app.db.models import AgentRun, RunStatus, User
from app.db.session import SessionLocal
from app.media import container
from app.media.references import (
    contains_ordinal_word,
    media_action_from_text,
    parse_ordinal,
    resolve_position,
)
from app.util.persian import normalize

_provider: LLMProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> LLMProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = DeepSeekProvider()
        return _provider


def get_or_create_user(session, tg_user) -> User:
    user = session.scalar(select(User).where(User.telegram_user_id == tg_user.id))
    if user is None:
        user = User(
            telegram_user_id=tg_user.id,
            name=tg_user.full_name,
            timezone=get_settings().default_timezone,
        )
        session.add(user)
        session.flush()
    return user


@dataclass
class Reply:
    kind: str  # schedule | email | media | clarify | failed | blocked
    run_id: int | None = None
    text: str | None = None  # clarification question / short media answer
    action: Action | None = None


def analyse(
    tg_user,
    text: str,
    *,
    chat_id: int = 0,
    voice_message_id: int | None = None,
    editing_run_id: int | None = None,
) -> Reply:
    with SessionLocal() as session:
        user = get_or_create_user(session, tg_user)
        return analyse_for_user(
            session,
            user,
            text,
            chat_id=chat_id,
            voice_message_id=voice_message_id,
            editing_run_id=editing_run_id,
        )


def _try_media_reference(session, user: User, chat_id: int, text: str) -> Reply | None:
    """Deterministic fast path for «دومی را به واچ‌لیست اضافه کن».

    It only fires when the message contains BOTH a clear media action and an ordinal AND the chat has
    a live recent list. Everything else falls through to the normal agent.
    """
    position = parse_ordinal(text)
    action = media_action_from_text(text)
    if position is None or action is None or chat_id == 0:
        return None
    # A bare number plus a generic verb must not hijack a calendar/email request.
    media_word = any(w in normalize(text) for w in ("فیلم", "سریال", "مورد", "گزینه", "شماره"))
    if not media_word and not contains_ordinal_word(text):
        return None
    contexts = container.contexts_for(session)
    recent = contexts.recent(user.id, chat_id)
    if not recent:
        return None
    item = resolve_position([i for _, i in recent], position)
    if item is None:
        return None
    preferences = container.preferences_for(session)
    try:
        result = preferences.set(user.id, item.id, action)
    except ValueError:
        return None
    labels = {
        "liked": "پسندیدم",
        "disliked": "نپسندیدم",
        "watched": "تماشا کردم",
        "watchlist": "واچ‌لیست",
    }
    text_fa = f"«{item.title_fa}» برای {labels[action]} ثبت شد."
    if not result.get("created", True):
        text_fa = f"«{item.title_fa}» قبلاً هم در {labels[action]} ثبت شده بود."
    return Reply("media", text=text_fa, action=MediaReply(text=text_fa))


def analyse_for_user(
    session,
    user: User,
    text: str,
    *,
    chat_id: int = 0,
    voice_message_id: int | None = None,
    editing_run_id: int | None = None,
) -> Reply:
    if budget.is_blocked(session):
        return Reply("blocked")
    reference = _try_media_reference(session, user, chat_id, text)
    if reference is not None:
        return reference
    draft = None
    if editing_run_id is not None:
        old = session.get(AgentRun, editing_run_id)
        if old and old.user_id == user.id and old.status == RunStatus.pending_confirmation:
            draft = old.action_json

    try:
        provider = get_provider()
    except LLMError:
        return Reply("failed")
    agent_ctx = build_context(session, user, text, draft)
    run, outcome = process_message(
        session,
        provider,
        user,
        text,
        agent_ctx,
        voice_message_id=voice_message_id,
        chat_id=chat_id,
    )

    if outcome.kind in ("schedule", "email"):
        if draft is not None:
            supersede_run(session, user, editing_run_id)
        action = ScheduleAction if outcome.kind == "schedule" else EmailAction
        return Reply(outcome.kind, run.id, action=action.model_validate(run.action_json))
    if outcome.kind == "media":
        action = MediaReply.model_validate(run.action_json)
        return Reply("media", run.id, text=action.text, action=action)
    if outcome.kind == "clarify":
        return Reply("clarify", run.id, text=outcome.question)
    return Reply("failed", run.id)

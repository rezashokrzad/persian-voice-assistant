import datetime as dt
import enum
from typing import Any

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Intent(str, enum.Enum):
    schedule = "schedule"
    email = "email"
    movie_series = "movie_series"
    unknown = "unknown"


class RunStatus(str, enum.Enum):
    pending_confirmation = "pending_confirmation"
    needs_clarification = "needs_clarification"  # agent asked the user a question instead
    confirmed = "confirmed"
    executed = "executed"
    rejected = "rejected"
    failed = "failed"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    # Stored as VARCHAR + CHECK constraint: easy to extend in later migrations.
    return Enum(
        e,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda x: [m.value for m in x],
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Tehran", server_default="Asia/Tehran"
    )

    contacts: Mapped[list["Contact"]] = relationship(back_populates="user")


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Persian and English spellings, e.g. ["علی", "Ali"]
    aliases: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    email: Mapped[str | None] = mapped_column(String(320))

    user: Mapped[User] = relationship(back_populates="contacts")


class VoiceMessage(TimestampMixin, Base):
    __tablename__ = "voice_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(255))
    duration_s: Mapped[int | None] = mapped_column(Integer)
    # Local file path; the audio is deleted after transcription (see SPEC), then this is NULL.
    audio_path: Mapped[str | None] = mapped_column(String(500))
    transcript: Mapped[str | None] = mapped_column(Text)
    stt_confidence: Mapped[float | None] = mapped_column(Float)


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    voice_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_messages.id", ondelete="SET NULL"), index=True
    )
    # What the agent was given: the transcript, or the typed text (the bot also accepts text).
    input_text: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    intent: Mapped[Intent] = mapped_column(_enum(Intent, "intent"), default=Intent.unknown)
    action_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), default=RunStatus.pending_confirmation
    )
    model: Mapped[str | None] = mapped_column(String(100))
    tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class ToolCall(TimestampMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_tool_calls_idempotency_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    idempotency_key: Mapped[str | None] = mapped_column(String(255))


class CalendarEvent(TimestampMixin, Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(255))
    date: Mapped[dt.date] = mapped_column(Date)
    time: Mapped[dt.time] = mapped_column(Time)
    duration_min: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    guests: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")


class Email(TimestampMixin, Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    to_address: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(998))
    body: Mapped[str] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="pending")


class MemoryFact(TimestampMixin, Base):
    __tablename__ = "memory_facts"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_memory_facts_user_id_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(100))


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class LlmPurpose(str, enum.Enum):
    stt_fix = "stt_fix"
    intent = "intent"
    tool_step = "tool_step"
    review = "review"


class LlmCall(TimestampMixin, Base):
    """One row per call to the LLM provider (written by the provider itself)."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True)
    model: Mapped[str] = mapped_column(String(100), index=True)
    purpose: Mapped[LlmPurpose] = mapped_column(_enum(LlmPurpose, "llm_purpose"))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)  # ok | error
    error: Mapped[str | None] = mapped_column(Text)
    retries: Mapped[int] = mapped_column(Integer, default=0)


class RunFeedback(TimestampMixin, Base):
    """Optional thumbs up (1) / down (-1) under a bot reply."""

    __tablename__ = "run_feedback"
    __table_args__ = (UniqueConstraint("run_id", "user_id", name="uq_run_feedback_run_id_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    rating: Mapped[int] = mapped_column(SmallInteger)


class BudgetAlert(TimestampMixin, Base):
    """Remembers which budget thresholds were already announced, so each fires once per period."""

    __tablename__ = "budget_alerts"
    __table_args__ = (UniqueConstraint("period", "threshold", name="uq_budget_alerts_period_threshold"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(32))  # e.g. "day:2026-10-15", "month:1405-07"
    threshold: Mapped[int] = mapped_column(Integer)  # 80 or 100
    tokens_used: Mapped[int] = mapped_column(BigInteger)


# ---------- movies and series ----------


class MediaType(str, enum.Enum):
    movie = "movie"
    tv = "tv"


class MediaAction(str, enum.Enum):
    liked = "liked"
    disliked = "disliked"
    watched = "watched"
    watchlist = "watchlist"


class MediaItem(TimestampMixin, Base):
    """One movie or series, cached from TMDb (and optionally enriched by OMDb)."""

    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("tmdb_id", "media_type", name="uq_media_items_tmdb_id_media_type"),
        Index("ix_media_items_original_title_year", "original_title", "year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True)
    media_type: Mapped[MediaType] = mapped_column(_enum(MediaType, "media_type"))
    imdb_id: Mapped[str | None] = mapped_column(String(32))
    title_fa: Mapped[str] = mapped_column(String(500))
    original_title: Mapped[str] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)
    # [{"id": 28, "name": "اکشن"}, ...]: JSONB keeps the cache schema flexible (no genre join table).
    genres: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    vote_average: Mapped[float | None] = mapped_column(Float)
    vote_source: Mapped[str | None] = mapped_column(String(20))  # tmdb | omdb
    poster_path: Mapped[str | None] = mapped_column(String(500))


class UserMediaPreference(TimestampMixin, Base):
    """Liked, disliked, watched and watchlist are deliberately separate facts."""

    __tablename__ = "user_media_preferences"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "media_id", "action_type", name="uq_user_media_preferences_user_id_media_id_action_type"
        ),
        Index("ix_user_media_preferences_user_id_action_type", "user_id", "action_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[MediaAction] = mapped_column(_enum(MediaAction, "media_action"))
    note: Mapped[str | None] = mapped_column(Text)


class RecommendationContext(TimestampMixin, Base):
    """The last list shown to a user in a chat, so «دومی» can be resolved and repeats avoided.

    status:
      active       one of the current list positions
      superseded   an older list (kept for the repeat penalty until it expires)
      pending      an ambiguous import title waiting for the owner's choice
      resolved     the owner chose one of the pending candidates
      expired      TTL passed (rows are also physically cleaned up opportunistically)
    """

    __tablename__ = "recommendation_contexts"
    __table_args__ = (
        Index("ix_recommendation_contexts_user_id_chat_id_status", "user_id", "chat_id", "status"),
        Index("ix_recommendation_contexts_request_hash", "request_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, default=0)
    media_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)  # 1-based for recommendation lists
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    kind: Mapped[str] = mapped_column(String(20), default="recommendation")  # recommendation | pending_import
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    query_text: Mapped[str | None] = mapped_column(String(500))
    target_action: Mapped[MediaAction | None] = mapped_column(_enum(MediaAction, "media_action_target"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)

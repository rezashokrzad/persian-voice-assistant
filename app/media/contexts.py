"""Recommendation contexts: the last list in a chat, TTL, replacement and pending imports.

This is what makes «دومی را اضافه کن» deterministic and stops the same five titles from being
recommended over and over.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any, Callable, Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import MediaAction, MediaItem, RecommendationContext
from app.media.schemas import MediaItemOut


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def request_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]


class ContextService:
    def __init__(
        self,
        session: Session,
        *,
        ttl_minutes: int | None = None,
        now: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self.session = session
        self.ttl = dt.timedelta(minutes=ttl_minutes or get_settings().recommendation_context_ttl_minutes)
        self.now = now

    def purge_expired(self) -> None:
        self.session.execute(
            delete(RecommendationContext).where(RecommendationContext.expires_at <= self.now())
        )
        self.session.flush()

    def record_list(
        self,
        user_id: int,
        chat_id: int,
        items: Iterable[MediaItem],
        *,
        request_hash_value: str = "",
        kind: str = "recommendation",
        replace: bool = True,
        commit: bool = True,
    ) -> int:
        items = [i for i in items if i.id is not None]
        if not items:
            return 0
        self.purge_expired()
        if replace:
            self.session.execute(
                update(RecommendationContext)
                .where(
                    RecommendationContext.user_id == user_id,
                    RecommendationContext.chat_id == chat_id,
                    RecommendationContext.kind == kind,
                    RecommendationContext.status == "active",
                )
                .values(status="superseded")
            )
        expires = self.now() + self.ttl
        for position, item in enumerate(items, start=1):
            self.session.add(
                RecommendationContext(
                    user_id=user_id,
                    chat_id=chat_id,
                    media_id=item.id,
                    position=position,
                    request_hash=request_hash_value,
                    status="active",
                    kind=kind,
                    expires_at=expires,
                )
            )
        if commit:
            self.session.commit()
        return len(items)

    def recent(self, user_id: int, chat_id: int, *, kind: str = "recommendation") -> list[tuple[int, MediaItem]]:
        self.purge_expired()
        rows = self.session.scalars(
            select(RecommendationContext)
            .where(
                RecommendationContext.user_id == user_id,
                RecommendationContext.chat_id == chat_id,
                RecommendationContext.kind == kind,
                RecommendationContext.status == "active",
                RecommendationContext.expires_at > self.now(),
            )
            .order_by(RecommendationContext.position)
        )
        out: list[tuple[int, MediaItem]] = []
        for row in rows:
            item = self.session.get(MediaItem, row.media_id) if row.media_id else None
            if item is not None:
                out.append((row.position, item))
        return out

    def recent_media_ids(self, user_id: int, chat_id: int) -> set[int]:
        rows = self.session.scalars(
            select(RecommendationContext.media_id).where(
                RecommendationContext.user_id == user_id,
                RecommendationContext.chat_id == chat_id,
                RecommendationContext.media_id.is_not(None),
                RecommendationContext.expires_at > self.now(),
            )
        )
        return {int(i) for i in rows if i is not None}

    def create_pending(
        self,
        user_id: int,
        chat_id: int,
        *,
        query_text: str,
        candidates: list[MediaItemOut],
        target_action: str,
        commit: bool = True,
    ) -> RecommendationContext:
        self.purge_expired()
        row = RecommendationContext(
            user_id=user_id,
            chat_id=chat_id,
            media_id=None,
            position=0,
            request_hash=request_hash(f"pending:{user_id}:{chat_id}:{query_text}"),
            status="pending",
            kind="pending_import",
            candidates=[c.model_dump(mode="json") for c in candidates],
            query_text=query_text[:500],
            target_action=MediaAction(target_action),
            expires_at=self.now() + self.ttl,
        )
        self.session.add(row)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return row

    def pending(self, user_id: int, chat_id: int, pending_id: int) -> RecommendationContext | None:
        self.purge_expired()
        return self.session.scalar(
            select(RecommendationContext).where(
                RecommendationContext.id == pending_id,
                RecommendationContext.user_id == user_id,
                RecommendationContext.chat_id == chat_id,
                RecommendationContext.kind == "pending_import",
            )
        )

    def mark_resolved(self, row: RecommendationContext, media_id: int) -> None:
        row.status = "resolved"
        row.media_id = media_id
        self.session.flush()

    def candidates(self, row: RecommendationContext) -> list[dict[str, Any]]:
        return list(row.candidates or [])

"""Preference/watchlist service. Every query is scoped by user_id (no cross-user reads or writes)."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import MediaAction, MediaItem, MediaType, RecommendationContext, UserMediaPreference
from app.media.catalog import item_out
from app.media.schemas import MediaItemOut


class PreferenceService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- writes

    def set(
        self, user_id: int, media_id: int, action: str, note: str | None = None, *, commit: bool = True
    ) -> dict[str, Any]:
        media = self.session.get(MediaItem, media_id)
        if media is None:
            raise ValueError("Unknown media item.")
        kind = MediaAction(action)
        row = self.session.scalar(
            select(UserMediaPreference).where(
                UserMediaPreference.user_id == user_id,
                UserMediaPreference.media_id == media_id,
                UserMediaPreference.action_type == kind,
            )
        )
        created = row is None
        if row is None:
            row = UserMediaPreference(
                user_id=user_id, media_id=media_id, action_type=kind, note=note
            )
            self.session.add(row)
            try:
                with self.session.begin_nested():
                    self.session.flush()
            except IntegrityError:  # concurrent duplicate: unique constraint makes it idempotent
                row = self.session.scalar(
                    select(UserMediaPreference).where(
                        UserMediaPreference.user_id == user_id,
                        UserMediaPreference.media_id == media_id,
                        UserMediaPreference.action_type == kind,
                    )
                )
                if row is None:
                    raise
        elif note is not None:
            row.note = note
        if commit:
            self.session.commit()
        return {
            "media_id": media_id,
            "action": kind.value,
            "created": created,
            "item": item_out(media).model_dump(mode="json"),
        }

    def remove(
        self, user_id: int, media_id: int, action: str = "watchlist", *, commit: bool = True
    ) -> dict[str, Any]:
        kind = MediaAction(action)
        result = self.session.execute(
            delete(UserMediaPreference).where(
                UserMediaPreference.user_id == user_id,
                UserMediaPreference.media_id == media_id,
                UserMediaPreference.action_type == kind,
            )
        )
        if commit:
            self.session.commit()
        return {"media_id": media_id, "action": kind.value, "removed": bool(result.rowcount)}

    def watchlist_add(self, user_id: int, media_id: int) -> dict[str, Any]:
        return self.set(user_id, media_id, MediaAction.watchlist.value)

    def watchlist_remove(self, user_id: int, media_id: int) -> dict[str, Any]:
        return self.remove(user_id, media_id, MediaAction.watchlist.value)

    # ---- reads

    def ids(self, user_id: int, action: str) -> set[int]:
        return set(
            self.session.scalars(
                select(UserMediaPreference.media_id).where(
                    UserMediaPreference.user_id == user_id,
                    UserMediaPreference.action_type == MediaAction(action),
                )
            )
        )

    def liked_genres(self, user_id: int) -> Counter[int]:
        rows = self.session.scalars(
            select(MediaItem)
            .join(UserMediaPreference, UserMediaPreference.media_id == MediaItem.id)
            .where(
                UserMediaPreference.user_id == user_id,
                UserMediaPreference.action_type == MediaAction.liked,
            )
        )
        counter: Counter[int] = Counter()
        for item in rows:
            for g in item.genres or []:
                if isinstance(g, dict) and "id" in g:
                    counter[int(g["id"])] += 1
        return counter

    def watchlist(self, user_id: int) -> list[MediaItemOut]:
        rows = self.session.scalars(
            select(MediaItem)
            .join(UserMediaPreference, UserMediaPreference.media_id == MediaItem.id)
            .where(
                UserMediaPreference.user_id == user_id,
                UserMediaPreference.action_type == MediaAction.watchlist,
            )
            .order_by(UserMediaPreference.id.desc())
        )
        return [item_out(i) for i in rows]

    def list_action(self, user_id: int, action: str) -> list[MediaItemOut]:
        rows = self.session.scalars(
            select(MediaItem)
            .join(UserMediaPreference, UserMediaPreference.media_id == MediaItem.id)
            .where(
                UserMediaPreference.user_id == user_id,
                UserMediaPreference.action_type == MediaAction(action),
            )
            .order_by(UserMediaPreference.id.desc())
        )
        return [item_out(i) for i in rows]

    def is_visible_to_user(self, user_id: int, chat_id: int, media_id: int) -> bool:
        """A callback may only touch a title this user was actually shown (or already has)."""
        pref = self.session.scalar(
            select(UserMediaPreference.id).where(
                UserMediaPreference.user_id == user_id,
                UserMediaPreference.media_id == media_id,
            )
        )
        if pref is not None:
            return True
        ctx = self.session.scalar(
            select(RecommendationContext.id).where(
                RecommendationContext.user_id == user_id,
                RecommendationContext.chat_id == chat_id,
                RecommendationContext.media_id == media_id,
                RecommendationContext.status.in_(("active", "superseded", "resolved")),
                RecommendationContext.expires_at > dt.datetime.now(dt.timezone.utc),
            )
        )
        return ctx is not None

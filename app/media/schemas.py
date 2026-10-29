"""Pydantic models shared by the TMDb/OMDb clients, services, tools and the bot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.db.models import MediaType


class MediaCandidate(BaseModel):
    tmdb_id: int
    media_type: MediaType
    title_fa: str
    original_title: str
    year: int | None = None
    overview: str | None = None
    genre_ids: list[int] = Field(default_factory=list)
    genres: list[dict[str, Any]] = Field(default_factory=list)
    vote_average: float | None = None
    vote_source: str | None = "tmdb"
    poster_path: str | None = None
    poster_url: str | None = None
    imdb_id: str | None = None
    popularity: float = 0.0

    @property
    def genre_names(self) -> list[str]:
        return [str(g.get("name")) for g in self.genres if g.get("name")]


class MediaItemOut(BaseModel):
    id: int
    tmdb_id: int | None = None
    media_type: MediaType
    imdb_id: str | None = None
    title_fa: str
    original_title: str
    year: int | None = None
    overview: str | None = None
    genres: list[dict[str, Any]] = Field(default_factory=list)
    vote_average: float | None = None
    vote_source: str | None = None
    poster_path: str | None = None
    poster_url: str | None = None


class Recommendation(BaseModel):
    item: MediaItemOut
    score: float
    why: list[str] = Field(default_factory=list)
    recently_shown: bool = False


class RecommendationResult(BaseModel):
    items: list[Recommendation] = Field(default_factory=list)
    personalized: bool = False
    source: str = "trending"  # trending | discover
    note: str | None = None


class ImportPending(BaseModel):
    pending_id: int
    title: str
    candidates: list[MediaItemOut] = Field(default_factory=list)


class ImportReport(BaseModel):
    added: list[MediaItemOut] = Field(default_factory=list)
    needs_confirmation: list[ImportPending] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    target_action: str

    def summary_fa(self) -> str:
        parts = [f"✅ {len(self.added)} عنوان ثبت شد."] if self.added else []
        if self.needs_confirmation:
            parts.append(f"❓ {len(self.needs_confirmation)} عنوان نیاز به تأیید دارد.")
        if self.not_found:
            parts.append(f"🔍 {len(self.not_found)} عنوان پیدا نشد.")
        return " ".join(parts) or "موردی انجام نشد."


class GoogleSearchLink(BaseModel):
    url: str
    query: str
    notice: str = (
        "این فقط یک لینک جست‌وجوی گوگل است و به معنای تأیید دسترس‌پذیری یا قانونی بودن محتوا نیست."
    )

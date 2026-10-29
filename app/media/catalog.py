"""Catalog service: the only layer that reads/writes media_items.

Both the agent tools and MCP call this service through the registry; provider errors propagate as
explicit exceptions so callers never treat them as "no results".
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import MediaItem, MediaType
from app.media.providers import MediaProviderError, OMDbClient, TMDbClient
from app.media.schemas import MediaCandidate, MediaItemOut
from app.util.persian import normalize

log = logging.getLogger("media.catalog")


def item_out(item: MediaItem, settings: Settings | None = None) -> MediaItemOut:
    s = settings or get_settings()
    poster_url = (
        f"{s.tmdb_image_base_url.rstrip('/')}{item.poster_path}" if item.poster_path else None
    )
    return MediaItemOut(
        id=item.id,
        tmdb_id=item.tmdb_id,
        media_type=item.media_type,
        imdb_id=item.imdb_id,
        title_fa=item.title_fa,
        original_title=item.original_title,
        year=item.year,
        overview=item.overview,
        genres=item.genres or [],
        vote_average=item.vote_average,
        vote_source=item.vote_source,
        poster_path=item.poster_path,
        poster_url=poster_url,
    )


def genre_ids(item: MediaItem) -> set[int]:
    return {int(g["id"]) for g in item.genres or [] if isinstance(g, dict) and "id" in g}


def is_exact(title: str, query: str, item_year: int | None, query_year: int | None) -> bool:
    if normalize(title) != normalize(query):
        return False
    return query_year is None or item_year == query_year


class CatalogService:
    def __init__(
        self,
        session: Session,
        tmdb: TMDbClient,
        omdb: OMDbClient | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.tmdb = tmdb
        self.omdb = omdb
        self.settings = settings or get_settings()

    # ---- reads

    def search(self, media_type: str, title: str, year: int | None = None, limit: int = 5) -> list[MediaItem]:
        kind = MediaType(media_type)
        candidates = self.tmdb.search(kind.value, title, year)
        items = [self._upsert(c) for c in candidates if c.tmdb_id]
        self.session.commit()

        def rank(item: MediaItem) -> tuple[int, int, float]:
            exact = int(is_exact(item.title_fa, title, item.year, year) or is_exact(
                item.original_title, title, item.year, year
            ))
            year_match = int(year is not None and item.year == year)
            return (-exact, -year_match, -(item.vote_average or 0.0))

        items.sort(key=rank)
        return items[:limit]

    def details(self, tmdb_id: int, media_type: str) -> tuple[MediaItem, str | None]:
        kind = MediaType(media_type)
        candidate = self.tmdb.details(kind.value, tmdb_id)
        item = self._upsert(candidate)
        warning = self._enrich_from_omdb(item)
        self.session.commit()
        return item, warning

    def discover(
        self,
        media_type: str,
        *,
        genre_ids: Iterable[int] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_rating: float | None = None,
        sort: str | None = None,
        limit: int = 20,
    ) -> list[MediaItem]:
        kind = MediaType(media_type)
        candidates = self.tmdb.discover(
            kind.value,
            genre_ids=genre_ids,
            year_from=year_from,
            year_to=year_to,
            min_rating=min_rating,
            sort=sort,
        )
        items = [self._upsert(c) for c in candidates if c.tmdb_id][:limit]
        self.session.commit()
        return items

    def trending(self, media_type: str = "all", window: str = "week", limit: int = 20) -> list[MediaItem]:
        candidates = self.tmdb.trending(media_type, window)
        items = [self._upsert(c) for c in candidates if c.tmdb_id and c.media_type in (MediaType.movie, MediaType.tv)]
        self.session.commit()
        return items[:limit]

    def genres(self, media_type: str | None = None) -> dict[int, str]:
        if media_type in ("movie", "tv"):
            return dict(self.tmdb.genres(media_type))
        merged = dict(self.tmdb.genres("movie"))
        merged.update(self.tmdb.genres("tv"))
        return merged

    def get(self, tmdb_id: int, media_type: str) -> MediaItem | None:
        return self.session.scalar(
            select(MediaItem).where(
                MediaItem.tmdb_id == tmdb_id, MediaItem.media_type == MediaType(media_type)
            )
        )

    def get_or_fetch(self, tmdb_id: int, media_type: str) -> MediaItem:
        existing = self.get(tmdb_id, media_type)
        if existing is not None:
            return existing
        item, _ = self.details(tmdb_id, media_type)
        return item

    def get_by_id(self, media_id: int) -> MediaItem | None:
        return self.session.get(MediaItem, media_id)

    # ---- cache writes

    def _upsert(self, candidate: MediaCandidate) -> MediaItem:
        if not candidate.tmdb_id:
            raise ValueError("A TMDb candidate without tmdb_id cannot be cached.")
        item = self.session.scalar(
            select(MediaItem).where(
                MediaItem.tmdb_id == candidate.tmdb_id,
                MediaItem.media_type == MediaType(candidate.media_type),
            )
        )
        if item is None:
            item = MediaItem(
                tmdb_id=candidate.tmdb_id,
                media_type=MediaType(candidate.media_type),
                title_fa=candidate.title_fa,
                original_title=candidate.original_title,
            )
            self.session.add(item)
            try:
                with self.session.begin_nested():
                    self.session.flush()
            except IntegrityError:  # concurrent insert: use the row the other call created
                item = self.session.scalar(
                    select(MediaItem).where(
                        MediaItem.tmdb_id == candidate.tmdb_id,
                        MediaItem.media_type == MediaType(candidate.media_type),
                    )
                )
                if item is None:
                    raise
        self._merge(item, candidate)
        return item

    @staticmethod
    def _merge(item: MediaItem, candidate: MediaCandidate, *, from_omdb: bool = False) -> None:
        # OMDb is enrichment only: fill blanks, never replace TMDb's Persian title, year, genres,
        # poster or rating. A weaker secondary source must not degrade a good row.
        if from_omdb:
            if not item.title_fa and candidate.title_fa:
                item.title_fa = candidate.title_fa
            if not item.original_title and candidate.original_title:
                item.original_title = candidate.original_title
            if item.year is None and candidate.year:
                item.year = candidate.year
            if not item.imdb_id and candidate.imdb_id:
                item.imdb_id = candidate.imdb_id
            if not item.overview and candidate.overview:
                item.overview = candidate.overview
            if not item.genres and candidate.genres:
                item.genres = candidate.genres
            if item.vote_average is None and candidate.vote_average is not None:
                item.vote_average = candidate.vote_average
                item.vote_source = candidate.vote_source or "omdb"
            if not item.poster_path and candidate.poster_path:
                item.poster_path = candidate.poster_path
            return

        # TMDb is the primary source and may correct a previously cached row.
        if candidate.title_fa:
            item.title_fa = candidate.title_fa
        if candidate.original_title:
            item.original_title = candidate.original_title
        if candidate.year:
            item.year = candidate.year
        if candidate.imdb_id:
            item.imdb_id = candidate.imdb_id
        if candidate.overview and not item.overview:
            item.overview = candidate.overview
        if candidate.genres:
            item.genres = candidate.genres
        if candidate.vote_average is not None:
            if candidate.vote_source == "tmdb" or item.vote_average is None:
                item.vote_average = candidate.vote_average
                item.vote_source = candidate.vote_source or item.vote_source
        if candidate.poster_path:
            item.poster_path = candidate.poster_path

    def _enrich_from_omdb(self, item: MediaItem) -> str | None:
        if self.omdb is None or not self.omdb.available:
            return None
        if item.imdb_id and item.overview and item.vote_average is not None:
            return None
        try:
            enriched = (
                self.omdb.by_imdb(item.imdb_id)
                if item.imdb_id
                else self.omdb.by_title(item.original_title, item.year)
            )
        except MediaProviderError as exc:
            log.warning("OMDb enrichment failed: %s", exc.code)
            return exc.code
        if enriched is not None:
            self._merge(item, enriched, from_omdb=True)
        return None


def candidate_payload(candidate: MediaCandidate) -> dict[str, Any]:
    return candidate.model_dump(mode="json")

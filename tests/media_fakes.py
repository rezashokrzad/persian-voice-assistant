"""Helpers shared by the media tests: real DB rows plus a fake catalog (no network)."""

from __future__ import annotations

from app.db.models import MediaItem, MediaType


def add_media(
    session,
    *,
    tmdb_id: int,
    title_fa: str,
    media_type: str = "movie",
    year: int | None = 2020,
    genres: list[tuple[int, str]] | None = None,
    overview: str = "خلاصه",
    vote_average: float | None = 7.0,
    vote_source: str = "tmdb",
    poster_path: str | None = "/poster.jpg",
    imdb_id: str | None = None,
    original_title: str | None = None,
) -> MediaItem:
    item = MediaItem(
        tmdb_id=tmdb_id,
        media_type=MediaType(media_type),
        title_fa=title_fa,
        original_title=original_title or title_fa,
        year=year,
        genres=[{"id": gid, "name": name} for gid, name in (genres or [])],
        overview=overview,
        vote_average=vote_average,
        vote_source=vote_source,
        poster_path=poster_path,
        imdb_id=imdb_id,
    )
    session.add(item)
    session.flush()
    return item


class FakeCatalog:
    """Minimal CatalogService stand-in used by recommender/import tests."""

    def __init__(
        self,
        *,
        items=None,
        search_results=None,
        trending_results=None,
        genre_map=None,
    ) -> None:
        self.items = list(items or [])
        self.search_results = search_results or {}
        self.trending_results = list(trending_results or self.items)
        self.genre_map = genre_map or {35: "کمدی", 18: "درام", 28: "اکشن", 27: "ترسناک"}
        self.discover_calls: list[dict] = []
        self.trending_calls: list[tuple] = []

    def search(self, media_type: str, title: str, year: int | None = None, limit: int = 5):
        return list(self.search_results.get((media_type, title), []))

    def discover(self, media_type: str, **kwargs):
        self.discover_calls.append({"media_type": media_type, **kwargs})
        pool = [i for i in self.items if i.media_type.value == media_type]
        wanted = set(kwargs.get("genre_ids") or [])
        if wanted:
            pool = [
                i
                for i in pool
                if wanted & {int(g["id"]) for g in (i.genres or []) if isinstance(g, dict) and "id" in g}
            ]
        return pool[: kwargs.get("limit", 20) or 20]

    def trending(self, media_type: str = "all", window: str = "week", limit: int = 20):
        self.trending_calls.append((media_type, window))
        return list(self.trending_results)

    def genres(self, media_type: str | None = None):
        return dict(self.genre_map)

    def get(self, tmdb_id: int, media_type: str):
        return next(
            (i for i in self.items if i.tmdb_id == tmdb_id and i.media_type.value == media_type),
            None,
        )

    def details(self, tmdb_id: int, media_type: str):
        item = self.get(tmdb_id, media_type)
        return item, None

"""Deterministic recommendation engine: explicit filters first, then liked genres, repeat penalty."""

from __future__ import annotations

from typing import Iterable

from app.db.models import MediaItem, MediaType
from app.media.catalog import CatalogService, item_out
from app.media.contexts import ContextService, request_hash
from app.media.preferences import PreferenceService
from app.media.schemas import Recommendation, RecommendationResult
from app.util.persian import normalize

PUBLIC_NOTE = "پیشنهاد عمومی است و شخصی‌سازی نشده است."
NO_DATA_NOTE = "برای این فیلترها عنوانی پیدا نشد."

# Fallback aliases when TMDb's localized genre list is unavailable. IDs follow TMDb.
_GENRE_ALIASES: dict[str, int] = {
    "اکشن": 28, "action": 28,
    "ماجراجویی": 12, "adventure": 12,
    "انیمیشن": 16, "animation": 16,
    "کمدی": 35, "comedy": 35,
    "جنایی": 80, "crime": 80,
    "مستند": 99, "documentary": 99,
    "درام": 18, "drama": 18,
    "خانوادگی": 10751, "family": 10751,
    "فانتزی": 14, "fantasy": 14,
    "تاریخی": 36, "history": 36,
    "ترسناک": 27, "horror": 27,
    "موزیکال": 10402, "music": 10402,
    "معمایی": 9648, "mystery": 9648,
    "عاشقانه": 10749, "romance": 10749,
    "علمی تخیلی": 878, "science fiction": 878, "scifi": 878,
    "هیجان‌انگیز": 53, "thriller": 53,
    "جنگی": 10752, "war": 10752,
    "وسترن": 37, "western": 37,
}  # fmt: skip
_TV_ALIASES = {"اکشن": 10759, "action": 10759, "ماجراجویی": 10759, "adventure": 10759}


class RecommendationService:
    def __init__(
        self,
        session,
        catalog: CatalogService,
        preferences: PreferenceService,
        contexts: ContextService,
    ) -> None:
        self.session = session
        self.catalog = catalog
        self.preferences = preferences
        self.contexts = contexts

    # ---- genre resolution

    def resolve_genre(self, genre: str | int | None, media_type: str | None = None) -> tuple[int | None, str | None]:
        if genre is None or (isinstance(genre, str) and not genre.strip()):
            return None, None
        if isinstance(genre, int) or str(genre).strip().isdigit():
            return int(genre), None
        raw = str(genre).strip()
        names = self.catalog.genres(media_type)
        wanted = normalize(raw)
        for gid, name in names.items():
            if normalize(name) == wanted:
                return gid, name
        aliases = _TV_ALIASES if media_type == "tv" else _GENRE_ALIASES
        alias_key = normalize(raw)
        if alias_key in aliases:
            gid = aliases[alias_key]
            return gid, names.get(gid) or raw
        if _TV_ALIASES.get(alias_key) and media_type is None:
            return _TV_ALIASES[alias_key], raw
        if alias_key in _GENRE_ALIASES:
            gid = _GENRE_ALIASES[alias_key]
            return gid, names.get(gid) or raw
        raise ValueError(f"Unknown genre: {raw}")

    # ---- recommendation

    def recommend(
        self,
        user_id: int,
        chat_id: int,
        *,
        media_type: MediaType | str | None = None,
        genre: str | int | None = None,
        count: int = 5,
        request_text: str = "",
        include_public_note: bool = True,
    ) -> RecommendationResult:
        count = max(1, min(int(count), 10))
        kind = MediaType(media_type) if media_type else None
        genre_id, genre_label = self.resolve_genre(genre, kind.value if kind else None)

        liked = self.preferences.liked_genres(user_id)
        disliked = self.preferences.ids(user_id, "disliked")
        watched = self.preferences.ids(user_id, "watched")
        recent = self.contexts.recent_media_ids(user_id, chat_id)
        personalized = bool(liked or disliked or watched)

        pool = self._pool(kind, genre_id, personalized)
        source = "trending" if (not personalized and genre_id is None) else "discover"
        if not pool and source == "discover" and genre_id is None:
            pool, source = self.catalog.trending("all"), "trending"

        scored: list[Recommendation] = []
        for item in self._dedupe(pool):
            if item.id in disliked or item.id in watched:
                continue  # both are hard exclusions when looking for something new
            reasons: list[str] = []
            score = 0.0
            overlap = {g for g in liked if g in self._item_genres(item)}
            if genre_id is not None:
                score += 5.0
                reasons.append(f"ژانر درخواستی: {genre_label or genre_id}")
            if overlap:
                names = [self._genre_name(item, gid) for gid in sorted(overlap)]
                score += 2.0 * len(overlap)
                reasons.append("ژانر مورد علاقه: " + "، ".join(n for n in names if n))
            if item.vote_average is not None:
                score += 0.5 * (item.vote_average / 10.0)
            if item.id in recent:
                score -= 3.0
                reasons.append("اخیراً پیشنهاد شده بود")
            if not reasons:
                reasons.append(
                    "از فهرست پرطرفدار" if source == "trending" and not personalized else "نزدیک به سلیقه شما"
                )
            scored.append(
                Recommendation(
                    item=item_out(item),
                    score=round(score, 4),
                    why=reasons,
                    recently_shown=item.id in recent,
                )
            )

        scored.sort(key=lambda r: (-r.score, -(r.item.vote_average or 0), -(r.item.year or 0), r.item.id))
        chosen = scored[:count]
        note = None
        if not personalized and include_public_note:
            note = PUBLIC_NOTE
        if not chosen and genre_id is not None:
            note = NO_DATA_NOTE

        if chosen:
            self.contexts.record_list(
                user_id,
                chat_id,
                [self._item_from_out(r.item) for r in chosen],
                request_hash_value=request_hash(
                    f"rec|{user_id}|{chat_id}|{kind}|{genre_id}|{count}|{normalize(request_text)}"
                ),
                kind="recommendation",
            )
        return RecommendationResult(items=chosen, personalized=personalized, source=source, note=note)

    # ---- helpers

    def _pool(
        self, media_type: MediaType | None, genre_id: int | None, personalized: bool
    ) -> list[MediaItem]:
        if media_type is not None:
            return self.catalog.discover(
                media_type.value, genre_ids=[genre_id] if genre_id else None
            )
        if genre_id is not None:
            # A movie genre id may not exist on TV; query both and let TMDb return what it has.
            return self.catalog.discover("movie", genre_ids=[genre_id]) + self.catalog.discover(
                "tv", genre_ids=[genre_id]
            )
        if personalized:
            return self.catalog.discover("movie") + self.catalog.discover("tv")
        return self.catalog.trending("all")

    @staticmethod
    def _dedupe(items: Iterable[MediaItem]) -> list[MediaItem]:
        seen: set[tuple[int | None, str]] = set()
        out: list[MediaItem] = []
        for item in items:
            key = (item.tmdb_id, item.media_type.value)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    @staticmethod
    def _item_genres(item: MediaItem) -> set[int]:
        return {int(g["id"]) for g in item.genres or [] if isinstance(g, dict) and "id" in g}

    @staticmethod
    def _genre_name(item: MediaItem, gid: int) -> str:
        for g in item.genres or []:
            if isinstance(g, dict) and int(g.get("id", -1)) == gid:
                return str(g.get("name") or "")
        return ""

    def _item_from_out(self, out) -> MediaItem:
        item = self.session.get(MediaItem, out.id)
        if item is None:
            raise ValueError("Recommendation context refers to a missing media item.")
        return item

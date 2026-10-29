"""Movie/series tools registered in the existing registry.

Thin wrappers only: validation comes from Pydantic, user identity from ToolContext (never from model
arguments), and all business logic lives in app.media.* so MCP and Telegram share one implementation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.db.models import MediaType
from app.media import container
from app.media.catalog import is_exact, item_out
from app.media.links import google_search_link
from app.media.references import parse_ordinal, resolve_position
from app.tools.registry import Tool, ToolContext, register

MediaKind = Literal["movie", "tv"]


# ---------- argument models ----------


class TitleSearchArgs(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    year: int | None = Field(default=None, ge=1888, le=2100)


class DetailsArgs(BaseModel):
    tmdb_id: int = Field(gt=0)
    media_type: MediaKind


class DiscoverArgs(BaseModel):
    genre_ids: list[int] = Field(default_factory=list, max_length=5)
    year_from: int | None = Field(default=None, ge=1888, le=2100)
    year_to: int | None = Field(default=None, ge=1888, le=2100)
    min_rating: float | None = Field(default=None, ge=0, le=10)
    sort: str | None = Field(default=None, max_length=50)


class TrendingArgs(BaseModel):
    media_type: Literal["movie", "tv", "all"]
    window: Literal["day", "week"] = "week"


class RecommendationArgs(BaseModel):
    media_type: MediaKind | None = None
    genre: str | None = Field(default=None, max_length=100)
    count: int = Field(default=5, ge=1, le=10)


class PreferenceArgs(BaseModel):
    tmdb_id: int = Field(gt=0)
    media_type: MediaKind
    action: Literal["liked", "disliked", "watched"]


class RemovePreferenceArgs(BaseModel):
    tmdb_id: int = Field(gt=0)
    media_type: MediaKind
    action: Literal["liked", "disliked", "watched"]


class ListPreferencesArgs(BaseModel):
    action: Literal["liked", "disliked", "watched"]


class WatchlistArgs(BaseModel):
    tmdb_id: int = Field(gt=0)
    media_type: MediaKind


class WatchlistListArgs(BaseModel):
    pass


class ImportArgs(BaseModel):
    titles: list[str] = Field(min_length=1, max_length=20)
    target_action: Literal["liked", "watched", "watchlist"]


class ResolvePendingArgs(BaseModel):
    pending_id: int = Field(gt=0)
    choice_index: int = Field(ge=1, le=10)


class GoogleLinkArgs(BaseModel):
    title_fa: str = Field(min_length=1, max_length=300)
    year: int | None = Field(default=None, ge=1888, le=2100)
    mode: Literal["dubbed", "subtitle", "none"] = "none"


class RecentListArgs(BaseModel):
    pass


# ---------- helpers ----------


def _record_shown(ctx: ToolContext, items, request_text: str) -> None:
    if not items:
        return
    services = container.services_for(ctx.session)
    services.contexts.record_list(
        ctx.user.id,
        ctx.chat_id,
        items,
        request_hash_value=container_context_hash(ctx, request_text),
    )


def container_context_hash(ctx: ToolContext, text: str) -> str:
    from app.media.contexts import request_hash

    return request_hash(f"tool|{ctx.user.id}|{ctx.chat_id}|{text}")


def _search_payload(items, title: str, year: int | None) -> dict:
    exact = [i for i in items if is_exact(i.title_fa, title, i.year, year) or is_exact(
        i.original_title, title, i.year, year
    )]
    if len(exact) == 1:
        match_type = "exact"
    elif exact or len(items) > 1:
        match_type = "ambiguous"
    elif items:
        match_type = "partial"
    else:
        match_type = "none"
    return {
        "match_type": match_type,
        "items": [item_out(i).model_dump(mode="json") for i in items],
        "note": (
            "چند گزینه متمایز وجود دارد؛ قبل از انتخاب از کاربر بپرس."
            if match_type in ("ambiguous", "partial")
            else None
        ),
    }


def _get_item(ctx: ToolContext, tmdb_id: int, media_type: str):
    services = container.services_for(ctx.session)
    item = services.catalog.get(tmdb_id, media_type)
    if item is None:
        item, _ = services.catalog.details(tmdb_id, media_type)
    return item


# ---------- implementations ----------


def search_movie(ctx: ToolContext, args: TitleSearchArgs) -> dict:
    services = container.services_for(ctx.session)
    items = services.catalog.search("movie", args.title, args.year)
    _record_shown(ctx, items, f"search_movie:{args.title}:{args.year}")
    return _search_payload(items, args.title, args.year)


def search_tv(ctx: ToolContext, args: TitleSearchArgs) -> dict:
    services = container.services_for(ctx.session)
    items = services.catalog.search("tv", args.title, args.year)
    _record_shown(ctx, items, f"search_tv:{args.title}:{args.year}")
    return _search_payload(items, args.title, args.year)


def get_title_details(ctx: ToolContext, args: DetailsArgs) -> dict:
    services = container.services_for(ctx.session)
    item, warning = services.catalog.details(args.tmdb_id, args.media_type)
    return {
        "item": item_out(item).model_dump(mode="json"),
        "enrichment_warning": warning,
    }


def discover_movies(ctx: ToolContext, args: DiscoverArgs) -> dict:
    services = container.services_for(ctx.session)
    items = services.catalog.discover(
        "movie",
        genre_ids=args.genre_ids,
        year_from=args.year_from,
        year_to=args.year_to,
        min_rating=args.min_rating,
        sort=args.sort,
    )
    _record_shown(ctx, items, f"discover_movies:{args.model_dump_json()}")
    return {"items": [item_out(i).model_dump(mode="json") for i in items]}


def discover_tv(ctx: ToolContext, args: DiscoverArgs) -> dict:
    services = container.services_for(ctx.session)
    items = services.catalog.discover(
        "tv",
        genre_ids=args.genre_ids,
        year_from=args.year_from,
        year_to=args.year_to,
        min_rating=args.min_rating,
        sort=args.sort,
    )
    _record_shown(ctx, items, f"discover_tv:{args.model_dump_json()}")
    return {"items": [item_out(i).model_dump(mode="json") for i in items]}


def get_trending(ctx: ToolContext, args: TrendingArgs) -> dict:
    services = container.services_for(ctx.session)
    items = services.catalog.trending(args.media_type, args.window)
    _record_shown(ctx, items, f"trending:{args.media_type}:{args.window}")
    return {
        "media_type": args.media_type,
        "window": args.window,
        "items": [item_out(i).model_dump(mode="json") for i in items],
    }


def get_recommendations(ctx: ToolContext, args: RecommendationArgs) -> dict:
    services = container.services_for(ctx.session)
    result = services.recommendations.recommend(
        ctx.user.id,
        ctx.chat_id,
        media_type=args.media_type,
        genre=args.genre,
        count=args.count,
        request_text=args.model_dump_json(),
    )
    return result.model_dump(mode="json")


def set_preference(ctx: ToolContext, args: PreferenceArgs) -> dict:
    item = _get_item(ctx, args.tmdb_id, args.media_type)
    services = container.services_for(ctx.session)
    return services.preferences.set(ctx.user.id, item.id, args.action)


def remove_preference(ctx: ToolContext, args: RemovePreferenceArgs) -> dict:
    """Undo a wrong like/dislike/watched; watchlist removal has its own tool."""
    services = container.services_for(ctx.session)
    item = services.catalog.get(args.tmdb_id, args.media_type)
    if item is None:
        return {"removed": False, "note": "این عنوان در سابقه شما نبود."}
    return services.preferences.remove(ctx.user.id, item.id, args.action)


def list_preferences(ctx: ToolContext, args: ListPreferencesArgs) -> dict:
    services = container.services_for(ctx.session)
    items = services.preferences.list_action(ctx.user.id, args.action)
    return {
        "action": args.action,
        "items": [i.model_dump(mode="json") for i in items],
        "count": len(items),
    }


def watchlist_add(ctx: ToolContext, args: WatchlistArgs) -> dict:
    item = _get_item(ctx, args.tmdb_id, args.media_type)
    services = container.services_for(ctx.session)
    return services.preferences.watchlist_add(ctx.user.id, item.id)


def watchlist_remove(ctx: ToolContext, args: WatchlistArgs) -> dict:
    services = container.services_for(ctx.session)
    item = services.catalog.get(args.tmdb_id, args.media_type)
    if item is None:
        return {"removed": False, "note": "این عنوان در فهرست شما نبود."}
    return services.preferences.watchlist_remove(ctx.user.id, item.id)


def watchlist_list(ctx: ToolContext, args: WatchlistListArgs) -> dict:
    services = container.services_for(ctx.session)
    items = services.preferences.watchlist(ctx.user.id)
    return {"items": [i.model_dump(mode="json") for i in items], "count": len(items)}


def import_title_list(ctx: ToolContext, args: ImportArgs) -> dict:
    services = container.services_for(ctx.session)
    report = services.imports.import_titles(
        ctx.user.id, ctx.chat_id, args.titles, args.target_action
    )
    return {
        **report.model_dump(mode="json"),
        "summary_fa": report.summary_fa(),
    }


def resolve_pending(ctx: ToolContext, args: ResolvePendingArgs) -> dict:
    services = container.services_for(ctx.session)
    return services.imports.resolve_pending(
        ctx.user.id, ctx.chat_id, args.pending_id, args.choice_index
    )


def make_google_search_link(ctx: ToolContext, args: GoogleLinkArgs) -> dict:
    return google_search_link(args.title_fa, args.year, args.mode).model_dump(mode="json")


def get_recent_list(ctx: ToolContext, args: RecentListArgs) -> dict:
    services = container.services_for(ctx.session)
    recent = services.contexts.recent(ctx.user.id, ctx.chat_id)
    return {
        "items": [
            {"position": position, "item": item_out(item).model_dump(mode="json")}
            for position, item in recent
        ],
        "count": len(recent),
    }


# ---------- registration ----------


register(Tool(
    "search_movie",
    "Search films on TMDb. Returns up to 5 distinct candidates with tmdb_id, year, type and a "
    "match_type. If match_type is ambiguous or partial, do NOT choose yourself: show 3-5 distinct "
    "candidates (title + year + type) and ask the owner with ask_clarification.",
    TitleSearchArgs, search_movie, agent_visible=True,
))  # fmt: skip
register(Tool(
    "search_tv",
    "Search TV series on TMDb. Same rules as search_movie: never choose an ambiguous match.",
    TitleSearchArgs, search_tv, agent_visible=True,
))  # fmt: skip
register(Tool(
    "get_title_details",
    "Full details for one TMDb id, including IMDb id when available. Use after the owner chose a "
    "candidate. Provider errors are returned as errors, never as an empty title.",
    DetailsArgs, get_title_details, agent_visible=True,
))  # fmt: skip
register(Tool(
    "discover_movies",
    "Discover films by genre ids, year range and minimum rating. Use when the owner asks for "
    "suggestions by genre/year, not for a specific title.",
    DiscoverArgs, discover_movies, agent_visible=True,
))  # fmt: skip
register(Tool(
    "discover_tv",
    "Discover TV series by genre ids, year range and minimum rating.",
    DiscoverArgs, discover_tv, agent_visible=True,
))  # fmt: skip
register(Tool(
    "get_trending",
    "Trending films/series for today or this week. Use only when there is no preference data.",
    TrendingArgs, get_trending, agent_visible=True,
))  # fmt: skip
register(Tool(
    "get_recommendations",
    "Deterministic personalised recommendations. Explicit genre wins, disliked and watched titles "
    "are excluded, liked genres raise the score, recent repeats are penalised. The result says "
    "whether it is personalised; if not, repeat that it is a general suggestion.",
    RecommendationArgs, get_recommendations, agent_visible=True,
))  # fmt: skip
register(Tool(
    "set_preference",
    "Record liked, disliked or watched. These are different facts: watched does not mean liked. "
    "Idempotent.",
    PreferenceArgs, set_preference, agent_visible=True,
))  # fmt: skip
register(Tool(
    "remove_preference",
    "Undo a wrong liked, disliked or watched record. Does not touch the watchlist.",
    RemovePreferenceArgs, remove_preference, agent_visible=True,
))  # fmt: skip
register(Tool(
    "list_preferences",
    "List the owner's liked, disliked or watched titles, newest first.",
    ListPreferencesArgs, list_preferences, agent_visible=True,
))  # fmt: skip
register(Tool("watchlist_add", "Add one title to the owner's watchlist (idempotent).",
             WatchlistArgs, watchlist_add, agent_visible=True))  # fmt: skip
register(Tool("watchlist_remove", "Remove one title from the owner's watchlist.",
             WatchlistArgs, watchlist_remove, agent_visible=True))  # fmt: skip
register(Tool("watchlist_list", "List the owner's watchlist, newest first.",
             WatchlistListArgs, watchlist_list, agent_visible=True))  # fmt: skip
register(Tool(
    "import_title_list",
    "Import several titles at once as liked, watched or watchlist. Exact matches are recorded; "
    "ambiguous titles are kept as pending and returned with candidates. Never pick for the owner.",
    ImportArgs, import_title_list, agent_visible=True,
))  # fmt: skip
register(Tool(
    "resolve_pending",
    "Record the owner's choice for one ambiguous imported title. choice_index is 1-based.",
    ResolvePendingArgs, resolve_pending, agent_visible=True,
))  # fmt: skip
register(Tool(
    "make_google_search_link",
    "Build a Google search URL for a Persian title. Only adds «دوبله فارسی» or «زیرنویس فارسی» "
    "when the owner explicitly asked. Never scrapes and never claims availability or legality.",
    GoogleLinkArgs, make_google_search_link, agent_visible=True,
))  # fmt: skip
register(Tool(
    "get_recent_list",
    "The list most recently shown in this chat with 1-based positions, so «دومی» can be resolved "
    "without guessing.",
    RecentListArgs, get_recent_list, agent_visible=True,
))  # fmt: skip

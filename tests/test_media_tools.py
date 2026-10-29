import pytest

from app.db.models import User
from app.media import container
from app.media.providers import MediaUnavailable
from app.tools import impl
from app.tools.registry import ToolContext, run_tool
from tests.media_fakes import FakeCatalog, add_media


@pytest.fixture
def media(monkeypatch, session):
    a = add_media(
        session, tmdb_id=100, title_fa="ماتریکس", original_title="The Matrix", year=1999,
        genres=[(28, "اکشن")], poster_path="/m.jpg",
    )  # fmt: skip
    b = add_media(
        session, tmdb_id=101, title_fa="ماتریکس", original_title="The Matrix", year=2021,
        genres=[(28, "اکشن")], poster_path=None,
    )  # fmt: skip
    tv = add_media(
        session, tmdb_id=200, title_fa="بریکینگ بد", original_title="Breaking Bad",
        media_type="tv", year=2008, genres=[(18, "درام")],
    )  # fmt: skip
    unique = add_media(
        session, tmdb_id=102, title_fa="فیلم یکتا", original_title="Unique", year=2020
    )  # fmt: skip
    catalog = FakeCatalog(
        items=[a, b, tv, unique],
        search_results={
            ("movie", "The Matrix"): [a, b],
            ("movie", "Unique"): [unique],
            ("tv", "Breaking Bad"): [tv],
        },
    )
    monkeypatch.setattr(container, "catalog_for", lambda s: catalog)
    return catalog, a, b, tv


def test_search_movie_and_tv_are_separate(ctx, media):
    _, a, b, tv = media
    movie = run_tool(ctx, "search_movie", {"title": "The Matrix"})
    assert movie.ok and movie.data["match_type"] == "ambiguous"
    assert {i["id"] for i in movie.data["items"]} == {a.id, b.id}
    assert any(i["poster_url"] for i in movie.data["items"])
    assert any(i["poster_url"] is None for i in movie.data["items"])

    show = run_tool(ctx, "search_tv", {"title": "Breaking Bad"})
    assert show.ok and show.data["items"][0]["media_type"] == "tv"
    assert show.data["match_type"] == "exact"


def test_exact_match_is_reported(ctx, media):
    r = run_tool(ctx, "search_movie", {"title": "Unique"})
    assert r.ok and r.data["match_type"] == "exact"
    assert r.data["items"][0]["tmdb_id"] == 102


def test_provider_errors_are_errors_not_empty_results(ctx, media, monkeypatch):
    class Down(FakeCatalog):
        def search(self, media_type, title, year=None, limit=5):
            raise MediaUnavailable("TMDb down")

    monkeypatch.setattr(container, "catalog_for", lambda s: Down())
    r = run_tool(ctx, "search_movie", {"title": "x"})
    assert r.ok is False and "TMDb down" in r.error


def test_validation_is_pydantic(ctx):
    assert run_tool(ctx, "search_movie", {"title": ""}).ok is False
    assert run_tool(ctx, "search_movie", {"title": "x", "year": 1200}).ok is False
    assert run_tool(ctx, "get_recommendations", {"count": 99}).ok is False


def test_preference_and_watchlist_tools_are_idempotent_and_isolated(ctx, session, media):
    _, a, _, _ = media
    assert run_tool(ctx, "set_preference", {"tmdb_id": 100, "media_type": "movie", "action": "liked"}).ok
    assert run_tool(ctx, "set_preference", {"tmdb_id": 100, "media_type": "movie", "action": "liked"}).ok
    assert run_tool(ctx, "watchlist_add", {"tmdb_id": 100, "media_type": "movie"}).data["created"] is True
    assert run_tool(ctx, "watchlist_add", {"tmdb_id": 100, "media_type": "movie"}).data["created"] is False
    listed = run_tool(ctx, "watchlist_list", {})
    assert listed.ok and [i["id"] for i in listed.data["items"]] == [a.id]

    other = User(telegram_user_id=999, name="Other")
    session.add(other)
    session.flush()
    other_ctx = ToolContext(session=session, user=other, chat_id=2)
    assert run_tool(other_ctx, "watchlist_list", {}).data["items"] == []

    assert run_tool(ctx, "watchlist_remove", {"tmdb_id": 100, "media_type": "movie"}).data["removed"]
    assert run_tool(ctx, "watchlist_list", {}).data["count"] == 0


def test_remove_and_list_preferences(ctx, media):
    _, a, _, _ = media
    assert run_tool(ctx, "set_preference", {"tmdb_id": 100, "media_type": "movie", "action": "watched"}).ok
    listed = run_tool(ctx, "list_preferences", {"action": "watched"})
    assert listed.ok and [i["id"] for i in listed.data["items"]] == [a.id]

    removed = run_tool(
        ctx, "remove_preference", {"tmdb_id": 100, "media_type": "movie", "action": "watched"}
    )
    assert removed.ok and removed.data["removed"] is True
    assert run_tool(ctx, "list_preferences", {"action": "watched"}).data["count"] == 0


def test_import_and_resolve_via_tools(ctx, media):
    r = run_tool(ctx, "import_title_list", {"titles": ["The Matrix"], "target_action": "watched"})
    assert r.ok and len(r.data["needs_confirmation"]) == 1
    pending = r.data["needs_confirmation"][0]
    resolved = run_tool(
        ctx,
        "resolve_pending",
        {"pending_id": pending["pending_id"], "choice_index": 2},
    )
    assert resolved.ok and resolved.data["item"]["tmdb_id"] == 101
    again = run_tool(
        ctx, "resolve_pending", {"pending_id": pending["pending_id"], "choice_index": 2}
    )
    assert again.ok and again.data["already_resolved"] is True


def test_recent_list_positions(ctx, media):
    run_tool(ctx, "search_movie", {"title": "The Matrix"})
    recent = run_tool(ctx, "get_recent_list", {})
    assert recent.ok
    assert [i["position"] for i in recent.data["items"]] == [1, 2]
    assert recent.data["items"][1]["item"]["tmdb_id"] == 101


def test_google_link_tool(ctx):
    r = run_tool(
        ctx, "make_google_search_link", {"title_fa": "ماتریکس", "year": 1999, "mode": "dubbed"}
    )
    assert r.ok and r.data["query"] == "ماتریکس 1999 دوبله فارسی"
    assert r.data["url"].startswith("https://www.google.com/search?q=")

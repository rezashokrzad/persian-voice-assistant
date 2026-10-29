"""MCP media tools use the same service layer as Telegram, without any network calls."""

import contextlib

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AuditEvent
from app.mcp.service import McpService, McpToolError
from app.media import container
from app.media.contexts import ContextService
from app.media.import_service import ImportService
from app.media.preferences import PreferenceService
from app.media.recommender import RecommendationService
from app.media.container import MediaServices
from tests.conftest import FIXED_NOW
from tests.media_fakes import FakeCatalog, add_media


def _services(session, catalog):
    preferences = PreferenceService(session)
    contexts = ContextService(session, now=lambda: FIXED_NOW)
    return MediaServices(
        catalog=catalog,
        preferences=preferences,
        contexts=contexts,
        recommendations=RecommendationService(session, catalog, preferences, contexts),
        imports=ImportService(session, catalog, preferences, contexts),
    )


@pytest.fixture
def media(session, user, monkeypatch):
    exact = add_media(session, tmdb_id=603, title_fa="ماتریکس", genres=[(28, "اکشن")])
    first = add_media(session, tmdb_id=700, title_fa="آرو", year=2000, genres=[(18, "درام")])
    second = add_media(session, tmdb_id=701, title_fa="آرو", year=2010, genres=[(18, "درام")])
    catalog = FakeCatalog(
        items=[exact, first, second],
        search_results={
            ("movie", "ماتریکس"): [exact],
            ("tv", "ماتریکس"): [],
            ("movie", "آرو"): [first, second],
            ("tv", "آرو"): [],
        },
    )
    services = _services(session, catalog)
    monkeypatch.setattr(container, "services_for", lambda _session: services)
    monkeypatch.setattr(get_settings(), "allowed_user_ids", "111")
    return McpService(lambda: contextlib.nullcontext(session), now=lambda: FIXED_NOW), catalog, services


def _events(session):
    return list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))


def test_search_uses_the_shared_catalog_and_records_the_shown_list(media, session, user):
    svc, catalog, services = media
    result = svc.search_movie("ماتریکس")

    assert result["match_type"] == "exact"
    assert result["items"][0]["tmdb_id"] == 603
    assert result["items"][0]["title_fa"] == "ماتریکس"
    # The shown list is reusable by «دومی» in the same chat.
    recent = services.contexts.recent(user.id, 0)
    assert [item.tmdb_id for _, item in recent] == [603]


def test_media_writes_require_confirmation_and_are_audited(media, session, user):
    svc, _, services = media

    preview = svc.set_preference(603, "movie", "liked")
    assert preview["status"] == "confirmation_required"
    assert services.preferences.ids(user.id, "liked") == set()
    assert _events(session)[-1].event_type == "mcp_confirmation_required"

    done = svc.set_preference(603, "movie", "liked", confirm=True)
    assert done["status"] == "done" and done["created"] is True
    assert services.preferences.ids(user.id, "liked") == {done["media_id"]}
    assert _events(session)[-1].event_type == "mcp_write_executed"

    listed = svc.list_preferences("liked")
    assert [i["id"] for i in listed["items"]] == [done["media_id"]]
    removed = svc.remove_preference(603, "movie", "liked", confirm=True)
    assert removed["status"] == "done" and removed["removed"] is True
    assert services.preferences.ids(user.id, "liked") == set()


def test_ambiguous_import_is_pending_then_resolved_by_the_owner(media, session, user):
    svc, _, services = media

    report = svc.import_title_list(["آرو"], "liked", confirm=True)
    assert report["added"] == []
    (pending,) = report["needs_confirmation"]
    assert [c["year"] for c in pending["candidates"]] == [2000, 2010]
    assert services.preferences.ids(user.id, "liked") == set()

    resolved = svc.resolve_pending(pending["pending_id"], 2)
    assert resolved["already_resolved"] is False
    assert resolved["item"]["year"] == 2010
    assert services.preferences.ids(user.id, "liked") == {resolved["item"]["id"]}


def test_unknown_media_is_a_clear_mcp_error(media):
    svc, _, _ = media
    with pytest.raises(McpToolError):
        svc.set_preference(999999, "movie", "liked", confirm=True)

import datetime as dt

import pytest
from sqlalchemy import select

from app.db.models import User, UserMediaPreference
from app.media.contexts import ContextService
from app.media.import_service import ImportService, parse_title
from app.media.preferences import PreferenceService
from app.media.providers import MediaUnavailable
from app.media.recommender import PUBLIC_NOTE, RecommendationService
from tests.media_fakes import FakeCatalog, add_media


def test_preferences_are_idempotent_and_user_scoped(session, user):
    a = add_media(session, tmdb_id=1, title_fa="فیلم الف", genres=[(35, "کمدی")])
    b = add_media(session, tmdb_id=2, title_fa="فیلم ب", genres=[(18, "درام")])
    other = User(telegram_user_id=222, name="Other")
    session.add(other)
    session.flush()

    prefs = PreferenceService(session)
    first = prefs.set(user.id, a.id, "liked")
    second = prefs.set(user.id, a.id, "liked")
    assert first["created"] is True and second["created"] is False
    assert prefs.set(user.id, b.id, "watched")["created"] is True

    # watched is not liked: both rows exist independently
    assert prefs.ids(user.id, "liked") == {a.id}
    assert prefs.ids(user.id, "watched") == {b.id}
    assert prefs.ids(user.id, "watchlist") == set()

    # isolation: another user sees nothing and cannot affect the first user's lists
    assert prefs.watchlist(other.id) == []
    prefs.watchlist_add(other.id, a.id)
    assert [i.id for i in prefs.watchlist(other.id)] == [a.id]
    assert prefs.watchlist(user.id) == []
    assert session.scalar(select(UserMediaPreference).where(UserMediaPreference.user_id == other.id))


def test_watchlist_add_is_idempotent_and_remove_works(session, user):
    item = add_media(session, tmdb_id=3, title_fa="سریال الف", media_type="tv")
    prefs = PreferenceService(session)
    assert prefs.watchlist_add(user.id, item.id)["created"] is True
    assert prefs.watchlist_add(user.id, item.id)["created"] is False
    assert len(prefs.watchlist(user.id)) == 1
    assert prefs.watchlist_remove(user.id, item.id)["removed"] is True
    assert prefs.watchlist_remove(user.id, item.id)["removed"] is False
    assert prefs.watchlist(user.id) == []


def test_context_positions_replacement_and_expiry(session, user):
    a = add_media(session, tmdb_id=10, title_fa="اول")
    b = add_media(session, tmdb_id=11, title_fa="دوم")
    c = add_media(session, tmdb_id=12, title_fa="سوم")
    clock = [dt.datetime(2026, 10, 18, 12, tzinfo=dt.timezone.utc)]
    ctx = ContextService(session, ttl_minutes=30, now=lambda: clock[0])

    assert ctx.record_list(user.id, 5, [a, b]) == 2
    assert [(p, i.id) for p, i in ctx.recent(user.id, 5)] == [(1, a.id), (2, b.id)]
    assert ctx.recent(user.id, 6) == []  # other chats never see this list

    ctx.record_list(user.id, 5, [c])
    assert [(p, i.id) for p, i in ctx.recent(user.id, 5)] == [(1, c.id)]
    # superseded rows still count for the repeat penalty within the TTL
    assert {a.id, b.id, c.id} <= ctx.recent_media_ids(user.id, 5)

    clock[0] += dt.timedelta(minutes=31)
    assert ctx.recent(user.id, 5) == []
    assert ctx.recent_media_ids(user.id, 5) == set()


def test_recommendations_respect_explicit_genre_and_exclude_disliked_watched(session, user):
    comedy = add_media(session, tmdb_id=20, title_fa="کمدی", genres=[(35, "کمدی")])
    drama = add_media(session, tmdb_id=21, title_fa="درام", genres=[(18, "درام")])
    horror = add_media(session, tmdb_id=22, title_fa="ترسناک", genres=[(27, "ترسناک")])
    catalog = FakeCatalog(items=[comedy, drama, horror], genre_map={35: "کمدی", 18: "درام", 27: "ترسناک"})
    prefs = PreferenceService(session)
    ctx = ContextService(session)
    rec = RecommendationService(session, catalog, prefs, ctx)
    prefs.set(user.id, horror.id, "disliked")
    prefs.set(user.id, drama.id, "watched")

    result = rec.recommend(user.id, 7, media_type="movie", genre="کمدی", count=5)
    assert catalog.discover_calls[-1]["genre_ids"] == [35]
    assert [r.item.id for r in result.items] == [comedy.id]
    assert any("کمدی" in w for w in result.items[0].why)
    assert result.personalized is True


def test_liked_genres_raise_the_score_and_why_is_grounded(session, user):
    liked_genre = add_media(
        session, tmdb_id=30, title_fa="کمدی دوست‌داشتنی", genres=[(35, "کمدی")], vote_average=6.0
    )
    other = add_media(session, tmdb_id=31, title_fa="درام", genres=[(18, "درام")], vote_average=9.0)
    catalog = FakeCatalog(items=[liked_genre, other], genre_map={35: "کمدی", 18: "درام"})
    prefs = PreferenceService(session)
    prefs.set(user.id, liked_genre.id, "liked")
    result = RecommendationService(session, catalog, prefs, ContextService(session)).recommend(
        user.id, 8, media_type="movie", count=2
    )
    assert [r.item.id for r in result.items] == [liked_genre.id, other.id]
    assert any("کمدی" in w for w in result.items[0].why)
    assert result.items[0].score > result.items[1].score


def test_no_history_uses_trending_and_says_it_is_not_personalized(session, user):
    a = add_media(session, tmdb_id=40, title_fa="محبوب")
    catalog = FakeCatalog(items=[a], trending_results=[a])
    result = RecommendationService(
        session, catalog, PreferenceService(session), ContextService(session)
    ).recommend(user.id, 9, count=3)
    assert result.personalized is False
    assert result.source == "trending"
    assert result.note == PUBLIC_NOTE
    assert catalog.trending_calls == [("all", "week")]


def test_recent_titles_are_penalised(session, user):
    recent_item = add_media(session, tmdb_id=50, title_fa="تکراری", vote_average=8.0)
    fresh = add_media(session, tmdb_id=51, title_fa="تازه", vote_average=5.0)
    catalog = FakeCatalog(items=[recent_item, fresh], trending_results=[recent_item, fresh])
    ctx = ContextService(session)
    ctx.record_list(user.id, 10, [recent_item])
    result = RecommendationService(session, catalog, PreferenceService(session), ctx).recommend(
        user.id, 10, count=2
    )
    assert [r.item.id for r in result.items] == [fresh.id, recent_item.id]
    assert result.items[1].recently_shown is True
    assert any("اخیراً" in w for w in result.items[1].why)


def test_import_exact_ambiguous_and_not_found(session, user):
    exact = add_media(session, tmdb_id=60, title_fa="Inception", year=2010)
    old = add_media(session, tmdb_id=61, title_fa="Dune", year=1984)
    new = add_media(session, tmdb_id=62, title_fa="Dune", year=2021)
    catalog = FakeCatalog(
        search_results={
            ("movie", "Inception"): [exact],
            ("movie", "Dune"): [old, new],
        }
    )
    svc = ImportService(session, catalog, PreferenceService(session), ContextService(session))
    report = svc.import_titles(
        user.id, 11, ["Inception 2010", "Dune", "Nothing Here"], "watched"
    )
    assert [i.id for i in report.added] == [exact.id]
    assert len(report.needs_confirmation) == 1
    assert report.needs_confirmation[0].title == "Dune"
    assert len(report.needs_confirmation[0].candidates) == 2
    assert report.not_found == ["Nothing Here"]
    # exact match only: the ambiguous title was not silently chosen
    assert PreferenceService(session).ids(user.id, "watched") == {exact.id}


def test_resolve_pending_is_owner_scoped_and_idempotent(session, user):
    old = add_media(session, tmdb_id=70, title_fa="Dune", year=1984)
    new = add_media(session, tmdb_id=71, title_fa="Dune", year=2021)
    catalog = FakeCatalog(search_results={("movie", "Dune"): [old, new]})
    svc = ImportService(session, catalog, PreferenceService(session), ContextService(session))
    report = svc.import_titles(user.id, 12, ["Dune"], "watchlist")
    pending_id = report.needs_confirmation[0].pending_id

    other = User(telegram_user_id=333, name="Other")
    session.add(other)
    session.flush()
    with pytest.raises(ValueError):
        svc.resolve_pending(other.id, 12, pending_id, 1)  # different user
    with pytest.raises(ValueError):
        svc.resolve_pending(user.id, 99, pending_id, 1)  # different chat
    with pytest.raises(ValueError):
        svc.resolve_pending(user.id, 12, pending_id, 9)  # out of range

    chosen = svc.resolve_pending(user.id, 12, pending_id, 2)
    assert chosen["item"]["id"] == new.id and chosen["action"] == "watchlist"
    assert PreferenceService(session).watchlist(user.id)[0].id == new.id
    again = svc.resolve_pending(user.id, 12, pending_id, 2)
    assert again["already_resolved"] is True


def test_import_provider_failure_is_not_not_found(session, user):
    class Failing(FakeCatalog):
        def search(self, media_type, title, year=None, limit=5):
            raise MediaUnavailable("down")

    svc = ImportService(
        session, Failing(), PreferenceService(session), ContextService(session)
    )
    with pytest.raises(MediaUnavailable):
        svc.import_titles(user.id, 13, ["Dune"], "liked")


def test_parse_title_and_persian_digits():
    assert parse_title("Dune (2021)") == ("Dune", 2021)
    assert parse_title("فیلم ۲۰۲۱") == ("فیلم", 2021)
    assert parse_title("  Inception  2010 ") == ("Inception", 2010)
    # A year-like number can be part of the title.
    assert parse_title("Blade Runner 2049") == ("Blade Runner 2049", None)
    assert parse_title("1984") == ("1984", None)

import httpx
import pytest

from app.config import get_settings
from app.db.models import MediaItem
from app.media.catalog import CatalogService
from app.media.providers import (
    MediaRateLimited,
    MediaTimeout,
    MediaUnavailable,
    OMDbClient,
    TMDbClient,
)


def tmdb(handler, monkeypatch, *, retries=0):
    s = get_settings()
    monkeypatch.setattr(s, "tmdb_api_key", "tmdb-key")
    monkeypatch.setattr(s, "omdb_api_key", "")
    return TMDbClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=s,
        retries=retries,
        sleep=lambda _: None,
    )


def omdb(handler, monkeypatch, *, retries=0):
    s = get_settings()
    monkeypatch.setattr(s, "omdb_api_key", "omdb-key")
    return OMDbClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=s,
        retries=retries,
        sleep=lambda _: None,
    )


def genre_handler(request):
    return httpx.Response(200, json={"genres": [{"id": 35, "name": "کمدی"}]})


def test_search_movie_success_and_poster(monkeypatch):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/genre/movie/list"):
            return genre_handler(request)
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 603,
                        "title": "ماتریکس",
                        "original_title": "The Matrix",
                        "release_date": "1999-03-30",
                        "overview": "خلاصه",
                        "genre_ids": [35],
                        "vote_average": 8.2,
                        "poster_path": "/abc.jpg",
                        "popularity": 100,
                    }
                ]
            },
        )

    out = tmdb(handler, monkeypatch).search("movie", "ماتریکس", 1999)
    assert seen["path"].endswith("/search/movie")
    assert seen["params"]["year"] == "1999" and seen["params"]["language"] == "fa-IR"
    assert out[0].tmdb_id == 603 and out[0].year == 1999
    assert out[0].poster_url.endswith("/w500/abc.jpg")
    assert out[0].genres == [{"id": 35, "name": "کمدی"}]


def test_search_tv_uses_the_tv_endpoint_and_air_year(monkeypatch):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/genre/tv/list"):
            return genre_handler(request)
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": []})

    out = tmdb(handler, monkeypatch).search("tv", "Breaking Bad", 2008)
    assert out == []  # an empty result is not an error
    assert seen["path"].endswith("/search/tv")
    assert seen["params"]["first_air_date_year"] == "2008"


def test_poster_missing_is_none(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/genre/movie/list"):
            return httpx.Response(200, json={"genres": []})
        return httpx.Response(
            200,
            json={"results": [{"id": 1, "title": "x", "original_title": "x", "poster_path": None}]},
        )

    out = tmdb(handler, monkeypatch).search("movie", "x")
    assert out[0].poster_url is None and out[0].poster_path is None


def test_timeout_is_not_an_empty_result(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(MediaTimeout):
        tmdb(handler, monkeypatch).search("movie", "x")


def test_429_retries_then_reports_rate_limit(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"})

    client = tmdb(handler, monkeypatch, retries=1)
    with pytest.raises(MediaRateLimited):
        client.search("movie", "x")
    assert len(calls) == 2


def test_500_is_a_provider_error_not_not_found(monkeypatch):
    def handler(request):
        return httpx.Response(503, json={"error": "down"})

    with pytest.raises(MediaUnavailable):
        tmdb(handler, monkeypatch).search("movie", "x")


def test_omdb_enriches_missing_imdb_without_overwriting_tmdb(monkeypatch, session):
    def thandler(request):
        if request.url.path.endswith("/genre/movie/list"):
            return httpx.Response(200, json={"genres": []})
        return httpx.Response(
            200,
            json={
                "id": 603,
                "title": "ماتریکس",
                "original_title": "The Matrix",
                "release_date": "1999-03-30",
                "overview": None,
                "vote_average": 8.7,
                "poster_path": "/p.jpg",
                "genres": [],
            },
        )

    def ohandler(request):
        return httpx.Response(
            200,
            json={
                "Response": "True",
                "imdbID": "tt0133093",
                "Title": "The Matrix",
                "Year": "1999",
                "Type": "movie",
                "Plot": "داستان ماتریکس",
                "imdbRating": "9.9",
            },
        )

    catalog = CatalogService(session, tmdb(thandler, monkeypatch), omdb(ohandler, monkeypatch))
    item, warning = catalog.details(603, "movie")
    assert warning is None
    assert item.imdb_id == "tt0133093"
    assert item.title_fa == "ماتریکس"  # OMDb's English title never replaces the Persian one
    assert item.overview == "داستان ماتریکس"  # filled because TMDb had none
    assert item.vote_average == 8.7 and item.vote_source == "tmdb"  # never downgraded
    assert session.query(MediaItem).count() == 1


def test_omdb_rating_is_used_only_when_tmdb_has_none(monkeypatch, session):
    def thandler(request):
        return httpx.Response(
            200,
            json={
                "id": 10,
                "title": "x",
                "original_title": "x",
                "overview": "موجود",
                "vote_average": None,
                "genres": [],
            },
        )

    def ohandler(request):
        return httpx.Response(
            200,
            json={
                "Response": "True",
                "imdbID": "tt1",
                "Title": "x",
                "Year": "2000",
                "Type": "movie",
                "Plot": "plot",
                "imdbRating": "6.5",
            },
        )

    item, _ = CatalogService(session, tmdb(thandler, monkeypatch), omdb(ohandler, monkeypatch)).details(
        10, "movie"
    )
    assert item.vote_average == 6.5 and item.vote_source == "omdb"
    assert item.overview == "موجود"


def test_omdb_failure_is_reported_but_keeps_good_tmdb_data(monkeypatch, session):
    def thandler(request):
        return httpx.Response(
            200,
            json={
                "id": 11,
                "title": "x",
                "original_title": "x",
                "overview": "خلاصه تی‌ام‌دی‌بی",
                "vote_average": 7.5,
                "genres": [],
            },
        )

    def ohandler(request):
        return httpx.Response(500, json={"error": "down"})

    item, warning = CatalogService(
        session, tmdb(thandler, monkeypatch), omdb(ohandler, monkeypatch)
    ).details(11, "movie")
    assert warning == "provider_unavailable"
    assert item.overview == "خلاصه تی‌ام‌دی‌بی" and item.vote_average == 7.5


def test_omdb_not_found_is_not_a_provider_error(monkeypatch, session):
    def thandler(request):
        return httpx.Response(
            200,
            json={
                "id": 12,
                "title": "x",
                "original_title": "x",
                "overview": "خلاصه",
                "vote_average": 7,
                "genres": [],
            },
        )

    def ohandler(request):
        return httpx.Response(200, json={"Response": "False", "Error": "Movie not found!"})

    item, warning = CatalogService(
        session, tmdb(thandler, monkeypatch), omdb(ohandler, monkeypatch)
    ).details(12, "movie")
    assert warning is None and item.imdb_id is None and item.overview == "خلاصه"

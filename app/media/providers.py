"""TMDb and OMDb clients. Provider failures are explicit: never confused with an empty result."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable

import httpx

from app.config import Settings, get_settings
from app.http_client import build_http_client
from app.media.schemas import MediaCandidate
from app.util.persian import to_fa_digits

log = logging.getLogger("media.providers")


class MediaProviderError(RuntimeError):
    code = "provider_error"


class MediaConfigError(MediaProviderError):
    code = "provider_not_configured"


class MediaTimeout(MediaProviderError):
    code = "provider_timeout"


class MediaRateLimited(MediaProviderError):
    code = "provider_rate_limited"


class MediaUnavailable(MediaProviderError):
    code = "provider_unavailable"


class MediaNotFound(MediaProviderError):
    code = "not_found"


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(max(float(raw), 0.0), 5.0)
            except ValueError:
                pass
    return min(0.5 * (attempt + 1), 2.0)


class _HttpProvider:
    def __init__(
        self,
        client: httpx.Client,
        *,
        retries: int = 1,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._retries = max(0, retries)
        self._sleep = sleep

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: str = "unknown error"
        for attempt in range(self._retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._retries:
                    self._sleep(_retry_delay(None, attempt))
                    continue
                raise MediaTimeout("The media provider timed out.") from exc
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._retries:
                    self._sleep(_retry_delay(None, attempt))
                    continue
                raise MediaUnavailable("The media provider is unreachable.") from exc

            if response.status_code == 429:
                if attempt < self._retries:
                    self._sleep(_retry_delay(response, attempt))
                    continue
                raise MediaRateLimited("The media provider rate-limited the request (HTTP 429).")
            if response.status_code == 404:
                raise MediaNotFound("The requested title was not found at the provider.")
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                if attempt < self._retries:
                    self._sleep(_retry_delay(response, attempt))
                    continue
                raise MediaUnavailable(f"The media provider failed ({last_error}).")
            if response.status_code >= 400:
                raise MediaUnavailable(f"The media provider rejected the request (HTTP {response.status_code}).")
            try:
                data = response.json()
            except ValueError as exc:
                raise MediaUnavailable("The media provider returned invalid JSON.") from exc
            if not isinstance(data, dict):
                raise MediaUnavailable("The media provider returned an unexpected payload.")
            return data
        raise MediaUnavailable(f"The media provider failed ({last_error}).")


class TMDbClient(_HttpProvider):
    """Only the endpoints the assistant needs, with Persian metadata and a poster URL helper."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        image_base_url: str | None = None,
        language: str | None = None,
        client: httpx.Client | None = None,
        settings: Settings | None = None,
        retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        s = settings or get_settings()
        self.api_key = (api_key if api_key is not None else s.tmdb_api_key).strip()
        self.base_url = (base_url or s.tmdb_base_url).rstrip("/")
        self.image_base_url = (image_base_url or s.tmdb_image_base_url).rstrip("/")
        self.language = language or s.media_language
        super().__init__(
            client or build_http_client(s),
            retries=s.media_max_retries if retries is None else retries,
            sleep=sleep,
        )
        self._genre_cache: dict[str, dict[int, str]] = {}

    def _require_key(self) -> None:
        if not self.api_key:
            raise MediaConfigError("TMDB_API_KEY is not set.")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_key()
        payload = {"api_key": self.api_key, "language": self.language, **(params or {})}
        return self._get_json(f"{self.base_url}{path}", payload)

    # ---- lookups

    def search(self, media_type: str, query: str, year: int | None = None) -> list[MediaCandidate]:
        path = "/search/movie" if media_type == "movie" else "/search/tv"
        params: dict[str, Any] = {"query": query, "include_adult": "false", "page": 1}
        if year:
            params["year" if media_type == "movie" else "first_air_date_year"] = year
        data = self._get(path, params)
        return self._candidates(data.get("results") or [], media_type)

    def details(self, media_type: str, tmdb_id: int) -> MediaCandidate:
        path = f"/{'movie' if media_type == 'movie' else 'tv'}/{int(tmdb_id)}"
        params = {"append_to_response": "external_ids"} if media_type == "tv" else None
        raw = self._get(path, params)
        return self._candidate(raw, media_type)

    def discover(
        self,
        media_type: str,
        *,
        genre_ids: Iterable[int] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_rating: float | None = None,
        sort: str | None = None,
    ) -> list[MediaCandidate]:
        path = "/discover/movie" if media_type == "movie" else "/discover/tv"
        params: dict[str, Any] = {
            "include_adult": "false",
            "page": 1,
            "sort_by": sort or "popularity.desc",
        }
        ids = [str(i) for i in (genre_ids or [])]
        if ids:
            params["with_genres"] = ",".join(ids)
        date_gte = "primary_release_date.gte" if media_type == "movie" else "first_air_date.gte"
        date_lte = "primary_release_date.lte" if media_type == "movie" else "first_air_date.lte"
        if year_from:
            params[date_gte] = f"{int(year_from)}-01-01"
        if year_to:
            params[date_lte] = f"{int(year_to)}-12-31"
        if min_rating is not None:
            params["vote_average.gte"] = min_rating
        data = self._get(path, params)
        return self._candidates(data.get("results") or [], media_type)

    def trending(self, media_type: str = "all", window: str = "week") -> list[MediaCandidate]:
        data = self._get(f"/trending/{media_type}/{window}")
        return self._candidates(data.get("results") or [], media_type)

    def genres(self, media_type: str) -> dict[int, str]:
        key = "movie" if media_type == "movie" else "tv"
        if key not in self._genre_cache:
            try:
                data = self._get(f"/genre/{key}/list")
                self._genre_cache[key] = {
                    int(g["id"]): str(g["name"]) for g in data.get("genres") or [] if "id" in g
                }
            except MediaProviderError:
                # Genre names are cosmetic; a failed genre call must not break a valid search.
                self._genre_cache[key] = {}
        return self._genre_cache[key]

    # ---- normalisation

    def _candidates(self, rows: list[dict[str, Any]], media_type: str) -> list[MediaCandidate]:
        out: list[MediaCandidate] = []
        for row in rows:
            kind = self._row_media_type(row, media_type)
            if kind is None:
                continue
            out.append(self._candidate(row, kind))
        return out

    @staticmethod
    def _row_media_type(row: dict[str, Any], fallback: str) -> str | None:
        if fallback in ("movie", "tv"):
            return fallback
        kind = row.get("media_type")
        return kind if kind in ("movie", "tv") else None

    def _candidate(self, row: dict[str, Any], media_type: str) -> MediaCandidate:
        tmdb_id = int(row.get("id") or 0)
        title = row.get("title") or row.get("name") or row.get("original_title") or row.get("original_name") or ""
        original = row.get("original_title") or row.get("original_name") or title
        date = row.get("release_date") or row.get("first_air_date") or ""
        year = int(str(date)[:4]) if str(date)[:4].isdigit() else None
        raw_genres = row.get("genres")
        if isinstance(raw_genres, list) and raw_genres:
            genres = [
                {"id": int(g["id"]), "name": str(g.get("name") or "")}
                for g in raw_genres
                if isinstance(g, dict) and "id" in g
            ]
        else:
            names = self.genres(media_type) if row.get("genre_ids") else {}
            genres = [
                {"id": int(gid), "name": names.get(int(gid), "")}
                for gid in row.get("genre_ids") or []
            ]
        imdb_id = row.get("imdb_id") or (row.get("external_ids") or {}).get("imdb_id")
        if not imdb_id and row.get("imdb_id") is None and "imdb_id" in row:
            imdb_id = row.get("imdb_id")
        poster_path = row.get("poster_path") or None
        vote = row.get("vote_average")
        return MediaCandidate(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title_fa=title or original,
            original_title=original,
            year=year,
            overview=row.get("overview") or None,
            genre_ids=[g["id"] for g in genres],
            genres=genres,
            vote_average=float(vote) if vote is not None else None,
            vote_source="tmdb" if vote is not None else None,
            poster_path=poster_path,
            poster_url=f"{self.image_base_url}{poster_path}" if poster_path else None,
            imdb_id=imdb_id or None,
            popularity=float(row.get("popularity") or 0.0),
        )


class OMDbClient(_HttpProvider):
    """Secondary source, deliberately limited to IMDb IDs and missing fields."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        settings: Settings | None = None,
        retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        s = settings or get_settings()
        self.api_key = (api_key if api_key is not None else s.omdb_api_key).strip()
        self.base_url = (base_url or s.omdb_base_url).rstrip("/")
        super().__init__(
            client or build_http_client(s),
            retries=s.media_max_retries if retries is None else retries,
            sleep=sleep,
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _lookup(self, params: dict[str, Any]) -> MediaCandidate | None:
        if not self.api_key:
            raise MediaConfigError("OMDB_API_KEY is not set.")
        data = self._get_json(self.base_url, {"apikey": self.api_key, "plot": "full", **params})
        if str(data.get("Response", "")).lower() == "false":
            return None  # a real "not found", not a provider failure
        imdb_id = data.get("imdbID") or None
        if not imdb_id:
            return None
        year_raw = str(data.get("Year") or "")
        year = int(year_raw[:4]) if year_raw[:4].isdigit() else None
        rating = data.get("imdbRating")
        try:
            vote = float(rating) if rating not in (None, "", "N/A") else None
        except (TypeError, ValueError):
            vote = None
        kind = "tv" if str(data.get("Type", "")).lower() in ("series", "episode") else "movie"
        title = str(data.get("Title") or "")
        return MediaCandidate(
            tmdb_id=0,
            media_type=kind,
            title_fa=title,
            original_title=title,
            year=year,
            overview=(data.get("Plot") if data.get("Plot") not in (None, "N/A") else None),
            vote_average=vote,
            vote_source="omdb" if vote is not None else None,
            imdb_id=imdb_id,
        )

    def by_imdb(self, imdb_id: str) -> MediaCandidate | None:
        return self._lookup({"i": imdb_id})

    def by_title(self, title: str, year: int | None = None) -> MediaCandidate | None:
        params: dict[str, Any] = {"t": title}
        if year:
            params["y"] = year
        return self._lookup(params)


def fa_year(year: int | None) -> str:
    return to_fa_digits(year) if year else ""

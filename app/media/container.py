"""Lazy wiring for the media services. Tools and MCP both go through here."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.media.catalog import CatalogService
from app.media.contexts import ContextService
from app.media.import_service import ImportService
from app.media.preferences import PreferenceService
from app.media.providers import OMDbClient, TMDbClient
from app.media.recommender import RecommendationService

_lock = threading.Lock()
_tmdb: TMDbClient | None = None
_omdb: OMDbClient | None = None


def tmdb_client() -> TMDbClient:
    global _tmdb
    with _lock:
        if _tmdb is None:
            _tmdb = TMDbClient()
        return _tmdb


def omdb_client() -> OMDbClient:
    global _omdb
    with _lock:
        if _omdb is None:
            _omdb = OMDbClient()
        return _omdb


def reset_clients() -> None:
    """Tests use this after changing settings; production never needs it."""
    global _tmdb, _omdb
    with _lock:
        _tmdb = None
        _omdb = None


def catalog_for(session: Session) -> CatalogService:
    return CatalogService(session, tmdb_client(), omdb_client())


def preferences_for(session: Session) -> PreferenceService:
    return PreferenceService(session)


def contexts_for(session: Session) -> ContextService:
    return ContextService(session)


def recommender_for(session: Session, catalog: CatalogService | None = None) -> RecommendationService:
    catalog = catalog or catalog_for(session)
    return RecommendationService(
        session, catalog, PreferenceService(session), ContextService(session)
    )


def imports_for(session: Session, catalog: CatalogService | None = None) -> ImportService:
    catalog = catalog or catalog_for(session)
    return ImportService(session, catalog, PreferenceService(session), ContextService(session))


@dataclass
class MediaServices:
    catalog: CatalogService
    preferences: PreferenceService
    contexts: ContextService
    recommendations: RecommendationService
    imports: ImportService


def services_for(session: Session, catalog: CatalogService | None = None) -> MediaServices:
    catalog = catalog or catalog_for(session)
    return MediaServices(
        catalog=catalog,
        preferences=PreferenceService(session),
        contexts=ContextService(session),
        recommendations=RecommendationService(
            session, catalog, PreferenceService(session), ContextService(session)
        ),
        imports=ImportService(session, catalog, PreferenceService(session), ContextService(session)),
    )

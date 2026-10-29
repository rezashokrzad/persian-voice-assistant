"""MCP server: python -m app.mcp.server [--transport stdio|streamable-http]"""

import argparse
import json
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import get_settings
from app.logging_config import setup_logging
from app.mcp.service import McpService

INSTRUCTIONS = (
    "Tools for the owner's personal assistant (Persian speaker, timezone Asia/Tehran): calendar, "
    "email, and films/series. create_calendar_event and send_email change the real world, and media "
    "preferences are local writes: all of them do nothing unless confirm=true, so first show the user "
    "what you will do and call again with confirm=true only after they agree. Never invent email "
    "addresses: use find_contact. For ambiguous film/TV titles show 3-5 candidates and let the owner "
    "choose; never pick for them."
)


def build_server(service: McpService | None = None, host: str = "127.0.0.1", port: int = 8765) -> FastMCP:
    svc = service or McpService()
    mcp = FastMCP(
        "voice-assistant",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )

    @mcp.tool()
    def find_contact(name: str) -> dict:
        """Find saved contacts by name or alias (Persian or English). Returns name and email; an empty list means unknown."""
        return svc.find_contact(name)

    @mcp.tool()
    def list_calendar_events(date: str) -> dict:
        """List calendar events on one date (YYYY-MM-DD, Asia/Tehran)."""
        return svc.list_calendar_events(date)

    @mcp.tool()
    def create_calendar_event(
        date: str,
        time: str,
        duration_min: int,
        title: str,
        description: str = "",
        guests: list[str] | None = None,
        confirm: bool = False,
    ) -> dict:
        """Create a Google Calendar event (date YYYY-MM-DD, time HH:MM Asia/Tehran) and invite guests by email.
        Requires confirm=true; without it nothing happens and a preview is returned."""
        return svc.create_calendar_event(date, time, duration_min, title, description, guests, confirm)

    @mcp.tool()
    def send_email(to: str, subject: str, body: str, confirm: bool = False) -> dict:
        """Send one email to one recipient. Requires confirm=true; without it nothing is sent."""
        return svc.send_email(to, subject, body, confirm)

    # ---- films and series

    @mcp.tool()
    def search_movie(title: str, year: int | None = None) -> dict:
        """Search films on TMDb. Returns candidates with internal id, tmdb_id, year and match_type.
        Never pick an ambiguous match for the owner."""
        return svc.search_movie(title, year)

    @mcp.tool()
    def search_tv(title: str, year: int | None = None) -> dict:
        """Search TV series on TMDb (same rules as search_movie)."""
        return svc.search_tv(title, year)

    @mcp.tool()
    def get_title_details(tmdb_id: int, media_type: str) -> dict:
        """Full details for one TMDb id (media_type: movie or tv), including IMDb id when available."""
        return svc.get_title_details(tmdb_id, media_type)

    @mcp.tool()
    def discover_movies(
        genre_ids: list[int] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_rating: float | None = None,
        sort: str | None = None,
    ) -> dict:
        """Discover films by TMDb genre ids, year range and minimum rating."""
        return svc.discover_movies(genre_ids, year_from, year_to, min_rating, sort)

    @mcp.tool()
    def discover_tv(
        genre_ids: list[int] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_rating: float | None = None,
        sort: str | None = None,
    ) -> dict:
        """Discover TV series by TMDb genre ids, year range and minimum rating."""
        return svc.discover_tv(genre_ids, year_from, year_to, min_rating, sort)

    @mcp.tool()
    def get_trending(media_type: str = "all", window: str = "week") -> dict:
        """Trending titles (media_type: movie, tv or all; window: day or week)."""
        return svc.get_trending(media_type, window)

    @mcp.tool()
    def get_recommendations(
        media_type: str | None = None, genre: str | None = None, count: int = 5
    ) -> dict:
        """Deterministic recommendations. Explicit genre wins, disliked/watched are excluded.
        If personalized is false, tell the user it is a general suggestion."""
        return svc.get_recommendations(media_type, genre, count)

    @mcp.tool()
    def set_preference(tmdb_id: int, media_type: str, action: str, confirm: bool = False) -> dict:
        """Record liked, disliked or watched (watched is not liked). Requires confirm=true."""
        return svc.set_preference(tmdb_id, media_type, action, confirm)

    @mcp.tool()
    def remove_preference(
        tmdb_id: int, media_type: str, action: str, confirm: bool = False
    ) -> dict:
        """Undo a wrong liked/disliked/watched record. Requires confirm=true."""
        return svc.remove_preference(tmdb_id, media_type, action, confirm)

    @mcp.tool()
    def list_preferences(action: str) -> dict:
        """List the owner's liked, disliked or watched titles (newest first)."""
        return svc.list_preferences(action)

    @mcp.tool()
    def watchlist_add(tmdb_id: int, media_type: str, confirm: bool = False) -> dict:
        """Add a title to the watchlist. Requires confirm=true."""
        return svc.watchlist_add(tmdb_id, media_type, confirm)

    @mcp.tool()
    def watchlist_remove(tmdb_id: int, media_type: str, confirm: bool = False) -> dict:
        """Remove a title from the watchlist. Requires confirm=true."""
        return svc.watchlist_remove(tmdb_id, media_type, confirm)

    @mcp.tool()
    def watchlist_list() -> dict:
        """The owner's watchlist, newest first."""
        return svc.watchlist_list()

    @mcp.tool()
    def import_title_list(titles: list[str], target_action: str, confirm: bool = False) -> dict:
        """Import several titles as liked/watched/watchlist. Exact matches are saved; ambiguous
        titles come back with a pending_id and candidates. Requires confirm=true."""
        return svc.import_title_list(titles, target_action, confirm)

    @mcp.tool()
    def resolve_pending(pending_id: int, choice_index: int, chat_id: int = 0) -> dict:
        """Apply the owner's 1-based choice for one ambiguous imported title."""
        return svc.resolve_pending(pending_id, choice_index, chat_id)

    @mcp.tool()
    def make_google_search_link(title_fa: str, year: int | None = None, mode: str = "none") -> dict:
        """Build a Google search URL (mode: dubbed, subtitle or none). No scraping; no legality claim."""
        return svc.make_google_search_link(title_fa, year, mode)

    @mcp.tool()
    def get_recent_list(chat_id: int = 0) -> dict:
        """The most recent media list in a chat with 1-based positions, for «دومی» references."""
        return svc.get_recent_list(chat_id)

    @mcp.resource("calendar://upcoming", mime_type="application/json")
    def upcoming_events() -> str:
        """The owner's calendar events for the next 7 days."""
        return json.dumps(svc.upcoming_events(7), ensure_ascii=False)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.mcp.server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    settings = get_settings()
    # stdout carries the protocol on stdio, so logs always go to stderr here.
    setup_logging(settings.log_level, stream=sys.stderr)
    build_server(host=args.host, port=settings.mcp_port).run(transport=args.transport)


if __name__ == "__main__":
    main()

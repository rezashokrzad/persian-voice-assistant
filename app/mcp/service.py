"""MCP tool logic. Thin wrappers over the agent's own tool functions: one implementation, two interfaces."""

import contextlib
from typing import Any, Callable, ContextManager

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AuditEvent, User
from app.db.session import SessionLocal
from app.tools import impl
from app.tools.registry import ToolContext, run_tool, tehran_now

SessionFactory = Callable[[], ContextManager[Session]]


class McpToolError(ValueError):
    """Raised for failed tool calls; the MCP SDK turns it into an error result for the client."""


class McpService:
    def __init__(self, session_factory: SessionFactory = SessionLocal, now=tehran_now) -> None:
        self._session_factory = session_factory
        self._now = now

    # ---- helpers

    @contextlib.contextmanager
    def _context(self, chat_id: int = 0):
        with self._session_factory() as session:
            settings = get_settings()
            tg_id = settings.mcp_owner_telegram_id or next(iter(sorted(settings.allowed_ids)), 0)
            owner = session.scalar(select(User).where(User.telegram_user_id == tg_id)) if tg_id else None
            if owner is None:
                raise McpToolError("The owner has no user record yet: send /start to the bot or run the seed.")
            yield ToolContext(session=session, user=owner, now=self._now, chat_id=chat_id)

    @staticmethod
    def _audit(ctx: ToolContext, event: str, payload: dict[str, Any]) -> None:
        ctx.session.add(AuditEvent(event_type=event, actor="mcp", payload_json=payload))
        ctx.session.commit()

    @staticmethod
    def _call(ctx: ToolContext, tool: str, args: dict[str, Any]) -> Any:
        result = run_tool(ctx, tool, args)
        if not result.ok:
            raise McpToolError(result.error)
        return result.data

    # ---- read tools

    def find_contact(self, name: str) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "find_contact", {"name": name})

    def list_calendar_events(self, date: str) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "list_calendar_events", {"date": date})

    def upcoming_events(self, days: int = 7) -> dict:
        with self._context() as ctx:
            today = ctx.now().date()
            return {
                "from": today.isoformat(),
                "days": days,
                "events": impl.calendar().list_range(today, days),
            }

    # ---- write tools: refuse unless confirm=true, and always leave an audit trail

    def _write(self, tool: str, args: dict[str, Any], confirm: bool, summary: dict[str, Any]) -> dict:
        with self._context() as ctx:
            if not confirm:
                self._audit(ctx, "mcp_confirmation_required", {"tool": tool, **summary})
                return {
                    "status": "confirmation_required",
                    "message": f"Nothing was done. Show this to the user and call {tool} again with confirm=true "
                    "only after they approve.",
                    "preview": args,
                }
            try:
                data = self._call(ctx, tool, args)
            except McpToolError as exc:
                self._audit(ctx, "mcp_write_failed", {"tool": tool, **summary, "error": str(exc)[:300]})
                raise
            self._audit(ctx, "mcp_write_executed", {"tool": tool, **summary, "result": data})
            return {"status": "done", **data}

    # ---- media read tools

    def search_movie(self, title: str, year: int | None = None) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "search_movie", {"title": title, "year": year})

    def search_tv(self, title: str, year: int | None = None) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "search_tv", {"title": title, "year": year})

    def get_title_details(self, tmdb_id: int, media_type: str) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "get_title_details", {"tmdb_id": tmdb_id, "media_type": media_type})

    def discover_movies(
        self,
        genre_ids: list[int] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_rating: float | None = None,
        sort: str | None = None,
    ) -> dict:
        with self._context() as ctx:
            return self._call(
                ctx,
                "discover_movies",
                {
                    "genre_ids": genre_ids or [],
                    "year_from": year_from,
                    "year_to": year_to,
                    "min_rating": min_rating,
                    "sort": sort,
                },
            )

    def discover_tv(
        self,
        genre_ids: list[int] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_rating: float | None = None,
        sort: str | None = None,
    ) -> dict:
        with self._context() as ctx:
            return self._call(
                ctx,
                "discover_tv",
                {
                    "genre_ids": genre_ids or [],
                    "year_from": year_from,
                    "year_to": year_to,
                    "min_rating": min_rating,
                    "sort": sort,
                },
            )

    def get_trending(self, media_type: str = "all", window: str = "week") -> dict:
        with self._context() as ctx:
            return self._call(ctx, "get_trending", {"media_type": media_type, "window": window})

    def get_recommendations(
        self, media_type: str | None = None, genre: str | None = None, count: int = 5
    ) -> dict:
        with self._context() as ctx:
            return self._call(
                ctx,
                "get_recommendations",
                {"media_type": media_type, "genre": genre, "count": count},
            )

    def watchlist_list(self) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "watchlist_list", {})

    def list_preferences(self, action: str) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "list_preferences", {"action": action})

    def get_recent_list(self, chat_id: int = 0) -> dict:
        with self._context(chat_id) as ctx:
            return self._call(ctx, "get_recent_list", {})

    def make_google_search_link(self, title_fa: str, year: int | None = None, mode: str = "none") -> dict:
        with self._context() as ctx:
            return self._call(
                ctx, "make_google_search_link", {"title_fa": title_fa, "year": year, "mode": mode}
            )

    # ---- media writes (local, reversible; still require explicit confirmation over MCP)

    def set_preference(
        self, tmdb_id: int, media_type: str, action: str, confirm: bool = False
    ) -> dict:
        args = {"tmdb_id": tmdb_id, "media_type": media_type, "action": action}
        return self._write(
            "set_preference", args, confirm, {"tmdb_id": tmdb_id, "media_type": media_type, "action": action}
        )

    def watchlist_add(self, tmdb_id: int, media_type: str, confirm: bool = False) -> dict:
        args = {"tmdb_id": tmdb_id, "media_type": media_type}
        return self._write("watchlist_add", args, confirm, args)

    def remove_preference(self, tmdb_id: int, media_type: str, action: str, confirm: bool = False) -> dict:
        args = {"tmdb_id": tmdb_id, "media_type": media_type, "action": action}
        return self._write("remove_preference", args, confirm, args)

    def watchlist_remove(self, tmdb_id: int, media_type: str, confirm: bool = False) -> dict:
        args = {"tmdb_id": tmdb_id, "media_type": media_type}
        return self._write("watchlist_remove", args, confirm, args)

    def import_title_list(self, titles: list[str], target_action: str, confirm: bool = False) -> dict:
        args = {"titles": titles, "target_action": target_action}
        return self._write(
            "import_title_list", args, confirm, {"count": len(titles), "target_action": target_action}
        )

    def resolve_pending(self, pending_id: int, choice_index: int, chat_id: int = 0) -> dict:
        with self._context(chat_id) as ctx:
            return self._call(
                ctx, "resolve_pending", {"pending_id": pending_id, "choice_index": choice_index}
            )

    def create_calendar_event(
        self,
        date: str,
        time: str,
        duration_min: int,
        title: str,
        description: str = "",
        guests: list[str] | None = None,
        confirm: bool = False,
    ) -> dict:
        args = {
            "date": date,
            "time": time,
            "duration_min": duration_min,
            "title": title,
            "description": description,
            "guests": guests or [],
        }
        return self._write(
            "create_calendar_event", args, confirm, {"date": date, "time": time, "title": title, "guests": guests or []}
        )

    def send_email(self, to: str, subject: str, body: str, confirm: bool = False) -> dict:
        args = {"to": to, "subject": subject, "body": body}
        # The body is not copied into the audit log, only its size.
        return self._write("send_email", args, confirm, {"to": to, "subject": subject, "body_chars": len(body)})


"""Import a plain list of titles: exact matches are recorded, ambiguous ones become pending."""

from __future__ import annotations

import datetime as dt
import re

from sqlalchemy.orm import Session

from app.db.models import MediaAction, MediaItem, MediaType
from app.media.catalog import CatalogService, is_exact, item_out
from app.media.contexts import ContextService
from app.media.preferences import PreferenceService
from app.media.schemas import ImportPending, ImportReport, MediaItemOut
from app.util.persian import normalize

_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _clean(title: str) -> str:
    title = re.sub(r"[\(\)\[\]{}،,]+", " ", title)
    return " ".join(title.split())


def parse_title(raw: str) -> tuple[str, int | None]:
    text = " ".join(str(raw).translate(_DIGITS).split())
    match = _YEAR.search(text)
    if match is None:
        return _clean(text), None
    year = int(match.group(1))
    remainder = _clean(_YEAR.sub(" ", text))
    # A release year must leave a title behind and cannot be in the far future:
    # «Blade Runner 2049» and «1984» are titles, while «Dune (2021)» has a year.
    if not remainder or year > dt.date.today().year + 1:
        return _clean(text), None
    return remainder, year


class ImportService:
    def __init__(
        self,
        session: Session,
        catalog: CatalogService,
        preferences: PreferenceService,
        contexts: ContextService,
    ) -> None:
        self.session = session
        self.catalog = catalog
        self.preferences = preferences
        self.contexts = contexts

    def import_titles(
        self,
        user_id: int,
        chat_id: int,
        titles: list[str],
        target_action: str,
        *,
        commit: bool = True,
    ) -> ImportReport:
        action = MediaAction(target_action)
        if action not in (MediaAction.liked, MediaAction.watched, MediaAction.watchlist):
            raise ValueError("target_action must be liked, watched or watchlist.")
        report = ImportReport(target_action=action.value)
        for raw in titles:
            title, year = parse_title(raw)
            if not title:
                report.not_found.append(str(raw).strip())
                continue
            candidates = self._search(title, year)
            exact = [c for c in candidates if self._exact(c, title, year)]
            if len(exact) == 1:
                chosen = exact[0]
                self.preferences.set(user_id, chosen.id, action.value, commit=False)
                report.added.append(item_out(chosen))
                continue
            if len(exact) > 1 or candidates:
                # Never pick for the owner: 3-5 distinct candidates, one pending row.
                pool = exact or candidates
                pending = self.contexts.create_pending(
                    user_id,
                    chat_id,
                    query_text=str(raw).strip(),
                    candidates=[item_out(i) for i in pool[:5]],
                    target_action=action.value,
                    commit=False,
                )
                report.needs_confirmation.append(
                    ImportPending(
                        pending_id=pending.id,
                        title=str(raw).strip(),
                        candidates=[item_out(i) for i in pool[:5]],
                    )
                )
                continue
            report.not_found.append(str(raw).strip())
        if commit:
            self.session.commit()
        return report

    def resolve_pending(
        self, user_id: int, chat_id: int, pending_id: int, choice_index: int
    ) -> dict:
        row = self.contexts.pending(user_id, chat_id, pending_id)
        if row is None:
            raise ValueError("This pending choice was not found, or it has expired.")
        candidates = list(row.candidates or [])
        if row.status == "resolved":
            item = self.session.get(MediaItem, row.media_id) if row.media_id else None
            if item is None:
                raise ValueError("This pending choice was already resolved.")
            return {
                "pending_id": row.id,
                "item": item_out(item).model_dump(mode="json"),
                "action": row.target_action.value if row.target_action else None,
                "already_resolved": True,
            }
        if row.status != "pending":
            raise ValueError("This pending choice is no longer open.")
        if not 1 <= int(choice_index) <= len(candidates):
            raise ValueError(f"choice_index must be between 1 and {len(candidates)}.")
        payload = candidates[int(choice_index) - 1]
        item = self.session.get(MediaItem, int(payload.get("id", 0)))
        if item is None:
            raise ValueError("The chosen title is no longer available.")
        self.contexts.mark_resolved(row, item.id)
        action = row.target_action or MediaAction.watchlist
        self.preferences.set(user_id, item.id, action.value, commit=False)
        self.session.commit()
        return {
            "pending_id": row.id,
            "item": item_out(item).model_dump(mode="json"),
            "action": action.value,
            "already_resolved": False,
        }

    # ---- helpers

    def _search(self, title: str, year: int | None) -> list[MediaItem]:
        from app.media.providers import MediaProviderError

        out: list[MediaItem] = []
        seen: set[tuple] = set()
        errors: list[MediaProviderError] = []
        for kind in (MediaType.movie, MediaType.tv):
            try:
                found = self.catalog.search(kind.value, title, year, limit=5)
            except MediaProviderError as exc:
                # One type failing should not hide the other; if both fail the caller gets the
                # provider error (never a misleading "not found").
                errors.append(exc)
                continue
            for item in found:
                key = (item.tmdb_id, item.media_type.value)
                if key not in seen:
                    seen.add(key)
                    out.append(item)
        if len(errors) == 2:
            raise errors[0]
        return out

    @staticmethod
    def _exact(item: MediaItem, title: str, year: int | None) -> bool:
        return is_exact(item.title_fa, title, item.year, year) or is_exact(
            item.original_title, title, item.year, year
        )

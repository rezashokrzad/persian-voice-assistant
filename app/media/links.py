"""لینک‌های جست‌وجو برای پاسخ‌های فیلم و سریال."""

from __future__ import annotations

import re
from urllib.parse import unquote_plus, urlencode

from app.media.schemas import GoogleSearchLink

_MODES = {
    "dubbed": "دوبله فارسی",
    "subtitle": "زیرنویس فارسی",
    "none": "",
}

UNCENSORED_BASE = "https://www.simindad.top/"

# نشانه‌های قسمت/فصل/پارت که نباید در عبارت جست‌وجو بمانند
_PART_MARKER = re.compile(
    r"\b(?:part|chapter|episode|ep|season|vol(?:ume)?|book)\s*"
    r"(?:[0-9]+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}
_ARTICLES = {"the", "a", "an"}
_GENERIC_SUFFIX = {"saga", "franchise", "collection", "series", "trilogy", "quadrilogy", "chronicles"}


def google_search_url(title_fa: str, year: int | None = None, mode: str = "none") -> str:
    mode = (mode or "none").lower()
    if mode not in _MODES:
        raise ValueError("mode must be dubbed, subtitle or none.")
    query = title_fa.strip()
    if year:
        query = f"{query} {int(year)}"
    suffix = _MODES[mode]
    if suffix:
        query = f"{query} {suffix}"
    return "https://www.google.com/search?" + urlencode({"q": query})


def google_search_link(title_fa: str, year: int | None = None, mode: str = "none") -> GoogleSearchLink:
    url = google_search_url(title_fa, year, mode)
    return GoogleSearchLink(url=url, query=unquote_plus(url.split("q=", 1)[1]))


def base_title(original_title: str) -> str:
    """فقط نام پایهٔ انگلیسی اثر: بدون زیرعنوان، شمارهٔ قسمت، سال یا ژانر.

    «The Twilight Saga: New Moon» → «twilight»
    «Twilight 3» → «twilight»
    """
    title = " ".join((original_title or "").split())
    if not title:
        return ""
    # زیرعنوان‌ها بعد از «:» یا خط تیره جدا می‌شوند
    title = re.split(r"\s*[:：]\s*|\s+[-–—]\s+|\s*[|(]\s*", title, maxsplit=1)[0]
    title = _PART_MARKER.sub(" ", title)
    title = re.sub(r"\s+\d+$", "", title)  # شمارهٔ قسمت در انتها
    words = [w for w in title.split() if w]
    while words and words[0].lower().strip(".,") in _ARTICLES:
        words.pop(0)
    while words and words[-1].lower() in _ROMAN:
        words.pop()
    while words and words[-1].lower().strip(".,") in _GENERIC_SUFFIX:
        words.pop()
    cleaned = " ".join(words).strip(" -–—.,'\"")
    return cleaned or title.strip()


def uncensored_search_url(original_title: str) -> str:
    """لینک «جستجوی بدون سانسور» بر پایهٔ نام اصلی انگلیسی (فاصله‌ها با +)."""
    query = base_title(original_title).lower()
    return UNCENSORED_BASE + "?" + urlencode({"s": query})

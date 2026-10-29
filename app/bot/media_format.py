"""قالب‌بندی فارسی و دکمه‌های شیشه‌ای پاسخ فیلم/سریال."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.media.links import google_search_url, uncensored_search_url
from app.media.schemas import MediaItemOut
from app.util.persian import to_fa_digits

_TYPE_FA = {"movie": "فیلم", "tv": "سریال"}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def media_caption(item: MediaItemOut, reason: str | None = None) -> str:
    head = f"🎬 {item.title_fa}"
    if item.year:
        head += f" ({to_fa_digits(item.year)})"
    lines = [head, _TYPE_FA.get(item.media_type.value, item.media_type.value)]
    names = [str(g.get("name")) for g in item.genres or [] if g.get("name")]
    if names:
        lines.append("ژانر: " + "، ".join(names[:4]))
    if item.vote_average is not None:
        source = "TMDB" if item.vote_source == "tmdb" else (item.vote_source or "").upper()
        lines.append(f"امتیاز {source}: {to_fa_digits(f'{item.vote_average:.1f}')}")
    if item.overview:
        lines.append(_clip(item.overview, 240))
    if reason:
        lines.append("چرا: " + _clip(reason, 200))
    return _clip("\n".join(lines), 1000)


def media_keyboard(item: MediaItemOut) -> InlineKeyboardMarkup:
    mid = item.id
    rows = [
        [
            InlineKeyboardButton("👍 پسندیدم", callback_data=f"md:lk:{mid}"),
            InlineKeyboardButton("👎 نپسندیدم", callback_data=f"md:dl:{mid}"),
        ],
        [
            InlineKeyboardButton("✅ دیدم", callback_data=f"md:wt:{mid}"),
            InlineKeyboardButton("➕ واچ‌لیست", callback_data=f"md:wl:{mid}"),
        ],
        [InlineKeyboardButton("🔎 جست‌وجوی گوگل", url=google_search_url(item.title_fa, item.year, "none"))],
        [
            InlineKeyboardButton(
                "🔓 جستجوی بدون سانسور",
                url=uncensored_search_url(item.original_title or item.title_fa),
            )
        ],
    ]
    return InlineKeyboardMarkup(rows)


def pending_text(title: str, candidates: list[MediaItemOut]) -> str:
    lines = [f"کدام «{title}» را می‌خواهید؟"]
    for index, item in enumerate(candidates, start=1):
        year = f" ({to_fa_digits(item.year)})" if item.year else ""
        lines.append(f"{to_fa_digits(index)}. {item.title_fa}{year} — {_TYPE_FA.get(item.media_type.value, '')}")
    lines.append("یکی را انتخاب کنید؛ خودم حدس نمی‌زنم.")
    return "\n".join(lines)


def pending_keyboard(pending_id: int, candidates: list[MediaItemOut]) -> InlineKeyboardMarkup:
    rows = []
    for index, item in enumerate(candidates, start=1):
        year = f" ({to_fa_digits(item.year)})" if item.year else ""
        label = f"{to_fa_digits(index)}. {_clip(item.title_fa, 32)}{year}"
        rows.append([InlineKeyboardButton(label, callback_data=f"md:rp:{pending_id}:{index}")])
    return InlineKeyboardMarkup(rows)


def is_media_callback(data: str) -> bool:
    return data.startswith("md:")

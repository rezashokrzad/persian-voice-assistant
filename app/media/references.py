"""Parsing «دومی»، «سومی»، «آخری» and the media action in a short Persian sentence."""

from __future__ import annotations

import re

from app.util.persian import normalize

_ORDINALS = {
    "اول": 1, "اولی": 1, "اولین": 1,
    "دوم": 2, "دومی": 2, "دومین": 2,
    "سوم": 3, "سومی": 3, "سومین": 3,
    "چهارم": 4, "چهارمی": 4, "چهارمین": 4,
    "پنجم": 5, "پنجمی": 5, "پنجمین": 5,
    "ششم": 6, "ششمی": 6, "ششمین": 6,
    "هفتم": 7, "هفتمی": 7, "هفتمین": 7,
    "هشتم": 8, "هشتمی": 8, "هشتمین": 8,
    "نهم": 9, "نهمی": 9, "نهمین": 9,
    "دهم": 10, "دهمی": 10, "دهمین": 10,
    "آخر": -1, "آخری": -1, "آخرین": -1,
}  # fmt: skip
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def contains_ordinal_word(text: str) -> bool:
    tokens = normalize(str(text).translate(_DIGITS)).replace("‌", " ").split()
    return any(token in _ORDINALS for token in tokens)


def parse_ordinal(text: str) -> int | None:
    """1-based position, -1 for the last item, None when the message has no ordinal."""
    tokens = normalize(str(text).translate(_DIGITS)).replace("‌", " ").split()
    for index, token in enumerate(tokens):
        if token in _ORDINALS:
            return _ORDINALS[token]
        # A bare digit is only a position with «مورد/گزینه/شماره» or before «را»: «ساعت ۲» is not one.
        if token.isdigit() and 1 <= int(token) <= 20:
            before = tokens[index - 1] if index else ""
            after = tokens[index + 1] if index + 1 < len(tokens) else ""
            if before in ("مورد", "گزینه", "شماره") or after == "را":
                return int(token)
    return None


def media_action_from_text(text: str) -> str | None:
    t = normalize(text)
    if any(w in t for w in ("دیسلایک", "نپسندیدم", "بدم اومد", "بدم آمد", "دوست نداشتم")):
        return "disliked"
    if any(w in t for w in ("لایک", "پسندیدم", "خوشم اومد", "خوشم آمد", "دوست داشتم")):
        return "liked"
    if any(w in t for w in ("دیدم", "تماشا کردم", "watch کردم")):
        return "watched"
    if any(w in t for w in ("واچ لیست", "واچلیست", "watchlist", "ذخیره", "نگه دار")):
        return "watchlist"
    return None


def resolve_position(items: list, position: int):
    if not items:
        return None
    if position == -1:
        return items[-1]
    if 1 <= position <= len(items):
        return items[position - 1]
    return None

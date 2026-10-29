from urllib.parse import parse_qs, unquote_plus, urlparse

import pytest

from app.media.links import base_title, google_search_link, google_search_url, uncensored_search_url


def decoded_query(url: str) -> str:
    return parse_qs(urlparse(url).query)["q"][0]


def test_exact_persian_query_without_mode():
    url = google_search_url("ارباب حلقه‌ها", 2001)
    assert decoded_query(url) == "ارباب حلقه‌ها 2001"
    assert unquote_plus(url.split("q=", 1)[1]) == "ارباب حلقه‌ها 2001"


def test_dubbed_and_subtitle_are_only_added_when_requested():
    assert decoded_query(google_search_url("جدایی نادر از سیمین", 2011, "dubbed")) == (
        "جدایی نادر از سیمین 2011 دوبله فارسی"
    )
    assert decoded_query(google_search_url("جدایی نادر از سیمین", 2011, "subtitle")) == (
        "جدایی نادر از سیمین 2011 زیرنویس فارسی"
    )
    assert decoded_query(google_search_url("جدایی نادر از سیمین", 2011, "none")) == (
        "جدایی نادر از سیمین 2011"
    )


def test_no_english_download_or_watch_words_are_added():
    query = decoded_query(google_search_url("فیلم تست", 2020, "none"))
    for forbidden in ("movie", "watch online", "download", "stream"):
        assert forbidden not in query.lower()


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        google_search_url("فیلم", mode="free")


def test_link_model_carries_the_disclaimer_and_decoded_query():
    link = google_search_link("فیلم تست", 2020, "dubbed")
    assert link.query == "فیلم تست 2020 دوبله فارسی"
    assert "دسترس‌پذیری" in link.notice and "قانونی" in link.notice


# ---- جستجوی بدون سانسور: فقط نام پایهٔ انگلیسی ----

@pytest.mark.parametrize(
    "original, expected",
    [
        ("The Twilight Saga: New Moon", "Twilight"),
        ("Twilight 3", "Twilight"),
        ("The Lord of the Rings: The Fellowship of the Ring", "Lord of the Rings"),
        ("Breaking Bad: Season 5", "Breaking Bad"),
        ("Dune Part Two", "Dune"),
        ("A Quiet Place", "Quiet Place"),
        ("گرگ‌ومیش: ماه نو", "گرگ‌ومیش"),
    ],
)
def test_base_title_drops_subtitle_part_numbers_and_filler(original, expected):
    assert base_title(original) == expected


def test_uncensored_search_url_is_built_from_the_base_title_only():
    assert uncensored_search_url("The Twilight Saga: New Moon") == "https://www.simindad.top/?s=twilight"
    assert uncensored_search_url("Twilight") == "https://www.simindad.top/?s=twilight"
    assert uncensored_search_url("The Lord of the Rings: The Two Towers") == (
        "https://www.simindad.top/?s=lord+of+the+rings"
    )


def test_uncensored_search_does_not_change_the_google_link_behaviour():
    # قواعد حذف زیرعنوان فقط مخصوص همین دکمه است و به لینک گوگل سرایت نمی‌کند
    assert decoded_query(google_search_url("گرگ‌ومیش", 2010)) == "گرگ‌ومیش 2010"

"""Generate synthetic local examples with the production renderer. Never sends mail."""

from pathlib import Path

from app.tools.email_renderer import render_email


EXAMPLES = {
    "friendly": (
        "خبرهای تازه",
        "سلام نرگس جان،\n\nخوشحال شدم ازت خبر گرفتم. هر وقت فرصت داشتی، از کارهای تازه‌ات برام بنویس.\n\nدوستت دارم،\nمینا",
    ),
    "formal": (
        "درخواست دریافت راهنمای خدمات",
        "با سلام و احترام،\n\nخواهشمندم راهنمای خدمات و مراحل ثبت درخواست را ارسال فرمایید. لطفاً در صورت نیاز به اطلاعات بیشتر، از طریق همین ایمیل اطلاع دهید.\n\nنشانی مکاتبه: mina@example.com\n\nبا سپاس،\nمینا",
    ),
    "meeting": (
        "هماهنگی زمان گفت‌وگو",
        "سلام،\n\nبرای گفت‌وگو دربارهٔ Project Atlas، آیا چهارشنبه ساعت ۱۴:۳۰ برای شما مناسب است؟ لطفاً زمان پیشنهادی خود را اعلام کنید.\n\nنشانی پیشنهادی گفت‌وگو:\nhttps://example.com/atlas?lang=fa&view=meeting\n\nبا سپاس،\nمینا",
    ),
}


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font-url", default="", help="Optional public HTTPS Vazir font URL")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, (subject, body) in EXAMPLES.items():
        (args.output / f"{name}.html").write_text(
            render_email(subject, body, font_url=args.font_url), encoding="utf-8"
        )


if __name__ == "__main__":
    main()

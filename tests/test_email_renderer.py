"""No real provider calls: verify approved content through the shared send path."""

from html.parser import HTMLParser

import httpx
import pytest

from app.bot.preview import format_preview
from app.agent.schemas import EmailAction
from app.config import get_settings
from app.tools import impl, resend_client
from app.tools.email_renderer import public_font_url, render_email
from app.util.telegram_text import message_parts


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.in_body = True
        if self.in_body and tag == "br":
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag == "body":
            self.in_body = False

    def handle_data(self, data):
        if self.in_body:
            self.text.append(data)


@pytest.mark.parametrize("body", [
    'سلام <script>alert("x")</script> & <img src=x onerror=bad>',
    "سلام،\nخط دوم\n\nپاراگراف دوم\n\nبا سپاس،\nمینا\n",
    "فایل Project A را به team@example.com بفرستید.\nنشانی: https://example.com/a?q=1&b=2",
    "  فاصله  و متن\n\n\nپایان",
])
def test_renderer_preserves_approved_content_as_inert_text(body):
    subject = 'موضوع <b> & "'
    html = render_email(subject, body)
    assert '<html lang="fa" dir="rtl">' in html
    assert "<script>" not in html and "<img" not in html and "<b>" not in html
    assert "&lt;" in html and "&gt;" in html
    parser = VisibleText()
    parser.feed(html)
    # Template indentation surrounds the heading and body; body itself is unchanged.
    assert subject + "\n\n" + body in "".join(parser.text)
    assert "assets/fonts" not in html and "@font-face" not in html


def test_mixed_direction_is_isolated_without_rewriting():
    html = render_email("موضوع", "ایمیل team@example.com و جلسه Project A در 10:30")
    assert 'dir="ltr"' in html and "unicode-bidi:isolate" in html
    assert ">team@example.com</span>" in html
    assert ">Project A</span>" in html
    assert ">۱۴:۳۰</span>" in render_email("موضوع", "ساعت ۱۴:۳۰")
    assert ': <span dir="ltr"' in render_email("موضوع", "نشانی: team@example.com")


@pytest.mark.parametrize("url", ["assets/Vazir.woff2", "file:///font.woff2", "http://example.com/font",
    "https://localhost/font", "https://127.0.0.1/font", "https://[::1]/font",
    "https://10.0.0.1/font", "https://user:secret@example.com/font", "https://example.com/font?token=secret"])
def test_rejects_non_public_or_credentialed_font_urls(url):
    with pytest.raises(ValueError):
        public_font_url(url)


def test_configured_font_url_is_validated_and_cannot_inject_css():
    url = "https://fonts.example.com/Vazir.woff2"
    assert "@font-face" in render_email("موضوع", "متن", font_url=url)
    html = render_email("موضوع", "متن", font_url="https://fonts.example.com/a');}</style><script>x</script>")
    assert "<script>" not in html and "%27" in html and "%3C" in html


def test_shared_send_payload_contains_matching_text_html_and_idempotency(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", "test-only")
    monkeypatch.setattr(settings, "email_from", "sender@example.com")
    monkeypatch.setattr(impl, "resend_send", resend_client.send_email)
    captured = []

    def post(url, *, json, headers, timeout):
        captured.append((json, headers))
        return httpx.Response(200, json={"id": "msg_test"})

    monkeypatch.setattr(resend_client.httpx, "post", post)
    from types import SimpleNamespace

    args = impl.SendEmailArgs(to="one@example.com", subject="سلام", body="سلام،\n\nمتن <b> Project A\nمینا")
    assert impl.send_email(SimpleNamespace(run_id=42), args) == {"provider_message_id": "msg_test"}
    payload, headers = captured[0]
    assert payload["to"] == ["one@example.com"]
    assert payload["text"] == args.body
    assert payload["html"] == render_email(args.subject, args.body)
    assert payload["subject"] == args.subject
    assert headers["Idempotency-Key"] == "run-42-send_email"


def test_complete_long_confirmation_is_split_losslessly():
    body = "سلام،\n\n" + "متن فارسی Project A 😀\n" * 700 + "\nبا سپاس،\nمینا"
    action = EmailAction(to="one@example.com", subject="پیگیری", body=body)
    preview = format_preview(action)
    parts = message_parts(preview)
    assert len(parts) > 1
    assert "".join(parts) == preview
    assert body in preview and action.to in preview and action.subject in preview
    assert all(len(part.encode("utf-16-le")) // 2 <= 4000 for part in parts)


def test_retry_preview_sends_all_parts_before_confirmation_buttons(monkeypatch):
    from app.notify import telegram_send
    from app import notify

    monkeypatch.setattr(get_settings(), "telegram_bot_token", "test-only")
    monkeypatch.setattr(get_settings(), "telegram_api_base_url", "")
    calls = []

    def post(url, *, json, **kwargs):
        calls.append(json)
        return httpx.Response(200, request=httpx.Request("POST", "https://example.com"))

    monkeypatch.setattr(notify.httpx, "post", post)
    markup = {"inline_keyboard": [[{"text": "تأیید", "callback_data": "ok:1"}]]}
    text = "متن 😀\n" * 2000
    assert telegram_send(111, text, markup)
    assert "".join(call["text"] for call in calls) == text
    assert all("reply_markup" not in call for call in calls[:-1])
    assert calls[-1]["reply_markup"] == markup

    calls.clear()

    def failing_post(url, *, json, **kwargs):
        calls.append(json)
        raise httpx.ReadTimeout("unavailable")

    monkeypatch.setattr(notify.httpx, "post", failing_post)
    assert not telegram_send(111, text, markup)
    assert len(calls) == 1 and "reply_markup" not in calls[0]


def test_bot_preview_sends_complete_body_before_confirmation_buttons(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.bot import main

    action = EmailAction(to="one@example.com", subject="پیگیری", body="سلام\n" * 2000)
    monkeypatch.setattr(main, "analyse", lambda *a, **kw: SimpleNamespace(
        kind="email", action=action, run_id=42
    ))
    sent = []

    async def reply_text(text, **kwargs):
        sent.append((text, kwargs))

    async def chat_action(*args):
        pass

    update = SimpleNamespace(effective_message=SimpleNamespace(chat_id=111, reply_text=reply_text),
                             effective_user=SimpleNamespace(id=111))
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_chat_action=chat_action))
    asyncio.run(main.respond(update, context, "درخواست"))
    assert "".join(text for text, _ in sent) == format_preview(action)
    assert all("reply_markup" not in kwargs for _, kwargs in sent[:-1])
    assert sent[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data == "ok:42"

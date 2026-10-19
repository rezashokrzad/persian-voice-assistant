"""Pure presentation of approved email text; never adds content or a signature."""

from html import escape
from ipaddress import ip_address
import re
from urllib.parse import quote, urlsplit


def public_font_url(value: str) -> str:
    """Accept a credential-free public HTTPS font URL, never a file/private URL.

    This is URL validation only; rendering never resolves DNS or fetches assets.
    Operators must host the font at a publicly accessible address.
    """
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        port = url.port
        if (url.scheme != "https" or not host or url.username or url.password
                or port not in (None, 443) or url.query or url.fragment
                or any(c.isspace() for c in value)):
            raise ValueError
        try:
            if not ip_address(host).is_global:
                raise ValueError
        except ValueError:
            if ":" in host or re.fullmatch(r"[0-9.]+", host):
                raise ValueError
            if ("." not in host or host.endswith((".localhost", ".local", ".internal", ".test"))
                    or not re.fullmatch(r"[A-Za-z0-9.-]+", host)):
                raise ValueError
        return value
    except ValueError:
        raise ValueError("Email font must use a public HTTPS URL without credentials or query parameters.") from None


def _text(value: str) -> str:
    # Isolate ASCII runs (including addresses and URLs) within Persian prose.
    # This is presentation only: no links or meeting fields are inferred.
    parts = re.split(
        r"([A-Za-z0-9۰-۹٠-٩][\x21-\x7e۰-۹٠-٩]*(?: [A-Za-z0-9۰-۹٠-٩][\x21-\x7e۰-۹٠-٩]*)*)",
        value,
    )
    return "".join(
        f'<span dir="ltr" style="direction:ltr;unicode-bidi:isolate;">{escape(part)}</span>'
        if i % 2 else escape(part)
        for i, part in enumerate(parts)
    )


def render_email(subject: str, body: str, *, font_url: str = "") -> str:
    """Render untrusted subject/body without changing their wording or whitespace.

    No remote assets are required. A configured public font is optional;
    clients that block web fonts use Tahoma/Arial/sans-serif.
    """
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    content = '<br>'.join(_text(line) for line in lines)
    font = "Vazir, Tahoma, Arial, sans-serif"
    font_style = ""
    if font_url:
        safe_url = escape(quote(public_font_url(font_url), safe=":/%-._~"))
        font_style = ("<style>@font-face{font-family:Vazir;"
                      f"src:url('{safe_url}');font-weight:400;font-style:normal;" + "}</style>")
    return f'''<!doctype html>
<html lang="fa" dir="rtl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(subject)}</title>{font_style}</head>
<body dir="rtl" style="margin:0;padding:0;background-color:#f3f5f7;font-family:{font};">
<table role="presentation" dir="rtl" width="100%" cellpadding="0" cellspacing="0" style="width:100%;background-color:#f3f5f7;">
<tr><td align="center" style="padding:40px 16px;">
<!--[if mso]><table role="presentation" width="600" align="center"><tr><td><![endif]-->
<table role="presentation" dir="rtl" width="100%" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;table-layout:fixed;background-color:#ffffff;border:1px solid #e3e8ed;border-top:3px solid #26776c;border-radius:16px;">
<tr><td dir="rtl" align="right" style="padding:24px 24px 16px;font-family:{font};text-align:right;overflow-wrap:anywhere;word-wrap:break-word;">
<h1 style="margin:0;color:#192b38;font-size:22px;font-weight:700;line-height:1.7;">{_text(subject)}</h1>
</td></tr>
<tr><td dir="rtl" align="right" style="padding:0 24px 28px;font-family:{font};font-size:16px;line-height:2;color:#263343;text-align:right;white-space:pre-wrap;overflow-wrap:anywhere;word-wrap:break-word;">{content}</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr></table>
</body></html>'''

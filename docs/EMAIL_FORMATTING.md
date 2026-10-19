# Persian email presentation

The agent proposes Persian **plain text**, including the greeting, paragraphs and known owner
signature. Prompt version is `v4`. The owner approves the complete recipient, subject and body
in Telegram. Long previews are split without truncation, and confirmation buttons appear only
on the last message. Failure to deliver a part stops delivery before the buttons.

`app/tools/email_renderer.py` is an independent, deterministic renderer. It escapes all content,
preserves blank lines and line breaks, and isolates Latin text, addresses, URLs and number runs
for RTL reading. Embedded HTML is visible inert text. URLs are ordinary text, not generated links.
The subject becomes the heading; the body is not rewritten, signed again or interpreted as meeting
fields. The general-purpose card uses presentation tables, inline styles, `lang="fa"`, RTL, a
600px maximum width and the colors of `docs/email-preview.html`. No reference content is copied.

The shared registered email tool supplies the generated HTML through Resend's existing `html`
argument, keeping the original `text` body and subject unchanged. Bot execution, execution retries
and confirmed MCP calls all use this path. The EmailAction schema, single-recipient validation,
agent tool visibility, confirmation gates, idempotency keys, audit records and logging are retained.
The operational CLI's explicit `send-test-email` command remains unchanged; it was not run.

## Fonts and email clients

By default there are no external font requests or local file references. The font stack is
`Vazir, Tahoma, Arial, sans-serif`. Set optional `EMAIL_FONT_URL` to a public HTTPS Vazir font file
to add `@font-face`. Credentials, query strings, fragments, nonstandard ports, local paths and
private IP literals are rejected. Validation is syntactic: the renderer does not resolve DNS,
check reachability or download fonts. The operator must provide a stable publicly reachable font
file and appropriate hosting/CORS permissions. One regular face is configured; bold may be
synthesized by the client.

Many mail clients strip or block `@font-face`; Vazir cannot be guaranteed. Fallback fonts remain
necessary. Rounded corners, bidi isolation, whitespace and wrapping support can also differ in
older email clients. Browser screenshots verify layout, not delivery or every mail-client engine.
No real emails were sent during validation.

## Local previews

Generate synthetic examples (never sends mail):

```sh
python -m scripts.preview_emails docs/generated-email-previews
# To match a deployment's optional custom font:
python -m scripts.preview_emails docs/generated-email-previews --font-url https://fonts.example.com/Vazir.woff2
```

The examples are local fixtures only; they are not defaults for real emails. Each HTML document
is produced by the outgoing renderer. The generator defaults to the same fallback-only rendering
as an unconfigured deployment. The optional font URL must be explicitly supplied for a deployment
using that setting.

With Playwright installed, run `node scripts/inspect_email_previews.cjs`. Set `CHROMIUM_PATH` if
using an installed browser executable. Browser QA blocks remote HTTPS requests and checks all
examples at 1000px and 360px. HTML and screenshots are in `docs/generated-email-previews/`:

- `friendly.html`, `friendly-1000.png`, `friendly-360.png`
- `formal.html`, `formal-1000.png`, `formal-360.png`
- `meeting.html`, `meeting-1000.png`, `meeting-360.png`

All six screenshots were visually inspected. Card widths were 600px at desktop and 328px on
mobile, with no horizontal overflow. Persian/Latin text, addresses, URLs and Persian times were
checked. Custom-font rendering in actual mail clients remains unverified.

## Validation

109 tests passed across `test_email_renderer.py`, `test_confirmation.py`, `test_mcp.py`,
`test_tools.py`, `test_ops.py`, `test_api.py`, `test_agent.py` and `test_env_example.py`.
Tests ran in the existing project image with mocked providers and an isolated disposable
PostgreSQL database. One existing Starlette/httpx deprecation warning was emitted.

Coverage includes final Resend text/HTML content, escaping, paragraph/line preservation,
mixed direction, font URL validation, complete multi-message confirmation previews, partial
Telegram delivery failure, confirmation controls, retry content/provider-key reuse, tool logging,
audit records, recipient validation and duplicate-send prevention.

## Changed files

- Rendering and configuration: `app/tools/email_renderer.py`, `app/tools/impl.py`,
  `app/config.py`, `.env.example`
- Content generation: `app/agent/prompt.py`
- Complete confirmations: `app/util/telegram_text.py`, `app/bot/main.py`, `app/notify.py`
- Tests: `tests/test_email_renderer.py`, `tests/test_confirmation.py`, `tests/test_mcp.py`
- Preview tooling: `scripts/preview_emails.py`, `scripts/inspect_email_previews.cjs`
- Documentation/artifacts: this file and `docs/generated-email-previews/`

The supplied `docs/email-preview.html` and `docs/assets/` are visual references only and are
excluded from the implementation commits.

import asyncio
import contextlib
import json

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AuditEvent
from app.mcp.server import build_server
from app.mcp.service import McpService, McpToolError
from app.tools import impl
from tests.conftest import FIXED_NOW


class FakeCalendar:
    def __init__(self):
        self.created, self.listed = [], []

    def create_event(self, **kw):
        self.created.append(kw)
        return {"id": kw["event_id"], "html_link": "https://cal"}

    def list_events(self, date):
        self.listed.append(date)
        return [{"title": "جلسه", "start": f"{date}T10:00:00+03:30", "end": None}]

    def list_range(self, first_day, days):
        return [{"title": "آینده", "start": first_day.isoformat(), "end": None}]


@pytest.fixture
def fakes(monkeypatch):
    cal, sent = FakeCalendar(), []
    monkeypatch.setattr(impl, "calendar", lambda: cal)
    monkeypatch.setattr(impl, "resend_send", lambda to, subject, body, **kw: sent.append((to, subject, body, kw)) or "msg_9")
    monkeypatch.setattr(get_settings(), "allowed_user_ids", "111")
    return cal, sent


@pytest.fixture
def svc(session, user, fakes):
    return McpService(lambda: contextlib.nullcontext(session), now=lambda: FIXED_NOW)


def events(session):
    return [(e.event_type, e.actor, e.payload_json) for e in session.scalars(select(AuditEvent).order_by(AuditEvent.id))]


def test_read_tools_use_the_agents_implementations(svc, fakes):
    cal, _ = fakes
    assert svc.find_contact("Ali")["matches"][0]["email"] == "ali@example.com"
    assert svc.find_contact("ناشناس")["matches"] == []
    assert svc.list_calendar_events("2026-10-20")["events"][0]["title"] == "جلسه"
    assert cal.listed and str(cal.listed[0]) == "2026-10-20"
    with pytest.raises(McpToolError):
        svc.list_calendar_events("not-a-date")


def test_write_tools_do_nothing_without_confirm(svc, fakes, session):
    cal, sent = fakes
    r1 = svc.create_calendar_event("2026-10-20", "10:00", 30, "جلسه", guests=["ali@example.com"])
    r2 = svc.send_email("ali@example.com", "سلام", "متن")
    assert r1["status"] == r2["status"] == "confirmation_required"
    assert cal.created == [] and sent == []
    assert [e[0] for e in events(session)] == ["mcp_confirmation_required"] * 2


def test_confirmed_event_is_created_and_audited(svc, fakes, session):
    cal, _ = fakes
    r = svc.create_calendar_event("2026-10-20", "10:00", 45, "جلسه", "توضیح", ["ali@example.com"], confirm=True)
    assert r["status"] == "done" and r["id"]
    (kw,) = cal.created
    assert kw["duration_min"] == 45 and kw["guests"] == ["ali@example.com"]
    kind, actor, payload = events(session)[-1]
    assert (kind, actor) == ("mcp_write_executed", "mcp")
    assert payload["tool"] == "create_calendar_event" and payload["title"] == "جلسه"


def test_two_adhoc_events_get_different_ids(svc, fakes):
    cal, _ = fakes
    for _ in range(2):
        svc.create_calendar_event("2026-10-20", "10:00", 30, "t", confirm=True)
    assert cal.created[0]["event_id"] != cal.created[1]["event_id"]


def test_confirmed_email_is_sent_and_audited_without_the_body(svc, fakes, session):
    _, sent = fakes
    r = svc.send_email("ali@example.com", "موضوع", "متن خصوصی ایمیل", confirm=True)
    assert r == {"status": "done", "provider_message_id": "msg_9"}
    assert sent[0][:3] == ("ali@example.com", "موضوع", "متن خصوصی ایمیل")
    from app.tools.email_renderer import render_email

    assert sent[0][3]["html"] == render_email("موضوع", "متن خصوصی ایمیل")
    payload = events(session)[-1][2]
    assert payload["body_chars"] == len("متن خصوصی ایمیل") and "متن خصوصی" not in json.dumps(payload, ensure_ascii=False)


def test_invalid_input_is_an_error_and_audited_as_failed(svc, fakes, session):
    _, sent = fakes
    with pytest.raises(McpToolError):
        svc.send_email("not-an-email", "s", "b", confirm=True)
    assert sent == [] and events(session)[-1][0] == "mcp_write_failed"


def test_provider_errors_are_reported_and_audited(svc, fakes, session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("resend down")

    monkeypatch.setattr(impl, "resend_send", boom)
    with pytest.raises(McpToolError, match="resend down"):
        svc.send_email("ali@example.com", "s", "b", confirm=True)
    assert events(session)[-1][0] == "mcp_write_failed"


def test_missing_owner_is_a_clear_error(session, fakes, monkeypatch):
    monkeypatch.setattr(get_settings(), "allowed_user_ids", "424242")
    svc = McpService(lambda: contextlib.nullcontext(session))
    with pytest.raises(McpToolError, match="owner"):
        svc.find_contact("x")


def test_upcoming_events(svc):
    out = svc.upcoming_events(7)
    assert out["from"] == "2026-10-14" and out["events"][0]["title"] == "آینده"


# ---- through the real MCP server object (tool schemas, resource, protocol-level errors)


def test_server_exposes_the_four_tools_and_the_resource(svc):
    server = build_server(svc)

    async def go():
        tools = {t.name: t for t in await server.list_tools()}
        resources = [str(r.uri) for r in await server.list_resources()]
        return tools, resources

    tools, resources = asyncio.run(go())
    assert set(tools) == {"find_contact", "list_calendar_events", "create_calendar_event", "send_email"}
    assert "confirm" in tools["send_email"].inputSchema["properties"]
    assert tools["send_email"].inputSchema["properties"]["confirm"]["default"] is False
    assert resources == ["calendar://upcoming"]


def test_server_calls_and_resource_read(svc, fakes):
    _, sent = fakes
    server = build_server(svc)

    async def go():
        blocked = await server.call_tool("send_email", {"to": "ali@example.com", "subject": "s", "body": "b"})
        res = list(await server.read_resource("calendar://upcoming"))
        return blocked, res

    blocked, res = asyncio.run(go())
    assert sent == []
    assert "confirmation_required" in json.dumps(blocked, default=str)
    assert "آینده" in res[0].content

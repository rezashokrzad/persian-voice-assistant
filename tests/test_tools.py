import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models import AgentRun, Contact, MemoryFact, ToolCall
from app.tools import impl
from app.tools.google_calendar import GoogleCalendar, GoogleError
from app.tools.registry import REGISTRY, agent_tool_schemas, run_tool


def new_run(session, user):
    run = AgentRun(user_id=user.id, input_text="x")
    session.add(run)
    session.flush()
    return run


def test_agent_only_sees_read_only_and_media_tools():
    names = {s["function"]["name"] for s in agent_tool_schemas()}
    assert {
        "get_current_datetime",
        "convert_jalali_date",
        "find_contact",
        "list_calendar_events",
    } <= names
    assert {
        "search_movie",
        "search_tv",
        "get_title_details",
        "discover_movies",
        "discover_tv",
        "get_trending",
        "get_recommendations",
        "set_preference",
        "remove_preference",
        "list_preferences",
        "watchlist_add",
        "watchlist_remove",
        "watchlist_list",
        "import_title_list",
        "resolve_pending",
        "make_google_search_link",
        "get_recent_list",
    } <= names
    # Outside-world writes still never reach the agent.
    assert "send_email" not in names and "create_calendar_event" not in names
    assert REGISTRY["send_email"].side_effect and REGISTRY["create_calendar_event"].side_effect


def test_schemas_are_json_serialisable():
    json.dumps(agent_tool_schemas())
    json.dumps(REGISTRY["create_calendar_event"].openai_schema())


def test_get_current_datetime(ctx):
    r = run_tool(ctx, "get_current_datetime", {})
    assert r.ok and r.data["date"] == "2026-10-14" and r.data["jalali"] == "1405/07/22"
    assert r.data["next_15_days"][1] == {"date": "2026-10-15", "weekday": "پنجشنبه", "jalali": "1405/07/23"}


def test_convert_jalali(ctx):
    r = run_tool(ctx, "convert_jalali_date", {"year": 1405, "month": 8, "day": 1})
    assert r.ok and r.data["date"] == "2026-10-23"
    assert not run_tool(ctx, "convert_jalali_date", {"year": 1405, "month": 13, "day": 1}).ok


@pytest.mark.parametrize("query", ["علی", "Ali", "ali", "عَلي", "علی رضایی"])
def test_find_contact_spellings(ctx, query):
    r = run_tool(ctx, "find_contact", {"name": query})
    assert r.data["matches"][0]["email"] == "ali@example.com"
    assert "ali@example.com" in ctx.known_emails


def test_find_contact_unknown(ctx):
    r = run_tool(ctx, "find_contact", {"name": "رضا"})
    assert r.ok and r.data["matches"] == [] and not ctx.known_emails


def test_validation_errors_are_returned_not_raised(ctx, session, user):
    ctx.run_id = new_run(session, user).id
    r = run_tool(ctx, "send_email", {"to": "not-an-email", "subject": "s", "body": "b"})
    assert not r.ok and "to" in r.error
    row = session.scalars(select(ToolCall)).one()
    assert row.status == "invalid"
    assert not run_tool(ctx, "no_such_tool", {}).ok
    assert not run_tool(ctx, "find_contact", "{broken").ok


def test_save_contact_and_remember(ctx, session, user):
    ctx.run_id = new_run(session, user).id
    assert run_tool(ctx, "save_contact", {"name": "رضا", "email": "reza@example.com"}).ok
    assert session.scalar(select(Contact).where(Contact.email == "reza@example.com")).name == "رضا"
    assert run_tool(ctx, "remember", {"key": "default_meeting_duration_min", "value": "30"}).ok
    assert run_tool(ctx, "remember", {"key": "default_meeting_duration_min", "value": "45"}).ok
    facts = session.scalars(select(MemoryFact)).all()
    assert [(f.key, f.value) for f in facts] == [("default_meeting_duration_min", "45")]
    assert not run_tool(ctx, "remember", {"key": "Bad Key", "value": "x"}).ok


def test_idempotency_key_runs_the_tool_once(ctx, session, user, monkeypatch):
    ctx.run_id = new_run(session, user).id
    calls = []
    monkeypatch.setattr(impl, "resend_send", lambda *a, **k: calls.append(a) or "msg_1")
    args = {"to": "ali@example.com", "subject": "سلام", "body": "متن"}
    first = run_tool(ctx, "send_email", args, idempotency_key=f"run:{ctx.run_id}")
    second = run_tool(ctx, "send_email", args, idempotency_key=f"run:{ctx.run_id}")
    assert first.ok and second.ok and second.data == first.data
    assert len(calls) == 1


# ---- Google Calendar client against a mocked API ----


def google(handler, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "google_client_id", "cid")
    monkeypatch.setattr(s, "google_client_secret", "sec")
    monkeypatch.setattr(s, "google_refresh_token", "rt")
    return GoogleCalendar(httpx.Client(transport=httpx.MockTransport(handler)))


def test_create_event_uses_tehran_time_attendees_and_send_updates(monkeypatch):
    import datetime as dt

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"id": seen["body"]["id"], "htmlLink": "https://cal/x"})

    cal = google(handler, monkeypatch)
    out = cal.create_event(
        date=dt.date(2026, 10, 20), time_=dt.time(15, 0), duration_min=45, title="جلسه",
        description="توضیح", guests=["ali@example.com"], event_id="abc123def",
    )  # fmt: skip
    assert out["id"] == "abc123def"
    assert seen["params"]["sendUpdates"] == "all" and seen["auth"] == "Bearer tok"
    b = seen["body"]
    assert b["start"] == {"dateTime": "2026-10-20T15:00:00", "timeZone": "Asia/Tehran"}
    assert b["end"]["dateTime"] == "2026-10-20T15:45:00"
    assert b["attendees"] == [{"email": "ali@example.com"}]


def test_duplicate_event_id_is_treated_as_already_created(monkeypatch):
    import datetime as dt

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.method == "POST":
            return httpx.Response(409, json={"error": "duplicate"})
        return httpx.Response(200, json={"id": "abc123def", "htmlLink": "https://cal/x"})

    out = google(handler, monkeypatch).create_event(
        date=dt.date(2026, 10, 20), time_=dt.time(9, 0), duration_min=30, title="t",
        description=None, guests=[], event_id="abc123def",
    )  # fmt: skip
    assert out["id"] == "abc123def"


def test_missing_google_credentials_fail_clearly():
    with pytest.raises(GoogleError):
        GoogleCalendar(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))).list_events(
            __import__("datetime").date(2026, 10, 20)
        )

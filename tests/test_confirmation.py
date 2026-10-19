import datetime as dt

import pytest
from sqlalchemy import select

from app.agent.agent import process_message
from app.agent.context import AgentContext
from app.agent.executor import ALREADY_HANDLED, cancel_run, confirm_run, supersede_run
from app.agent.schemas import EmailAction, ScheduleAction
from app.bot.preview import confirmation_keyboard, format_preview
from app.db.models import AgentRun, AuditEvent, CalendarEvent, Email, RunStatus, ToolCall, User
from app.tools import impl
from tests.conftest import FIXED_NOW
from tests.fakes import ScriptedLLM, call, turn


class Spy:
    def __init__(self):
        self.calendar_calls, self.email_calls = [], []

    def install(self, monkeypatch, *, fail=False):
        spy = self

        class FakeCalendar:
            def create_event(self, **kw):
                spy.calendar_calls.append(kw)
                if fail:
                    raise RuntimeError("calendar down")
                return {"id": kw["event_id"], "html_link": "https://cal"}

        def fake_send(to, subject, body, **kw):
            spy.email_calls.append((to, subject, body, kw))
            return "msg_123"

        monkeypatch.setattr(impl, "calendar", lambda: FakeCalendar())
        monkeypatch.setattr(impl, "resend_send", fake_send)
        return spy


@pytest.fixture
def spy(monkeypatch):
    return Spy().install(monkeypatch)


def make_run(session, user, *turns, text="x"):
    run, outcome = process_message(
        session, ScriptedLLM(*turns), user, text, AgentContext(), now=lambda: FIXED_NOW
    )
    assert outcome.kind in ("schedule", "email"), outcome
    return run


SCHEDULE = turn(call(
    "propose_schedule", date="2026-10-15", time="10:00", duration_min=45, title="جلسه با علی",
    description="بررسی قرارداد", guests=[{"name": "علی رضایی", "email": "ali@example.com"}],
))
EMAIL = turn(call("propose_email", to="owner@example.com", subject="موضوع", body="متن ایمیل"))


def schedule_run(session, user):
    return make_run(session, user, turn(call("find_contact", name="علی")), SCHEDULE)


def test_nothing_runs_before_confirmation(session, user, spy):
    run = schedule_run(session, user)
    assert run.status == RunStatus.pending_confirmation
    assert spy.calendar_calls == [] and spy.email_calls == []


def test_confirm_creates_the_event_once(session, user, spy):
    run = schedule_run(session, user)
    first = confirm_run(session, user, run.id, now=lambda: FIXED_NOW)
    assert first.ok and "رویداد «جلسه با علی» در تقویم ثبت شد" in first.message

    (call_kwargs,) = spy.calendar_calls
    assert call_kwargs["date"] == dt.date(2026, 10, 15) and call_kwargs["time_"] == dt.time(10, 0)
    assert call_kwargs["duration_min"] == 45 and call_kwargs["guests"] == ["ali@example.com"]

    session.refresh(run)
    assert run.status == RunStatus.executed
    ev = session.scalars(select(CalendarEvent)).one()
    assert ev.run_id == run.id and ev.provider_event_id == call_kwargs["event_id"]


def test_double_tap_never_acts_twice(session, user, spy):
    run = schedule_run(session, user)
    results = [confirm_run(session, user, run.id, now=lambda: FIXED_NOW) for _ in range(3)]
    assert [r.ok for r in results] == [True, False, False]
    assert results[1].message == ALREADY_HANDLED
    assert len(spy.calendar_calls) == 1
    assert len(session.scalars(select(CalendarEvent)).all()) == 1


def test_idempotency_key_blocks_a_replay_even_if_status_is_reset(session, user, spy):
    """Second line of defence: the unique key on tool_calls, independent of the status claim."""
    run = schedule_run(session, user)
    confirm_run(session, user, run.id, now=lambda: FIXED_NOW)
    run.status = RunStatus.pending_confirmation  # simulate a bug / race resetting the status
    session.commit()
    confirm_run(session, user, run.id, now=lambda: FIXED_NOW)
    assert len(spy.calendar_calls) == 1
    key = f"run:{run.id}:create_calendar_event"
    assert len(session.scalars(select(ToolCall).where(ToolCall.idempotency_key == key)).all()) == 1


def test_confirm_sends_the_email_once_with_a_provider_idempotency_key(session, user, spy):
    run = make_run(session, user, EMAIL)
    assert confirm_run(session, user, run.id, now=lambda: FIXED_NOW).ok
    confirm_run(session, user, run.id, now=lambda: FIXED_NOW)
    (to, subject, body, kw) = spy.email_calls[0]
    assert len(spy.email_calls) == 1 and to == "owner@example.com"
    assert kw["idempotency_key"] == f"run-{run.id}-send_email"
    from app.tools.email_renderer import render_email

    assert kw["html"] == render_email(subject, body)
    row = session.scalars(select(Email)).one()
    assert row.provider_message_id == "msg_123" and row.status == "sent" and row.to_address == to


def test_cancel_runs_nothing_and_blocks_a_later_confirm(session, user, spy):
    run = schedule_run(session, user)
    assert cancel_run(session, user, run.id).ok
    assert confirm_run(session, user, run.id, now=lambda: FIXED_NOW).message == ALREADY_HANDLED
    session.refresh(run)
    assert run.status == RunStatus.rejected and spy.calendar_calls == []


def test_superseded_draft_cannot_be_confirmed(session, user, spy):
    run = schedule_run(session, user)
    supersede_run(session, user, run.id)
    assert not confirm_run(session, user, run.id, now=lambda: FIXED_NOW).ok
    assert spy.calendar_calls == []


def test_another_user_cannot_confirm_my_run(session, user, spy):
    run = schedule_run(session, user)
    other = User(telegram_user_id=999, name="Other")
    session.add(other)
    session.flush()
    assert not confirm_run(session, other, run.id, now=lambda: FIXED_NOW).ok
    assert spy.calendar_calls == []


def test_clarification_and_failed_runs_are_not_confirmable(session, user, spy):
    run, _ = process_message(
        session, ScriptedLLM(turn(call("ask_clarification", question="چه ساعتی؟"))), user, "x",
        AgentContext(), now=lambda: FIXED_NOW,
    )  # fmt: skip
    assert run.status == RunStatus.needs_clarification
    assert not confirm_run(session, user, run.id, now=lambda: FIXED_NOW).ok


def test_provider_failure_marks_failed_and_is_not_retried_by_a_second_tap(session, user, monkeypatch):
    spy = Spy().install(monkeypatch, fail=True)
    run = schedule_run(session, user)
    result = confirm_run(session, user, run.id, now=lambda: FIXED_NOW)
    assert not result.ok and "انجام نشد" in result.message
    session.refresh(run)
    assert run.status == RunStatus.failed and "calendar down" in run.error
    assert not confirm_run(session, user, run.id, now=lambda: FIXED_NOW).ok
    assert len(spy.calendar_calls) == 1
    events = session.scalars(select(AuditEvent.event_type)).all()
    assert "action_failed" in events and "action_executed" not in events


def test_tool_calls_record_the_executed_action(session, user, spy):
    run = schedule_run(session, user)
    confirm_run(session, user, run.id, now=lambda: FIXED_NOW)
    row = session.scalars(
        select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.tool_name == "create_calendar_event")
    ).one()
    assert row.status == "ok" and row.arguments_json["title"] == "جلسه با علی"
    assert row.result_json["id"]
    assert [e for e in session.scalars(select(AuditEvent.event_type))] == [
        "action_confirmed",
        "action_executed",
    ]


# ---- previews and buttons ----


def test_schedule_preview_is_persian_and_shows_both_calendars():
    text = format_preview(
        ScheduleAction(
            date=dt.date(2026, 10, 15), time=dt.time(10, 0), duration_min=45, title="جلسه با علی",
            description="بررسی", guests=[{"name": "علی رضایی", "email": "ali@example.com"}],
        )  # fmt: skip
    )
    for part in ["پنجشنبه ۲۳ مهر ۱۴۰۵", "2026-10-15", "۱۰:۰۰ تا ۱۰:۴۵", "ali@example.com", "جلسه با علی"]:
        assert part in text


def test_email_preview_shows_recipient_subject_body():
    text = format_preview(EmailAction(to="ali@example.com", to_name="علی", subject="سلام", body="متن"))
    assert "ali@example.com" in text and "موضوع: سلام" in text and "متن" in text


def test_email_execution_retry_reuses_approved_content_and_provider_key(session, user, monkeypatch):
    from app.tools.email_renderer import render_email

    calls = []

    def send(to, subject, body, **kw):
        calls.append((to, subject, body, kw))
        if len(calls) == 1:
            raise RuntimeError("temporary provider failure")
        return "msg_retry"

    monkeypatch.setattr(impl, "resend_send", send)
    run = make_run(session, user, EMAIL)
    assert calls == []
    assert not confirm_run(session, user, run.id).ok
    assert confirm_run(session, user, run.id, retry=True).ok
    assert not confirm_run(session, user, run.id, retry=True).ok
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[1][3]["html"] == render_email(calls[1][1], calls[1][2])
    assert len(session.scalars(select(Email).where(Email.run_id == run.id)).all()) == 1


def test_keyboard_has_confirm_edit_cancel():
    rows = confirmation_keyboard(42).inline_keyboard
    assert [b.callback_data for b in rows[0]] == ["ok:42", "ed:42", "no:42"]

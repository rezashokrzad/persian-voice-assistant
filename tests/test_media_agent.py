from sqlalchemy import select

from app.agent.agent import process_message
from app.agent.context import AgentContext
from app.agent.prompt import SYSTEM_PROMPT
from app.db.models import AgentRun, Intent, RunStatus, ToolCall
from app.media import container
from tests.conftest import FIXED_NOW
from tests.fakes import ScriptedLLM, call, turn
from tests.media_fakes import FakeCatalog, add_media


def _catalog(monkeypatch, session):
    a = add_media(
        session, tmdb_id=300, title_fa="فیلم یکتا", original_title="Unique", year=2020
    )  # fmt: skip
    b = add_media(
        session, tmdb_id=301, title_fa="ماتریکس", original_title="The Matrix", year=1999
    )  # fmt: skip
    c = add_media(
        session, tmdb_id=302, title_fa="ماتریکس", original_title="The Matrix", year=2021
    )  # fmt: skip
    fake = FakeCatalog(
        items=[a, b, c],
        search_results={
            ("movie", "Unique"): [a],
            ("movie", "The Matrix"): [b, c],
        },
    )
    monkeypatch.setattr(container, "catalog_for", lambda s: fake)
    return a, b, c


def test_media_intent_is_routed_and_recorded(session, user, monkeypatch):
    item, _, _ = _catalog(monkeypatch, session)
    llm = ScriptedLLM(
        turn(call("search_movie", title="Unique")),
        turn(
            call(
                "propose_media_reply",
                text="این فیلم را پیشنهاد می‌کنم.",
                items=[{"media_id": item.id, "reason": "ژانر درخواستی"}],
            )
        ),
    )
    run, outcome = process_message(
        session, llm, user, "یک فیلم پیشنهاد بده", AgentContext(), chat_id=77, now=lambda: FIXED_NOW
    )
    assert outcome.kind == "media"
    row = session.get(AgentRun, run.id)
    assert row.intent == Intent.movie_series and row.status == RunStatus.executed
    assert row.action_json["text"] == "این فیلم را پیشنهاد می‌کنم."
    assert row.action_json["items"][0]["media_id"] == item.id
    names = [
        t.tool_name
        for t in session.scalars(select(ToolCall).where(ToolCall.run_id == row.id).order_by(ToolCall.id))
    ]
    assert names == ["search_movie", "propose_media_reply"]


def test_ambiguous_search_asks_instead_of_choosing(session, user, monkeypatch):
    _catalog(monkeypatch, session)
    llm = ScriptedLLM(
        turn(call("search_movie", title="The Matrix")),
        turn(call("ask_clarification", question="کدام ماتریکس؟ ۱۹۹۹ یا ۲۰۲۱؟")),
    )
    run, outcome = process_message(
        session, llm, user, "ماتریکس", AgentContext(), chat_id=78, now=lambda: FIXED_NOW
    )
    assert outcome.kind == "clarify"
    result = llm.last_tool_results()[0]
    assert result["ok"] is True and result["result"]["match_type"] == "ambiguous"
    assert "گزینه" in result["result"]["note"]


def test_media_reply_cannot_invent_ids(session, user, monkeypatch):
    _catalog(monkeypatch, session)
    llm = ScriptedLLM(
        turn(call("propose_media_reply", text="فیلم ساختگی", items=[{"media_id": 999999}])),
        turn(call("ask_clarification", question="دوباره بگویید کدام فیلم؟")),
    )
    _, outcome = process_message(
        session, llm, user, "یک فیلم", AgentContext(), chat_id=79, now=lambda: FIXED_NOW
    )
    assert outcome.kind == "clarify"
    assert "Unknown media id" in llm.last_tool_results()[0]["error"]


def test_prompt_declares_three_intents_and_no_audio():
    assert "movie_series" in SYSTEM_PROMPT
    assert "calendar" in SYSTEM_PROMPT.lower() and "email" in SYSTEM_PROMPT.lower()
    assert "NEVER produce audio" in SYSTEM_PROMPT
    assert "propose_media_reply" in SYSTEM_PROMPT

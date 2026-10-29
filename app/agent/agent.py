"""The agent loop: a bounded tool-calling conversation that ends in exactly one result."""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.agent.context import AgentContext, render_user_message
from app.agent.llm import LLMError, LLMProvider
from app.agent.llm_log import purpose_var
from app.agent.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from app.agent.schemas import Action, Clarification, EmailAction, MediaReply, ScheduleAction
from app.db.models import AgentRun, Intent, MediaItem, RecommendationContext, RunStatus, User
from app.logging_config import bind_run_id
from app.tools import impl as _tool_implementations  # noqa: F401  (registers the tools)
from app.tools.registry import (
    ToolContext,
    agent_tool_schemas,
    record_tool_call,
    run_tool,
    tehran_now,
)

log = logging.getLogger("agent")

MAX_STEPS = 6
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

TERMINAL_TOOLS: dict[str, type[BaseModel]] = {
    "propose_schedule": ScheduleAction,
    "propose_email": EmailAction,
    "propose_media_reply": MediaReply,
    "ask_clarification": Clarification,
}
_TERMINAL_DESCRIPTIONS = {
    "propose_schedule": "FINAL. Propose one calendar event for the owner to confirm.",
    "propose_email": "FINAL. Propose one email to a single recipient for the owner to confirm.",
    "propose_media_reply": "FINAL. Answer a movie/series request in short Persian. Include the "
    "media tools' internal media_id values in items; never invent a title, score, genre or id.",
    "ask_clarification": "FINAL. Ask the owner one short Persian question when information is missing, "
    "a title match is ambiguous, or the request is out of scope.",
}


def _terminal_schemas() -> list[dict[str, Any]]:
    out = []
    for name, model in TERMINAL_TOOLS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        out.append(
            {
                "type": "function",
                "function": {"name": name, "description": _TERMINAL_DESCRIPTIONS[name], "parameters": schema},
            }
        )
    return out


@dataclass
class AgentOutcome:
    kind: Literal["schedule", "email", "media", "clarify", "failed"]
    action: Action | None = None
    question: str | None = None
    error: str | None = None
    tokens: int = 0
    latency_ms: int = 0
    steps: int = 0


def _validate_action(action: Action, ctx: ToolContext, input_text: str) -> str | None:
    """Business rules beyond the schema. Returns an error for the model, or None if fine."""
    now = ctx.now()
    if isinstance(action, ScheduleAction):
        if (action.date, action.time) < (now.date(), now.time().replace(tzinfo=None)):
            return (
                f"The event is in the past (now is {now:%Y-%m-%d %H:%M}). "
                "Re-check the date, or ask the owner."
            )
        emails = [str(g.email) for g in action.guests]
    else:
        emails = [str(action.to)]

    allowed = set(ctx.known_emails)
    if ctx.user.email:
        allowed.add(ctx.user.email.lower())
    allowed |= {m.lower() for m in _EMAIL_RE.findall(input_text)}
    unknown = [e for e in emails if e.lower() not in allowed]
    if unknown:
        return (
            f"Email address(es) {unknown} did not come from find_contact or the owner's message. "
            "Never invent addresses: call find_contact, or ask_clarification."
        )
    if isinstance(action, ScheduleAction) and len({str(g.email).lower() for g in action.guests}) != len(action.guests):
        return "Duplicate guests."
    return None


def _validate_media(action: MediaReply, ctx: ToolContext) -> str | None:
    """Every media card must be a real cached item; a pending id must belong to this chat."""
    seen: set[int] = set()
    for card in action.items:
        if card.media_id in seen:
            return "Duplicate media cards."
        seen.add(card.media_id)
        if ctx.session.get(MediaItem, card.media_id) is None:
            return f"Unknown media id {card.media_id}: only use ids returned by media tools."
    if action.pending_id is not None:
        pending = ctx.session.get(RecommendationContext, action.pending_id)
        if (
            pending is None
            or pending.user_id != ctx.user.id
            or pending.chat_id != ctx.chat_id
            or pending.kind != "pending_import"
            or pending.status != "pending"
        ):
            return "Unknown pending choice: only use a pending_id returned by import_title_list."
    return None


def run_agent(
    provider: LLMProvider,
    ctx: ToolContext,
    text: str,
    agent_ctx: AgentContext,
    *,
    max_steps: int = MAX_STEPS,
) -> AgentOutcome:
    """Run the loop. Provider errors propagate as LLMError; everything else becomes an outcome."""
    started = time.perf_counter()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_user_message(text, ctx.now().isoformat(timespec="minutes"), agent_ctx)},
    ]
    tools = agent_tool_schemas() + _terminal_schemas()
    tokens = 0

    def done(kind, **kw) -> AgentOutcome:
        return AgentOutcome(
            kind=kind,
            tokens=tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            steps=step,
            **kw,
        )

    step = 0
    for step in range(1, max_steps + 1):
        purpose_var.set("intent" if step == 1 else "tool_step")
        resp = provider.chat(messages, tools)
        tokens += resp.total_tokens
        log.info("llm step", extra={"step": step, "tokens": resp.total_tokens, "tool_calls": len(resp.tool_calls)})
        messages.append(resp.as_message())

        if not resp.tool_calls:
            messages.append(
                {
                    "role": "user",
                    "content": "Finish by calling propose_schedule, propose_email, "
                    "propose_media_reply or ask_clarification.",
                }
            )
            continue

        terminals = [tc for tc in resp.tool_calls if tc.name in TERMINAL_TOOLS]
        outcome: AgentOutcome | None = None

        # Read-only tools first, so a final call in the same step sees their results.
        results: dict[str, str] = {}
        for tc in resp.tool_calls:
            if tc.name not in TERMINAL_TOOLS:
                r = run_tool(ctx, tc.name, tc.arguments, allow=lambda t: t.agent_visible)
                results[tc.id] = r.to_json()

        if len(terminals) > 1:
            for tc in terminals:
                results[tc.id] = json.dumps({"ok": False, "error": "Call exactly ONE final tool."})
        elif terminals:
            tc = terminals[0]
            error, outcome_parts = _handle_terminal(ctx, tc.name, tc.arguments, text)
            if error:
                results[tc.id] = json.dumps({"ok": False, "error": error}, ensure_ascii=False)
            else:
                outcome = done(**outcome_parts)
                results[tc.id] = json.dumps({"ok": True})

        for tc in resp.tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": results[tc.id]})
        if outcome is not None:
            return outcome

    return done("failed", error=f"No final answer within {max_steps} steps.")


def _handle_terminal(ctx: ToolContext, name: str, raw: str, text: str):
    """Validate a final tool call. Returns (error, outcome kwargs)."""
    model = TERMINAL_TOOLS[name]
    try:
        data = json.loads(raw or "{}")
        parsed = model.model_validate(data)
    except (ValueError, ValidationError) as exc:
        msg = str(exc)[:400]
        record_tool_call(ctx, name, {"raw": str(raw)[:500]}, None, "invalid", None, msg)
        return f"Invalid arguments: {msg}", None

    if isinstance(parsed, Clarification):
        record_tool_call(ctx, name, parsed.model_dump(mode="json"), {"accepted": True}, "ok", None, None)
        return None, {"kind": "clarify", "question": parsed.question}

    if isinstance(parsed, MediaReply):
        problem = _validate_media(parsed, ctx)
        if problem:
            record_tool_call(ctx, name, parsed.model_dump(mode="json"), None, "rejected", None, problem)
            return problem, None
        record_tool_call(ctx, name, parsed.model_dump(mode="json"), {"accepted": True}, "ok", None, None)
        return None, {"kind": "media", "action": parsed}

    problem = _validate_action(parsed, ctx, text)
    if problem:
        record_tool_call(ctx, name, parsed.model_dump(mode="json"), None, "rejected", None, problem)
        return problem, None
    record_tool_call(ctx, name, parsed.model_dump(mode="json"), {"accepted": True}, "ok", None, None)
    return None, {"kind": parsed.intent, "action": parsed}


# ---------- persistence wrapper ----------


def process_message(
    session: Session,
    provider: LLMProvider,
    user: User,
    text: str,
    agent_ctx: AgentContext,
    *,
    voice_message_id: int | None = None,
    chat_id: int = 0,
    now=tehran_now,
) -> tuple[AgentRun, AgentOutcome]:
    """Run the agent for one message and record the run, its tool calls, tokens and latency."""
    run = AgentRun(
        user_id=user.id,
        voice_message_id=voice_message_id,
        input_text=text,
        prompt_version=PROMPT_VERSION,
        model=provider.model,
        status=RunStatus.failed,  # stays 'failed' unless the run completes: a crash is never confirmable
        error="incomplete",
    )
    session.add(run)
    session.commit()

    ctx = ToolContext(session=session, user=user, run_id=run.id, now=now, chat_id=chat_id)
    ctx.known_emails |= {c["email"].lower() for c in agent_ctx.contacts if c.get("email")}
    started = time.perf_counter()
    try:
        with bind_run_id(run.id):
            outcome = run_agent(provider, ctx, text, agent_ctx)
    except LLMError as exc:
        outcome = AgentOutcome(kind="failed", error=str(exc))
    except Exception as exc:
        log.exception("Agent crashed")
        outcome = AgentOutcome(kind="failed", error=f"{type(exc).__name__}: {exc}")

    run.tokens = outcome.tokens
    run.latency_ms = int((time.perf_counter() - started) * 1000)
    if outcome.kind in ("schedule", "email"):
        run.intent = Intent(outcome.kind)
        run.action_json = outcome.action.model_dump(mode="json")
        run.status = RunStatus.pending_confirmation
        run.error = None
    elif outcome.kind == "media":
        # A media answer is already complete: no outside-world action, nothing to confirm.
        run.intent = Intent.movie_series
        run.action_json = outcome.action.model_dump(mode="json")
        run.status = RunStatus.executed
        run.error = None
    elif outcome.kind == "clarify":
        run.intent = Intent.unknown
        run.action_json = {"question": outcome.question}
        run.status = RunStatus.needs_clarification
        run.error = None
    else:
        run.intent = Intent.unknown
        run.error = outcome.error
    session.commit()
    with bind_run_id(run.id):
        log.info(
            "run finished",
            extra={"kind": outcome.kind, "status": run.status.value, "tokens": run.tokens,
                   "latency_ms": run.latency_ms, "steps": outcome.steps},
        )  # fmt: skip
    return run, outcome

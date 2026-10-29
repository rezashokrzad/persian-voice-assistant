"""Tool registry: JSON schemas from Pydantic models, validated calls, and tool_calls logging."""

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ToolCall, User

log = logging.getLogger("tools")
TEHRAN = ZoneInfo("Asia/Tehran")


def tehran_now() -> dt.datetime:
    return dt.datetime.now(TEHRAN)


@dataclass
class ToolContext:
    session: Session
    user: User
    run_id: int | None = None
    now: Callable[[], dt.datetime] = tehran_now
    # Telegram chat id for «دومی» resolution; 0 outside a chat (MCP).
    chat_id: int = 0
    # Emails the model may legitimately use: filled by find_contact / context, checked by the agent.
    known_emails: set[str] = field(default_factory=set)


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[ToolContext, Any], Any]
    # True for tools that change the outside world. The agent never sees these; only the
    # confirmation executor (and the memory step, for local writes) calls them.
    side_effect: bool = False
    agent_visible: bool = False

    def openai_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    REGISTRY[tool.name] = tool
    return tool


def agent_tool_schemas() -> list[dict[str, Any]]:
    return [t.openai_schema() for t in REGISTRY.values() if t.agent_visible]


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None

    def to_json(self) -> str:
        payload = {"ok": True, "result": self.data} if self.ok else {"ok": False, "error": self.error}
        return json.dumps(payload, ensure_ascii=False, default=str)


def run_tool(
    ctx: ToolContext,
    name: str,
    raw_args: str | dict | None,
    *,
    idempotency_key: str | None = None,
    allow: Callable[[Tool], bool] = lambda t: True,
) -> ToolResult:
    """Validate and execute one tool, writing a tool_calls row either way.

    With an idempotency_key the call runs at most once: a repeat returns the stored result.
    """
    tool = REGISTRY.get(name)
    if tool is None or not allow(tool):
        record_tool_call(
            ctx, name, {"raw": str(raw_args)[:500]}, None, "rejected", None, "unknown or not allowed"
        )
        return ToolResult(False, error=f"Unknown tool: {name}")

    try:
        args_dict = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        if not isinstance(args_dict, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as exc:
        record_tool_call(ctx, name, {"raw": str(raw_args)[:500]}, None, "invalid", None, str(exc))
        return ToolResult(False, error=f"Invalid arguments JSON: {exc}")

    try:
        args = tool.args_model.model_validate(args_dict)
    except ValidationError as exc:
        msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        record_tool_call(ctx, name, args_dict, None, "invalid", None, msg)
        return ToolResult(False, error=f"Invalid arguments: {msg}")

    row = None
    if ctx.run_id is not None:
        row = ToolCall(
            run_id=ctx.run_id,
            tool_name=name,
            arguments_json=json.loads(args.model_dump_json()),
            status="running",
            idempotency_key=idempotency_key,
        )
        try:
            with ctx.session.begin_nested():
                ctx.session.add(row)
                ctx.session.flush()
        except IntegrityError:
            existing = ctx.session.scalar(
                select(ToolCall).where(ToolCall.idempotency_key == idempotency_key)
            )
            if existing is not None and existing.status == "ok":
                return ToolResult(True, existing.result_json)
            return ToolResult(False, error="This action is already being processed.")
        ctx.session.commit()  # the 'running' marker must survive a crash mid-call

    started = time.perf_counter()
    try:
        data = tool.fn(ctx, args)
        result = ToolResult(True, data)
    except Exception as exc:  # tool failures go back to the caller as data, never crash the loop
        result = ToolResult(False, error=f"{type(exc).__name__}: {exc}")

    log.info(
        "tool call",
        extra={"tool": name, "ok": result.ok, "ms": int((time.perf_counter() - started) * 1000)},
    )
    if row is not None:
        row.status = "ok" if result.ok else "error"
        row.result_json = json.loads(result.to_json())["result"] if result.ok else {"error": result.error}
        ctx.session.commit()
    return result


def record_tool_call(ctx: ToolContext, name: str, args, result, status: str, key, error: str | None) -> None:
    if ctx.run_id is None:
        return
    ctx.session.add(
        ToolCall(
            run_id=ctx.run_id,
            tool_name=name,
            arguments_json=args,
            result_json={"error": error} if error else result,
            status=status,
            idempotency_key=key,
        )
    )
    ctx.session.commit()

"""Concrete tools. Importing this module fills the registry."""

import datetime as dt
import hashlib
import uuid
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.db.models import Contact, MemoryFact
from app.tools.google_calendar import GoogleCalendar
from app.tools.email_renderer import render_email
from app.tools.registry import Tool, ToolContext, register
from app.tools.resend_client import send_email as resend_send
from app.util.persian import (
    JALALI_MONTHS_FA,
    WEEKDAYS_FA,
    jalali_date,
    jalali_str,
    jalali_to_gregorian,
    normalize,
)


# ---------- argument models ----------


class NoArgs(BaseModel):
    pass


class ConvertJalaliArgs(BaseModel):
    year: int = Field(ge=1300, le=1500, description="Jalali year, e.g. 1405")
    month: int = Field(ge=1, le=12, description="Jalali month number, 1=فروردین")
    day: int = Field(ge=1, le=31)


class FindContactArgs(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Name or alias, Persian or English")


class SaveContactArgs(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    aliases: list[str] = Field(default_factory=list, max_length=10)


class RememberArgs(BaseModel):
    key: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$", description="snake_case key")]
    value: str = Field(min_length=1, max_length=500)


class ListEventsArgs(BaseModel):
    date: dt.date = Field(description="ISO date, e.g. 2026-10-20")


class CreateEventArgs(BaseModel):
    date: dt.date
    time: dt.time = Field(description="Local Asia/Tehran start time, HH:MM")
    duration_min: int = Field(ge=5, le=1440)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=4000)
    guests: list[EmailStr] = Field(default_factory=list, max_length=20)


class SendEmailArgs(BaseModel):
    to: EmailStr
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)


# ---------- implementations ----------


def get_current_datetime(ctx: ToolContext, args: NoArgs) -> dict:
    now = ctx.now()
    today = now.date()
    jy, jm, jd = jalali_date(today)
    upcoming = []
    for i in range(0, 15):
        d = today + dt.timedelta(days=i)
        upcoming.append(
            {"date": d.isoformat(), "weekday": WEEKDAYS_FA[d.weekday()], "jalali": jalali_str(d)}
        )
    return {
        "timezone": "Asia/Tehran",
        "iso": now.isoformat(timespec="minutes"),
        "date": today.isoformat(),
        "time": now.strftime("%H:%M"),
        "weekday": WEEKDAYS_FA[today.weekday()],
        "jalali": f"{jy:04d}/{jm:02d}/{jd:02d}",
        "jalali_month_name": JALALI_MONTHS_FA[jm - 1],
        "week_starts_on": "شنبه",
        "next_15_days": upcoming,
    }


def convert_jalali_date(ctx: ToolContext, args: ConvertJalaliArgs) -> dict:
    gy, gm, gd = jalali_to_gregorian(args.year, args.month, args.day)
    d = dt.date(gy, gm, gd)  # raises for impossible dates, reported back to the model
    return {"date": d.isoformat(), "weekday": WEEKDAYS_FA[d.weekday()]}


def _contact_dict(c: Contact) -> dict:
    return {"name": c.name, "email": c.email, "aliases": c.aliases}


def search_contacts(ctx: ToolContext, query: str) -> list[Contact]:
    q = normalize(query)
    if not q:
        return []
    scored: list[tuple[int, Contact]] = []
    for c in ctx.session.scalars(select(Contact).where(Contact.user_id == ctx.user.id)):
        names = [normalize(n) for n in [c.name, *c.aliases]]
        best = 0
        for n in names:
            if n == q:
                best = max(best, 3)
            elif n and (set(q.split()) <= set(n.split()) or set(n.split()) <= set(q.split())):
                best = max(best, 2)  # whole-word overlap only: "رضا" must not match "رضایی"
        if best:
            scored.append((best, c))
    scored.sort(key=lambda p: -p[0])
    return [c for _, c in scored]


def find_contact(ctx: ToolContext, args: FindContactArgs) -> dict:
    matches = search_contacts(ctx, args.name)
    for c in matches:
        if c.email:
            ctx.known_emails.add(c.email.lower())
    return {"matches": [_contact_dict(c) for c in matches]}


def save_contact(ctx: ToolContext, args: SaveContactArgs) -> dict:
    existing = next(
        (c for c in ctx.session.scalars(select(Contact).where(Contact.user_id == ctx.user.id))
         if c.email and c.email.lower() == str(args.email).lower()),
        None,
    )  # fmt: skip
    if existing is not None:
        merged = list(dict.fromkeys([*existing.aliases, args.name, *args.aliases]))
        existing.aliases = [a for a in merged if normalize(a) != normalize(existing.name)]
        return {"saved": "updated", "name": existing.name}
    ctx.session.add(
        Contact(
            user_id=ctx.user.id,
            name=args.name,
            email=str(args.email),
            aliases=list(dict.fromkeys(args.aliases)),
        )
    )
    ctx.session.flush()
    return {"saved": "created", "name": args.name}


def remember(ctx: ToolContext, args: RememberArgs) -> dict:
    fact = ctx.session.scalar(
        select(MemoryFact).where(MemoryFact.user_id == ctx.user.id, MemoryFact.key == args.key)
    )
    if fact is None:
        ctx.session.add(
            MemoryFact(user_id=ctx.user.id, key=args.key, value=args.value, source="agent")
        )
    else:
        fact.value = args.value
        fact.source = "agent"
    ctx.session.flush()
    return {"remembered": args.key}


_calendar: GoogleCalendar | None = None


def calendar() -> GoogleCalendar:
    global _calendar
    if _calendar is None:
        _calendar = GoogleCalendar()
    return _calendar


def list_calendar_events(ctx: ToolContext, args: ListEventsArgs) -> dict:
    return {"events": calendar().list_events(args.date)}


def event_id_for(key: str) -> str:
    # Google event ids must be base32hex (0-9, a-v), 5-1024 chars: a hex digest qualifies.
    return hashlib.sha1(key.encode()).hexdigest()


def create_calendar_event(ctx: ToolContext, args: CreateEventArgs) -> dict:
    # Outside an agent run (e.g. the MCP server) there is no run id: use a fresh unique id.
    key = f"run:{ctx.run_id}:create_calendar_event" if ctx.run_id else f"adhoc:{uuid.uuid4()}"
    return calendar().create_event(
        date=args.date,
        time_=args.time,
        duration_min=args.duration_min,
        title=args.title,
        description=args.description,
        guests=[str(g) for g in args.guests],
        event_id=event_id_for(key),
    )


def send_email(ctx: ToolContext, args: SendEmailArgs) -> dict:
    from app.config import get_settings

    key = f"run-{ctx.run_id}-send_email" if ctx.run_id else None
    message_id = resend_send(
        str(args.to), args.subject, args.body,
        html=render_email(args.subject, args.body, font_url=get_settings().email_font_url), idempotency_key=key,
    )
    return {"provider_message_id": message_id}


register(Tool("get_current_datetime",
    "Current date and time in Asia/Tehran (Gregorian and Jalali) plus the next 15 days with "
    "Persian weekday names. Use it to resolve relative dates like فردا or پنجشنبه.",
    NoArgs, get_current_datetime, agent_visible=True))  # fmt: skip
register(Tool("convert_jalali_date",
    "Convert a Jalali (Shamsi) date to the Gregorian ISO date. Always use this for explicit "
    "Jalali dates; never convert by hand.",
    ConvertJalaliArgs, convert_jalali_date, agent_visible=True))  # fmt: skip
register(Tool("find_contact",
    "Find saved contacts by name or alias (Persian or English). Returns name and email. "
    "An empty list means the contact is unknown.",
    FindContactArgs, find_contact, agent_visible=True))  # fmt: skip
register(Tool("list_calendar_events",
    "List the owner's calendar events on one date, to check for conflicts.",
    ListEventsArgs, list_calendar_events, agent_visible=True))  # fmt: skip
register(Tool("save_contact", "Save or update a contact with an email address.",
    SaveContactArgs, save_contact))  # fmt: skip
register(Tool("remember", "Store one preference or fact under a snake_case key.",
    RememberArgs, remember))  # fmt: skip
register(Tool("create_calendar_event",
    "Create a Google Calendar event and email invitations to guests.",
    CreateEventArgs, create_calendar_event, side_effect=True))  # fmt: skip
register(Tool("send_email", "Send an email through Resend.",
    SendEmailArgs, send_email, side_effect=True))  # fmt: skip

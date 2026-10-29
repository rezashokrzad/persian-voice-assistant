"""The only things the agent may output: one ScheduleAction, one EmailAction, or a question."""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class Guest(BaseModel):
    name: str | None = Field(default=None, max_length=200, description="Contact name as saved")
    email: EmailStr


class ScheduleAction(BaseModel):
    intent: Literal["schedule"] = "schedule"
    date: dt.date = Field(description="ISO date (YYYY-MM-DD), Gregorian")
    time: dt.time = Field(description="Start time HH:MM, 24-hour, Asia/Tehran")
    duration_min: int = Field(ge=5, le=1440, description="Duration in minutes")
    title: str = Field(min_length=1, max_length=300, description="Event title, in Persian")
    description: str = Field(default="", max_length=4000, description="Short description, in Persian")
    guests: list[Guest] = Field(default_factory=list, max_length=20)


class EmailAction(BaseModel):
    intent: Literal["email"] = "email"
    to: EmailStr = Field(description="Exactly one recipient, taken from find_contact or the user's words")
    to_name: str | None = Field(default=None, max_length=200, description="Recipient name as saved")
    subject: str = Field(min_length=1, max_length=300, description="Subject, in Persian")
    body: str = Field(min_length=1, max_length=20000, description="Email body, in Persian")


class Clarification(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="One short question, in Persian")


class MediaCard(BaseModel):
    media_id: int = Field(
        gt=0, description="Internal id returned by the media tools (not tmdb_id)"
    )
    reason: str | None = Field(
        default=None,
        max_length=300,
        description="One short grounded reason, copied from the tool's why/reason; never invent",
    )


class MediaReply(BaseModel):
    """A short Persian answer plus zero or more media cards (no confirmation needed)."""

    intent: Literal["movie_series"] = "movie_series"
    text: str = Field(min_length=1, max_length=1200, description="Short Persian answer")
    items: list[MediaCard] = Field(default_factory=list, max_length=10)
    pending_id: int | None = Field(
        default=None, description="pending import id whose candidates the owner must choose"
    )


Action = ScheduleAction | EmailAction | MediaReply

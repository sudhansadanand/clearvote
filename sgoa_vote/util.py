"""Small shared helpers: identifiers, time, and display formatting."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def now_iso() -> str:
    """UTC, ISO-8601, `Z` suffix. Every timestamp in every database uses this."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def future_iso(**delta) -> str:
    moment = datetime.now(UTC) + timedelta(**delta)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def is_past(value: str) -> bool:
    return parse_iso(value) < datetime.now(UTC)


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


def display_time(value: str | None, tz_name: str = "Asia/Kolkata") -> str:
    """Render a stored UTC timestamp in the meeting's local timezone."""
    if not value:
        return "-"
    try:
        moment = parse_iso(value)
    except (ValueError, TypeError):
        return value
    try:
        from zoneinfo import ZoneInfo

        moment = moment.astimezone(ZoneInfo(tz_name))
    except Exception:  # pragma: no cover - tzdata missing on a bare system
        pass
    return moment.strftime("%d %b %Y, %H:%M:%S")

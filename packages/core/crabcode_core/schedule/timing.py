"""Schedule parsing and next-run calculation helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from crabcode_core.schedule.models import ScheduleType


_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_WEEKDAY_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_interval(value: str) -> int:
    """Parse a positive interval such as ``30m`` or ``2h`` into seconds."""
    raw = str(value).strip().lower()
    if not raw:
        raise ValueError("interval cannot be empty")

    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    unit = raw[-1]
    number = raw[:-1] if unit in multipliers else raw
    multiplier = multipliers.get(unit, 1)
    try:
        amount = int(number)
    except ValueError as exc:
        raise ValueError(f"invalid interval: {value!r}") from exc
    seconds = amount * multiplier
    if seconds <= 0:
        raise ValueError("interval must be greater than zero")
    return seconds


def parse_iso_timestamp(value: str) -> datetime:
    """Parse an aware ISO 8601 timestamp and normalize it to UTC."""
    raw = str(value).strip()
    if raw.endswith(("Z", "z")):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    return parsed.astimezone(timezone.utc)


def _parse_atom(
    value: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None,
) -> int:
    lowered = value.strip().lower()
    if names and lowered in names:
        result = names[lowered]
    else:
        try:
            result = int(lowered)
        except ValueError as exc:
            raise ValueError(f"invalid cron value: {value!r}") from exc
    if result < minimum or result > maximum:
        raise ValueError(
            f"cron value {result} is outside the range {minimum}-{maximum}"
        )
    return result


def _expand_cron_field(
    expression: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None = None,
    normalize_weekday: bool = False,
) -> tuple[set[int], bool]:
    """Expand one cron field and report whether it is unrestricted."""
    raw = expression.strip().lower()
    if not raw:
        raise ValueError("cron field cannot be empty")
    values: set[int] = set()
    unrestricted = raw == "*"

    for component in raw.split(","):
        if not component:
            raise ValueError(f"invalid cron field: {expression!r}")
        base, separator, step_text = component.partition("/")
        if separator:
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError(f"invalid cron step: {step_text!r}") from exc
            if step <= 0:
                raise ValueError("cron step must be greater than zero")
        else:
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _parse_atom(
                start_text,
                minimum=minimum,
                maximum=maximum,
                names=names,
            )
            end = _parse_atom(
                end_text,
                minimum=minimum,
                maximum=maximum,
                names=names,
            )
            if end < start:
                raise ValueError(f"cron range must be ascending: {base!r}")
        else:
            start = _parse_atom(
                base,
                minimum=minimum,
                maximum=maximum,
                names=names,
            )
            # ``5/10`` means every ten units beginning at five.
            end = maximum if separator else start

        values.update(range(start, end + 1, step))

    if normalize_weekday and 7 in values:
        values.discard(7)
        values.add(0)
    if not values:
        raise ValueError(f"cron field has no values: {expression!r}")
    return values, unrestricted


def _cron_fields(expression: str) -> tuple[
    set[int], set[int], set[int], set[int], set[int], bool, bool
]:
    parts = str(expression).split()
    if len(parts) != 5:
        raise ValueError("cron expression must contain exactly five fields")
    minutes, _ = _expand_cron_field(parts[0], minimum=0, maximum=59)
    hours, _ = _expand_cron_field(parts[1], minimum=0, maximum=23)
    days, day_unrestricted = _expand_cron_field(parts[2], minimum=1, maximum=31)
    months, _ = _expand_cron_field(
        parts[3], minimum=1, maximum=12, names=_MONTH_NAMES
    )
    weekdays, weekday_unrestricted = _expand_cron_field(
        parts[4],
        minimum=0,
        maximum=7,
        names=_WEEKDAY_NAMES,
        normalize_weekday=True,
    )
    return (
        minutes,
        hours,
        days,
        months,
        weekdays,
        day_unrestricted,
        weekday_unrestricted,
    )


def next_cron_run(expression: str, after: datetime | None = None) -> datetime:
    """Return the next UTC run for a standard five-field cron expression.

    Day-of-month and day-of-week use the traditional cron OR rule when both
    fields are restricted.
    """
    (
        minutes,
        hours,
        days,
        months,
        weekdays,
        day_unrestricted,
        weekday_unrestricted,
    ) = _cron_fields(expression)

    base = after or utc_now()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base = base.astimezone(timezone.utc)
    day_start = base.replace(hour=0, minute=0, second=0, microsecond=0)

    # Iterate by calendar day, then only through the allowed hour/minute
    # combinations.  This keeps sparse expressions (for example February
    # 29th) cheap and prevents malformed schedules from blocking an event loop
    # while scanning millions of individual minutes.
    for offset in range(366 * 8 + 1):
        current_day = day_start + timedelta(days=offset)
        cron_weekday = (current_day.weekday() + 1) % 7
        day_matches = current_day.day in days
        weekday_matches = cron_weekday in weekdays
        if day_unrestricted:
            calendar_day_matches = weekday_matches
        elif weekday_unrestricted:
            calendar_day_matches = day_matches
        else:
            calendar_day_matches = day_matches or weekday_matches

        if (
            current_day.month not in months
            or not calendar_day_matches
        ):
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                candidate = current_day.replace(hour=hour, minute=minute)
                if candidate > base:
                    return candidate
    raise ValueError("cron expression has no run time within the next eight years")


def compute_next_run(
    schedule: str,
    schedule_type: ScheduleType | str,
    *,
    now: datetime | None = None,
    previous_run: datetime | None = None,
) -> str:
    """Validate a schedule and return its next UTC ISO timestamp."""
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    kind = ScheduleType(schedule_type)

    if kind == ScheduleType.ONCE:
        raw = str(schedule).strip()
        if raw.startswith("+"):
            return (current + timedelta(seconds=parse_interval(raw[1:]))).isoformat()
        return parse_iso_timestamp(raw).isoformat()

    if kind == ScheduleType.INTERVAL:
        seconds = parse_interval(schedule)
        anchor = previous_run or current
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        candidate = anchor.astimezone(timezone.utc) + timedelta(seconds=seconds)
        if candidate <= current:
            missed = int((current - candidate).total_seconds() // seconds) + 1
            candidate += timedelta(seconds=missed * seconds)
        return candidate.isoformat()

    return next_cron_run(schedule, after=current).isoformat()


def earliest_timestamp(values: Iterable[str | None]) -> str | None:
    """Return the earliest valid ISO timestamp from *values*."""
    parsed: list[datetime] = []
    for value in values:
        if value:
            try:
                parsed.append(parse_iso_timestamp(value))
            except ValueError:
                continue
    return min(parsed).isoformat() if parsed else None

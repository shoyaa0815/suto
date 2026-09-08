from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import MissedRunPolicy, Schedule, ScheduleKind
from .store import JobStore


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC)


def parse_once(value: str, timezone: str) -> datetime:
    zone = validate_timezone(timezone)
    normalized = value.strip().replace("Z", "+00:00")
    try:
        instant = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("--at must be an ISO-8601 date and time") from error
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=zone)
    return _utc(instant)


def validate_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown timezone: {name}") from error


@dataclass(frozen=True)
class _CronField:
    values: frozenset[int]
    wildcard: bool


def _parse_cron_field(text: str, minimum: int, maximum: int, name: str) -> _CronField:
    values: set[int] = set()
    wildcard = text == "*"
    for item in text.split(","):
        base, separator, step_text = item.partition("/")
        if separator:
            try:
                step = int(step_text)
            except ValueError as error:
                raise ValueError(f"invalid cron {name} step: {item}") from error
            if step < 1:
                raise ValueError(f"cron {name} step must be positive")
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as error:
                raise ValueError(f"invalid cron {name} range: {base}") from error
        else:
            try:
                start = end = int(base)
            except ValueError as error:
                raise ValueError(f"invalid cron {name} value: {base}") from error
        if start < minimum or end > maximum or start > end:
            raise ValueError(
                f"cron {name} values must be between {minimum} and {maximum}"
            )
        values.update(range(start, end + 1, step))
    return _CronField(frozenset(values), wildcard)


@dataclass(frozen=True)
class CronExpression:
    minute: _CronField
    hour: _CronField
    day: _CronField
    month: _CronField
    weekday: _CronField

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("cron expression must contain 5 fields")
        minute, hour, day, month, weekday = fields
        parsed_weekday = _parse_cron_field(weekday, 0, 7, "weekday")
        weekday_values = frozenset(
            0 if value == 7 else value for value in parsed_weekday.values
        )
        return cls(
            _parse_cron_field(minute, 0, 59, "minute"),
            _parse_cron_field(hour, 0, 23, "hour"),
            _parse_cron_field(day, 1, 31, "day"),
            _parse_cron_field(month, 1, 12, "month"),
            _CronField(weekday_values, parsed_weekday.wildcard),
        )

    def matches(self, value: datetime) -> bool:
        cron_weekday = (value.weekday() + 1) % 7
        day_matches = value.day in self.day.values
        weekday_matches = cron_weekday in self.weekday.values
        if self.day.wildcard:
            calendar_day_matches = weekday_matches
        elif self.weekday.wildcard:
            calendar_day_matches = day_matches
        else:
            calendar_day_matches = day_matches or weekday_matches
        return (
            value.minute in self.minute.values
            and value.hour in self.hour.values
            and value.month in self.month.values
            and calendar_day_matches
        )

    def next_after(self, after: datetime, timezone: str) -> datetime:
        zone = validate_timezone(timezone)
        candidate = _utc(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
        deadline = candidate + timedelta(days=366 * 2)
        while candidate <= deadline:
            if self.matches(candidate.astimezone(zone)):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("cron expression has no occurrence in the next two years")


def next_occurrence(schedule: Schedule, after: datetime) -> datetime | None:
    after = _utc(after)
    if schedule.kind == ScheduleKind.ONCE:
        return None
    if schedule.kind == ScheduleKind.INTERVAL:
        return after + timedelta(seconds=int(schedule.expression))
    return CronExpression.parse(schedule.expression).next_after(
        after, schedule.timezone
    )


class Scheduler:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    def create(
        self,
        *,
        kind: str | ScheduleKind,
        expression: str,
        prompt: str,
        timezone: str = "UTC",
        workspace: str | Path = ".",
        allow_write: bool = False,
        allow_command: bool = False,
        missed_run_policy: str | MissedRunPolicy = MissedRunPolicy.RUN_ONCE,
        retry_limit: int = 0,
        retry_delay_seconds: int = 60,
        now: datetime | None = None,
    ) -> Schedule:
        schedule_kind = ScheduleKind(kind)
        validate_timezone(timezone)
        current = _utc(now or datetime.now(UTC))
        if not prompt.strip():
            raise ValueError("schedule requires a task")
        workspace_path = Path(workspace).resolve()
        if not workspace_path.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace_path}")
        if schedule_kind == ScheduleKind.ONCE:
            first = parse_once(expression, timezone)
            canonical_expression = first.isoformat()
        elif schedule_kind == ScheduleKind.INTERVAL:
            try:
                seconds = int(expression)
            except ValueError as error:
                raise ValueError("--every must be a whole number of seconds") from error
            if seconds < 1 or seconds > 31_536_000:
                raise ValueError("interval must be between 1 and 31536000 seconds")
            canonical_expression = str(seconds)
            first = current + timedelta(seconds=seconds)
        else:
            cron = CronExpression.parse(expression)
            canonical_expression = " ".join(expression.split())
            first = cron.next_after(current, timezone)
        return self.store.create_schedule(
            kind=schedule_kind,
            expression=canonical_expression,
            timezone=timezone,
            prompt=prompt,
            workspace=str(workspace_path),
            allow_write=allow_write,
            allow_command=allow_command,
            missed_run_policy=missed_run_policy,
            retry_limit=retry_limit,
            retry_delay_seconds=retry_delay_seconds,
            next_run_at=first.isoformat(),
        )

    def tick(self, now: datetime | None = None) -> int:
        current = _utc(now or datetime.now(UTC))
        created = 0
        for trigger in self.store.list_retryable_triggers(current.isoformat()):
            if self.store.retry_trigger(trigger.id) is not None:
                created += 1

        for schedule in self.store.list_due_schedules(current.isoformat()):
            scheduled_for = datetime.fromisoformat(schedule.next_run_at)
            following = next_occurrence(schedule, scheduled_for)
            skip_detail = None
            if (
                schedule.missed_run_policy == MissedRunPolicy.SKIP
                and following is not None
                and following <= current
            ):
                skip_detail = "skipped missed occurrence"
                while following <= current:
                    following = next_occurrence(schedule, following)
            elif schedule.missed_run_policy == MissedRunPolicy.RUN_ONCE:
                following = next_occurrence(schedule, current)
            trigger = self.store.fire_schedule(
                schedule.id,
                schedule.next_run_at,
                following.isoformat() if following is not None else None,
                skip_detail=skip_detail,
            )
            if trigger is not None and trigger.job_id is not None:
                created += 1
        return created

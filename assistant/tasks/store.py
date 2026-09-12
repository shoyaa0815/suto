import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from automation.storage.redaction import redact_text

from .models import Reminder, Task


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: str, label: str, maximum: int) -> str:
    result = redact_text(value).strip()
    if not result or len(result) > maximum:
        raise ValueError(f"{label} must contain 1-{maximum} characters")
    return result


def _instant(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{label} must be an ISO-8601 date and time") from error
    if instant.tzinfo is None:
        raise ValueError(f"{label} must include a timezone offset")
    return instant.isoformat()


def _reference_time(value: str | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return datetime.fromisoformat(_instant(value, "now"))


class TaskStore:
    @staticmethod
    def _to_task(row) -> Task | None:
        return Task(**dict(row)) if row is not None else None

    @staticmethod
    def _to_reminder(row) -> Reminder | None:
        return Reminder(**dict(row)) if row is not None else None

    def create_task(
        self,
        user_id: str,
        title: str,
        *,
        notes: str = "",
        due_at: str | None = None,
    ) -> Task:
        task_id = f"task_{uuid4().hex[:10]}"
        now = _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO assistant_tasks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    user_id,
                    _text(title, "task title", 500),
                    redact_text(notes).strip()[:4000],
                    "open",
                    _instant(due_at, "due_at"),
                    now,
                    now,
                    None,
                ),
            )
        return self.get_task(user_id, task_id)

    def get_task(self, user_id: str, task_id: str) -> Task | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM assistant_tasks WHERE id=? AND user_id=?",
                (task_id, user_id),
            ).fetchone()
        return self._to_task(row)

    def list_tasks(self, user_id: str, *, include_completed: bool = False) -> list[Task]:
        query = "SELECT * FROM assistant_tasks WHERE user_id=?"
        if not include_completed:
            query += " AND status='open'"
        query += " ORDER BY due_at IS NULL,due_at,created_at LIMIT 100"
        with self._connect() as db:
            rows = db.execute(query, (user_id,)).fetchall()
        return [self._to_task(row) for row in rows]

    def complete_task(self, user_id: str, task_id: str) -> Task | None:
        now = _now()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE assistant_tasks SET status='completed',updated_at=?,completed_at=? "
                "WHERE id=? AND user_id=? AND status='open'",
                (now, now, task_id, user_id),
            )
        return self.get_task(user_id, task_id) if cursor.rowcount else None

    def create_reminder(
        self,
        user_id: str,
        title: str,
        remind_at: str,
        *,
        timezone: str = "UTC",
        channel_identity_id: str | None = None,
        now: str | None = None,
    ) -> Reminder:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown timezone: {timezone}") from error
        remind_at = _instant(remind_at, "remind_at")
        if datetime.fromisoformat(remind_at) <= _reference_time(now):
            raise ValueError("remind_at must be in the future")
        reminder_id = f"rem_{uuid4().hex[:10]}"
        created_at = _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO reminders VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    reminder_id,
                    user_id,
                    _text(title, "reminder title", 500),
                    remind_at,
                    timezone,
                    "scheduled",
                    channel_identity_id,
                    created_at,
                    created_at,
                ),
            )
        return self.get_reminder(user_id, reminder_id)

    def create_relative_reminder(
        self,
        user_id: str,
        title: str,
        minutes: int,
        *,
        timezone: str = "UTC",
        now: str | None = None,
    ) -> Reminder:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 525_600:
            raise ValueError("minutes must be an integer between 1 and 525600")
        reference = _reference_time(now)
        remind_at = reference + timedelta(minutes=minutes)
        return self.create_reminder(
            user_id,
            title,
            remind_at.isoformat(),
            timezone=timezone,
            now=reference.isoformat(),
        )

    def create_clock_reminder(
        self,
        user_id: str,
        title: str,
        clock_time: str,
        *,
        timezone: str = "UTC",
        now: str | None = None,
    ) -> Reminder:
        if not isinstance(clock_time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d",
            clock_time,
        ):
            raise ValueError("time must use 24-hour HH:MM format")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown timezone: {timezone}") from error
        reference = _reference_time(now).astimezone(zone)
        hour, minute = (int(part) for part in clock_time.split(":"))
        remind_at = reference.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if remind_at <= reference:
            remind_at += timedelta(days=1)
        return self.create_reminder(
            user_id,
            title,
            remind_at.isoformat(),
            timezone=timezone,
            now=reference.isoformat(),
        )

    def get_reminder(self, user_id: str, reminder_id: str) -> Reminder | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM reminders WHERE id=? AND user_id=?",
                (reminder_id, user_id),
            ).fetchone()
        return self._to_reminder(row)

    def list_reminders(
        self,
        user_id: str,
        *,
        include_cancelled: bool = False,
        limit: int | None = 100,
    ) -> list[Reminder]:
        query = "SELECT * FROM reminders WHERE user_id=?"
        if not include_cancelled:
            query += " AND status='scheduled'"
        query += " ORDER BY remind_at"
        parameters: tuple = (user_id,)
        if limit is not None:
            if limit < 1:
                raise ValueError("reminder limit must be positive")
            query += " LIMIT ?"
            parameters += (limit,)
        with self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [self._to_reminder(row) for row in rows]

    def claim_due_reminders(
        self,
        user_id: str,
        *,
        now: str | None = None,
    ) -> list[Reminder]:
        """Atomically mark and return reminders that are due for delivery."""
        cutoff = datetime.fromisoformat(_instant(now, "now")) if now else datetime.now(UTC)
        claimed = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM reminders "
                "WHERE user_id=? AND status='scheduled' ORDER BY remind_at",
                (user_id,),
            ).fetchall()
            for row in rows:
                remind_at = datetime.fromisoformat(row["remind_at"])
                if remind_at > cutoff:
                    continue
                cursor = db.execute(
                    "UPDATE reminders SET status='delivered',updated_at=? "
                    "WHERE id=? AND status='scheduled'",
                    (_now(), row["id"]),
                )
                if cursor.rowcount:
                    claimed.append(self._to_reminder(row))
        return claimed

    def reschedule_reminder(
        self,
        user_id: str,
        reminder_id: str,
        remind_at: str,
        *,
        now: str | None = None,
    ) -> Reminder | None:
        remind_at = _instant(remind_at, "remind_at")
        if datetime.fromisoformat(remind_at) <= _reference_time(now):
            raise ValueError("remind_at must be in the future")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE reminders SET remind_at=?,status='scheduled',updated_at=? "
                "WHERE id=? AND user_id=?",
                (remind_at, _now(), reminder_id, user_id),
            )
        return self.get_reminder(user_id, reminder_id) if cursor.rowcount else None

    def cancel_reminder(self, user_id: str, reminder_id: str) -> Reminder | None:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE reminders SET status='cancelled',updated_at=? "
                "WHERE id=? AND user_id=? AND status='scheduled'",
                (_now(), reminder_id, user_id),
            )
        return self.get_reminder(user_id, reminder_id) if cursor.rowcount else None

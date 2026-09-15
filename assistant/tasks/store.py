import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workflows.storage.redaction import redact_text

from .models import DeliveryTarget, DueReminderDelivery, Reminder, Task


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

    @staticmethod
    def _to_delivery_target(row) -> DeliveryTarget | None:
        return DeliveryTarget(**dict(row)) if row is not None else None

    def get_or_create_delivery_target(
        self,
        user_id: str,
        platform: str,
        destination_id: str,
        destination_type: str,
        display_name: str,
        *,
        guild_id: str | None = None,
        requester_id: str | None = None,
    ) -> DeliveryTarget:
        platform = _text(platform.casefold(), "delivery platform", 32)
        destination_id = _text(destination_id, "destination id", 255)
        if destination_type not in {"dm", "guild_channel"}:
            raise ValueError("destination type must be dm or guild_channel")
        display_name = _text(display_name, "destination name", 100)
        guild_id = _text(guild_id, "guild id", 255) if guild_id else None
        requester_id = (
            _text(requester_id, "requester id", 255) if requester_id else None
        )
        now = _now()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM delivery_targets WHERE user_id=? AND platform=? "
                "AND destination_id=?",
                (user_id, platform, destination_id),
            ).fetchone()
            if row is None:
                target_id = f"dest_{uuid4().hex[:12]}"
                db.execute(
                    "INSERT INTO delivery_targets VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        target_id,
                        user_id,
                        platform,
                        destination_id,
                        destination_type,
                        display_name,
                        guild_id,
                        requester_id,
                        now,
                        now,
                    ),
                )
            else:
                target_id = row["id"]
                db.execute(
                    "UPDATE delivery_targets SET destination_type=?,display_name=?,"
                    "guild_id=?,requester_id=?,updated_at=? WHERE id=?",
                    (
                        destination_type,
                        display_name,
                        guild_id,
                        requester_id,
                        now,
                        target_id,
                    ),
                )
            row = db.execute(
                "SELECT * FROM delivery_targets WHERE id=?", (target_id,)
            ).fetchone()
        return self._to_delivery_target(row)

    def get_delivery_target(
        self, user_id: str, target_id: str
    ) -> DeliveryTarget | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM delivery_targets WHERE id=? AND user_id=?",
                (target_id, user_id),
            ).fetchone()
        return self._to_delivery_target(row)

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
        delivery_target_id: str | None = None,
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
            if delivery_target_id is not None:
                target = db.execute(
                    "SELECT 1 FROM delivery_targets WHERE id=? AND user_id=?",
                    (delivery_target_id, user_id),
                ).fetchone()
                if target is None:
                    raise ValueError("delivery target does not belong to the user")
            db.execute(
                "INSERT INTO reminders(id,user_id,title,remind_at,timezone,status,"
                "channel_identity_id,created_at,updated_at,delivery_target_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                    delivery_target_id,
                ),
            )
            if delivery_target_id is not None:
                db.execute(
                    "INSERT INTO reminder_deliveries VALUES (?,?,?,?,?,?)",
                    (reminder_id, "pending", 0, None, None, created_at),
                )
        return self.get_reminder(user_id, reminder_id)

    def create_relative_reminder(
        self,
        user_id: str,
        title: str,
        minutes: int,
        *,
        timezone: str = "UTC",
        delivery_target_id: str | None = None,
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
            delivery_target_id=delivery_target_id,
            now=reference.isoformat(),
        )

    def create_clock_reminder(
        self,
        user_id: str,
        title: str,
        clock_time: str,
        *,
        timezone: str = "UTC",
        delivery_target_id: str | None = None,
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
            delivery_target_id=delivery_target_id,
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
                "WHERE user_id=? AND status='scheduled' "
                "AND delivery_target_id IS NULL ORDER BY remind_at",
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

    def claim_due_reminder_deliveries(
        self,
        platform: str,
        *,
        now: str | None = None,
        stale_after_seconds: int = 300,
    ) -> list[DueReminderDelivery]:
        """Claim due external deliveries, including abandoned in-flight claims."""
        cutoff = _reference_time(now)
        stale_before = cutoff - timedelta(seconds=stale_after_seconds)
        claimed = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT r.id AS reminder_id,r.user_id,r.title,r.remind_at,"
                "t.id AS target_id,t.platform,t.destination_id,t.destination_type,"
                "t.display_name,t.guild_id,t.requester_id,d.attempt_count "
                "FROM reminders AS r "
                "JOIN delivery_targets AS t ON t.id=r.delivery_target_id "
                "JOIN reminder_deliveries AS d ON d.reminder_id=r.id "
                "WHERE t.platform=? AND r.status='scheduled' "
                "AND julianday(r.remind_at)<=julianday(?) AND ("
                "d.status='pending' OR "
                "(d.status='retrying' AND julianday(d.retry_at)<=julianday(?)) OR "
                "(d.status='sending' AND julianday(d.updated_at)<=julianday(?))) "
                "ORDER BY r.remind_at",
                (
                    platform.casefold(),
                    cutoff.isoformat(),
                    cutoff.isoformat(),
                    stale_before.isoformat(),
                ),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE reminder_deliveries SET status='sending',"
                    "attempt_count=attempt_count+1,retry_at=NULL,last_error=NULL,"
                    "updated_at=? WHERE reminder_id=?",
                    (cutoff.isoformat(), row["reminder_id"]),
                )
                values = dict(row)
                values["attempt_count"] += 1
                claimed.append(DueReminderDelivery(**values))
        return claimed

    def complete_reminder_delivery(self, reminder_id: str) -> bool:
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE reminder_deliveries SET status='delivered',updated_at=? "
                "WHERE reminder_id=? AND status='sending'",
                (now, reminder_id),
            )
            if not cursor.rowcount:
                return False
            db.execute(
                "UPDATE reminders SET status='delivered',updated_at=? "
                "WHERE id=? AND status='scheduled'",
                (now, reminder_id),
            )
        return True

    def reminder_delivery_is_current(self, delivery: DueReminderDelivery) -> bool:
        """Revalidate a claimed reminder immediately before its external send."""
        with self._connect() as db:
            row = db.execute(
                "SELECT r.status,r.remind_at,r.delivery_target_id,d.status AS delivery_status "
                "FROM reminders AS r JOIN reminder_deliveries AS d "
                "ON d.reminder_id=r.id WHERE r.id=? AND r.user_id=?",
                (delivery.reminder_id, delivery.user_id),
            ).fetchone()
        return bool(
            row is not None
            and row["status"] == "scheduled"
            and row["delivery_status"] == "sending"
            and row["remind_at"] == delivery.remind_at
            and row["delivery_target_id"] == delivery.target_id
        )

    def fail_reminder_delivery(
        self,
        reminder_id: str,
        error: str,
        *,
        retry_delay_seconds: int = 30,
        max_attempts: int = 3,
        now: str | None = None,
    ) -> str | None:
        current = _reference_time(now)
        message = redact_text(str(error)).strip()[:1000] or "delivery failed"
        with self._connect() as db:
            row = db.execute(
                "SELECT attempt_count FROM reminder_deliveries "
                "WHERE reminder_id=? AND status='sending'",
                (reminder_id,),
            ).fetchone()
            if row is None:
                return None
            failed = row["attempt_count"] >= max_attempts
            status = "failed" if failed else "retrying"
            retry_at = (
                None
                if failed
                else (current + timedelta(seconds=retry_delay_seconds)).isoformat()
            )
            db.execute(
                "UPDATE reminder_deliveries SET status=?,retry_at=?,last_error=?,"
                "updated_at=? WHERE reminder_id=? AND status='sending'",
                (status, retry_at, message, current.isoformat(), reminder_id),
            )
        return status

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
            if cursor.rowcount:
                db.execute(
                    "UPDATE reminder_deliveries SET status='pending',attempt_count=0,"
                    "retry_at=NULL,last_error=NULL,updated_at=? WHERE reminder_id=?",
                    (_now(), reminder_id),
                )
        return self.get_reminder(user_id, reminder_id) if cursor.rowcount else None

    def cancel_reminder(self, user_id: str, reminder_id: str) -> Reminder | None:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE reminders SET status='cancelled',updated_at=? "
                "WHERE id=? AND user_id=? AND status='scheduled'",
                (_now(), reminder_id, user_id),
            )
            if cursor.rowcount:
                db.execute(
                    "UPDATE reminder_deliveries SET status='failed',retry_at=NULL,"
                    "last_error='cancelled',updated_at=? WHERE reminder_id=?",
                    (_now(), reminder_id),
                )
        return self.get_reminder(user_id, reminder_id) if cursor.rowcount else None

    def cancel_reminder_by_title(
        self,
        user_id: str,
        title: str,
    ) -> tuple[Reminder | None, list[Reminder]]:
        """Cancel one exact title match, leaving ambiguous matches unchanged."""
        wanted = title.strip().casefold()
        if not wanted:
            return None, []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM reminders WHERE user_id=? AND status='scheduled' "
                "ORDER BY remind_at",
                (user_id,),
            ).fetchall()
            matches = [
                self._to_reminder(row)
                for row in rows
                if row["title"].strip().casefold() == wanted
            ]
            if len(matches) != 1:
                return None, matches
            reminder = matches[0]
            now = _now()
            cursor = db.execute(
                "UPDATE reminders SET status='cancelled',updated_at=? "
                "WHERE id=? AND user_id=? AND status='scheduled'",
                (now, reminder.id, user_id),
            )
            if cursor.rowcount != 1:
                return None, matches
            db.execute(
                "UPDATE reminder_deliveries SET status='failed',retry_at=NULL,"
                "last_error='cancelled',updated_at=? WHERE reminder_id=?",
                (now, reminder.id),
            )
        return self.get_reminder(user_id, reminder.id), matches

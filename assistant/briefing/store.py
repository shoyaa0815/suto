from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workflows.storage.redaction import redact_text

from .models import DueBriefingDelivery
from .service import TIME_PATTERN


class BriefingStore:
    def configure_external_briefing(
        self,
        user_id: str,
        clock_time: str,
        delivery_target_id: str,
    ) -> str:
        """Atomically bind a daily briefing time to an external delivery target."""
        if not isinstance(clock_time, str) or not TIME_PATTERN.fullmatch(clock_time):
            raise ValueError("briefing time must use 24-hour HH:MM format")
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute(
                "SELECT 1 FROM delivery_targets WHERE id=? AND user_id=?",
                (delivery_target_id, user_id),
            ).fetchone()
            if target is None:
                raise ValueError("delivery target does not belong to the user")
            for key, value in (
                ("briefing_time", clock_time),
                ("briefing_delivery_target_id", delivery_target_id),
            ):
                db.execute(
                    "INSERT INTO user_preferences VALUES (?,?,?,?) "
                    "ON CONFLICT(user_id,key) DO UPDATE SET "
                    "value=excluded.value,updated_at=excluded.updated_at",
                    (user_id, key, value, now),
                )
        return clock_time

    def disable_external_briefing(self, user_id: str) -> bool:
        """Atomically remove an external briefing schedule and its target."""
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM user_preferences WHERE user_id=? AND key IN "
                "('briefing_time','briefing_delivery_target_id')",
                (user_id,),
            )
        return cursor.rowcount > 0

    def claim_briefing_delivery(
        self,
        user_id: str,
        local_date: str,
        *,
        delivered_at: str | None = None,
    ) -> bool:
        """Atomically claim one automatic briefing for a user's local date."""
        instant = delivered_at or datetime.now(UTC).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO briefing_deliveries VALUES (?,?,?)",
                (user_id, local_date, instant),
            )
        return cursor.rowcount == 1

    def claim_due_briefing_deliveries(
        self,
        platform: str,
        *,
        now: str | None = None,
        stale_after_seconds: int = 300,
    ) -> list[DueBriefingDelivery]:
        """Claim due external briefings using the user's current schedule and target."""
        current = datetime.fromisoformat(now) if now else datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        stale_before = current - timedelta(seconds=stale_after_seconds)
        claimed = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            schedules = db.execute(
                "SELECT u.id AS user_id,u.timezone,time.value AS scheduled_time,"
                "t.id AS target_id,t.platform,t.destination_id,t.destination_type,"
                "t.display_name,t.guild_id,t.requester_id "
                "FROM users AS u "
                "JOIN user_preferences AS time ON time.user_id=u.id "
                "AND time.key='briefing_time' "
                "JOIN user_preferences AS target ON target.user_id=u.id "
                "AND target.key='briefing_delivery_target_id' "
                "JOIN delivery_targets AS t ON t.id=target.value AND t.user_id=u.id "
                "WHERE t.platform=?",
                (platform.casefold(),),
            ).fetchall()
            for schedule in schedules:
                clock_time = schedule["scheduled_time"]
                if not TIME_PATTERN.fullmatch(clock_time):
                    continue
                try:
                    local = current.astimezone(ZoneInfo(schedule["timezone"]))
                except ZoneInfoNotFoundError:
                    continue
                hour, minute = (int(part) for part in clock_time.split(":"))
                scheduled = local.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if local < scheduled:
                    continue
                local_date = local.date().isoformat()
                db.execute(
                    "INSERT OR IGNORE INTO external_briefing_deliveries "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        schedule["user_id"],
                        local_date,
                        schedule["target_id"],
                        clock_time,
                        "pending",
                        0,
                        None,
                        None,
                        current.isoformat(),
                    ),
                )
                row = db.execute(
                    "SELECT status,retry_at,updated_at,attempt_count,"
                    "delivery_target_id,scheduled_time "
                    "FROM external_briefing_deliveries "
                    "WHERE user_id=? AND local_date=?",
                    (schedule["user_id"], local_date),
                ).fetchone()
                still_current = (
                    row["delivery_target_id"] == schedule["target_id"]
                    and row["scheduled_time"] == clock_time
                )
                retry_due = row["status"] == "retrying" and (
                    row["retry_at"] is not None
                    and datetime.fromisoformat(row["retry_at"]) <= current
                )
                abandoned = row["status"] == "sending" and (
                    datetime.fromisoformat(row["updated_at"]) <= stale_before
                )
                if not still_current or not (
                    row["status"] == "pending" or retry_due or abandoned
                ):
                    continue
                db.execute(
                    "UPDATE external_briefing_deliveries SET status='sending',"
                    "attempt_count=attempt_count+1,retry_at=NULL,last_error=NULL,"
                    "updated_at=? WHERE user_id=? AND local_date=?",
                    (current.isoformat(), schedule["user_id"], local_date),
                )
                claimed.append(
                    DueBriefingDelivery(
                        user_id=schedule["user_id"],
                        local_date=local_date,
                        scheduled_time=clock_time,
                        target_id=schedule["target_id"],
                        platform=schedule["platform"],
                        destination_id=schedule["destination_id"],
                        destination_type=schedule["destination_type"],
                        display_name=schedule["display_name"],
                        guild_id=schedule["guild_id"],
                        requester_id=schedule["requester_id"],
                        attempt_count=row["attempt_count"] + 1,
                    )
                )
        return claimed

    def briefing_delivery_is_current(
        self,
        delivery: DueBriefingDelivery,
        *,
        now: str | None = None,
    ) -> bool:
        """Revalidate a claimed briefing immediately before its external send."""
        current = datetime.fromisoformat(now) if now else datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        with self._connect() as db:
            row = db.execute(
                "SELECT u.timezone,time.value AS scheduled_time,target.value AS target_id "
                "FROM external_briefing_deliveries AS d "
                "JOIN users AS u ON u.id=d.user_id "
                "JOIN user_preferences AS time ON time.user_id=u.id "
                "AND time.key='briefing_time' "
                "JOIN user_preferences AS target ON target.user_id=u.id "
                "AND target.key='briefing_delivery_target_id' "
                "WHERE d.user_id=? AND d.local_date=? AND d.status='sending'",
                (delivery.user_id, delivery.local_date),
            ).fetchone()
        if row is None or row["scheduled_time"] != delivery.scheduled_time:
            return False
        if row["target_id"] != delivery.target_id:
            return False
        if not TIME_PATTERN.fullmatch(row["scheduled_time"]):
            return False
        try:
            local = current.astimezone(ZoneInfo(row["timezone"]))
        except ZoneInfoNotFoundError:
            return False
        hour, minute = (int(part) for part in row["scheduled_time"].split(":"))
        scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return local.date().isoformat() == delivery.local_date and local >= scheduled

    def complete_briefing_delivery(self, user_id: str, local_date: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE external_briefing_deliveries SET status='delivered',updated_at=? "
                "WHERE user_id=? AND local_date=? AND status='sending'",
                (datetime.now(UTC).isoformat(), user_id, local_date),
            )
        return cursor.rowcount == 1

    def fail_briefing_delivery(
        self,
        user_id: str,
        local_date: str,
        error: str,
        *,
        retry_delay_seconds: int = 30,
        max_attempts: int = 3,
        now: str | None = None,
    ) -> str | None:
        current = datetime.fromisoformat(now) if now else datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        message = redact_text(str(error)).strip()[:1000] or "delivery failed"
        with self._connect() as db:
            row = db.execute(
                "SELECT attempt_count FROM external_briefing_deliveries "
                "WHERE user_id=? AND local_date=? AND status='sending'",
                (user_id, local_date),
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
                "UPDATE external_briefing_deliveries SET status=?,retry_at=?,"
                "last_error=?,updated_at=? WHERE user_id=? AND local_date=? "
                "AND status='sending'",
                (
                    status,
                    retry_at,
                    message,
                    current.isoformat(),
                    user_id,
                    local_date,
                ),
            )
        return status

from datetime import UTC, datetime


class BriefingStore:
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

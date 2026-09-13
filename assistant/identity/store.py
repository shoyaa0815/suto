from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from automation.storage.redaction import redact_text

from .models import ChannelIdentity, User


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown timezone: {value}") from error
    return value


class IdentityStore:
    @staticmethod
    def _to_user(row) -> User | None:
        return User(**dict(row)) if row is not None else None

    @staticmethod
    def _to_channel_identity(row) -> ChannelIdentity | None:
        return ChannelIdentity(**dict(row)) if row is not None else None

    def create_user(
        self,
        display_name: str,
        *,
        timezone: str = "UTC",
        locale: str = "th",
    ) -> User:
        name = redact_text(display_name).strip()
        if not name or len(name) > 100:
            raise ValueError("display name must contain 1-100 characters")
        if not isinstance(locale, str) or not 2 <= len(locale) <= 16:
            raise ValueError("locale must contain 2-16 characters")
        now = _now()
        user_id = f"usr_{uuid4().hex[:12]}"
        with self._connect() as db:
            db.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,?)",
                (user_id, name, _timezone(timezone), locale, now, now),
            )
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> User | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._to_user(row)

    def update_user_timezone(self, user_id: str, timezone: str) -> User | None:
        timezone = _timezone(timezone.strip())
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE users SET timezone=?,updated_at=? WHERE id=?",
                (timezone, _now(), user_id),
            )
        return self.get_user(user_id) if cursor.rowcount else None

    def resolve_channel_identity(
        self,
        channel: str,
        external_id: str,
        *,
        display_name: str = "User",
        timezone: str = "UTC",
        locale: str = "th",
        user_id: str | None = None,
    ) -> User:
        channel = channel.strip().casefold()
        external_id = external_id.strip()
        if not channel or not external_id or len(channel) > 32 or len(external_id) > 255:
            raise ValueError("channel identity is invalid")
        with self._connect() as db:
            row = db.execute(
                "SELECT users.* FROM users JOIN channel_identities AS identities "
                "ON identities.user_id=users.id WHERE identities.channel=? "
                "AND identities.external_id=?",
                (channel, external_id),
            ).fetchone()
        if row is not None:
            return self._to_user(row)
        user = self.get_user(user_id) if user_id else None
        if user_id and user is None:
            raise ValueError(f"user not found: {user_id}")
        user = user or self.create_user(
            display_name,
            timezone=timezone,
            locale=locale,
        )
        identity_id = f"ident_{uuid4().hex[:12]}"
        with self._connect() as db:
            db.execute(
                "INSERT INTO channel_identities VALUES (?,?,?,?,?)",
                (identity_id, user.id, channel, external_id, _now()),
            )
        return user

    def set_user_preference(self, user_id: str, key: str, value: str) -> None:
        if self.get_user(user_id) is None:
            raise ValueError(f"user not found: {user_id}")
        key = key.strip().casefold()
        value = redact_text(value).strip()
        if not key or len(key) > 64 or len(value) > 2000:
            raise ValueError("invalid user preference")
        with self._connect() as db:
            db.execute(
                "INSERT INTO user_preferences VALUES (?,?,?,?) "
                "ON CONFLICT(user_id,key) DO UPDATE SET "
                "value=excluded.value,updated_at=excluded.updated_at",
                (user_id, key, value, _now()),
            )

    def user_preferences(self, user_id: str) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT key,value FROM user_preferences WHERE user_id=? ORDER BY key",
                (user_id,),
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def delete_user_preference(self, user_id: str, key: str) -> bool:
        key = key.strip().casefold()
        if not key:
            raise ValueError("invalid user preference")
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM user_preferences WHERE user_id=? AND key=?",
                (user_id, key),
            )
        return cursor.rowcount == 1

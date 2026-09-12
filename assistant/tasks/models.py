from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    id: str
    user_id: str
    title: str
    notes: str
    status: str
    due_at: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class Reminder:
    id: str
    user_id: str
    title: str
    remind_at: str
    timezone: str
    status: str
    channel_identity_id: str | None
    created_at: str
    updated_at: str

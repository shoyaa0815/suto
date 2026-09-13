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
    delivery_target_id: str | None = None


@dataclass(frozen=True)
class DeliveryTarget:
    id: str
    user_id: str
    platform: str
    destination_id: str
    destination_type: str
    display_name: str
    guild_id: str | None
    requester_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DueReminderDelivery:
    reminder_id: str
    user_id: str
    title: str
    remind_at: str
    target_id: str
    platform: str
    destination_id: str
    destination_type: str
    display_name: str
    guild_id: str | None
    requester_id: str | None
    attempt_count: int

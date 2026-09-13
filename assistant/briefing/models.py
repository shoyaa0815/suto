from dataclasses import dataclass


@dataclass(frozen=True)
class DueBriefingDelivery:
    user_id: str
    local_date: str
    scheduled_time: str
    target_id: str
    platform: str
    destination_id: str
    destination_type: str
    display_name: str
    guild_id: str | None
    requester_id: str | None
    attempt_count: int

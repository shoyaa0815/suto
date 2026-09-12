from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: str
    display_name: str
    timezone: str
    locale: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChannelIdentity:
    id: str
    user_id: str
    channel: str
    external_id: str
    created_at: str

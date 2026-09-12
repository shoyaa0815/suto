from dataclasses import dataclass


@dataclass(frozen=True)
class Conversation:
    id: str
    user_id: str
    channel: str
    external_thread_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Message:
    id: int
    conversation_id: str
    role: str
    content: str
    created_at: str

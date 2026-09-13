from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeliveryTargetContext:
    platform: str
    destination_id: str
    destination_type: str
    display_name: str
    guild_id: str | None = None
    requester_id: str | None = None


@dataclass(frozen=True)
class AssistantContext:
    store: Any
    user_id: str
    conversation_id: str
    default_delivery_target: DeliveryTargetContext | None = None
    current_delivery_target: DeliveryTargetContext | None = None
    available_delivery_targets: tuple[DeliveryTargetContext, ...] = ()

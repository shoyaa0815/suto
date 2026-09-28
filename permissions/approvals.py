"""An exact action presented to an approval provider."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Approval:
    action_type: str
    action: dict[str, Any]
    summary: str
    preview: str

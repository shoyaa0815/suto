"""Result of evaluating an action against a permission policy."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""

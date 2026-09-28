"""Action rules shared by agent and job permissions."""

from dataclasses import dataclass, field
from typing import Mapping

from .models import PermissionDecision


@dataclass(frozen=True)
class PermissionPolicy:
    rules: Mapping[str, str] = field(default_factory=dict)
    default: str = "deny"

    def decide(self, action: str) -> PermissionDecision:
        rule = self.rules.get(action, self.default)
        if rule == "allow":
            return PermissionDecision(True)
        if rule == "require_approval":
            return PermissionDecision(False, True, f"approval required for {action} action")
        if rule == "deny":
            return PermissionDecision(False, False, f"{action} actions are not allowed")
        # Unknown policy values must never turn into an implicit grant.
        return PermissionDecision(False, False, f"invalid permission policy for {action}")

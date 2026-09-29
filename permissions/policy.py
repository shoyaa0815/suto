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


@dataclass(frozen=True)
class IntersectionPolicy:
    """Allow only when every constituent policy explicitly allows an action."""

    policies: tuple[object, ...]

    def decide(self, action: str) -> PermissionDecision:
        if not self.policies:
            return PermissionDecision(False, reason="permission policy is unavailable")
        for policy in self.policies:
            try:
                decision = policy.decide(action)
            except Exception:
                return PermissionDecision(False, reason="permission decision failed")
            if (not isinstance(decision, PermissionDecision) or
                decision.allowed is not True or decision.requires_confirmation is not False):
                return PermissionDecision(False, reason="action is not allowed by inherited policy")
        return PermissionDecision(True)

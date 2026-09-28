"""Central permission decision and approval enforcement."""

from collections.abc import Callable

from .approvals import Approval
from .models import PermissionDecision
from .policy import PermissionPolicy


class PermissionEngine:
    def __init__(self, policy: PermissionPolicy) -> None:
        self.policy = policy

    def decide(self, action: str) -> PermissionDecision:
        return self.policy.decide(action)

    def require(
        self,
        action: str,
        *,
        approval: Approval | None = None,
        approval_callback: Callable[[Approval], bool | None] | None = None,
    ) -> None:
        decision = self.decide(action)
        if decision.allowed:
            return
        if not decision.requires_confirmation:
            raise PermissionError(decision.reason)
        if approval is None or approval.action_type != action or approval_callback is None:
            raise PermissionError(f"approval is unavailable for {action} action")
        if approval_callback(approval) is False:
            raise PermissionError(f"approval denied for {action} action")

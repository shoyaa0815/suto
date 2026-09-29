"""Central permission decision and approval enforcement."""

from collections.abc import Callable

from .approvals import Approval
from .models import PermissionDecision
from .policy import PermissionPolicy


class PermissionEngine:
    def __init__(self, policy: PermissionPolicy | None) -> None:
        self.policy = policy

    def decide(self, action: str) -> PermissionDecision:
        if self.policy is None:
            return PermissionDecision(False, reason="permission policy is unavailable")
        try:
            decision = self.policy.decide(action)
        except Exception:
            return PermissionDecision(False, reason="permission decision failed")
        if (
            not isinstance(decision, PermissionDecision)
            or type(decision.allowed) is not bool
            or type(decision.requires_confirmation) is not bool
            or not isinstance(decision.reason, str)
            or (decision.allowed and decision.requires_confirmation)
        ):
            return PermissionDecision(False, reason="invalid permission decision")
        return decision

    def require(
        self,
        action: str,
        *,
        approval: Approval | None = None,
        approval_callback: Callable[[Approval], bool | None] | None = None,
    ) -> None:
        decision = self.decide(action)
        if decision.allowed is True and decision.requires_confirmation is False:
            return
        if not decision.requires_confirmation:
            raise PermissionError(decision.reason)
        if approval is None or approval.action_type != action or approval_callback is None:
            raise PermissionError(f"approval is unavailable for {action} action")
        if approval_callback(approval) is not True:
            raise PermissionError(f"approval denied for {action} action")

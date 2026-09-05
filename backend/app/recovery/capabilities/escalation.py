"""Internal handoff capability for the automated recovery boundary."""

from __future__ import annotations

from app.recovery.capabilities.models import (
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    RecoveryCapability,
)


class RecoveryEscalationCapability(RecoveryCapability):
    """Create an auditable internal merchant-follow-up handoff."""

    @property
    def capability_id(self) -> str:
        return "recovery_escalation"

    @property
    def action_type(self) -> str:
        return "escalate_recovery"

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        attempted = context.decision_context.get("attempted_capabilities", [])
        bounded_attempts = sorted(
            capability_id
            for capability_id in attempted
            if capability_id in {"payment_link_recovery", "invoice_recovery"}
        )
        return ExecutionResult(
            case_id=context.case_id,
            decision_id=context.decision_id,
            capability_id=self.capability_id,
            action_type=self.action_type,
            status=ExecutionStatus.RECOVERY_ESCALATED,
            provider="internal",
            metadata={
                "attempted_capabilities": bounded_attempts,
                "escalation_reason": "automated_recovery_boundary_reached",
                "next_action": "merchant_follow_up",
            },
        )

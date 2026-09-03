"""Capability executor — orchestrates policy → registry → execution.

This is the service boundary between the agent decision and the
actual recovery action:

    AgentDecision + RecoveryCase
        → Policy Engine (ALLOW / BLOCK)
        → Capability Registry (resolve capability)
        → Capability.execute(context)
        → ExecutionResult

The executor NEVER bypasses the policy layer.  The agent decides WHAT
to do.  The capability executes HOW to do it.  The policy decides
WHETHER it is allowed.

This module lives in recovery/capabilities/ — NOT inside the agent.
"""

from __future__ import annotations

import logging

from app.policy.engine import PolicyEngine
from app.policy.models import PolicyVerdict
from app.recovery.agent.models import AgentDecision
from app.recovery.capabilities.models import (
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
)
from app.recovery.capabilities.registry import CapabilityRegistry
from app.recovery.models import RecoveryCase

logger = logging.getLogger(__name__)


class CapabilityExecutor:
    """Orchestrates the Policy → Registry → Execution pipeline.

    Stateless service — each call to ``execute()`` runs the full
    pipeline and produces an ExecutionResult.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy_engine: PolicyEngine,
    ) -> None:
        self._registry = registry
        self._policy = policy_engine

    def execute(
        self,
        decision: AgentDecision,
        case: RecoveryCase,
    ) -> ExecutionResult:
        """Execute a recovery capability based on an agent decision.

        Flow:
            1. Evaluate policy (ALLOW / BLOCK)
            2. Resolve capability from registry
            3. Build execution context
            4. Execute capability
            5. Return structured result

        Every failure path returns a structured ExecutionResult — the
        recovery pipeline never crashes due to execution issues.
        """
        # 1. Policy check.
        policy_decision = self._policy.evaluate(decision, case)

        if policy_decision.verdict == PolicyVerdict.BLOCK:
            logger.warning(
                "capability_execution_blocked",
                extra={
                    "case_id": case.case_id,
                    "decision_id": decision.decision_id,
                    "capability_id": decision.selected_capability_id,
                    "block_reasons": policy_decision.reasons,
                },
            )
            return ExecutionResult(
                case_id=case.case_id,
                decision_id=decision.decision_id,
                capability_id=decision.selected_capability_id,
                action_type=decision.selected_action_type.value,
                status=ExecutionStatus.BLOCKED,
                policy_decision=policy_decision,
                error_message=(
                    f"Policy blocked: {'; '.join(policy_decision.reasons)}"
                ),
            )

        # 2. Resolve capability.
        capability = self._registry.get(decision.selected_capability_id)

        if capability is None:
            # This should not happen if the policy checked registration,
            # but we handle it defensively.
            logger.error(
                "capability_not_found",
                extra={
                    "case_id": case.case_id,
                    "decision_id": decision.decision_id,
                    "capability_id": decision.selected_capability_id,
                },
            )
            return ExecutionResult(
                case_id=case.case_id,
                decision_id=decision.decision_id,
                capability_id=decision.selected_capability_id,
                action_type=decision.selected_action_type.value,
                status=ExecutionStatus.FAILED,
                policy_decision=policy_decision,
                error_message=(
                    f"Capability '{decision.selected_capability_id}' not found in registry."
                ),
            )

        # 3. Build execution context.
        context = ExecutionContext(
            case_id=case.case_id,
            decision_id=decision.decision_id,
            merchant_id=case.merchant_id,
            customer_id=case.customer_id,
            amount_minor=case.amount_at_risk_minor,
            currency=case.currency,
            capability_id=decision.selected_capability_id,
            action_type=decision.selected_action_type.value,
            signal_id=case.signal_id,
            reason_codes=case.reason_codes,
            decision_context=decision.decision_context,
        )

        logger.info(
            "capability_resolved",
            extra={
                "case_id": case.case_id,
                "decision_id": decision.decision_id,
                "capability_id": decision.selected_capability_id,
                "action_type": decision.selected_action_type.value,
            },
        )

        # 4. Execute capability.
        try:
            result = capability.execute(context)
        except Exception as exc:
            # Defensive: capabilities should NOT raise, but if they do
            # the pipeline must not crash.
            logger.exception(
                "capability_execution_unexpected_error",
                extra={
                    "case_id": case.case_id,
                    "decision_id": decision.decision_id,
                    "capability_id": decision.selected_capability_id,
                    "error": str(exc),
                },
            )
            result = ExecutionResult(
                case_id=case.case_id,
                decision_id=decision.decision_id,
                capability_id=decision.selected_capability_id,
                action_type=decision.selected_action_type.value,
                status=ExecutionStatus.FAILED,
                error_message=f"Unexpected execution error: {exc}",
            )

        return result.model_copy(update={"policy_decision": policy_decision})

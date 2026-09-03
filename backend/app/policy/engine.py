"""Policy / Guardrails Engine — deterministic policy boundary.

This is the mandatory boundary between the agent's decision and
capability execution:

    AgentDecision  →  PolicyEngine.evaluate()  →  PolicyDecision

The agent must NEVER directly call Razorpay APIs or any other
external service.  Every execution must be explicitly ALLOWED by
the policy engine.

MVP rules are deliberately conservative.  The stopping-rule concept
is present so that future blocks can add:

- maximum attempt count
- cooldown periods
- maximum recovery exposure
- already-recovered case checks
- merchant-specific restrictions
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.policy.models import PolicyDecision, PolicyVerdict
from app.recovery.models import RecoveryCase, RiskStatus

if TYPE_CHECKING:
    from app.recovery.agent.models import AgentDecision

logger = logging.getLogger(__name__)


class PolicyEngine:
    """Deterministic, conservative policy engine.

    Evaluates whether an AgentDecision should be executed or blocked.
    The implementation intentionally avoids LLM involvement — policy
    decisions are deterministic and auditable.
    """

    def __init__(self, registered_capability_ids: frozenset[str]) -> None:
        """Initialise with the set of known/registered capability IDs.

        Args:
            registered_capability_ids: The capability IDs available in the
                capability registry.  A decision referencing an unregistered
                capability will be blocked.
        """
        self._registered = registered_capability_ids

    def evaluate(
        self,
        decision: AgentDecision,
        case: RecoveryCase,
    ) -> PolicyDecision:
        """Evaluate an agent decision against policy rules.

        Returns a PolicyDecision with verdict ALLOW or BLOCK.  Every
        blocking rule appends a reason so the outcome is auditable.
        """
        block_reasons: list[str] = []

        # --- Core eligibility checks ---

        # 1. Case must be AT_RISK.
        if case.risk_status != RiskStatus.AT_RISK:
            block_reasons.append(
                f"Case risk_status is {case.risk_status.value}; expected at_risk."
            )

        # 2. Amount must be positive.
        if case.amount_at_risk_minor <= 0:
            block_reasons.append(
                f"amount_at_risk_minor={case.amount_at_risk_minor}; must be positive."
            )

        # 3. Merchant ID must be present.
        if not case.merchant_id:
            block_reasons.append("Missing merchant_id on recovery case.")

        # 4. Currency must be present.
        if not case.currency:
            block_reasons.append("Missing currency on recovery case.")

        # 5. Capability must be registered.
        if decision.selected_capability_id not in self._registered:
            block_reasons.append(
                f"Capability '{decision.selected_capability_id}' is not registered."
            )

        # --- Stopping-rule boundary ---
        # These are architectural placeholders for future enforcement.
        # Each returns early with a block reason when the condition is met.

        self._check_stopping_rules(decision, case, block_reasons)

        # --- Final verdict ---
        verdict = PolicyVerdict.BLOCK if block_reasons else PolicyVerdict.ALLOW

        policy_decision = PolicyDecision(
            verdict=verdict,
            case_id=case.case_id,
            decision_id=decision.decision_id,
            capability_id=decision.selected_capability_id,
            reasons=block_reasons if block_reasons else ["All policy checks passed."],
        )

        logger.info(
            "policy_decision",
            extra={
                "case_id": case.case_id,
                "decision_id": decision.decision_id,
                "capability_id": decision.selected_capability_id,
                "verdict": verdict.value,
                "reasons": policy_decision.reasons,
            },
        )

        return policy_decision

    def _check_stopping_rules(
        self,
        decision: AgentDecision,
        case: RecoveryCase,
        block_reasons: list[str],
    ) -> None:
        """Evaluate stopping rules.

        Currently a minimal implementation.  Future blocks will add:
        - Maximum attempt count per case
        - Cooldown period between attempts
        - Maximum recovery exposure (total amount across active cases)
        - Already-recovered case detection
        - Merchant-specific restrictions / blocklist
        """
        # Placeholder: no stopping rules are currently active.
        # The architecture is in place for the next blocks.
        pass

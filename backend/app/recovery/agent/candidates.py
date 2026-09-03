"""Candidate action generator.

Generates a list of CandidateAction objects from the AgentContext and Diagnosis.

The generator is modular: additional capabilities can be added by writing a new
generator function and registering it in CANDIDATE_GENERATORS.

Current capabilities:
- payment_link_recovery (create_payment_link)

Only capabilities that can be honestly represented are included.
No pretend Razorpay capabilities.
"""

from __future__ import annotations

import logging
from typing import Callable

from app.recovery.agent.models import (
    ActionType,
    AgentContext,
    CandidateAction,
    Diagnosis,
    DiagnosisCategory,
    EligibilityStatus,
)

logger = logging.getLogger(__name__)

# Type alias for a candidate generator function.
CandidateGenerator = Callable[[AgentContext, Diagnosis], CandidateAction | None]


def _payment_link_recovery(
    context: AgentContext,
    diagnosis: Diagnosis,
) -> CandidateAction | None:
    """Generate a payment-link-recovery candidate for payment failures.

    Eligible when:
    - Diagnosis category is PAYMENT_FAILURE
    - Amount is positive
    """
    if diagnosis.category != DiagnosisCategory.PAYMENT_FAILURE:
        return None

    if context.amount_at_risk_minor <= 0:
        return None

    return CandidateAction(
        capability_id="payment_link_recovery",
        action_type=ActionType.CREATE_PAYMENT_LINK,
        priority=1,
        rationale=(
            f"Payment of {context.amount_at_risk_minor} {context.currency} "
            f"failed due to {diagnosis.primary_reason}. "
            f"A payment link allows the customer to retry via an alternative method."
        ),
        eligibility=EligibilityStatus.ELIGIBLE,
    )


# Registry of candidate generators.  Add new generators here to extend
# the agent's action space.
CANDIDATE_GENERATORS: list[CandidateGenerator] = [
    _payment_link_recovery,
]


def generate_candidates(
    context: AgentContext,
    diagnosis: Diagnosis,
) -> list[CandidateAction]:
    """Run all registered candidate generators and return eligible candidates.

    Returns an ordered list of CandidateAction objects sorted by priority.
    """
    candidates: list[CandidateAction] = []

    for generator in CANDIDATE_GENERATORS:
        candidate = generator(context, diagnosis)
        if candidate is not None:
            candidates.append(candidate)

    # Sort by priority (lower = higher priority).
    candidates.sort(key=lambda c: c.priority)

    logger.info(
        "agent_candidates_generated",
        extra={
            "case_id": context.case_id,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "capability_id": c.capability_id,
                    "action_type": c.action_type.value,
                    "priority": c.priority,
                    "eligibility": c.eligibility.value,
                }
                for c in candidates
            ],
        },
    )

    return candidates

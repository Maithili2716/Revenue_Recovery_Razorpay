"""Candidate action generator.

Generates a list of CandidateAction objects from the AgentContext and Diagnosis.

The generator is modular: additional capabilities can be added by writing a new
generator function and registering it in CANDIDATE_GENERATORS.

Current capabilities:
- payment_link_recovery (create_payment_link)
- payment_link_reminder (send_payment_link_reminder) — only when an existing
  pending Payment Link is associated with the case.

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


def _payment_link_reminder(
    context: AgentContext,
    diagnosis: Diagnosis,
    pending_payment_link_id: str | None,
) -> CandidateAction | None:
    """Generate a payment-link-reminder candidate.

    Eligible ONLY when:
    - There is an existing pending (unresolved) Payment Link for this case
      (i.e. pending_payment_link_id is not None).
    - Diagnosis category is PAYMENT_FAILURE.

    If there is no existing Payment Link, the reminder is INELIGIBLE.
    The bandit should never receive an ineligible reminder candidate.
    """
    if not pending_payment_link_id:
        logger.info(
            "payment_link_reminder_candidate_evaluated",
            extra={
                "case_id": context.case_id,
                "merchant_id": context.merchant_id,
                "payment_link_id": None,
                "eligible": False,
                "reason": "no_existing_pending_payment_link",
            },
        )
        return None

    if diagnosis.category != DiagnosisCategory.PAYMENT_FAILURE:
        logger.info(
            "payment_link_reminder_candidate_evaluated",
            extra={
                "case_id": context.case_id,
                "merchant_id": context.merchant_id,
                "payment_link_id": pending_payment_link_id,
                "eligible": False,
                "reason": f"diagnosis_category_is_{diagnosis.category.value}_not_payment_failure",
            },
        )
        return None

    logger.info(
        "payment_link_reminder_candidate_evaluated",
        extra={
            "case_id": context.case_id,
            "merchant_id": context.merchant_id,
            "payment_link_id": pending_payment_link_id,
            "eligible": True,
            "reason": "existing_pending_payment_link",
        },
    )

    return CandidateAction(
        capability_id="payment_link_reminder",
        action_type=ActionType.SEND_PAYMENT_LINK_REMINDER,
        priority=2,
        rationale=(
            f"Existing Payment Link {pending_payment_link_id} is pending. "
            f"Sending a reminder notification to the customer may prompt payment."
        ),
        eligibility=EligibilityStatus.ELIGIBLE,
    )


def generate_candidates(
    context: AgentContext,
    diagnosis: Diagnosis,
) -> list[CandidateAction]:
    """Run all registered candidate generators and return eligible candidates.

    Returns an ordered list of CandidateAction objects sorted by priority.

    NOTE: This function does NOT include reminder candidates because it has
    no knowledge of pending payment links. Use generate_candidates_with_context
    for the full candidate list including reminders.
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


def generate_candidates_with_context(
    context: AgentContext,
    diagnosis: Diagnosis,
    *,
    pending_payment_link_id: str | None = None,
) -> list[CandidateAction]:
    """Generate candidates including context-dependent ones like reminders.

    Args:
        context: Agent context for the case.
        diagnosis: Diagnosis of the case.
        pending_payment_link_id: If an existing pending Payment Link exists
            for this case, pass its ID here. Otherwise None.

    Returns:
        Ordered list of CandidateAction objects sorted by priority.
    """
    # Start with the standard generators.
    candidates = generate_candidates(context, diagnosis)

    # Add the reminder candidate if there is an existing pending payment link.
    reminder = _payment_link_reminder(context, diagnosis, pending_payment_link_id)
    if reminder is not None:
        candidates.append(reminder)

    # Re-sort after adding the reminder.
    candidates.sort(key=lambda c: c.priority)

    if reminder is not None:
        logger.info(
            "agent_reminder_candidate_added",
            extra={
                "case_id": context.case_id,
                "pending_payment_link_id": pending_payment_link_id,
                "total_candidates": len(candidates),
            },
        )

    return candidates


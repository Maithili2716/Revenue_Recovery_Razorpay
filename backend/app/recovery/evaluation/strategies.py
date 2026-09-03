"""Evaluation-only strategy adapter backed by the production bandit interface.

These candidates are deliberately not registered as live capabilities.  They
exist only to let the held-out simulator compare contextual strategy selection
without widening the production payment-failure action space.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.recovery.agent.bandit import BanditSelection, ContextualBandit
from app.recovery.agent.context import build_agent_context
from app.recovery.agent.models import EligibilityStatus
from app.recovery.evaluation.models import EvaluationCase


class EvaluationActionType(str, Enum):
    CREATE_PAYMENT_LINK = "create_payment_link"
    SEND_PAYMENT_LINK_REMINDER = "send_payment_link_reminder"
    INVOICE_RECOVERY = "invoice_recovery"


@dataclass(frozen=True)
class EvaluationCandidate:
    """Duck-typed candidate accepted by ``ContextualBandit.select``.

    Keeping this model in evaluation avoids modifying the production
    ``ActionType`` enum or live candidate registry for a simulated strategy.
    """

    capability_id: str
    action_type: EvaluationActionType
    priority: int
    rationale: str
    eligibility: EligibilityStatus


@dataclass(frozen=True)
class EvaluationDecision:
    """Minimal local decision shape for the existing policy check."""

    decision_id: str
    selected_capability_id: str
    selected_action_type: EvaluationActionType


def evaluation_candidates(case: EvaluationCase) -> list[EvaluationCandidate]:
    """Return all evaluation strategies with deterministic context eligibility."""
    context = case.recovery_context
    has_pending_link = case.pending_payment_link_id is not None
    return [
        EvaluationCandidate(
            capability_id="payment_link_recovery",
            action_type=EvaluationActionType.CREATE_PAYMENT_LINK,
            priority=1 if context == "new_payment_failure" else 2,
            rationale="Evaluation-only payment-link recovery candidate.",
            eligibility=(
                EligibilityStatus.ELIGIBLE
                if context in {"new_payment_failure", "existing_payment_link"}
                else EligibilityStatus.INELIGIBLE
            ),
        ),
        EvaluationCandidate(
            capability_id="payment_link_reminder",
            action_type=EvaluationActionType.SEND_PAYMENT_LINK_REMINDER,
            priority=1,
            rationale="Evaluation-only reminder candidate for an unpaid link.",
            eligibility=(
                EligibilityStatus.ELIGIBLE
                if context == "existing_payment_link" and has_pending_link
                else EligibilityStatus.INELIGIBLE
            ),
        ),
        EvaluationCandidate(
            capability_id="invoice_recovery",
            action_type=EvaluationActionType.INVOICE_RECOVERY,
            priority=1,
            rationale="Evaluation-only invoice/receivable recovery candidate.",
            eligibility=(
                EligibilityStatus.ELIGIBLE
                if context == "overdue_invoice"
                else EligibilityStatus.INELIGIBLE
            ),
        ),
    ]


def select_evaluation_strategy(
    case: EvaluationCase,
    bandit: ContextualBandit,
) -> tuple[BanditSelection | None, EvaluationDecision | None]:
    """Select an eligible evaluation strategy via the existing bandit."""
    context = build_agent_context(case.signal, case.recovery_case)
    selection = bandit.select(evaluation_candidates(case), context)
    if selection is None:
        return None, None
    return selection, EvaluationDecision(
        decision_id=f"eval_decision_{case.recovery_case.case_id}",
        selected_capability_id=selection.selected.capability_id,
        selected_action_type=selection.selected.action_type,
    )

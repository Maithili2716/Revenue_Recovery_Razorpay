"""Agent context builder.

Builds an AgentContext from a RevenueSignal and RecoveryCase.  This is the
first step of the agent decision pipeline.

Does NOT fabricate customer identity or historical information.  If data is
unavailable, it is represented as None or empty.
"""

from __future__ import annotations

import logging

from app.recovery.agent.models import AgentContext
from app.recovery.models import RecoveryCase
from app.signals.models import RevenueSignal

logger = logging.getLogger(__name__)


def build_agent_context(
    signal: RevenueSignal,
    case: RecoveryCase,
) -> AgentContext:
    """Assemble an AgentContext from the signal and case.

    Extracts payment method from signal metadata if available.
    Previous attempts are explicitly empty — no fabrication.
    """
    # Extract payment method from signal metadata if present.
    payment_method: str | None = signal.metadata.get("method") if signal.metadata else None

    context = AgentContext(
        case_id=case.case_id,
        signal_id=signal.signal_id,
        merchant_id=signal.merchant_id,
        customer_id=signal.customer_id,
        amount_at_risk_minor=signal.amount_minor,
        currency=signal.currency,
        signal_type=signal.signal_type.value,
        failure_reason=signal.reason,
        failure_source=signal.failure_source,
        failure_step=signal.failure_step,
        payment_method=payment_method,
        previous_attempts=[],  # Explicitly empty — no history available yet.
        signal_occurred_at=signal.occurred_at,
        recoverability=case.recoverability.value,
        urgency=case.urgency.value,
        reason_codes=case.reason_codes,
    )

    logger.info(
        "agent_context_built",
        extra={
            "case_id": context.case_id,
            "signal_id": context.signal_id,
            "merchant_id": context.merchant_id,
            "amount_at_risk_minor": context.amount_at_risk_minor,
            "currency": context.currency,
            "signal_type": context.signal_type,
            "failure_source": context.failure_source,
            "failure_step": context.failure_step,
            "recoverability": context.recoverability,
            "urgency": context.urgency,
            "previous_attempts_count": len(context.previous_attempts),
        },
    )

    return context

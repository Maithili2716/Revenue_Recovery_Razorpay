"""Revenue Risk Detector — deterministic rules only.

Converts a RevenueSignal into a RecoveryCase using deterministic, rule-based
logic.  No LLM, no ML, no random behaviour.

Flow:

    RevenueSignal
        -> detect_recovery_case()
        -> RecoveryCase | None

Returning None means the signal is not currently actionable as revenue at risk
(e.g. zero-amount event).  Callers must not treat None as an error.

This detector is intentionally provider-agnostic: it operates on RevenueSignal
fields, never on raw Razorpay payloads.

Rules encoded here are for Block 3B.  Future blocks may extend these rules or
replace them with learned policies; do not modify this file for those purposes.
"""

from __future__ import annotations

import logging

from app.recovery.models import (
    RecoveryCase,
    Recoverability,
    RiskStatus,
    Urgency,
    build_case_id,
)
from app.signals.models import RevenueSignal, SignalType

logger = logging.getLogger(__name__)

# Failure sources known to indicate a condition our current system cannot
# address through automated recovery outreach.  Any source NOT in this set
# defaults to LIKELY (i.e. we believe retry/outreach may help).
_UNRECOVERABLE_SOURCES: frozenset[str] = frozenset(
    {
        # Permanent hard declines from the bank — a retry will not help.
        "issuer",
        # Razorpay gateway-level hard blocks (fraud, limit exceeded, etc.).
        # Note: "gateway" for transient errors is left to LIKELY; only
        # explicit permanent-block categories belong here.
    }
)


def detect_recovery_case(signal: RevenueSignal) -> RecoveryCase | None:
    """Run deterministic risk detection on a normalized RevenueSignal.

    Returns:
        RecoveryCase if the signal represents actionable at-risk revenue.
        None if the signal should not generate a recovery case.
    """
    if signal.signal_type == SignalType.PAYMENT_FAILURE:
        return _assess_payment_failure(signal)

    # Future signal types (subscription.charged, invoice.expired, etc.) will
    # have their own assessment functions added here.
    logger.debug(
        "recovery_case_not_created",
        extra={
            "reason": "unhandled_signal_type",
            "signal_type": signal.signal_type.value,
            "signal_id": signal.signal_id,
        },
    )
    return None


# ---------------------------------------------------------------------------
# Payment failure assessment
# ---------------------------------------------------------------------------


def _assess_payment_failure(signal: RevenueSignal) -> RecoveryCase | None:
    """Apply Block 3B rules for PAYMENT_FAILURE signals."""

    # Rule 1: zero or negative amount → no recovery case.
    if signal.amount_minor <= 0:
        logger.info(
            "recovery_case_not_created",
            extra={
                "reason": "non_positive_amount",
                "signal_id": signal.signal_id,
                "amount_minor": signal.amount_minor,
            },
        )
        return None

    recoverability = _recoverability(signal.failure_source)
    urgency = _urgency(signal.amount_minor)
    reason_codes = _reason_codes(signal.failure_source)

    case = RecoveryCase(
        case_id=build_case_id(signal.signal_id),
        signal_id=signal.signal_id,
        merchant_id=signal.merchant_id,
        customer_id=signal.customer_id,
        amount_at_risk_minor=signal.amount_minor,
        currency=signal.currency,
        risk_status=RiskStatus.AT_RISK,
        recoverability=recoverability,
        urgency=urgency,
        reason_codes=reason_codes,
        created_at=signal.occurred_at,
    )

    logger.info(
        "recovery_case_created",
        extra={
            "case_id": case.case_id,
            "signal_id": case.signal_id,
            "merchant_id": case.merchant_id,
            "amount_at_risk_minor": case.amount_at_risk_minor,
            "currency": case.currency,
            "risk_status": case.risk_status.value,
            "recoverability": case.recoverability.value,
            "urgency": case.urgency.value,
            "reason_codes": case.reason_codes,
        },
    )
    return case


def _recoverability(failure_source: str | None) -> Recoverability:
    """Map failure_source to a Recoverability estimate.

    - None / empty → UNKNOWN  (we cannot assess without a source)
    - known unrecoverable source → LOW
    - any other non-empty source → LIKELY  (retry/outreach may help)
    """
    if not failure_source:
        return Recoverability.UNKNOWN
    if failure_source.lower() in _UNRECOVERABLE_SOURCES:
        return Recoverability.LOW
    return Recoverability.LIKELY


def _urgency(amount_minor: int) -> Urgency:
    """Map amount to urgency tier.

    Thresholds (INR paise, but units are currency-agnostic):
        >= 1_000_000 (₹10,000) → HIGH
        >= 100_000  (₹1,000)   → MEDIUM
        otherwise              → LOW
    """
    if amount_minor >= 1_000_000:
        return Urgency.HIGH
    if amount_minor >= 100_000:
        return Urgency.MEDIUM
    return Urgency.LOW


def _reason_codes(failure_source: str | None) -> list[str]:
    """Build a deterministic, ordered list of machine-readable reason codes."""
    codes: list[str] = ["payment_failed"]
    if failure_source:
        codes.append(f"failure_source:{failure_source}")
    return codes

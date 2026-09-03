"""Canonical RecoveryCase model.

A RecoveryCase is the output of the Revenue Risk Detector (future block).
It represents a single revenue-at-risk situation that the Adaptive Recovery
Agent will act on.

Flow:

    RevenueSignal
        -> [Revenue Risk Detector — not yet implemented]
        -> RecoveryCase
        -> [Adaptive Recovery Agent — not yet implemented]

This module defines the data contract only.  No risk-scoring logic,
business rules, LLM, or recovery actions live here.

All monetary amounts use the currency's minor unit (paise for INR).
No floating-point money.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class RiskStatus(str, Enum):
    """Whether the signal has been assessed as revenue at risk."""

    AT_RISK = "at_risk"
    NOT_AT_RISK = "not_at_risk"


class Recoverability(str, Enum):
    """Estimated likelihood that a recovery action will succeed."""

    UNKNOWN = "unknown"
    LIKELY = "likely"
    LOW = "low"


class Urgency(str, Enum):
    """How urgently the case should be acted on."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecoveryCase(BaseModel):
    """Canonical, provider-agnostic representation of a recovery case.

    Produced by the Revenue Risk Detector from a RevenueSignal.
    Consumed by the Adaptive Recovery Agent.

    Fields are intentionally minimal for Block 3A.  The Risk Detector
    and Agent will extend this model in their respective blocks.
    """

    # --- Internal identifiers ---
    case_id: str = Field(
        description=(
            "Deterministic, content-addressed identifier derived from the "
            "signal_id.  Re-processing the same signal produces the same case_id."
        )
    )
    signal_id: str = Field(
        description="The RevenueSignal that originated this case."
    )

    # --- Merchant and customer ---
    merchant_id: str = Field(
        description="Merchant identifier, propagated from the originating signal."
    )
    customer_id: str | None = Field(
        default=None,
        description="Customer identifier if resolved; None when unavailable.",
    )

    # --- Financial ---
    amount_at_risk_minor: int = Field(
        description=(
            "Amount at risk in the currency's minor unit (paise for INR). "
            "Propagated from the originating signal; not adjusted until "
            "partial recovery is verified."
        )
    )
    currency: str = Field(description="ISO 4217 currency code, e.g. 'INR'.")

    # --- Risk assessment ---
    risk_status: RiskStatus
    recoverability: Recoverability
    urgency: Urgency

    # --- Structured reason codes for downstream routing ---
    reason_codes: list[str] = Field(
        default_factory=list,
        description=(
            "Machine-readable codes that describe why this case was raised "
            "(e.g. 'insufficient_funds', 'card_declined').  Empty list is "
            "valid when no codes are available."
        ),
    )

    # --- Timing ---
    created_at: datetime = Field(
        description="When this RecoveryCase was created, in UTC."
    )


def build_case_id(signal_id: str) -> str:
    """Return a deterministic RecoveryCase identifier derived from signal_id.

    Using a hash of the signal_id means the same signal always produces the
    same case_id, enabling idempotent risk detection without a database lookup.

    The ``case_`` prefix distinguishes case IDs from signal IDs (``sig_``)
    and other internal identifiers.
    """
    return "case_" + hashlib.sha256(signal_id.encode()).hexdigest()[:24]

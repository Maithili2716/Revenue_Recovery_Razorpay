"""Verification models.

Defines:
- VerificationStatus — RECOVERED / NOT_RECOVERED / PENDING / UNKNOWN
- VerifiedOutcome — the structured verification result

CRITICAL:
    PENDING is NOT failure — the customer has not yet acted.
    UNKNOWN is NOT failure — the provider may be temporarily unavailable.
    Only RECOVERED means money was actually received.
    Only NOT_RECOVERED (expired/cancelled) means terminal failure.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    """Financial verification status.

    RECOVERED:     Provider confirms payment was captured.
    PENDING:       Payment link exists, customer has not yet acted.
                   Outcome is not yet known. Do NOT update learning.
    NOT_RECOVERED: Provider confirms terminal failure (expired/cancelled).
    UNKNOWN:       Cannot safely determine — API failure, timeout, etc.
    """

    RECOVERED = "recovered"
    PENDING = "pending"
    NOT_RECOVERED = "not_recovered"
    UNKNOWN = "unknown"


def _generate_outcome_id() -> str:
    return "vout_" + uuid.uuid4().hex[:24]


class VerifiedOutcome(BaseModel):
    """Structured result of independent verification.

    Contains the financial truth about a recovery action.
    This is the ONLY model that can claim money was recovered.
    """

    outcome_id: str = Field(default_factory=_generate_outcome_id)
    case_id: str
    execution_id: str
    capability_id: str

    provider: str = "razorpay"
    provider_reference: str | None = None

    status: VerificationStatus

    # Financial
    amount_at_risk_minor: int = Field(
        description="Original amount at risk (minor units)."
    )
    amount_recovered_minor: int = Field(
        default=0,
        description=(
            "Verified recovered amount (minor units). "
            "Only > 0 when status is RECOVERED. "
            "Never exceeds amount_at_risk_minor."
        ),
    )
    currency: str

    # Provider payment details (when payment was captured)
    provider_payment_id: str | None = Field(
        default=None,
        description="The payment ID from the provider (e.g. pay_...) if captured.",
    )

    verification_source: str = Field(
        default="razorpay_payment_link_api",
        description="Which verification method/API was used.",
    )
    verified_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Sanitized evidence from the provider (no secrets, no full payloads)
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Sanitized provider evidence. Contains only useful fields "
            "like payment_link_status, amount_paid, payment_id. "
            "Never contains API keys or full raw payloads."
        ),
    )

    reason: str | None = Field(
        default=None,
        description="Human-readable reason for the verification status.",
    )

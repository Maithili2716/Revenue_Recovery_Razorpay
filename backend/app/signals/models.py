"""Canonical internal Revenue Signal model.

A RevenueSignal is the normalized, provider-agnostic representation of a
revenue-relevant event.  It is produced by a normalizer from a raw provider
webhook event and consumed by the Revenue Risk Detector.

All monetary amounts are stored in the currency's minor unit (e.g. paise for
INR).  Conversion to major units happens only at presentation time.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    """The category of revenue signal."""

    PAYMENT_FAILURE = "payment_failure"


class SignalStatus(str, Enum):
    """The lifecycle status of the signal."""

    FAILED = "failed"


class RevenueSignal(BaseModel):
    """Canonical, provider-agnostic representation of a revenue signal.

    Fields are kept minimal for the current block (payment.failed only).
    Subscription / invoice normalization will add fields in the corresponding
    block rather than speculatively here.
    """

    # --- Our internal identifiers ---
    signal_id: str = Field(
        description=(
            "Deterministic, content-addressed identifier derived from the "
            "provider and provider_event_id so that re-delivery of the same "
            "raw event always produces the same signal_id."
        )
    )
    merchant_id: str = Field(description="Razorpay account ID of the merchant.")
    customer_id: str | None = Field(
        default=None,
        description="Customer contact/email if available in the event payload.",
    )

    # --- Classification ---
    signal_type: SignalType
    status: SignalStatus

    # --- Financial attributes ---
    amount_minor: int = Field(
        description="Amount in the currency's minor unit (paise for INR)."
    )
    currency: str = Field(description="ISO 4217 currency code, e.g. 'INR'.")

    # --- Provider-level provenance ---
    provider: str = Field(
        description="Integration provider name, e.g. 'razorpay'."
    )
    provider_event_id: str = Field(
        description="Unique event identifier from the provider."
    )
    provider_entity_id: str = Field(
        description="Identifier of the primary entity in the event (e.g. payment ID)."
    )

    # --- Failure context (nullable; set when applicable) ---
    reason: str | None = Field(
        default=None,
        description="Human-readable failure reason from the provider.",
    )
    failure_source: str | None = Field(
        default=None,
        description="Who/what caused the failure (e.g. 'customer', 'bank').",
    )
    failure_step: str | None = Field(
        default=None,
        description="At which payment step the failure occurred.",
    )

    # --- Timing ---
    occurred_at: datetime = Field(
        description=(
            "When the underlying event occurred, expressed as UTC datetime. "
            "Derived from the provider entity's created_at timestamp."
        )
    )

    # --- Original event ---
    raw_event_type: str = Field(
        description="The original event type string from the provider, e.g. 'payment.failed'."
    )

    # --- Extensible bag for provider-specific context ---
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arbitrary key-value context preserved from the provider payload. "
            "Downstream components should prefer the typed fields above and "
            "treat metadata as supplementary read-only context."
        ),
    )


def build_signal_id(provider: str, provider_event_id: str) -> str:
    """Return a deterministic signal identifier.

    Using a hash of ``provider:provider_event_id`` ensures that re-delivery of
    the same raw event always maps to the same internal signal without needing
    a database lookup.
    """
    raw = f"{provider}:{provider_event_id}"
    return "sig_" + hashlib.sha256(raw.encode()).hexdigest()[:24]

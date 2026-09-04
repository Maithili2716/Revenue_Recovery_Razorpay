"""Normalizer for Razorpay payment.failed webhook events.

Converts a verified RazorpayWebhookEvent into a canonical RevenueSignal.

Only payment.failed is supported.  Any other event type passed to this module
is a caller error and will raise UnsupportedEventType.

Razorpay payload structure (as observed in real Test Mode captures):

    {
        "entity": "event",
        "event": "payment.failed",
        "id": "<event-id>",          # top-level event ID
        "account_id": "<account-id>",
        "payload": {
            "payment": {
                "entity": {
                    "id": "<payment-id>",
                    "entity": "payment",
                    "amount": <int paise>,
                    "currency": "INR",
                    "status": "failed",
                    "contact": "<phone-or-null>",
                    "email": "<email-or-null>",
                    "error_code": "<code-or-null>",
                    "error_description": "<human-readable-or-null>",
                    "error_source": "<source-or-null>",
                    "error_step": "<step-or-null>",
                    "error_reason": "<reason-or-null>",
                    "created_at": <unix-timestamp-int>
                }
            }
        }
    }

Fields that are absent or null in the observed payload are mapped to None on
the RevenueSignal.  We do NOT invent fields that were not observed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.integrations.razorpay.events import RazorpayWebhookEvent
from app.signals.models import (
    RevenueSignal,
    SignalStatus,
    SignalType,
    build_signal_id,
)

SUPPORTED_EVENT_TYPE = "payment.failed"
PROVIDER_NAME = "razorpay"


class UnsupportedEventType(Exception):
    """Raised when a payment event type is not supported by this normalizer."""


def normalize_payment_failed(event: RazorpayWebhookEvent) -> RevenueSignal:
    """Convert a payment.failed RazorpayWebhookEvent into a RevenueSignal.

    Raises:
        UnsupportedEventType: If event.event_type is not 'payment.failed'.
        ValueError: If the payload is missing required fields (payment entity
            id, amount, or currency) that cannot be defaulted to None.
    """
    if event.event_type != SUPPORTED_EVENT_TYPE:
        raise UnsupportedEventType(
            f"Payment normalizer only handles '{SUPPORTED_EVENT_TYPE}'; "
            f"received '{event.event_type}'"
        )

    payment_entity = _extract_payment_entity(event.payload)

    provider_event_id = _extract_provider_event_id(event)
    provider_entity_id = _require_str(payment_entity, "id", "payment.id")
    merchant_id = _extract_merchant_id(event.payload)
    amount_minor = _require_int(payment_entity, "amount", "payment.amount")
    currency = _require_str(payment_entity, "currency", "payment.currency")
    occurred_at = _extract_occurred_at(payment_entity)

    # Razorpay's payment entity carries contact (phone) and email, not a
    # structured customer ID.  We do not map PII contact fields to customer_id.
    # A future enrichment step may resolve a customer ID from a CRM lookup.
    customer_id = None
    reason = _optional_str(payment_entity, "error_description")
    failure_source = _optional_str(payment_entity, "error_source")
    failure_step = _optional_str(payment_entity, "error_step")

    metadata = _build_metadata(payment_entity)

    signal_id = build_signal_id(PROVIDER_NAME, provider_event_id)

    return RevenueSignal(
        signal_id=signal_id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        signal_type=SignalType.PAYMENT_FAILURE,
        status=SignalStatus.FAILED,
        amount_minor=amount_minor,
        currency=currency,
        provider=PROVIDER_NAME,
        provider_event_id=provider_event_id,
        provider_entity_id=provider_entity_id,
        reason=reason,
        failure_source=failure_source,
        failure_step=failure_step,
        occurred_at=occurred_at,
        raw_event_type=SUPPORTED_EVENT_TYPE,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Private extraction helpers
# ---------------------------------------------------------------------------


def _extract_payment_entity(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the payment entity dict from the Razorpay payload.

    Raises ValueError if the expected structure is absent.
    """
    nested = payload.get("payload")
    if not isinstance(nested, dict):
        raise ValueError("Razorpay payload missing 'payload' key")

    payment_wrapper = nested.get("payment")
    if not isinstance(payment_wrapper, dict):
        raise ValueError("Razorpay payload missing 'payload.payment'")

    entity = payment_wrapper.get("entity")
    if not isinstance(entity, dict):
        raise ValueError("Razorpay payload missing 'payload.payment.entity'")

    return entity


def _extract_provider_event_id(event: RazorpayWebhookEvent) -> str:
    """Return the canonical provider event ID.

    Razorpay sends the event ID in the top-level 'id' field and optionally in
    the X-Razorpay-Event-Id header.  Both paths are already normalised into
    event.event_id by extract_event_id().  We fall back to a constructed key
    using the payment entity ID if event_id is somehow missing.
    """
    if event.event_id:
        return event.event_id

    # Fallback: construct from event type + payment entity id if available.
    nested = event.payload.get("payload", {})
    payment_entity = nested.get("payment", {}).get("entity", {})
    pay_id = payment_entity.get("id")
    if isinstance(pay_id, str) and pay_id:
        return f"payment.failed:{pay_id}"

    raise ValueError(
        "Cannot derive a provider_event_id: both event.event_id and "
        "payload.payment.entity.id are absent"
    )


def _extract_merchant_id(payload: dict[str, Any]) -> str:
    """Extract the merchant/account ID from the top-level payload.

    Razorpay places the account ID in the top-level 'account_id' field.
    Raises ValueError if the field is absent or not a non-empty string — we
    must not fabricate a merchant identity.
    """
    account_id = payload.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id

    raise ValueError(
        "Required field 'account_id' is absent or invalid in the Razorpay "
        f"payload; got {account_id!r}"
    )




def _extract_occurred_at(entity: dict[str, Any]) -> datetime:
    """Convert the payment entity's created_at Unix timestamp to a UTC datetime."""
    created_at = entity.get("created_at")
    if isinstance(created_at, (int, float)) and created_at > 0:
        return datetime.fromtimestamp(created_at, tz=timezone.utc)

    # created_at is required for meaningful time-based analysis; raise.
    raise ValueError(
        "payment.entity.created_at is missing or invalid; "
        f"got {created_at!r}"
    )


def _build_metadata(entity: dict[str, Any]) -> dict[str, Any]:
    """Preserve a small set of provider-specific fields for downstream use.

    Only fields that were observed in real Test Mode payloads are included.
    """
    metadata: dict[str, Any] = {}

    for key in ("error_code", "error_reason", "method", "bank", "wallet"):
        value = entity.get(key)
        if value is not None:
            metadata[key] = value

    invoice_id = entity.get("invoice_id")
    if (
        isinstance(invoice_id, str)
        and invoice_id.startswith("inv_")
        and bool(invoice_id[4:])
        and all(character.isalnum() or character == "_" for character in invoice_id[4:])
    ):
        metadata["invoice_id"] = invoice_id

    # A payment attempted through a Razorpay Payment Link carries the link ID
    # in its notes. Preserve only that bounded correlation identifier so the
    # recovery pipeline can associate a failed recovery attempt with its
    # existing case rather than treating it as fresh revenue at risk.
    notes = entity.get("notes")
    if isinstance(notes, dict):
        payment_link_id = notes.get("payment_link_id")
        if isinstance(payment_link_id, str) and payment_link_id:
            metadata["payment_link_id"] = payment_link_id
        case_id = notes.get("case_id")
        if isinstance(case_id, str) and case_id:
            metadata["case_id"] = case_id

    return metadata


def _require_str(entity: dict[str, Any], key: str, field_path: str) -> str:
    """Return entity[key] as str, raising ValueError if absent or wrong type."""
    value = entity.get(key)
    if isinstance(value, str) and value:
        return value
    raise ValueError(
        f"Required field '{field_path}' is missing or empty in payment entity; "
        f"got {value!r}"
    )


def _require_int(entity: dict[str, Any], key: str, field_path: str) -> int:
    """Return entity[key] as int, raising ValueError if absent or wrong type."""
    value = entity.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(
        f"Required field '{field_path}' is missing or not an integer in payment entity; "
        f"got {value!r}"
    )


def _optional_str(entity: dict[str, Any], key: str) -> str | None:
    """Return entity[key] as str if present and non-empty, else None."""
    value = entity.get(key)
    if isinstance(value, str) and value:
        return value
    return None

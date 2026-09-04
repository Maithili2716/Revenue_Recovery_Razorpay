"""Normalizer for Razorpay ``invoice.paid`` webhook events.

The result is a bounded, provider-agnostic revenue signal. It records an
authoritative invoice-payment outcome only; it does not create a recovery case
or start the recovery pipeline.
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

SUPPORTED_EVENT_TYPE = "invoice.paid"
PROVIDER_NAME = "razorpay"
_MAX_NOTE_VALUE_LENGTH = 128


class UnsupportedEventType(Exception):
    """Raised when an event is not an ``invoice.paid`` webhook."""


def normalize_invoice_paid(event: RazorpayWebhookEvent) -> RevenueSignal:
    """Convert a Razorpay ``invoice.paid`` event into a bounded signal."""
    if event.event_type != SUPPORTED_EVENT_TYPE:
        raise UnsupportedEventType(
            f"Invoice normalizer only handles '{SUPPORTED_EVENT_TYPE}'; "
            f"received '{event.event_type}'"
        )

    invoice = _extract_invoice_entity(event.payload)
    invoice_id = _require_str(invoice, "id", "invoice.id")
    provider_event_id = event.event_id or f"{SUPPORTED_EVENT_TYPE}:{invoice_id}"

    return RevenueSignal(
        signal_id=build_signal_id(PROVIDER_NAME, provider_event_id),
        merchant_id=_extract_merchant_id(event.payload),
        # Do not copy customer details from the provider event into the signal.
        customer_id=None,
        signal_type=SignalType.INVOICE_PAID,
        status=SignalStatus.PAID,
        amount_minor=_invoice_amount_minor(invoice),
        currency=_require_str(invoice, "currency", "invoice.currency"),
        provider=PROVIDER_NAME,
        provider_event_id=provider_event_id,
        provider_entity_id=invoice_id,
        reason=None,
        failure_source=None,
        failure_step=None,
        occurred_at=_extract_occurred_at(invoice),
        raw_event_type=SUPPORTED_EVENT_TYPE,
        metadata=_build_metadata(invoice, invoice_id),
    )


def _extract_invoice_entity(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("payload")
    if not isinstance(nested, dict):
        raise ValueError("Razorpay payload missing 'payload' key")
    wrapper = nested.get("invoice")
    if not isinstance(wrapper, dict):
        raise ValueError("Razorpay payload missing 'payload.invoice'")
    entity = wrapper.get("entity")
    if not isinstance(entity, dict):
        raise ValueError("Razorpay payload missing 'payload.invoice.entity'")
    return entity


def _extract_merchant_id(payload: dict[str, Any]) -> str:
    account_id = payload.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    raise ValueError("Required field 'account_id' is absent or invalid")


def _invoice_amount_minor(invoice: dict[str, Any]) -> int:
    """Use paid amount when supplied, otherwise the invoice amount."""
    for field in ("amount_paid", "amount"):
        value = invoice.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise ValueError("Required invoice amount is absent or invalid")


def _extract_occurred_at(invoice: dict[str, Any]) -> datetime:
    for field in ("paid_at", "created_at"):
        value = invoice.get(field)
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(value, tz=timezone.utc)
    raise ValueError("invoice.paid requires a valid paid_at or created_at timestamp")


def _build_metadata(invoice: dict[str, Any], invoice_id: str) -> dict[str, str]:
    """Keep only bounded recovery-correlation notes from the invoice."""
    metadata = {"invoice_id": invoice_id}
    notes = invoice.get("notes")
    if not isinstance(notes, dict):
        return metadata

    for key in ("case_id", "capability_id", "recovery_system"):
        value = notes.get(key)
        if isinstance(value, str) and 0 < len(value) <= _MAX_NOTE_VALUE_LENGTH:
            metadata[key] = value
    return metadata


def _require_str(entity: dict[str, Any], key: str, field_path: str) -> str:
    value = entity.get(key)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"Required field '{field_path}' is absent or invalid")

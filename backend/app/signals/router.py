"""Signal router: dispatches a RazorpayWebhookEvent to the correct normalizer.

The router is the single point of dispatch from the raw event layer to the
normalizer layer.  It selects the right normalizer based on event_type and
returns a canonical RevenueSignal.

Adding support for a new event type (subscription.charged, invoice.expired,
etc.) means adding a new normalizer module and one new branch here — the
payment normalizer is NOT modified.

Recovery-related events (payment_link.paid, payment.captured, etc.) are NOT
signal events — they are routed to the recovery verification handler instead.
"""

from __future__ import annotations

from app.integrations.razorpay.events import RazorpayWebhookEvent
from app.signals.models import RevenueSignal
from app.signals.normalizers.payment import (
    UnsupportedEventType as PaymentUnsupportedEventType,
    normalize_payment_failed,
)

# Event types handled by signal normalizers.
_SUPPORTED_SIGNAL_EVENT_TYPES: frozenset[str] = frozenset({"payment.failed"})

# Event types that are recovery-related (not signals — routed to verification).
RECOVERY_EVENT_TYPES: frozenset[str] = frozenset({
    "payment.authorized",
    "payment.captured",
    "payment_link.paid",
})

# All recognized event types (for webhook acknowledgement).
ALL_RECOGNIZED_EVENT_TYPES: frozenset[str] = (
    _SUPPORTED_SIGNAL_EVENT_TYPES | RECOVERY_EVENT_TYPES
)


class UnsupportedEventType(Exception):
    """Raised when the router receives an event type it cannot handle.

    Callers should treat this as a hard failure: do not create a partial or
    incorrect RevenueSignal when the event type is unknown.
    """


def is_recovery_event(event_type: str | None) -> bool:
    """Return True if the event type is a recovery-related webhook event.

    Recovery events are NOT normalized into RevenueSignals — they are
    dispatched to the recovery verification handler instead.
    """
    return event_type in RECOVERY_EVENT_TYPES


def route_webhook_to_signal(event: RazorpayWebhookEvent) -> RevenueSignal:
    """Dispatch a RazorpayWebhookEvent to the appropriate normalizer.

    Returns:
        A fully normalised RevenueSignal.

    Raises:
        UnsupportedEventType: If the event type is not yet supported.  This
            is intentionally an explicit error — do not silently skip events.
    """
    event_type = event.event_type

    if event_type == "payment.failed":
        return normalize_payment_failed(event)

    raise UnsupportedEventType(
        f"No normalizer registered for event type '{event_type}'. "
        f"Supported types: {sorted(_SUPPORTED_SIGNAL_EVENT_TYPES)}"
    )


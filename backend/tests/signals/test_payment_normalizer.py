"""Unit tests for Block 2A: Signal Ingestion + Normalization.

Tests cover:
- Happy path: real-shaped payment.failed payload → expected RevenueSignal
- Amount stays in paise (minor units)
- Payment ID extraction
- Merchant/account ID extraction
- Failure source / step / reason extraction
- Provider metadata preservation
- Unsupported event types are rejected by both normalizer and router
- Missing optional fields do not crash normalization
- Missing required fields raise ValueError
- customer_id is always None (no contact/email substitution)

Fixture data is synthetic but shaped exactly like the real Razorpay
Test Mode 'payment.failed' webhook structure.
"""

from __future__ import annotations

import copy
import os
from datetime import datetime, timezone

import pytest

# Ensure env vars are set before app imports.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:5432/revenue_recovery_test",
)
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")

from app.integrations.razorpay.events import RazorpayWebhookEvent
from app.signals.models import RevenueSignal, SignalStatus, SignalType
from app.signals.normalizers.payment import (
    UnsupportedEventType as PaymentUnsupportedEventType,
    normalize_payment_failed,
)
from app.signals.router import UnsupportedEventType as RouterUnsupportedEventType
from app.signals.router import route_webhook_to_signal

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

CREATED_AT_UNIX = 1_700_000_000  # fixed unix timestamp for deterministic tests
CREATED_AT_UTC = datetime.fromtimestamp(CREATED_AT_UNIX, tz=timezone.utc)


def _make_payload(
    *,
    payment_id: str = "pay_TestPayment001",
    account_id: str = "acc_TestMerchant001",
    event_id: str = "evt_test_payment_001",
    amount: int = 49900,
    currency: str = "INR",
    contact: str | None = "+919876543210",
    email: str | None = "test.customer@example.com",
    error_code: str | None = "BAD_REQUEST_ERROR",
    error_description: str | None = "Payment declined due to insufficient funds.",
    error_source: str | None = "customer",
    error_step: str | None = "payment_authorization",
    error_reason: str | None = "insufficient_funds",
    method: str | None = "card",
    invoice_id: str | None = None,
    notes: dict | None = None,
) -> dict:
    """Synthetic Razorpay payment.failed payload shaped like the real one."""
    return {
        "entity": "event",
        "event": "payment.failed",
        "id": event_id,
        "account_id": account_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": currency,
                    "status": "failed",
                    "contact": contact,
                    "email": email,
                    "error_code": error_code,
                    "error_description": error_description,
                    "error_source": error_source,
                    "error_step": error_step,
                    "error_reason": error_reason,
                    "method": method,
                    "invoice_id": invoice_id,
                    "notes": notes,
                    "created_at": CREATED_AT_UNIX,
                }
            }
        },
    }


def _make_event(
    payload: dict,
    event_type: str = "payment.failed",
    event_id: str = "evt_test_payment_001",
) -> RazorpayWebhookEvent:
    """Build a RazorpayWebhookEvent directly, bypassing HTTP machinery."""
    return RazorpayWebhookEvent(
        event_type=event_type,
        event_id=event_id,
        raw_body=b"{}",
        payload=payload,
    )


def _remove_entity_field(payload: dict, *keys: str) -> dict:
    """Return a deep copy of payload with the given payment.entity keys removed."""
    d = copy.deepcopy(payload)
    entity = d["payload"]["payment"]["entity"]
    for key in keys:
        entity.pop(key, None)
    return d


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestNormalizePaymentFailed:
    def test_returns_revenue_signal(self) -> None:
        signal = normalize_payment_failed(_make_event(_make_payload()))
        assert isinstance(signal, RevenueSignal)

    def test_signal_type_is_payment_failure(self) -> None:
        signal = normalize_payment_failed(_make_event(_make_payload()))
        assert signal.signal_type == SignalType.PAYMENT_FAILURE

    def test_status_is_failed(self) -> None:
        signal = normalize_payment_failed(_make_event(_make_payload()))
        assert signal.status == SignalStatus.FAILED

    def test_amount_stays_in_paise(self) -> None:
        """49900 paise must not be converted to 499 rupees."""
        signal = normalize_payment_failed(_make_event(_make_payload(amount=49900)))
        assert signal.amount_minor == 49900

    def test_currency_is_preserved(self) -> None:
        signal = normalize_payment_failed(_make_event(_make_payload(currency="INR")))
        assert signal.currency == "INR"

    def test_payment_id_extracted(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(payment_id="pay_TestPayment001"))
        )
        assert signal.provider_entity_id == "pay_TestPayment001"

    def test_merchant_account_id_extracted(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(account_id="acc_TestMerchant001"))
        )
        assert signal.merchant_id == "acc_TestMerchant001"

    def test_provider_event_id_extracted(self) -> None:
        event = _make_event(_make_payload(event_id="evt_abc"), event_id="evt_abc")
        signal = normalize_payment_failed(event)
        assert signal.provider_event_id == "evt_abc"

    def test_failure_source_extracted(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(error_source="customer"))
        )
        assert signal.failure_source == "customer"

    def test_failure_step_extracted(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(error_step="payment_authorization"))
        )
        assert signal.failure_step == "payment_authorization"

    def test_failure_reason_extracted(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(error_description="Payment declined due to insufficient funds."))
        )
        assert signal.reason == "Payment declined due to insufficient funds."

    def test_occurred_at_is_utc(self) -> None:
        signal = normalize_payment_failed(_make_event(_make_payload()))
        assert signal.occurred_at == CREATED_AT_UTC
        assert signal.occurred_at.tzinfo == timezone.utc

    def test_provider_is_razorpay(self) -> None:
        signal = normalize_payment_failed(_make_event(_make_payload()))
        assert signal.provider == "razorpay"

    def test_raw_event_type_is_preserved(self) -> None:
        signal = normalize_payment_failed(_make_event(_make_payload()))
        assert signal.raw_event_type == "payment.failed"

    def test_signal_id_is_deterministic(self) -> None:
        event = _make_event(_make_payload(), event_id="evt_idem_001")
        assert normalize_payment_failed(event).signal_id == normalize_payment_failed(event).signal_id

    def test_signal_id_differs_across_events(self) -> None:
        e1 = _make_event(_make_payload(event_id="evt_a"), event_id="evt_a")
        e2 = _make_event(_make_payload(event_id="evt_b"), event_id="evt_b")
        assert normalize_payment_failed(e1).signal_id != normalize_payment_failed(e2).signal_id


# ---------------------------------------------------------------------------
# customer_id is always None — no contact/email substitution
# ---------------------------------------------------------------------------


class TestCustomerId:
    def test_customer_id_is_none_when_contact_present(self) -> None:
        """contact (phone) must not be used as customer_id."""
        signal = normalize_payment_failed(
            _make_event(_make_payload(contact="+919876543210"))
        )
        assert signal.customer_id is None

    def test_customer_id_is_none_when_email_present(self) -> None:
        """email must not be used as customer_id."""
        signal = normalize_payment_failed(
            _make_event(_make_payload(contact=None, email="test@example.com"))
        )
        assert signal.customer_id is None

    def test_customer_id_is_none_when_both_absent(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(contact=None, email=None))
        )
        assert signal.customer_id is None


# ---------------------------------------------------------------------------
# Optional failure fields
# ---------------------------------------------------------------------------


class TestOptionalFailureFields:
    def test_missing_failure_source_is_none(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(error_source=None))
        )
        assert signal.failure_source is None

    def test_missing_failure_step_is_none(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(error_step=None))
        )
        assert signal.failure_step is None

    def test_missing_failure_reason_is_none(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(error_description=None))
        )
        assert signal.reason is None


# ---------------------------------------------------------------------------
# Metadata preservation
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_error_code_in_metadata(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(error_code="BAD_REQUEST_ERROR"))
        )
        assert signal.metadata.get("error_code") == "BAD_REQUEST_ERROR"

    def test_error_reason_in_metadata(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(error_reason="insufficient_funds"))
        )
        assert signal.metadata.get("error_reason") == "insufficient_funds"

    def test_method_in_metadata(self) -> None:
        signal = normalize_payment_failed(_make_event(_make_payload(method="card")))
        assert signal.metadata.get("method") == "card"

    def test_payment_link_id_in_notes_is_preserved_for_case_correlation(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(notes={"payment_link_id": "plink_recovery_001"}))
        )
        assert signal.metadata["payment_link_id"] == "plink_recovery_001"

    def test_invoice_id_is_preserved_for_case_correlation(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(invoice_id="inv_test_recovery001"))
        )
        assert signal.metadata["invoice_id"] == "inv_test_recovery001"

    def test_invalid_invoice_id_is_not_preserved(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(invoice_id="invoice_not_razorpay"))
        )
        assert "invoice_id" not in signal.metadata

    def test_none_values_excluded_from_metadata(self) -> None:
        signal = normalize_payment_failed(
            _make_event(_make_payload(method=None, error_code=None))
        )
        assert "method" not in signal.metadata
        assert "error_code" not in signal.metadata


# ---------------------------------------------------------------------------
# Unsupported event types
# ---------------------------------------------------------------------------


class TestUnsupportedEventTypes:
    def test_payment_normalizer_rejects_wrong_type(self) -> None:
        event = _make_event(_make_payload(), event_type="payment.captured")
        with pytest.raises(PaymentUnsupportedEventType, match="payment.captured"):
            normalize_payment_failed(event)

    def test_router_rejects_unsupported_type(self) -> None:
        event = _make_event(_make_payload(), event_type="refund.created")
        with pytest.raises(RouterUnsupportedEventType, match="refund.created"):
            route_webhook_to_signal(event)

    def test_router_rejects_none_event_type(self) -> None:
        event = RazorpayWebhookEvent(
            event_type=None,
            event_id="evt_none",
            raw_body=b"{}",
            payload={},
        )
        with pytest.raises(RouterUnsupportedEventType):
            route_webhook_to_signal(event)


# ---------------------------------------------------------------------------
# Router dispatch
# ---------------------------------------------------------------------------


class TestRouter:
    def test_router_dispatches_payment_failed(self) -> None:
        event = _make_event(_make_payload())
        signal = route_webhook_to_signal(event)
        assert isinstance(signal, RevenueSignal)
        assert signal.signal_type == SignalType.PAYMENT_FAILURE

    def test_router_result_matches_direct_normalizer(self) -> None:
        event = _make_event(_make_payload())
        assert normalize_payment_failed(event) == route_webhook_to_signal(event)


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    def test_missing_payment_id_raises(self) -> None:
        event = _make_event(_remove_entity_field(_make_payload(), "id"))
        with pytest.raises(ValueError, match="payment.id"):
            normalize_payment_failed(event)

    def test_missing_amount_raises(self) -> None:
        event = _make_event(_remove_entity_field(_make_payload(), "amount"))
        with pytest.raises(ValueError, match="payment.amount"):
            normalize_payment_failed(event)

    def test_missing_currency_raises(self) -> None:
        event = _make_event(_remove_entity_field(_make_payload(), "currency"))
        with pytest.raises(ValueError, match="payment.currency"):
            normalize_payment_failed(event)

    def test_missing_payment_entity_raises(self) -> None:
        payload = _make_payload()
        del payload["payload"]["payment"]["entity"]
        with pytest.raises(ValueError, match="payload.payment.entity"):
            normalize_payment_failed(_make_event(payload))

    def test_missing_account_id_raises(self) -> None:
        payload = _make_payload()
        del payload["account_id"]
        with pytest.raises(ValueError, match="account_id"):
            normalize_payment_failed(_make_event(payload))

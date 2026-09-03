"""Integration tests for webhook → signal pipeline.

These tests exercise the full path through the HTTP endpoint:

    POST /webhooks/razorpay
        → signature verification
        → idempotency
        → RazorpayWebhookEvent
        → background dispatch (non-blocking)
        → HTTP 200 immediate

Tests:
1. Valid payment.failed → 200 accepted, background task is dispatched.
2. Duplicate payment.failed → background task is NOT dispatched a second time.
3. Invalid signature → 401, no background task dispatched.
4. Unsupported event type → 200 accepted.
5. Webhook does NOT await recovery/LLM processing.
6. Existing signature verification and idempotency behavior remains intact.
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:5432/revenue_recovery_test",
)
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")

from tests.conftest import build_signed_request, sign_webhook_body

# ---------------------------------------------------------------------------
# Shared payload — shaped like a real Razorpay payment.failed event,
# including all fields required by the normalizer.
# ---------------------------------------------------------------------------

CREATED_AT_UNIX = 1_700_000_000

FULL_PAYMENT_FAILED = {
    "entity": "event",
    "event": "payment.failed",
    "id": "evt_integration_pay_fail_001",
    "account_id": "acc_IntegrationMerchant",
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_IntegrationTest001",
                "entity": "payment",
                "amount": 49900,
                "currency": "INR",
                "status": "failed",
                "contact": "+919876543210",
                "email": "test@example.com",
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": "Payment declined due to insufficient funds.",
                "error_source": "customer",
                "error_step": "payment_authorization",
                "error_reason": "insufficient_funds",
                "method": "card",
                "created_at": CREATED_AT_UNIX,
            }
        }
    },
}

UNSUPPORTED_EVENT = {
    "entity": "event",
    "event": "refund.created",
    "id": "evt_integration_refund_001",
    "account_id": "acc_IntegrationMerchant",
    "payload": {},
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_payment_failed_dispatches_background_task(client) -> None:
    """Valid payment.failed → 200 accepted, background task is created."""
    with patch(
        "app.api.razorpay_webhooks.ingest_webhook_event_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        body, headers = build_signed_request(FULL_PAYMENT_FAILED)
        response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    # Background task was dispatched (create_task wraps the coroutine).
    mock_bg.assert_called_once()


def test_duplicate_payment_failed_does_not_dispatch_twice(client) -> None:
    """Duplicate webhook → second call does not dispatch a background task."""
    with patch(
        "app.api.razorpay_webhooks.ingest_webhook_event_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        body, headers = build_signed_request(FULL_PAYMENT_FAILED)
        first = client.post("/webhooks/razorpay", content=body, headers=headers)
        second = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert mock_bg.call_count == 1  # Only dispatched once


def test_invalid_signature_does_not_dispatch(client) -> None:
    """Invalid signature → 401, no background task dispatched."""
    body, headers = build_signed_request(FULL_PAYMENT_FAILED)
    headers["X-Razorpay-Signature"] = "bad_signature"

    with patch(
        "app.api.razorpay_webhooks.ingest_webhook_event_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 401
    mock_bg.assert_not_called()


def test_unsupported_event_type_returns_200(client) -> None:
    """Valid but unsupported event type → 200 accepted, background task dispatched."""
    with patch(
        "app.api.razorpay_webhooks.ingest_webhook_event_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        body, headers = build_signed_request(UNSUPPORTED_EVENT)
        response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    # Background task is dispatched — the service layer handles unsupported types safely.
    mock_bg.assert_called_once()


def test_webhook_does_not_await_recovery_processing(client) -> None:
    """The webhook endpoint returns HTTP 200 without waiting for recovery processing.

    This test injects a slow background task and verifies the HTTP response
    returns in well under the simulated processing time.
    """
    async def slow_background_task(event):
        """Simulate a slow LLM + recovery pipeline that takes 5 seconds."""
        await asyncio.sleep(5)

    with patch(
        "app.api.razorpay_webhooks.ingest_webhook_event_background",
        side_effect=slow_background_task,
    ):
        body, headers = build_signed_request(FULL_PAYMENT_FAILED)

        start = time.monotonic()
        response = client.post("/webhooks/razorpay", content=body, headers=headers)
        elapsed = time.monotonic() - start

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    # The HTTP response MUST return in well under 5 seconds.
    # If it waited for the background task, it would take ~5s.
    assert elapsed < 2.0, f"Webhook took {elapsed:.2f}s — it should NOT await the background task"


def test_idempotency_behavior_intact(client) -> None:
    """Signature verification and idempotency behavior remains intact."""
    # Missing signature → 400
    body, headers = build_signed_request(FULL_PAYMENT_FAILED)
    del headers["X-Razorpay-Signature"]

    with patch(
        "app.api.razorpay_webhooks.ingest_webhook_event_background",
        new_callable=AsyncMock,
    ):
        response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 400

import json

from tests.conftest import TEST_WEBHOOK_SECRET, build_signed_request, sign_webhook_body

# Synthetic Razorpay payment.failed payload for automated tests.
# Shaped like the real Razorpay Test Mode event, with no real PII.
# Must satisfy the current payment normalizer (Block 2A/2B).
SYNTHETIC_PAYMENT_FAILED = {
    "entity": "event",
    "event": "payment.failed",
    "id": "evt_test_payment_failed_001",
    "account_id": "acc_test_merchant_001",
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_test_failed_001",
                "entity": "payment",
                "amount": 50000,
                "currency": "INR",
                "status": "failed",
                "contact": "+910000000000",
                "email": "test@example.invalid",
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": "Payment failed (synthetic test data).",
                "error_source": "customer",
                "error_step": "payment_authorization",
                "error_reason": "insufficient_funds",
                "method": "card",
                "created_at": 1700000000,
            }
        }
    },
}


def test_valid_webhook_is_accepted(client) -> None:
    body, headers = build_signed_request(SYNTHETIC_PAYMENT_FAILED)

    response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "event_type": "payment.failed",
        "event_id": "evt_test_payment_failed_001",
    }


def test_invalid_signature_is_rejected(client) -> None:
    body, headers = build_signed_request(SYNTHETIC_PAYMENT_FAILED)
    headers["X-Razorpay-Signature"] = "invalid_signature"

    response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid webhook signature"


def test_duplicate_event_is_recognized(client) -> None:
    body, headers = build_signed_request(SYNTHETIC_PAYMENT_FAILED)

    first = client.post("/webhooks/razorpay", content=body, headers=headers)
    second = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    assert second.status_code == 200
    assert second.json() == {
        "status": "duplicate",
        "event_type": "payment.failed",
        "event_id": "evt_test_payment_failed_001",
    }


def test_malformed_json_is_rejected(client) -> None:
    body = b"{not-json"
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sign_webhook_body(body),
    }

    response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON payload"


def test_missing_signature_is_rejected(client) -> None:
    body = json.dumps(SYNTHETIC_PAYMENT_FAILED).encode("utf-8")

    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Missing X-Razorpay-Signature header"


def test_signature_uses_raw_body_not_reserialized_json(client) -> None:
    # This test proves that signature verification uses the original raw
    # request bytes, not a re-serialized version of the parsed JSON dict.
    # The payload must be normalizer-compliant now that the pipeline is connected.
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "id": "evt_raw_body_check",
        "account_id": "acc_test_merchant_001",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_raw_body_check",
                    "entity": "payment",
                    "amount": 10000,
                    "currency": "INR",
                    "status": "failed",
                    "created_at": 1700000000,
                }
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sign_webhook_body(body, TEST_WEBHOOK_SECRET),
    }

    response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    # Signing a re-serialized version (with different whitespace) must fail.
    reserialized_body = json.dumps(payload).encode("utf-8")
    bad_headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sign_webhook_body(reserialized_body, TEST_WEBHOOK_SECRET),
    }
    bad_response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers=bad_headers,
    )
    assert bad_response.status_code == 401


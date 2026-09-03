#!/usr/bin/env python3
"""Create one Razorpay Test Mode invoice for manual unpaid/expired testing.

Uses documented Razorpay Invoices REST APIs:
  - POST /v1/invoices
  - POST /v1/invoices/{invoice_id}/issue

No invoice tools exist in the project's Razorpay MCP namespace. MCP is not
invocable from a local Python CLI, so this script calls Test Mode APIs via httpx.

For invoices, failure means the receivable remains unpaid until it expires.
Razorpay does not provide an immediate "overdue" API in Test Mode. The earliest
supported expiry is 15 minutes after issue, after which status becomes expired.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
TEST_KEY_PREFIX = "rzp_test_"
INVOICE_AMOUNT_PAISE = 10000  # INR 100.00
MIN_EXPIRE_AFTER_SECONDS = 16 * 60  # Razorpay requires expire_by >= 15 minutes ahead


def _ensure_test_mode_credentials() -> None:
    if not settings.razorpay_key_id.startswith(TEST_KEY_PREFIX):
        print(
            "error=refusing_to_run; configured RAZORPAY_KEY_ID is not a Test Mode key "
            f"(expected prefix {TEST_KEY_PREFIX!r})",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _auth() -> tuple[str, str]:
    return settings.razorpay_key_id, settings.razorpay_key_secret


def _create_invoice(client: httpx.Client, *, expire_by: int) -> dict[str, Any]:
    response = client.post(
        "/invoices",
        json={
            "type": "invoice",
            "description": "Integration test: deliberate unpaid invoice expiry",
            "partial_payment": False,
            "customer": {
                "name": "Integration Test",
                "contact": "9876543210",
                "email": "integration.test@example.com",
                "billing_address": {
                    "line1": "Test Address Line 1",
                    "line2": "Test Address Line 2",
                    "zipcode": "560001",
                    "city": "Bengaluru",
                    "state": "Karnataka",
                    "country": "in",
                },
            },
            "line_items": [
                {
                    "name": "Integration test receivable",
                    "description": "Unpaid invoice for webhook reconnaissance",
                    "amount": INVOICE_AMOUNT_PAISE,
                    "currency": "INR",
                    "quantity": 1,
                }
            ],
            "sms_notify": False,
            "email_notify": False,
            "currency": "INR",
            "expire_by": expire_by,
            "notes": {
                "purpose": "trigger_invoice_failure",
            },
        },
    )
    response.raise_for_status()
    return response.json()


def _issue_invoice(client: httpx.Client, *, invoice_id: str) -> dict[str, Any]:
    response = client.post(f"/invoices/{invoice_id}/issue")
    response.raise_for_status()
    return response.json()


def main() -> int:
    _ensure_test_mode_credentials()

    expire_by = int(time.time()) + MIN_EXPIRE_AFTER_SECONDS
    expire_at = datetime.fromtimestamp(expire_by, tz=UTC).isoformat()

    with httpx.Client(base_url=RAZORPAY_API_BASE, auth=_auth(), timeout=30.0) as client:
        invoice = _create_invoice(client, expire_by=expire_by)
        issued_invoice = _issue_invoice(client, invoice_id=invoice["id"])

    invoice_id = issued_invoice["id"]
    invoice_status = issued_invoice.get("status")
    amount_due = issued_invoice.get("amount_due")
    short_url = issued_invoice.get("short_url")

    print(f"invoice_id={invoice_id}")
    print(f"invoice_status={invoice_status}")
    print(f"amount_due={amount_due}")
    print(f"expire_by_unix={expire_by}")
    print(f"expire_by_utc={expire_at}")
    if short_url:
        print(f"short_url={short_url}")

    print()
    print("MANUAL ACTION REQUIRED: DO NOT PAY THIS INVOICE.")
    print(
        "LEAVE IT UNPAID UNTIL expire_by IS REACHED. "
        "RAZORPAY REQUIRES AT LEAST 15 MINUTES BEFORE AN ISSUED INVOICE CAN EXPIRE."
    )
    print(f"WAIT UNTIL AT LEAST: {expire_at}")
    print("CONFIRM invoice.expired IS ENABLED ON YOUR TEST MODE WEBHOOK SUBSCRIPTION.")
    print("WATCH THE FASTAPI TERMINAL FOR POST /webhooks/razorpay.")
    print("EXPECTED WEBHOOK EVENT WHEN THE INVOICE BECOMES OVERDUE: invoice.expired")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.HTTPStatusError as exc:
        print(f"razorpay_http_error={exc.response.status_code}", file=sys.stderr)
        print(exc.response.text, file=sys.stderr)
        raise SystemExit(1) from exc

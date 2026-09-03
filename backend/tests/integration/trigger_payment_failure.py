#!/usr/bin/env python3
"""Create one Razorpay Test Mode Payment Link for manual failure testing.

Uses POST /v1/payment_links (same endpoint as the MCP create_payment_link tool).
MCP is not invocable from a local Python CLI, so this script calls the Test Mode
API directly via httpx and app.config.settings.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
TEST_KEY_PREFIX = "rzp_test_"
AMOUNT_PAISE = 10000  # INR 100.00


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


def _create_payment_link(client: httpx.Client) -> dict[str, Any]:
    response = client.post(
        "/payment_links",
        json={
            "amount": AMOUNT_PAISE,
            "currency": "INR",
            "description": "Integration test: deliberate Test Mode payment link failure",
            "customer": {
                "name": "Integration Test",
                "email": "integration.test@example.com",
                "contact": "9876543210",
            },
            "notify": {
                "sms": False,
                "email": False,
            },
            "notes": {
                "purpose": "trigger_payment_link_failure",
            },
        },
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    _ensure_test_mode_credentials()

    with httpx.Client(base_url=RAZORPAY_API_BASE, auth=_auth(), timeout=30.0) as client:
        payment_link = _create_payment_link(client)

    print(f"payment_link_id={payment_link['id']}")
    print(f"short_url={payment_link['short_url']}")
    print()
    print("OPEN THE PAYMENT LINK IN YOUR BROWSER.")
    print("USE RAZORPAY TEST MODE TO DELIBERATELY FAIL THE PAYMENT.")
    print("WATCH THE FASTAPI TERMINAL FOR POST /webhooks/razorpay.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.HTTPStatusError as exc:
        print(f"razorpay_http_error={exc.response.status_code}", file=sys.stderr)
        print(exc.response.text, file=sys.stderr)
        raise SystemExit(1) from exc

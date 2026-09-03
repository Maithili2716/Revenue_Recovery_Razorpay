#!/usr/bin/env python3
"""Create one Razorpay Test Mode subscription for manual charge-failure testing.

Uses documented Razorpay Subscriptions REST APIs:
  - POST /v1/plans
  - POST /v1/subscriptions

No subscription tools exist in the project's Razorpay MCP namespace. MCP is not
invocable from a local Python CLI, so this script calls Test Mode APIs via httpx.

Razorpay Test Mode does not expose an API to choose a failed subsequent charge.
After authentication, trigger failure manually from the Dashboard using
"Charge this now" and selecting failure.
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
PLAN_AMOUNT_PAISE = 10000  # INR 100.00
SUBSCRIPTION_TOTAL_COUNT = 6


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


def _create_plan(client: httpx.Client) -> dict[str, Any]:
    response = client.post(
        "/plans",
        json={
            "period": "monthly",
            "interval": 1,
            "item": {
                "name": "Integration test subscription plan",
                "amount": PLAN_AMOUNT_PAISE,
                "currency": "INR",
                "description": "Test plan for subscription failure reconnaissance",
            },
            "notes": {
                "purpose": "trigger_subscription_failure",
            },
        },
    )
    response.raise_for_status()
    return response.json()


def _create_subscription(client: httpx.Client, *, plan_id: str) -> dict[str, Any]:
    response = client.post(
        "/subscriptions",
        json={
            "plan_id": plan_id,
            "total_count": SUBSCRIPTION_TOTAL_COUNT,
            "customer_notify": True,
            "notes": {
                "purpose": "trigger_subscription_failure",
            },
        },
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    _ensure_test_mode_credentials()

    with httpx.Client(base_url=RAZORPAY_API_BASE, auth=_auth(), timeout=30.0) as client:
        plan = _create_plan(client)
        subscription = _create_subscription(client, plan_id=plan["id"])

    plan_id = plan["id"]
    subscription_id = subscription["id"]
    subscription_status = subscription.get("status")
    short_url = subscription.get("short_url")

    print(f"plan_id={plan_id}")
    print(f"subscription_id={subscription_id}")
    print(f"subscription_status={subscription_status}")
    if short_url:
        print(f"short_url={short_url}")

    print()
    print("MANUAL STEP 1: AUTHENTICATE THE SUBSCRIPTION")
    if short_url:
        print(f"OPEN THIS URL IN YOUR BROWSER: {short_url}")
    else:
        print(
            "OPEN RAZORPAY TEST MODE DASHBOARD > SUBSCRIPTIONS > "
            f"{subscription_id} > START SUBSCRIPTION"
        )
    print("COMPLETE THE AUTH PAYMENT WITH A RAZORPAY TEST CARD AND CHOOSE SUCCESS.")
    print()
    print("MANUAL STEP 2: TRIGGER A FAILED SUBSEQUENT CHARGE")
    print(
        "IN RAZORPAY TEST MODE DASHBOARD, OPEN THE SUBSCRIPTION AND CLICK "
        "'CHARGE THIS NOW'."
    )
    print("WHEN PROMPTED, CHOOSE FAILURE (NOT SUCCESS).")
    print()
    print("WATCH THE FASTAPI TERMINAL FOR POST /webhooks/razorpay.")
    print("EXPECTED WEBHOOK EVENTS FOR A FAILED TEST CHARGE: subscription.pending")
    print("AFTER REPEATED FAILURES: subscription.halted")
    print("A RELATED payment.failed EVENT MAY ALSO ARRIVE FOR THE UNDERLYING PAYMENT.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.HTTPStatusError as exc:
        print(f"razorpay_http_error={exc.response.status_code}", file=sys.stderr)
        print(exc.response.text, file=sys.stderr)
        raise SystemExit(1) from exc

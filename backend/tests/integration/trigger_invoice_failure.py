#!/usr/bin/env python3
"""Manually validate Razorpay Test Mode invoice create-and-issue support.

This development-only smoke script deliberately stays outside the recovery
pipeline. An operator may supply an existing legitimate Test Mode customer, or
create one from bounded operator-supplied demo details for this validation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.integrations.razorpay.client import RazorpayPaymentLinkClient  # noqa: E402

TEST_KEY_PREFIX = "rzp_test_"
INVOICE_AMOUNT_PAISE = 10_000  # INR 100.00
CUSTOMERS_URL = "https://api.razorpay.com/v1/customers"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and issue one Razorpay Test Mode invoice."
    )
    customer_source = parser.add_mutually_exclusive_group(required=True)
    customer_source.add_argument(
        "--customer-id",
        help="An existing legitimate Razorpay Test Mode customer ID (cust_...).",
    )
    customer_source.add_argument(
        "--create-customer",
        action="store_true",
        help="Create a Razorpay Test Mode customer from the supplied demo details.",
    )
    parser.add_argument(
        "--customer-name",
        help="Bounded operator-supplied Test Mode customer name (required with --create-customer).",
    )
    parser.add_argument(
        "--customer-email",
        help="Bounded operator-supplied Test Mode customer email (required with --create-customer).",
    )
    parser.add_argument(
        "--customer-contact",
        help="Bounded operator-supplied Test Mode customer contact (required with --create-customer).",
    )
    return parser.parse_args()


def _ensure_test_mode_credentials() -> None:
    if not settings.razorpay_key_id.startswith(TEST_KEY_PREFIX):
        print(
            "error=refusing_to_run; configured RAZORPAY_KEY_ID is not a Test Mode key",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _create_test_customer(args: argparse.Namespace) -> str | None:
    """Create one Test Mode customer without extending the production client."""
    customer_fields = {
        "name": args.customer_name,
        "email": args.customer_email,
        "contact": args.customer_contact,
    }
    if any(not isinstance(value, str) or not value.strip() for value in customer_fields.values()):
        print(
            "error=customer_name_email_and_contact_are_required_with_create_customer",
            file=sys.stderr,
        )
        return None
    if any(len(value.strip()) > 120 for value in customer_fields.values()):
        print("error=customer_fields_exceed_manual_tool_bounds", file=sys.stderr)
        return None

    try:
        with httpx.Client(timeout=30.0) as http_client:
            response = http_client.post(
                CUSTOMERS_URL,
                auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                json={key: value.strip() for key, value in customer_fields.items()},
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        print("create_customer_status=failed", file=sys.stderr)
        return None

    customer_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(customer_id, str) or not customer_id.startswith("cust_"):
        print("create_customer_status=failed", file=sys.stderr)
        return None

    print(f"customer_id={customer_id}")
    return customer_id


def main() -> int:
    args = _arguments()
    _ensure_test_mode_credentials()
    customer_id = args.customer_id
    if args.create_customer:
        customer_id = _create_test_customer(args)
        if customer_id is None:
            return 1
    elif not customer_id.startswith("cust_"):
        print(
            "error=customer_id_must_be_an_existing_razorpay_customer_id",
            file=sys.stderr,
        )
        return 1

    client = RazorpayPaymentLinkClient(
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
    )
    created = client.create_invoice(
        amount_minor=INVOICE_AMOUNT_PAISE,
        currency="INR",
        description="RecoveryLab provider-layer Test Mode invoice",
        customer_id=customer_id,
        notes={
            "capability_id": "invoice_recovery",
            "recovery_system": "adaptive_revenue_recovery",
        },
    )
    if not created.success or not created.invoice_id:
        print("create_status=failed")
        print(f"http_status={created.http_status_code}")
        print(f"error={created.error_message}")
        return 1

    print(f"created_invoice_id={created.invoice_id}")
    print(f"created_invoice_status={created.status}")
    print(f"amount={created.amount}")
    print(f"amount_due={created.amount_due}")
    print(f"currency={created.currency}")

    if created.status == "issued":
        ready = created
    else:
        ready = client.issue_invoice(invoice_id=created.invoice_id)
        if not ready.success or not ready.invoice_id:
            print("issue_status=failed")
            print(f"http_status={ready.http_status_code}")
            print(f"error={ready.error_message}")
            return 1

    print(f"invoice_ready_status={ready.status}")
    print(f"invoice_ready_id={ready.invoice_id}")
    print(f"payment_url={ready.payment_url}")
    print()
    print("Open payment_url only if Razorpay returned one. Do not connect this invoice to recovery yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

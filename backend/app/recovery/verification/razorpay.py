"""Razorpay payment-link verification provider.

Queries the Razorpay Payment Links API to determine whether a payment
link was actually paid:

    GET https://api.razorpay.com/v1/payment_links/{payment_link_id}

The response contains:
    id, amount, amount_paid, currency, status, payments, short_url

Verification rules:
    RECOVERED:     status == "paid" AND amount_paid > 0
    NOT_RECOVERED: status in ("created", "expired", "cancelled") with no payment
    UNKNOWN:       API error, timeout, or unexpected response

This module NEVER:
    - logs API keys or authorization headers
    - fabricates recovery claims
    - treats UNKNOWN as either success or failure
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PAYMENT_LINKS_URL = "https://api.razorpay.com/v1/payment_links/"
_INVOICES_URL = "https://api.razorpay.com/v1/invoices/"


@dataclass(frozen=True)
class PaymentLinkVerificationResponse:
    """Structured response from fetching a payment link."""

    success: bool
    payment_link_id: str | None = None
    status: str | None = None
    amount: int | None = None
    amount_paid: int | None = None
    currency: str | None = None
    payments: list[dict[str, Any]] | None = None
    error_message: str | None = None
    http_status_code: int | None = None


@dataclass(frozen=True)
class InvoiceVerificationResponse:
    """Structured response from fetching a Razorpay invoice."""

    success: bool
    invoice_id: str | None = None
    status: str | None = None
    amount: int | None = None
    amount_paid: int | None = None
    amount_due: int | None = None
    currency: str | None = None
    payment_id: str | None = None
    error_message: str | None = None
    http_status_code: int | None = None


class VerificationProvider(ABC):
    """Abstract verification provider interface.

    Allows different providers (Razorpay, mock, simulated) to be
    swapped without changing the verification service.
    """

    @abstractmethod
    def fetch_payment_link(
        self, payment_link_id: str
    ) -> PaymentLinkVerificationResponse:
        """Fetch the current state of a payment link from the provider."""
        ...

    def fetch_invoice(self, invoice_id: str) -> InvoiceVerificationResponse:
        """Fetch an invoice when the provider supports invoice verification."""
        raise NotImplementedError("Invoice verification is not supported by this provider.")


class RazorpayVerificationProvider(VerificationProvider):
    """Razorpay implementation of the verification provider.

    Uses GET /v1/payment_links/{id} with Basic Auth.
    """

    def __init__(self, key_id: str, key_secret: str) -> None:
        self._auth = (key_id, key_secret)

    def fetch_payment_link(
        self, payment_link_id: str
    ) -> PaymentLinkVerificationResponse:
        """Fetch payment link status from Razorpay.

        Returns a structured response — never raises to the caller.
        """
        url = f"{_PAYMENT_LINKS_URL}{payment_link_id}"

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, auth=self._auth)

            if response.status_code == 200:
                data = response.json()

                # Extract payments array safely.
                payments_raw = data.get("payments")
                payments = None
                if isinstance(payments_raw, dict):
                    payments = payments_raw.get("items", [])
                elif isinstance(payments_raw, list):
                    payments = payments_raw

                return PaymentLinkVerificationResponse(
                    success=True,
                    payment_link_id=data.get("id"),
                    status=data.get("status"),
                    amount=data.get("amount"),
                    amount_paid=data.get("amount_paid"),
                    currency=data.get("currency"),
                    payments=payments,
                    http_status_code=response.status_code,
                )

            # Non-200 — structured failure.
            error_msg = "Razorpay verification API error"
            try:
                error_body = response.json()
                if isinstance(error_body, dict):
                    err_detail = error_body.get("error", {})
                    if isinstance(err_detail, dict):
                        error_msg = err_detail.get("description", error_msg)
            except Exception:
                pass

            logger.warning(
                "razorpay_verification_api_error",
                extra={
                    "http_status": response.status_code,
                    "error_message": error_msg,
                    "payment_link_id": payment_link_id,
                },
            )

            return PaymentLinkVerificationResponse(
                success=False,
                error_message=error_msg,
                http_status_code=response.status_code,
            )

        except httpx.TimeoutException:
            logger.error(
                "razorpay_verification_timeout",
                extra={"payment_link_id": payment_link_id},
            )
            return PaymentLinkVerificationResponse(
                success=False,
                error_message="Razorpay verification request timed out.",
            )
        except httpx.HTTPError as exc:
            logger.error(
                "razorpay_verification_http_error",
                extra={
                    "payment_link_id": payment_link_id,
                    "error": str(exc),
                },
            )
            return PaymentLinkVerificationResponse(
                success=False,
                error_message=f"HTTP error: {exc}",
            )
        except Exception as exc:
            logger.exception(
                "razorpay_verification_unexpected_error",
                extra={"payment_link_id": payment_link_id},
            )
            return PaymentLinkVerificationResponse(
                success=False,
                error_message=f"Unexpected error: {exc}",
            )

    def fetch_invoice(self, invoice_id: str) -> InvoiceVerificationResponse:
        """Fetch invoice status from Razorpay's Invoice API."""
        url = f"{_INVOICES_URL}{invoice_id}"
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, auth=self._auth)

            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict):
                    return InvoiceVerificationResponse(
                        success=False,
                        error_message="Razorpay invoice API returned an invalid response.",
                        http_status_code=response.status_code,
                    )
                return InvoiceVerificationResponse(
                    success=True,
                    invoice_id=data.get("id") if isinstance(data.get("id"), str) else None,
                    status=data.get("status") if isinstance(data.get("status"), str) else None,
                    amount=data.get("amount") if isinstance(data.get("amount"), int) else None,
                    amount_paid=(
                        data.get("amount_paid")
                        if isinstance(data.get("amount_paid"), int)
                        else None
                    ),
                    amount_due=(
                        data.get("amount_due")
                        if isinstance(data.get("amount_due"), int)
                        else None
                    ),
                    currency=data.get("currency") if isinstance(data.get("currency"), str) else None,
                    payment_id=(
                        data.get("payment_id")
                        if isinstance(data.get("payment_id"), str)
                        else None
                    ),
                    http_status_code=response.status_code,
                )

            logger.warning(
                "razorpay_invoice_verification_api_error",
                extra={"invoice_id": invoice_id, "http_status": response.status_code},
            )
            return InvoiceVerificationResponse(
                success=False,
                error_message="Razorpay invoice verification API error.",
                http_status_code=response.status_code,
            )
        except httpx.TimeoutException:
            logger.error("razorpay_invoice_verification_timeout", extra={"invoice_id": invoice_id})
            return InvoiceVerificationResponse(
                success=False,
                error_message="Razorpay invoice verification request timed out.",
            )
        except httpx.HTTPError:
            logger.error("razorpay_invoice_verification_http_error", extra={"invoice_id": invoice_id})
            return InvoiceVerificationResponse(
                success=False,
                error_message="Razorpay invoice verification HTTP error.",
            )
        except Exception:
            logger.exception("razorpay_invoice_verification_unexpected_error", extra={"invoice_id": invoice_id})
            return InvoiceVerificationResponse(
                success=False,
                error_message="Unexpected Razorpay invoice verification error.",
            )

"""Razorpay API client — payment link operations.

Thin wrapper around the Razorpay Payment Links API.
Uses the existing RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET from settings.

API reference:
    POST https://api.razorpay.com/v1/payment_links/
    Auth: Basic Auth (key_id:key_secret)

This module NEVER:
- logs API keys or authorization headers
- fabricates a successful payment
- claims money was recovered

It only creates the payment link and returns the provider response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Razorpay Payment Links API endpoint.
_PAYMENT_LINKS_URL = "https://api.razorpay.com/v1/payment_links/"


@dataclass(frozen=True)
class PaymentLinkResponse:
    """Structured response from the Razorpay Payment Links API."""

    success: bool
    payment_link_id: str | None = None
    short_url: str | None = None
    status: str | None = None
    raw_response: dict[str, Any] | None = None
    error_message: str | None = None
    http_status_code: int | None = None


class RazorpayPaymentLinkClient:
    """Client for the Razorpay Payment Links API.

    Uses Basic Auth with the Razorpay key_id and key_secret.
    Does NOT store or log credentials.
    """

    def __init__(self, key_id: str, key_secret: str) -> None:
        self._auth = (key_id, key_secret)

    def create_payment_link(
        self,
        *,
        amount_minor: int,
        currency: str,
        description: str,
        reference_id: str | None = None,
        notes: dict[str, str] | None = None,
    ) -> PaymentLinkResponse:
        """Create a payment link via the Razorpay API.

        Args:
            amount_minor: Amount in minor currency units (paise for INR).
            currency: ISO 4217 currency code.
            description: Description shown on the payment page.
            reference_id: Optional reference for tracking.
            notes: Optional key-value metadata attached to the link.

        Returns:
            PaymentLinkResponse with the API outcome.
        """
        payload: dict[str, Any] = {
            "amount": amount_minor,
            "currency": currency,
            "description": description,
            "accept_partial": False,
        }

        if reference_id:
            payload["reference_id"] = reference_id

        if notes:
            payload["notes"] = notes

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    _PAYMENT_LINKS_URL,
                    json=payload,
                    auth=self._auth,
                )

            if response.status_code in (200, 201):
                data = response.json()
                return PaymentLinkResponse(
                    success=True,
                    payment_link_id=data.get("id"),
                    short_url=data.get("short_url"),
                    status=data.get("status"),
                    raw_response=data,
                    http_status_code=response.status_code,
                )

            # Non-2xx response — structured failure.
            error_body = None
            try:
                error_body = response.json()
            except Exception:
                pass

            error_msg = "Razorpay API error"
            if isinstance(error_body, dict):
                error_detail = error_body.get("error", {})
                if isinstance(error_detail, dict):
                    error_msg = error_detail.get("description", error_msg)

            logger.warning(
                "razorpay_payment_link_api_error",
                extra={
                    "http_status": response.status_code,
                    "error_message": error_msg,
                    # Do NOT log the full response — may contain sensitive data.
                },
            )

            return PaymentLinkResponse(
                success=False,
                error_message=error_msg,
                http_status_code=response.status_code,
            )

        except httpx.TimeoutException:
            logger.error("razorpay_payment_link_timeout")
            return PaymentLinkResponse(
                success=False,
                error_message="Razorpay API request timed out.",
            )
        except httpx.HTTPError as exc:
            logger.error(
                "razorpay_payment_link_http_error",
                extra={"error": str(exc)},
            )
            return PaymentLinkResponse(
                success=False,
                error_message=f"HTTP error: {exc}",
            )
        except Exception as exc:
            logger.exception("razorpay_payment_link_unexpected_error")
            return PaymentLinkResponse(
                success=False,
                error_message=f"Unexpected error: {exc}",
            )

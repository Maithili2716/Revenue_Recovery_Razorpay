"""Thin Razorpay Orders API client used only by the checkout demo."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ORDERS_URL = "https://api.razorpay.com/v1/orders"


@dataclass(frozen=True)
class OrderResponse:
    """Structured response from Razorpay's Orders API."""

    success: bool
    order_id: str | None = None
    amount: int | None = None
    currency: str | None = None
    error_message: str | None = None
    http_status_code: int | None = None


class RazorpayOrderClient:
    """Create Razorpay Test Mode orders without altering Payment Links behavior."""

    def __init__(self, key_id: str, key_secret: str) -> None:
        self._auth = (key_id, key_secret)

    def create_order(
        self, *, amount_minor: int, currency: str, receipt: str
    ) -> OrderResponse:
        payload: dict[str, Any] = {
            "amount": amount_minor,
            "currency": currency,
            "receipt": receipt,
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(_ORDERS_URL, json=payload, auth=self._auth)

            if response.status_code in (200, 201):
                data = response.json()
                return OrderResponse(
                    success=True,
                    order_id=data.get("id"),
                    amount=data.get("amount"),
                    currency=data.get("currency"),
                    http_status_code=response.status_code,
                )

            error_message = _error_message(response)
            logger.warning(
                "razorpay_order_api_error",
                extra={
                    "http_status": response.status_code,
                    "error_message": error_message,
                },
            )
            return OrderResponse(
                success=False,
                error_message=error_message,
                http_status_code=response.status_code,
            )
        except httpx.TimeoutException:
            logger.error("razorpay_order_timeout")
            return OrderResponse(
                success=False,
                error_message="Razorpay API request timed out.",
            )
        except httpx.HTTPError as exc:
            logger.error("razorpay_order_http_error", extra={"error": str(exc)})
            return OrderResponse(success=False, error_message=f"HTTP error: {exc}")
        except Exception as exc:
            logger.exception("razorpay_order_unexpected_error")
            return OrderResponse(
                success=False,
                error_message=f"Unexpected error: {exc}",
            )


def _error_message(response: httpx.Response) -> str:
    """Extract a safe provider error description without logging its body."""
    try:
        body = response.json()
    except Exception:
        return "Razorpay API error"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            description = error.get("description")
            if isinstance(description, str) and description:
                return description
    return "Razorpay API error"

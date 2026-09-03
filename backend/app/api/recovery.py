"""Recovery API endpoints.

Provides:
    POST /recovery/{case_id}/verify — Manual verification fallback

The manual verification endpoint allows operators to trigger independent
verification for a pending recovery case when webhooks are delayed or lost.

This endpoint does NOT bypass the verification engine — it triggers the
same independent Razorpay API verification that the webhook handler uses.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.dashboard import RecoveryCasesResponse, recovery_cases_response
from app.signals.service import verify_case_manually

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recovery", tags=["recovery"])


class ManualVerificationResponse(BaseModel):
    """Response from the manual verification endpoint."""

    case_id: str
    payment_link_id: str | None = None
    verification_status: str | None = None
    amount_recovered_minor: int = 0
    learning_updated: bool = False
    message: str = ""


@router.get(
    "/cases",
    response_model=RecoveryCasesResponse,
    summary="List known recovery cases from in-memory application state",
)
def list_recovery_cases() -> RecoveryCasesResponse:
    """Return recent cases without producing or changing recovery state."""
    return recovery_cases_response()


@router.post(
    "/{case_id}/verify",
    response_model=ManualVerificationResponse,
    summary="Manually trigger verification for a pending recovery case",
)
def manual_verify(case_id: str) -> ManualVerificationResponse:
    """Manually trigger independent verification for a pending recovery.

    Use this endpoint when:
    - The payment_link.paid webhook has not arrived
    - You want to check the current status of a recovery case
    - You need a reliable fallback for verification

    The endpoint independently verifies the payment link status via the
    Razorpay API — it does NOT trust cached state or webhook events.
    """
    logger.info(
        "manual_verification_request",
        extra={"case_id": case_id},
    )

    result = verify_case_manually(case_id)

    if result.verification_status is None and result.payment_link_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.message,
        )

    return ManualVerificationResponse(
        case_id=case_id,
        payment_link_id=result.payment_link_id,
        verification_status=result.verification_status,
        amount_recovered_minor=result.amount_recovered_minor,
        learning_updated=result.learning_updated,
        message=result.message,
    )

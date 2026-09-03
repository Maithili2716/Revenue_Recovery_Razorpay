import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.config import settings
from app.integrations.razorpay.events import (
    RazorpayWebhookEvent,
    extract_event_id,
    extract_event_type,
)
from app.integrations.razorpay.idempotency import (
    InMemoryWebhookIdempotencyStore,
    WebhookIdempotencyStore,
)
from app.integrations.razorpay.signature import verify_webhook_signature
from app.signals.router import is_recovery_event
from app.signals.service import (
    handle_recovery_webhook,
    ingest_webhook_event_background,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

_idempotency_store = InMemoryWebhookIdempotencyStore()


def get_idempotency_store() -> WebhookIdempotencyStore:
    return _idempotency_store


class WebhookReceiptResponse(BaseModel):
    status: Literal["accepted", "duplicate"]
    event_type: str | None = None
    event_id: str | None = None


@router.post(
    "/webhooks/razorpay",
    response_model=WebhookReceiptResponse,
    summary="Receive Razorpay webhook events",
)
async def receive_razorpay_webhook(
    request: Request,
    store: WebhookIdempotencyStore = Depends(get_idempotency_store),
) -> WebhookReceiptResponse:
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")

    if not signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Razorpay-Signature header",
        )

    if not verify_webhook_signature(
        raw_body,
        signature,
        settings.razorpay_webhook_secret,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    event_type = extract_event_type(payload)
    event_id = extract_event_id(payload, request.headers)
    webhook_event = RazorpayWebhookEvent(
        event_type=event_type,
        event_id=event_id,
        raw_body=raw_body,
        payload=payload,
    )

    is_duplicate = False
    if event_id is not None:
        is_duplicate = not store.record(event_id)
    else:
        logger.warning(
            "razorpay_webhook_missing_event_id",
            extra={"event_type": event_type},
        )

    response_status: Literal["accepted", "duplicate"] = (
        "duplicate" if is_duplicate else "accepted"
    )

    logger.info(
        "razorpay_webhook_received",
        extra={
            "event_type": webhook_event.event_type,
            "event_id": webhook_event.event_id,
            "status": response_status,
            "payload_bytes": len(webhook_event.raw_body),
            # raw_payload intentionally omitted: may contain PII.
        },
    )

    # Dispatch new events to the appropriate background handler.
    # The HTTP 200 is returned IMMEDIATELY — the webhook does NOT wait
    # for signal normalization, LLM diagnosis, verification, or learning.
    if not is_duplicate:
        if is_recovery_event(event_type):
            # Recovery events (payment_link.paid, etc.) → recovery verification
            asyncio.create_task(handle_recovery_webhook(webhook_event))
        else:
            # Signal events (payment.failed) → signal normalization pipeline
            asyncio.create_task(ingest_webhook_event_background(webhook_event))

    return WebhookReceiptResponse(
        status=response_status,
        event_type=event_type,
        event_id=event_id,
    )

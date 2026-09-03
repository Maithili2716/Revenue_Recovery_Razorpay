"""Payment Link Reminder capability.

Implements the RecoveryCapability interface for the payment_link_reminder
action.  Uses the Razorpay Payment Link notification API to resend/remind
the customer about an existing payment link.

API:
    POST /v1/payment_links/{payment_link_id}/notify_by/{medium}
    Supported mediums: "sms", "email"

Flow:
    ExecutionContext (with decision_context containing payment_link_id, medium)
        → validate inputs (payment_link_id required)
        → call Razorpay notify_by API
        → return ExecutionResult

CRITICAL:
    status=EXECUTED means the notification was accepted by Razorpay.
    It does NOT mean money was recovered.
    Recovery is determined by the existing Payment Link verification path
    (payment_link.paid → GET Payment Link → RECOVERED if paid).
"""

from __future__ import annotations

import logging

from app.integrations.razorpay.client import RazorpayPaymentLinkClient
from app.recovery.capabilities.models import (
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    RecoveryCapability,
)

logger = logging.getLogger(__name__)

# Default notification medium for the hackathon.
_DEFAULT_MEDIUM = "email"

# Supported notification mediums.
_SUPPORTED_MEDIUMS = frozenset({"sms", "email"})


class PaymentLinkReminderCapability(RecoveryCapability):
    """Sends a reminder/notification for an existing Razorpay payment link.

    The capability receives an ExecutionContext with the payment_link_id
    in the decision_context and calls the Razorpay notify_by API.

    It never:
    - creates a new payment link
    - fabricates a successful payment
    - claims money was recovered
    - bypasses the policy layer (that is the executor's responsibility)
    - exposes API secrets
    """

    def __init__(self, razorpay_client: RazorpayPaymentLinkClient) -> None:
        self._client = razorpay_client

    @property
    def capability_id(self) -> str:
        return "payment_link_reminder"

    @property
    def action_type(self) -> str:
        return "send_payment_link_reminder"

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        """Send a payment link reminder and return a structured ExecutionResult.

        Expects decision_context to contain:
        - payment_link_id: The Razorpay payment link ID to notify about.
        - medium (optional): "sms" or "email". Defaults to "email".

        Returns status=FAILED when payment_link_id is missing or API fails.
        Returns status=EXECUTED when the notification was accepted.
        """
        # --- Extract parameters from decision_context ---
        payment_link_id = context.decision_context.get("payment_link_id")
        medium = context.decision_context.get("medium", _DEFAULT_MEDIUM)

        # --- Input validation ---
        validation_error = self._validate(context, payment_link_id, medium)
        if validation_error:
            logger.warning(
                "capability_execution_validation_failed",
                extra={
                    "capability_id": self.capability_id,
                    "case_id": context.case_id,
                    "error": validation_error,
                },
            )
            return ExecutionResult(
                case_id=context.case_id,
                decision_id=context.decision_id,
                capability_id=self.capability_id,
                action_type=self.action_type,
                status=ExecutionStatus.FAILED,
                error_message=validation_error,
            )

        # --- Log: execution start ---
        logger.info(
            "capability_execution_started",
            extra={
                "case_id": context.case_id,
                "execution_id": "pending",
                "decision_id": context.decision_id,
                "capability_id": self.capability_id,
                "action_type": self.action_type,
                "payment_link_id": payment_link_id,
                "medium": medium,
                "merchant_id": context.merchant_id,
            },
        )

        # --- Log: Razorpay API call ---
        logger.info(
            "razorpay_payment_link_notification_requested",
            extra={
                "payment_link_id": payment_link_id,
                "medium": medium,
            },
        )

        # --- Call Razorpay notify_by API ---
        try:
            api_response = self._client.notify_by(
                payment_link_id=payment_link_id,
                medium=medium,
            )
        except Exception as exc:
            logger.exception(
                "capability_execution_completed",
                extra={
                    "case_id": context.case_id,
                    "capability_id": self.capability_id,
                    "payment_link_id": payment_link_id,
                    "medium": medium,
                    "status": "failed",
                    "provider_success": False,
                    "error": str(exc),
                },
            )
            return ExecutionResult(
                case_id=context.case_id,
                decision_id=context.decision_id,
                capability_id=self.capability_id,
                action_type=self.action_type,
                status=ExecutionStatus.FAILED,
                error_message=f"Razorpay notify API call failed: {exc}",
            )

        # --- Build ExecutionResult ---
        if api_response.success:
            result = ExecutionResult(
                case_id=context.case_id,
                decision_id=context.decision_id,
                capability_id=self.capability_id,
                action_type=self.action_type,
                status=ExecutionStatus.EXECUTED,
                provider="razorpay",
                provider_reference=payment_link_id,
                metadata={
                    "payment_link_id": payment_link_id,
                    "medium": medium,
                    "notification_accepted": True,
                },
            )
            # --- Log: execution completion (success) ---
            logger.info(
                "capability_execution_completed",
                extra={
                    "case_id": context.case_id,
                    "execution_id": result.execution_id,
                    "decision_id": context.decision_id,
                    "capability_id": self.capability_id,
                    "action_type": self.action_type,
                    "payment_link_id": payment_link_id,
                    "medium": medium,
                    "status": result.status.value,
                    "provider_success": True,
                },
            )

            # --- Log: verification boundary ---
            # Reminder execution does NOT constitute recovery.
            # The existing Payment Link verification path remains responsible.
            logger.info(
                "recovery_status_unchanged",
                extra={
                    "case_id": context.case_id,
                    "execution_id": result.execution_id,
                    "capability_id": self.capability_id,
                    "payment_link_id": payment_link_id,
                    "reason": "reminder_sent_waiting_for_payment_link_paid",
                    "verification_status": "pending",
                },
            )

            return result

        # API call returned a structured failure.
        result = ExecutionResult(
            case_id=context.case_id,
            decision_id=context.decision_id,
            capability_id=self.capability_id,
            action_type=self.action_type,
            status=ExecutionStatus.FAILED,
            provider="razorpay",
            error_message=api_response.error_message,
            metadata={
                "payment_link_id": payment_link_id,
                "medium": medium,
                "http_status_code": api_response.http_status_code,
            },
        )
        # --- Log: execution completion (failure) ---
        logger.warning(
            "capability_execution_completed",
            extra={
                "case_id": context.case_id,
                "execution_id": result.execution_id,
                "decision_id": context.decision_id,
                "capability_id": self.capability_id,
                "action_type": self.action_type,
                "payment_link_id": payment_link_id,
                "medium": medium,
                "status": result.status.value,
                "provider_success": False,
                "error_message": result.error_message,
            },
        )
        return result

    @staticmethod
    def _validate(
        context: ExecutionContext,
        payment_link_id: str | None,
        medium: str,
    ) -> str | None:
        """Validate the required inputs. Returns an error string or None."""
        if not context.case_id:
            return "Case ID is required."
        if not context.merchant_id:
            return "Merchant ID is required."
        if not payment_link_id:
            return (
                "payment_link_id is required in decision_context. "
                "Cannot send a reminder without an existing payment link."
            )
        if medium not in _SUPPORTED_MEDIUMS:
            return (
                f"Unsupported notification medium: '{medium}'. "
                f"Must be one of: {', '.join(sorted(_SUPPORTED_MEDIUMS))}."
            )
        return None

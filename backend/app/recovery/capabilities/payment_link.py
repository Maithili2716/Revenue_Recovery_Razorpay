"""Payment Link Recovery capability.

Implements the RecoveryCapability interface for the payment_link_recovery
action.  Uses the Razorpay Payment Links API to create a payment link
that allows the customer to retry their failed payment.

Flow:
    ExecutionContext
        → validate inputs
        → build Razorpay payment-link request
        → call Razorpay Payment Links API
        → capture provider response
        → return ExecutionResult

CRITICAL:
    status=EXECUTED means a payment link was created.
    It does NOT mean money was recovered.
    Recovery is determined by the Verification Engine (future block).
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


class PaymentLinkRecoveryCapability(RecoveryCapability):
    """Creates a Razorpay payment link for a failed payment.

    The capability receives a well-defined ExecutionContext (amount,
    currency, merchant, case identifiers) and calls the Razorpay
    Payment Links API.

    It never:
    - fabricates a successful payment
    - claims money was recovered
    - bypasses the policy layer (that is the executor's responsibility)
    - exposes API secrets
    """

    def __init__(self, razorpay_client: RazorpayPaymentLinkClient) -> None:
        self._client = razorpay_client

    @property
    def capability_id(self) -> str:
        return "payment_link_recovery"

    @property
    def action_type(self) -> str:
        return "create_payment_link"

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        """Create a payment link and return a structured ExecutionResult.

        Validates inputs before calling the Razorpay API.
        Returns status=FAILED on any error — never crashes the pipeline.
        """
        # --- Input validation ---
        validation_error = self._validate(context)
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

        # --- Build payment-link request ---
        description = (
            f"Recovery payment for case {context.case_id[:16]}... "
            f"({context.amount_minor} {context.currency} minor units)"
        )
        reference_id = context.case_id[:40]  # Razorpay limits reference_id length.
        notes = {
            "case_id": context.case_id,
            "decision_id": context.decision_id,
            "capability_id": self.capability_id,
            "recovery_system": "adaptive_revenue_recovery",
        }

        logger.info(
            "capability_execution_started",
            extra={
                "case_id": context.case_id,
                "decision_id": context.decision_id,
                "capability_id": self.capability_id,
                "action_type": self.action_type,
                "amount_minor": context.amount_minor,
                "currency": context.currency,
                "merchant_id": context.merchant_id,
            },
        )

        # --- Call Razorpay Payment Links API ---
        try:
            api_response = self._client.create_payment_link(
                amount_minor=context.amount_minor,
                currency=context.currency,
                description=description,
                reference_id=reference_id,
                notes=notes,
            )
        except Exception as exc:
            logger.exception(
                "capability_execution_failed",
                extra={
                    "case_id": context.case_id,
                    "capability_id": self.capability_id,
                    "error": str(exc),
                },
            )
            return ExecutionResult(
                case_id=context.case_id,
                decision_id=context.decision_id,
                capability_id=self.capability_id,
                action_type=self.action_type,
                status=ExecutionStatus.FAILED,
                error_message=f"Razorpay API call failed: {exc}",
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
                provider_reference=api_response.payment_link_id,
                payment_link_url=api_response.short_url,
                metadata={
                    "short_url": api_response.short_url,
                    "payment_link_status": api_response.status,
                },
            )
            logger.info(
                "capability_execution_completed",
                extra={
                    "case_id": context.case_id,
                    "decision_id": context.decision_id,
                    "capability_id": self.capability_id,
                    "action_type": self.action_type,
                    "execution_id": result.execution_id,
                    "status": result.status.value,
                    "provider_reference": result.provider_reference,
                    "payment_link_url": result.payment_link_url,
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
                "http_status_code": api_response.http_status_code,
            },
        )
        logger.warning(
            "capability_execution_failed",
            extra={
                "case_id": context.case_id,
                "decision_id": context.decision_id,
                "capability_id": self.capability_id,
                "execution_id": result.execution_id,
                "status": result.status.value,
                "error_message": result.error_message,
            },
        )
        return result

    @staticmethod
    def _validate(context: ExecutionContext) -> str | None:
        """Validate the execution context.  Returns an error string or None."""
        if context.amount_minor <= 0:
            return f"Invalid amount: {context.amount_minor}; must be positive."
        if not context.currency:
            return "Currency is required."
        if not context.merchant_id:
            return "Merchant ID is required."
        if not context.case_id:
            return "Case ID is required."
        return None

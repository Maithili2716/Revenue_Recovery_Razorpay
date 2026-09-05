"""Razorpay invoice recovery capability.

Creates and issues a real Razorpay invoice for an existing RecoveryCase. An
EXECUTED result means the invoice is ready for payment; it never means the
revenue was recovered.
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


class InvoiceRecoveryCapability(RecoveryCapability):
    """Create and issue a Razorpay invoice for a recovery case."""

    def __init__(self, razorpay_client: RazorpayPaymentLinkClient) -> None:
        self._client = razorpay_client

    @property
    def capability_id(self) -> str:
        return "invoice_recovery"

    @property
    def action_type(self) -> str:
        return "create_invoice"

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        validation_error = self._validate(context)
        if validation_error:
            return self._failed(context, validation_error)

        description = f"RecoveryLab invoice for case {context.case_id[:24]}"
        notes = {
            "case_id": context.case_id,
            "decision_id": context.decision_id,
            "capability_id": self.capability_id,
            "recovery_system": "adaptive_revenue_recovery",
        }
        logger.info(
            "invoice_recovery_execution_started",
            extra={
                "case_id": context.case_id,
                "decision_id": context.decision_id,
                "capability_id": self.capability_id,
                "amount_minor": context.amount_minor,
                "currency": context.currency,
                "has_customer_id": context.customer_id is not None,
            },
        )

        try:
            created = self._client.create_invoice(
                amount_minor=context.amount_minor,
                currency=context.currency,
                description=description,
                notes=notes,
                customer_id=context.customer_id,
            )
        except Exception:
            logger.exception(
                "invoice_recovery_execution_exception",
                extra={"case_id": context.case_id, "decision_id": context.decision_id},
            )
            return self._failed(context, "Razorpay invoice create request failed.")

        if not created.success or not created.invoice_id:
            return self._failed(
                context,
                created.error_message or "Razorpay did not create an invoice.",
                http_status_code=created.http_status_code,
            )

        invoice_id = created.invoice_id
        ready = created
        if created.status != "issued":
            try:
                ready = self._client.issue_invoice(invoice_id=invoice_id)
            except Exception:
                logger.exception(
                    "invoice_recovery_issue_exception",
                    extra={"case_id": context.case_id, "invoice_id": invoice_id},
                )
                return self._failed(context, "Razorpay invoice issue request failed.")
            if not ready.success:
                return self._failed(
                    context,
                    ready.error_message or "Razorpay did not issue the invoice.",
                    http_status_code=ready.http_status_code,
                )

        if ready.status != "issued":
            return self._failed(
                context,
                "Razorpay invoice is not in issued status.",
                http_status_code=ready.http_status_code,
            )

        provider_reference = ready.invoice_id or invoice_id
        if not provider_reference.startswith("inv_"):
            return self._failed(context, "Razorpay returned an invalid invoice reference.")

        result = ExecutionResult(
            case_id=context.case_id,
            decision_id=context.decision_id,
            capability_id=self.capability_id,
            action_type=self.action_type,
            status=ExecutionStatus.EXECUTED,
            provider="razorpay",
            provider_reference=provider_reference,
            payment_link_url=ready.payment_url or created.payment_url,
            metadata={
                "invoice_id": provider_reference,
                "invoice_status": ready.status,
                "invoice_payment_url": ready.payment_url or created.payment_url,
            },
        )
        logger.info(
            "invoice_created",
            extra={
                "case_id": context.case_id,
                "decision_id": context.decision_id,
                "execution_id": result.execution_id,
                "invoice_id": provider_reference,
                "status": ready.status,
                "amount_minor": context.amount_minor,
                "currency": context.currency,
            },
        )
        return result

    def _failed(
        self,
        context: ExecutionContext,
        error_message: str,
        *,
        http_status_code: int | None = None,
    ) -> ExecutionResult:
        logger.warning(
            "invoice_recovery_execution_failed",
            extra={
                "case_id": context.case_id,
                "decision_id": context.decision_id,
                "capability_id": self.capability_id,
                "error_message": error_message,
                "http_status_code": http_status_code,
            },
        )
        return ExecutionResult(
            case_id=context.case_id,
            decision_id=context.decision_id,
            capability_id=self.capability_id,
            action_type=self.action_type,
            status=ExecutionStatus.FAILED,
            provider="razorpay",
            error_message=error_message,
            metadata={"http_status_code": http_status_code},
        )

    @staticmethod
    def _validate(context: ExecutionContext) -> str | None:
        if context.amount_minor <= 0:
            return "Invoice amount must be positive."
        if context.currency != "INR":
            return "Invoice recovery currently supports INR only."
        if not context.merchant_id:
            return "Merchant ID is required."
        if not context.case_id or not context.decision_id:
            return "Case ID and decision ID are required."
        return None

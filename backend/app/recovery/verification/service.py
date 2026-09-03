"""Verification service — orchestrates verification for execution results.

Flow:
    ExecutionResult
        → determine provider + payment_link_id
        → fetch status from provider
        → interpret into VerifiedOutcome

This service is the single entry point for verification.
It does NOT execute recovery actions — only determines truth.

Rules:
    RECOVERED:     provider confirms paid AND amount_paid > 0
    PENDING:       provider status is 'created' — customer has not yet acted
    NOT_RECOVERED: provider status is terminal (expired/cancelled)
    UNKNOWN:       any ambiguity or API failure
"""

from __future__ import annotations

import logging

from app.recovery.capabilities.models import ExecutionResult, ExecutionStatus
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome
from app.recovery.verification.razorpay import (
    PaymentLinkVerificationResponse,
    VerificationProvider,
)

logger = logging.getLogger(__name__)


class VerificationService:
    """Orchestrates independent verification of execution results."""

    def __init__(self, provider: VerificationProvider) -> None:
        self._provider = provider

    def verify(
        self,
        execution_result: ExecutionResult,
        amount_at_risk_minor: int,
        currency: str,
    ) -> VerifiedOutcome:
        """Verify whether a recovery execution actually recovered money.

        Args:
            execution_result: The result of capability execution.
            amount_at_risk_minor: Original amount at risk (minor units).
            currency: ISO 4217 currency code.

        Returns:
            A VerifiedOutcome with the financial truth.
        """
        # Only verify EXECUTED results with a provider reference.
        if execution_result.status != ExecutionStatus.EXECUTED:
            return VerifiedOutcome(
                case_id=execution_result.case_id,
                execution_id=execution_result.execution_id,
                capability_id=execution_result.capability_id,
                provider=execution_result.provider,
                provider_reference=execution_result.provider_reference,
                status=VerificationStatus.NOT_RECOVERED,
                amount_at_risk_minor=amount_at_risk_minor,
                amount_recovered_minor=0,
                currency=currency,
                reason=(
                    f"Execution status was {execution_result.status.value}; "
                    "no recovery action was performed."
                ),
            )

        if not execution_result.provider_reference:
            return VerifiedOutcome(
                case_id=execution_result.case_id,
                execution_id=execution_result.execution_id,
                capability_id=execution_result.capability_id,
                provider=execution_result.provider,
                status=VerificationStatus.UNKNOWN,
                amount_at_risk_minor=amount_at_risk_minor,
                amount_recovered_minor=0,
                currency=currency,
                reason="No provider_reference available for verification.",
            )

        logger.info(
            "verification_started",
            extra={
                "case_id": execution_result.case_id,
                "execution_id": execution_result.execution_id,
                "provider_reference": execution_result.provider_reference,
                "capability_id": execution_result.capability_id,
            },
        )

        # Fetch from provider.
        response = self._provider.fetch_payment_link(
            execution_result.provider_reference
        )

        # Interpret the response.
        outcome = self._interpret(
            execution_result=execution_result,
            response=response,
            amount_at_risk_minor=amount_at_risk_minor,
            currency=currency,
        )

        logger.info(
            "verification_completed",
            extra={
                "case_id": outcome.case_id,
                "execution_id": outcome.execution_id,
                "provider_reference": outcome.provider_reference,
                "status": outcome.status.value,
                "amount_recovered_minor": outcome.amount_recovered_minor,
                "amount_at_risk_minor": outcome.amount_at_risk_minor,
                "reason": outcome.reason,
            },
        )

        return outcome

    def _interpret(
        self,
        *,
        execution_result: ExecutionResult,
        response: PaymentLinkVerificationResponse,
        amount_at_risk_minor: int,
        currency: str,
    ) -> VerifiedOutcome:
        """Interpret a provider response into a VerifiedOutcome."""

        base_kwargs = dict(
            case_id=execution_result.case_id,
            execution_id=execution_result.execution_id,
            capability_id=execution_result.capability_id,
            provider=execution_result.provider,
            provider_reference=execution_result.provider_reference,
            amount_at_risk_minor=amount_at_risk_minor,
            currency=currency,
        )

        # API failure → UNKNOWN.
        if not response.success:
            return VerifiedOutcome(
                **base_kwargs,
                status=VerificationStatus.UNKNOWN,
                amount_recovered_minor=0,
                reason=f"Provider API error: {response.error_message}",
                evidence={"http_status_code": response.http_status_code},
            )

        # Paid status with amount_paid > 0 → RECOVERED.
        status = (response.status or "").lower()
        amount_paid = response.amount_paid or 0

        if status == "paid" and amount_paid > 0:
            # Recovered amount must not exceed amount_at_risk.
            recovered = min(amount_paid, amount_at_risk_minor)

            # Extract the first captured payment ID if available.
            payment_id = self._extract_payment_id(response.payments)

            evidence = {
                "payment_link_status": response.status,
                "amount": response.amount,
                "amount_paid": response.amount_paid,
            }
            if payment_id:
                evidence["payment_id"] = payment_id

            return VerifiedOutcome(
                **base_kwargs,
                status=VerificationStatus.RECOVERED,
                amount_recovered_minor=recovered,
                provider_payment_id=payment_id,
                reason=f"Payment link paid: {recovered} {currency} recovered.",
                evidence=evidence,
            )

        # 'created' status — payment link exists, customer has not acted yet.
        # This is PENDING, NOT failure.
        if status == "created":
            return VerifiedOutcome(
                **base_kwargs,
                status=VerificationStatus.PENDING,
                amount_recovered_minor=0,
                reason="Payment link status is 'created'; awaiting customer action.",
                evidence={
                    "payment_link_status": response.status,
                    "amount": response.amount,
                    "amount_paid": response.amount_paid,
                },
            )

        # Terminal unpaid statuses → NOT_RECOVERED.
        if status in ("expired", "cancelled"):
            return VerifiedOutcome(
                **base_kwargs,
                status=VerificationStatus.NOT_RECOVERED,
                amount_recovered_minor=0,
                reason=f"Payment link status is '{status}'; terminal — no payment captured.",
                evidence={
                    "payment_link_status": response.status,
                    "amount": response.amount,
                    "amount_paid": response.amount_paid,
                },
            )

        # Partially paid — check if any amount was actually collected.
        if status == "partially_paid" and amount_paid > 0:
            recovered = min(amount_paid, amount_at_risk_minor)
            payment_id = self._extract_payment_id(response.payments)
            evidence = {
                "payment_link_status": response.status,
                "amount": response.amount,
                "amount_paid": response.amount_paid,
            }
            if payment_id:
                evidence["payment_id"] = payment_id
            return VerifiedOutcome(
                **base_kwargs,
                status=VerificationStatus.RECOVERED,
                amount_recovered_minor=recovered,
                provider_payment_id=payment_id,
                reason=f"Payment link partially paid: {recovered} {currency} recovered.",
                evidence=evidence,
            )

        # Unexpected status → UNKNOWN.
        return VerifiedOutcome(
            **base_kwargs,
            status=VerificationStatus.UNKNOWN,
            amount_recovered_minor=0,
            reason=f"Unexpected payment link status: '{status}'.",
            evidence={
                "payment_link_status": response.status,
                "amount": response.amount,
                "amount_paid": response.amount_paid,
            },
        )

    @staticmethod
    def _extract_payment_id(
        payments: list[dict] | None,
    ) -> str | None:
        """Extract the first captured payment ID from the payments array."""
        if not payments:
            return None
        for payment in payments:
            # Razorpay payment objects have 'payment_id' at top level
            # or nested under 'entity'.
            pid = payment.get("payment_id")
            if pid:
                return pid
            entity = payment.get("entity", {})
            if isinstance(entity, dict):
                pid = entity.get("id")
                if pid:
                    return pid
        return None

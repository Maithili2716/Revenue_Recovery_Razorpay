"""Deterministic diagnosis engine.

Produces a structured Diagnosis from an AgentContext using rule-based logic.
No LLM, no ML — just explicit if/else rules.

This is a component INSIDE the Adaptive Recovery Agent, not a standalone
top-level workflow.
"""

from __future__ import annotations

import logging

from app.recovery.agent.models import (
    AgentContext,
    Diagnosis,
    DiagnosisCategory,
    FailureStage,
)

logger = logging.getLogger(__name__)

# Mapping from Razorpay failure_step to our internal FailureStage enum.
_FAILURE_STEP_MAP: dict[str, FailureStage] = {
    "payment_authorization": FailureStage.PAYMENT_AUTHORIZATION,
    "payment_processing": FailureStage.PAYMENT_PROCESSING,
    "payment_capture": FailureStage.PAYMENT_CAPTURE,
}

# Mapping from failure_source to a human-readable primary_reason.
_SOURCE_TO_REASON: dict[str, str] = {
    "bank": "bank_decline",
    "customer": "customer_dropout",
    "gateway": "gateway_error",
    "internal": "internal_error",
    "issuer": "issuer_decline",
}


def diagnose(context: AgentContext) -> Diagnosis:
    """Produce a structured diagnosis from the agent context.

    Currently supports payment_failure signals only.  Unknown signal types
    receive a generic UNKNOWN diagnosis with reduced confidence.
    """
    if context.signal_type == "payment_failure":
        return _diagnose_payment_failure(context)

    # Fallback for future signal types not yet handled.
    diagnosis = Diagnosis(
        category=DiagnosisCategory.UNKNOWN,
        primary_reason="unknown_signal_type",
        failure_stage=FailureStage.UNKNOWN,
        confidence=0.3,
        reason_codes=context.reason_codes,
        details=f"No diagnosis rules for signal_type={context.signal_type}.",
    )
    _log_diagnosis(context, diagnosis)
    return diagnosis


def _diagnose_payment_failure(context: AgentContext) -> Diagnosis:
    """Apply deterministic rules for payment_failure signals."""

    # Determine failure stage from context.failure_step.
    failure_stage = _FAILURE_STEP_MAP.get(
        context.failure_step or "", FailureStage.UNKNOWN
    )

    # Determine primary reason from context.failure_source.
    primary_reason = _SOURCE_TO_REASON.get(
        (context.failure_source or "").lower(), "unknown_failure"
    )

    # Confidence: high when both source and step are present.
    has_source = context.failure_source is not None
    has_step = context.failure_step is not None
    if has_source and has_step:
        confidence = 0.9
    elif has_source or has_step:
        confidence = 0.7
    else:
        confidence = 0.4

    reason_codes = list(context.reason_codes)
    if primary_reason not in reason_codes:
        reason_codes.append(primary_reason)

    diagnosis = Diagnosis(
        category=DiagnosisCategory.PAYMENT_FAILURE,
        primary_reason=primary_reason,
        failure_stage=failure_stage,
        confidence=confidence,
        reason_codes=reason_codes,
        details=(
            f"Payment failed at {failure_stage.value} "
            f"due to {primary_reason} "
            f"(source={context.failure_source}, step={context.failure_step})."
        ),
    )

    _log_diagnosis(context, diagnosis)
    return diagnosis


def _log_diagnosis(context: AgentContext, diagnosis: Diagnosis) -> None:
    logger.info(
        "agent_diagnosis_created",
        extra={
            "case_id": context.case_id,
            "category": diagnosis.category.value,
            "primary_reason": diagnosis.primary_reason,
            "failure_stage": diagnosis.failure_stage.value,
            "confidence": diagnosis.confidence,
            "reason_codes": diagnosis.reason_codes,
        },
    )

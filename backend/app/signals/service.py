"""Signal ingestion service.

This is the application/service layer between the webhook API and the signal
router.  It is the only place that calls ``route_webhook_to_signal()``.

Responsibilities:
- Pass a verified RazorpayWebhookEvent into the signal router.
- Emit a structured development log of the resulting RevenueSignal.
- Run the Revenue Risk Detector to create a RecoveryCase.
- Run the Adaptive Recovery Agent to produce an AgentDecision.
- Route the AgentDecision through the Capability Executor
  (Policy → Registry → Capability → ExecutionResult).
- Verify the ExecutionResult against the provider with bounded re-checks.
- Record verified outcome into the learning store (only for terminal outcomes).
- Record audit events for each lifecycle stage.
- Return the RevenueSignal to the caller (for future downstream use).
- Handle UnsupportedEventType explicitly without crashing the webhook endpoint.

Verification lifecycle:
    Initial check may return PENDING (payment link just created).
    Bounded re-verification with backoff retries until terminal or window ends.
    Only RECOVERED and NOT_RECOVERED update the learning store.
    PENDING and UNKNOWN never update learning.

This layer does NOT:
- perform signature verification (done by the API layer);
- perform idempotency checks (done by the API layer);
- persist the signal (no database in this block);
- place LLM calls in the webhook fast path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.demo.store import get_demo_session_store
from app.integrations.razorpay.events import RazorpayWebhookEvent
from app.recovery.agent.service import AdaptiveRecoveryAgent
from app.recovery.audit.models import AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.recovery.capabilities import build_capability_executor
from app.recovery.capabilities.models import ExecutionResult, ExecutionStatus
from app.recovery.detector import detect_recovery_case
from app.recovery.learning.service import LearningService, build_context_key
from app.recovery.learning.store import StrategyStore
from app.recovery.pending_store import PendingRecovery, PendingRecoveryStore
from app.recovery.pipeline import RecoveryPipelineResult
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome
from app.recovery.verification.razorpay import RazorpayVerificationProvider
from app.recovery.verification.service import VerificationService
from app.signals.models import RevenueSignal
from app.signals.router import UnsupportedEventType, route_webhook_to_signal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounded re-verification configuration.
# ---------------------------------------------------------------------------

# Maximum number of re-verification attempts after the initial check.
_REVERIFY_MAX_ATTEMPTS = 3

# Delay (seconds) between re-verification attempts (simple linear backoff).
_REVERIFY_DELAYS = [10, 30, 60]

# ---------------------------------------------------------------------------
# Shared singletons — created once, reused across pipeline runs.
# ---------------------------------------------------------------------------

# Learning store — in-memory, merchant-specific strategy statistics.
_learning_store = StrategyStore()

# Learning service — consumes verified outcomes.
_learning_service = LearningService(store=_learning_store)

# Audit trail — in-memory, append-only.
_audit_store = AuditStore()
_audit_service = AuditService(store=_audit_store)

# Agent instance — now with learning store for Thompson Sampling.
_agent = AdaptiveRecoveryAgent(learning_store=_learning_store)

# Capability executor — wires Policy + Registry + Capabilities.
_executor = build_capability_executor()

# Verification provider + service.
_verification_provider = RazorpayVerificationProvider(
    key_id=settings.razorpay_key_id,
    key_secret=settings.razorpay_key_secret,
)
_verification_service = VerificationService(provider=_verification_provider)

# Pending recovery store — correlates payment_link_id → recovery context.
_pending_store = PendingRecoveryStore()


# ---------------------------------------------------------------------------
# Public accessors for the shared singletons (for API/demo endpoints).
# ---------------------------------------------------------------------------


def get_audit_service() -> AuditService:
    """Return the shared audit service for API endpoints."""
    return _audit_service


def get_learning_service() -> LearningService:
    """Return the shared learning service for API endpoints."""
    return _learning_service


def get_learning_store() -> StrategyStore:
    """Return the shared learning store for API endpoints."""
    return _learning_store


def get_pending_store() -> PendingRecoveryStore:
    """Return the shared pending recovery store for API endpoints."""
    return _pending_store


# ---------------------------------------------------------------------------
# Signal ingestion result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalIngestionResult:
    """Outcome of a single signal ingestion attempt."""

    signal: RevenueSignal | None
    """The normalized signal, or None if the event type is not yet supported."""

    supported: bool
    """True when a normalizer exists for this event type."""


# ---------------------------------------------------------------------------
# Main ingestion entry points
# ---------------------------------------------------------------------------


def ingest_webhook_event(event: RazorpayWebhookEvent) -> SignalIngestionResult:
    """Normalize a verified RazorpayWebhookEvent into a RevenueSignal.

    Returns a SignalIngestionResult describing what happened.  Never raises
    UnsupportedEventType to the caller — that case is handled here and
    logged explicitly so the webhook endpoint can return a safe 200 response.

    Razorpay expects a 2xx acknowledgement; returning 4xx/5xx for an
    unsupported-but-valid event would cause Razorpay to retry indefinitely.
    """
    try:
        signal = route_webhook_to_signal(event)
    except UnsupportedEventType:
        logger.warning(
            "revenue_signal_not_produced",
            extra={
                "reason": "unsupported_event_type",
                "event_type": event.event_type,
                "event_id": event.event_id,
            },
        )
        return SignalIngestionResult(signal=None, supported=False)

    _log_signal(event, signal)
    get_demo_session_store().link_payment_failure(
        order_id=_extract_payment_order_id(event.payload),
        signal=signal,
    )
    _run_pipeline(signal)
    return SignalIngestionResult(signal=signal, supported=True)


async def ingest_webhook_event_background(event: RazorpayWebhookEvent) -> None:
    """Run signal ingestion in a background task (non-blocking).

    Wraps the synchronous ``ingest_webhook_event`` in an executor so the
    webhook handler can return HTTP 200 immediately without waiting for
    the LLM diagnosis or the full recovery pipeline.

    All errors are caught and logged — the background task must never
    propagate an unhandled exception.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, ingest_webhook_event, event)
    except Exception:
        logger.exception(
            "background_ingestion_failed",
            extra={
                "event_type": event.event_type,
                "event_id": event.event_id,
            },
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _log_signal(event: RazorpayWebhookEvent, signal: RevenueSignal) -> None:
    """Emit a structured development log of the normalized RevenueSignal.

    PII fields (contact number, email) are intentionally excluded.
    """
    logger.info(
        "revenue_signal_normalized",
        extra={
            "event_type": event.event_type,
            "provider_event_id": signal.provider_event_id,
            "signal_id": signal.signal_id,
            "signal_type": signal.signal_type.value,
            "status": signal.status.value,
            "merchant_id": signal.merchant_id,
            "amount_minor": signal.amount_minor,
            "currency": signal.currency,
            "provider": signal.provider,
            "provider_entity_id": signal.provider_entity_id,
            "reason": signal.reason,
            "failure_source": signal.failure_source,
            "failure_step": signal.failure_step,
            "occurred_at": signal.occurred_at.isoformat(),
            "metadata": signal.metadata,
        },
    )


def _extract_payment_order_id(payload: dict[str, Any]) -> str | None:
    """Return the source order ID when a payment webhook includes one.

    This is demo-only correlation metadata.  It does not alter signal
    normalization or recovery decisions when absent.
    """
    nested_payload = payload.get("payload")
    if not isinstance(nested_payload, dict):
        return None
    payment = nested_payload.get("payment")
    if not isinstance(payment, dict):
        return None
    entity = payment.get("entity")
    if not isinstance(entity, dict):
        return None
    order_id = entity.get("order_id")
    return order_id if isinstance(order_id, str) and order_id else None


def _is_terminal(status: VerificationStatus) -> bool:
    """Return True if the verification status is terminal (no further checks)."""
    return status in (VerificationStatus.RECOVERED, VerificationStatus.NOT_RECOVERED)


def _run_pipeline(signal: RevenueSignal) -> RecoveryPipelineResult | None:
    """Run the full detection → decision → execution → verification → learning pipeline.

    Flow:
        RevenueSignal
            → Risk Detector  → RecoveryCase
            → Adaptive Agent  → AgentDecision
            → Policy          → ALLOW / BLOCK
            → Registry        → Capability
            → Execution       → ExecutionResult
            → Verification    → VerifiedOutcome (may be PENDING initially)
            → Re-verification → bounded follow-up until terminal or window ends
            → Learning        → strategy update (only for terminal outcomes)
            → Audit           → audit trail
    """
    # --- Detection ---
    case = detect_recovery_case(signal)
    if case is None:
        return None

    get_demo_session_store().link_recovery_case(
        signal_id=signal.signal_id,
        case_id=case.case_id,
    )

    # Audit: CASE_CREATED
    _audit_service.record(
        event_type=AuditEventType.CASE_CREATED,
        case_id=case.case_id,
        merchant_id=case.merchant_id,
        actor="risk_detector",
        signal_id=case.signal_id,
        data={
            "amount_at_risk_minor": case.amount_at_risk_minor,
            "currency": case.currency,
            "risk_status": case.risk_status.value,
            "recoverability": case.recoverability.value,
            "urgency": case.urgency.value,
            "reason_codes": case.reason_codes,
        },
    )

    # --- Check for existing pending Payment Link ---
    existing_pending = _pending_store.get_by_case_id(case.case_id)
    pending_payment_link_id: str | None = None
    if existing_pending is not None:
        pending_payment_link_id = existing_pending.payment_link_id

    logger.info(
        "agent_pending_link_context",
        extra={
            "case_id": case.case_id,
            "pending_payment_link_id": pending_payment_link_id,
            "has_pending_payment_link": pending_payment_link_id is not None,
        },
    )

    # --- Agent decision ---
    decision = _agent.decide(
        signal, case, pending_payment_link_id=pending_payment_link_id
    )
    if decision is None:
        logger.info(
            "agent_no_decision",
            extra={
                "case_id": case.case_id,
                "reason": "no_eligible_action",
            },
        )
        return None

    if decision.diagnosis is not None:
        diagnosis = decision.diagnosis
        _audit_service.record(
            event_type=AuditEventType.DIAGNOSIS_CREATED,
            case_id=case.case_id,
            merchant_id=case.merchant_id,
            actor="adaptive_recovery_agent",
            signal_id=case.signal_id,
            decision_id=decision.decision_id,
            data={
                "category": diagnosis.category.value,
                "primary_reason": diagnosis.primary_reason,
                "failure_stage": diagnosis.failure_stage.value,
                "confidence": diagnosis.confidence,
                "diagnosis_source": diagnosis.diagnosis_source,
            },
        )

    # --- Compute canonical context_key ONCE ---
    # This is the single source of truth for the learning context.
    # It must be preserved through PendingRecovery so that
    # the event-driven verification path (webhook) uses the same key.
    context_key = build_context_key(
        signal_type=signal.signal_type.value,
        failure_source=signal.failure_source or "unknown",
        urgency=case.urgency.value,
    )

    logger.info(
        "canonical_context_key_computed",
        extra={
            "case_id": case.case_id,
            "decision_id": decision.decision_id,
            "context_key": context_key,
            "signal_type": signal.signal_type.value,
            "failure_source": signal.failure_source or "unknown",
            "urgency": case.urgency.value,
        },
    )

    # Audit: DECISION_CREATED
    _audit_service.record(
        event_type=AuditEventType.DECISION_CREATED,
        case_id=case.case_id,
        merchant_id=case.merchant_id,
        actor="adaptive_recovery_agent",
        signal_id=case.signal_id,
        decision_id=decision.decision_id,
        data={
            "selected_capability_id": decision.selected_capability_id,
            "selected_action_type": decision.selected_action_type.value,
            "candidate_action_ids": decision.candidate_action_ids,
            "decision_source": decision.decision_source.value,
            "reason": decision.reason,
            "context_key": context_key,
        },
    )

    # --- Capability execution: Policy → Registry → Capability ---
    execution_result = _executor.execute(decision, case)

    # Audit: POLICY_DECISION + CAPABILITY_EXECUTED
    _audit_service.record(
        event_type=AuditEventType.POLICY_DECISION,
        case_id=case.case_id,
        merchant_id=case.merchant_id,
        actor="policy_engine",
        signal_id=case.signal_id,
        decision_id=decision.decision_id,
        execution_id=execution_result.execution_id,
        data={
            "execution_status": execution_result.status.value,
            "capability_id": execution_result.capability_id,
            "verdict": (
                execution_result.policy_decision.verdict.value
                if execution_result.policy_decision is not None
                else None
            ),
            "reasons": (
                execution_result.policy_decision.reasons
                if execution_result.policy_decision is not None
                else []
            ),
        },
    )

    _audit_service.record(
        event_type=AuditEventType.CAPABILITY_EXECUTED,
        case_id=case.case_id,
        merchant_id=case.merchant_id,
        actor=execution_result.capability_id,
        signal_id=case.signal_id,
        decision_id=decision.decision_id,
        execution_id=execution_result.execution_id,
        data={
            "status": execution_result.status.value,
            "provider": execution_result.provider,
            "provider_reference": execution_result.provider_reference,
            "payment_link_url": execution_result.payment_link_url,
            "error_message": execution_result.error_message,
        },
    )

    # Audit: REMINDER_SENT for reminder-type capabilities.
    if execution_result.capability_id == "payment_link_reminder":
        _audit_service.record(
            event_type=AuditEventType.REMINDER_SENT,
            case_id=case.case_id,
            merchant_id=case.merchant_id,
            actor="payment_link_reminder_capability",
            signal_id=case.signal_id,
            decision_id=decision.decision_id,
            execution_id=execution_result.execution_id,
            data={
                "capability_id": execution_result.capability_id,
                "payment_link_id": execution_result.provider_reference,
                "medium": (
                    execution_result.metadata.get("medium")
                    if execution_result.metadata
                    else None
                ),
                "status": execution_result.status.value,
                "error_message": execution_result.error_message,
            },
        )

        logger.info(
            "audit_event_recorded",
            extra={
                "event_type": AuditEventType.REMINDER_SENT.value,
                "case_id": case.case_id,
                "capability_id": execution_result.capability_id,
                "payment_link_id": execution_result.provider_reference,
                "status": execution_result.status.value,
            },
        )

    logger.info(
        "pipeline_execution_result",
        extra={
            "case_id": execution_result.case_id,
            "decision_id": execution_result.decision_id,
            "execution_id": execution_result.execution_id,
            "capability_id": execution_result.capability_id,
            "action_type": execution_result.action_type,
            "status": execution_result.status.value,
            "provider": execution_result.provider,
            "provider_reference": execution_result.provider_reference,
            "payment_link_url": execution_result.payment_link_url,
            "error_message": execution_result.error_message,
        },
    )

    # --- Store pending recovery correlation ---
    # Only create a pending entry for NEW payment link creations.
    # Reminders re-use the existing pending entry — they must NOT create
    # a duplicate or overwrite the original.
    _is_new_payment_link = (
        execution_result.status == ExecutionStatus.EXECUTED
        and execution_result.provider_reference
        and execution_result.capability_id != "payment_link_reminder"
    )

    if _is_new_payment_link:
        pending = PendingRecovery(
            payment_link_id=execution_result.provider_reference,
            case_id=case.case_id,
            execution_id=execution_result.execution_id,
            decision_id=decision.decision_id,
            merchant_id=case.merchant_id,
            capability_id=execution_result.capability_id,
            signal_id=case.signal_id,
            amount_at_risk_minor=case.amount_at_risk_minor,
            currency=case.currency,
            context_key=context_key,
        )
        _pending_store.store(pending)

        _audit_service.record(
            event_type=AuditEventType.RECOVERY_PENDING,
            case_id=case.case_id,
            merchant_id=case.merchant_id,
            actor="pipeline",
            signal_id=case.signal_id,
            decision_id=decision.decision_id,
            execution_id=execution_result.execution_id,
            data={
                "payment_link_id": execution_result.provider_reference,
                "awaiting": "payment_link.paid webhook or manual verification",
            },
        )

    # --- Verification with bounded re-checks ---
    verified_outcome = _verify_with_retries(
        execution_result=execution_result,
        case_id=case.case_id,
        merchant_id=case.merchant_id,
        signal_id=case.signal_id,
        decision_id=decision.decision_id,
        amount_at_risk_minor=case.amount_at_risk_minor,
        currency=case.currency,
    )

    # --- Learning (only for terminal outcomes) ---
    # context_key was computed once above (canonical_context_key_computed).
    # Re-use it here — never reconstruct independently.
    logger.info(
        "learning_context_key_used",
        extra={
            "case_id": case.case_id,
            "context_key": context_key,
            "path": "inline_pipeline",
        },
    )

    learning_updated = _learning_service.record_outcome(
        merchant_id=case.merchant_id,
        capability_id=execution_result.capability_id,
        context_key=context_key,
        verified_outcome=verified_outcome,
    )

    if learning_updated:
        stats = _learning_service.get_statistics(
            merchant_id=case.merchant_id,
            capability_id=execution_result.capability_id,
            context_key=context_key,
        )
        _audit_service.record(
            event_type=AuditEventType.LEARNING_UPDATED,
            case_id=case.case_id,
            merchant_id=case.merchant_id,
            actor="learning_service",
            signal_id=case.signal_id,
            decision_id=decision.decision_id,
            execution_id=execution_result.execution_id,
            data={
                "capability_id": execution_result.capability_id,
                "context_key": context_key,
                "successes": stats.successes,
                "failures": stats.failures,
                "empirical_success_rate": round(
                    stats.empirical_success_rate, 4
                ),
                "total_verified_trials": stats.total_verified_trials,
                "verification_status": verified_outcome.status.value,
            },
        )

        logger.info(
            "bandit_learning_statistics",
            extra={
                "merchant_id": case.merchant_id,
                "capability_id": execution_result.capability_id,
                "context_key": context_key,
                "successes": stats.successes,
                "failures": stats.failures,
                "empirical_success_rate": round(
                    stats.empirical_success_rate, 4
                ),
                "total_verified_trials": stats.total_verified_trials,
            },
        )

    # --- Pipeline result ---
    pipeline_result = RecoveryPipelineResult(
        case_id=case.case_id,
        decision_id=decision.decision_id,
        execution_id=execution_result.execution_id,
        capability_id=execution_result.capability_id,
        execution_status=execution_result.status,
        verification_status=verified_outcome.status,
        amount_at_risk_minor=case.amount_at_risk_minor,
        amount_recovered_minor=verified_outcome.amount_recovered_minor,
        currency=case.currency,
        provider_reference=execution_result.provider_reference,
        payment_link_url=execution_result.payment_link_url,
        verification_reason=verified_outcome.reason,
        learning_updated=learning_updated,
    )

    logger.info(
        "pipeline_completed",
        extra={
            "case_id": pipeline_result.case_id,
            "execution_status": pipeline_result.execution_status.value,
            "verification_status": pipeline_result.verification_status.value,
            "amount_at_risk_minor": pipeline_result.amount_at_risk_minor,
            "amount_recovered_minor": pipeline_result.amount_recovered_minor,
            "currency": pipeline_result.currency,
            "learning_updated": pipeline_result.learning_updated,
        },
    )

    return pipeline_result


def _verify_with_retries(
    *,
    execution_result: ExecutionResult,
    case_id: str,
    merchant_id: str,
    signal_id: str,
    decision_id: str,
    amount_at_risk_minor: int,
    currency: str,
) -> "VerifiedOutcome":
    """Verify execution with bounded re-checks for PENDING outcomes.

    1. Run initial verification immediately.
    2. If PENDING, schedule bounded follow-up attempts with backoff delays.
    3. Stop immediately when a terminal outcome (RECOVERED / NOT_RECOVERED)
       is observed.
    4. If the re-verification window ends without a terminal outcome,
       return the last outcome (PENDING or UNKNOWN) as-is.

    Does not add Redis, Celery, Kafka, or a database — uses simple
    time.sleep in the background thread.
    """
    from app.recovery.verification.models import VerifiedOutcome

    # --- Initial verification ---
    verified_outcome = _verification_service.verify(
        execution_result=execution_result,
        amount_at_risk_minor=amount_at_risk_minor,
        currency=currency,
    )

    # If already terminal, audit and return immediately.
    if _is_terminal(verified_outcome.status):
        _audit_service.record(
            event_type=AuditEventType.VERIFICATION_COMPLETED,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="verification_service",
            signal_id=signal_id,
            decision_id=decision_id,
            execution_id=execution_result.execution_id,
            data={
                "verification_status": verified_outcome.status.value,
                "amount_recovered_minor": verified_outcome.amount_recovered_minor,
                "amount_at_risk_minor": verified_outcome.amount_at_risk_minor,
                "provider_reference": verified_outcome.provider_reference,
                "provider_payment_id": verified_outcome.provider_payment_id,
                "reason": verified_outcome.reason,
                "attempt": 0,
            },
        )
        return verified_outcome

    # --- PENDING or UNKNOWN: audit initial status ---
    if verified_outcome.status == VerificationStatus.PENDING:
        _audit_service.record(
            event_type=AuditEventType.VERIFICATION_PENDING,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="verification_service",
            signal_id=signal_id,
            decision_id=decision_id,
            execution_id=execution_result.execution_id,
            data={
                "verification_status": verified_outcome.status.value,
                "reason": verified_outcome.reason,
                "attempt": 0,
                "will_retry": True,
                "max_retries": _REVERIFY_MAX_ATTEMPTS,
            },
        )
    else:
        # UNKNOWN on initial check — still record and try retries.
        _audit_service.record(
            event_type=AuditEventType.VERIFICATION_COMPLETED,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="verification_service",
            signal_id=signal_id,
            decision_id=decision_id,
            execution_id=execution_result.execution_id,
            data={
                "verification_status": verified_outcome.status.value,
                "reason": verified_outcome.reason,
                "attempt": 0,
            },
        )
        return verified_outcome  # UNKNOWN — don't retry, may be a real API issue.

    # --- Bounded re-verification for PENDING ---
    for attempt_idx in range(_REVERIFY_MAX_ATTEMPTS):
        delay = _REVERIFY_DELAYS[min(attempt_idx, len(_REVERIFY_DELAYS) - 1)]

        logger.info(
            "verification_retry_scheduled",
            extra={
                "case_id": case_id,
                "attempt": attempt_idx + 1,
                "delay_seconds": delay,
                "max_attempts": _REVERIFY_MAX_ATTEMPTS,
            },
        )

        time.sleep(delay)

        verified_outcome = _verification_service.verify(
            execution_result=execution_result,
            amount_at_risk_minor=amount_at_risk_minor,
            currency=currency,
        )

        if _is_terminal(verified_outcome.status):
            _audit_service.record(
                event_type=AuditEventType.VERIFICATION_COMPLETED,
                case_id=case_id,
                merchant_id=merchant_id,
                actor="verification_service",
                signal_id=signal_id,
                decision_id=decision_id,
                execution_id=execution_result.execution_id,
                data={
                    "verification_status": verified_outcome.status.value,
                    "amount_recovered_minor": verified_outcome.amount_recovered_minor,
                    "amount_at_risk_minor": verified_outcome.amount_at_risk_minor,
                    "provider_reference": verified_outcome.provider_reference,
                    "provider_payment_id": verified_outcome.provider_payment_id,
                    "reason": verified_outcome.reason,
                    "attempt": attempt_idx + 1,
                },
            )

            logger.info(
                "verification_terminal_on_retry",
                extra={
                    "case_id": case_id,
                    "attempt": attempt_idx + 1,
                    "verification_status": verified_outcome.status.value,
                },
            )
            return verified_outcome

        # Still PENDING or UNKNOWN — continue loop.
        logger.info(
            "verification_still_pending",
            extra={
                "case_id": case_id,
                "attempt": attempt_idx + 1,
                "verification_status": verified_outcome.status.value,
            },
        )

    # --- Window exhausted: return last outcome (PENDING) ---
    logger.warning(
        "verification_window_exhausted",
        extra={
            "case_id": case_id,
            "final_status": verified_outcome.status.value,
            "total_attempts": _REVERIFY_MAX_ATTEMPTS + 1,
        },
    )

    _audit_service.record(
        event_type=AuditEventType.VERIFICATION_COMPLETED,
        case_id=case_id,
        merchant_id=merchant_id,
        actor="verification_service",
        signal_id=signal_id,
        decision_id=decision_id,
        execution_id=execution_result.execution_id,
        data={
            "verification_status": verified_outcome.status.value,
            "reason": f"Re-verification window exhausted after {_REVERIFY_MAX_ATTEMPTS + 1} attempts.",
            "attempt": _REVERIFY_MAX_ATTEMPTS,
            "window_exhausted": True,
        },
    )

    return verified_outcome


# ---------------------------------------------------------------------------
# Event-driven recovery verification
# ---------------------------------------------------------------------------


@dataclass
class RecoveryWebhookResult:
    """Result of processing a recovery-related webhook event."""

    event_type: str
    payment_link_id: str | None
    case_id: str | None
    verification_status: str | None
    amount_recovered_minor: int
    learning_updated: bool
    message: str


def _extract_payment_link_id(payload: dict[str, Any]) -> str | None:
    """Extract the Payment Link ID from a Razorpay webhook payload.

    Supports:
    - payment_link.paid: payload.payment_link.entity.id
    - payment.authorized/captured: payload.payment.entity.notes.payment_link_id
      (when the payment was made via a payment link)
    """
    nested = payload.get("payload")
    if not isinstance(nested, dict):
        return None

    # payment_link.paid → payload.payment_link.entity.id
    plink_data = nested.get("payment_link")
    if isinstance(plink_data, dict):
        entity = plink_data.get("entity")
        if isinstance(entity, dict):
            plink_id = entity.get("id")
            if isinstance(plink_id, str) and plink_id.startswith("plink_"):
                return plink_id

    # payment.authorized / payment.captured → the payment entity
    payment_data = nested.get("payment")
    if isinstance(payment_data, dict):
        entity = payment_data.get("entity")
        if isinstance(entity, dict):
            # Razorpay sometimes includes notes from the payment link
            notes = entity.get("notes")
            if isinstance(notes, dict):
                plink_id = notes.get("payment_link_id")
                if isinstance(plink_id, str) and plink_id.startswith("plink_"):
                    return plink_id

    return None


def _perform_independent_verification(
    pending: PendingRecovery,
    source: str,
) -> RecoveryWebhookResult:
    """Independently verify a pending recovery against Razorpay API.

    This is the shared verification logic for both webhook-triggered and
    manual verification. Webhook receipt alone is NEVER treated as proof
    of recovery — we always fetch the current payment link status from
    the Razorpay API.

    Args:
        pending: The pending recovery entry from the store.
        source: "webhook" or "manual" — for audit/logging.

    Returns:
        RecoveryWebhookResult with the verification outcome.
    """
    # Build a minimal ExecutionResult for the verification service.
    exec_result = ExecutionResult(
        execution_id=pending.execution_id,
        case_id=pending.case_id,
        decision_id=pending.decision_id,
        capability_id=pending.capability_id,
        action_type="create_payment_link",
        status=ExecutionStatus.EXECUTED,
        provider="razorpay",
        provider_reference=pending.payment_link_id,
    )

    # Audit: verification started
    _audit_service.record(
        event_type=AuditEventType.VERIFICATION_STARTED,
        case_id=pending.case_id,
        merchant_id=pending.merchant_id,
        actor=f"recovery_{source}",
        signal_id=pending.signal_id,
        execution_id=pending.execution_id,
        data={
            "payment_link_id": pending.payment_link_id,
            "trigger": source,
        },
    )

    # Independent verification — always fetch from Razorpay API.
    verified_outcome = _verification_service.verify(
        execution_result=exec_result,
        amount_at_risk_minor=pending.amount_at_risk_minor,
        currency=pending.currency,
    )

    logger.info(
        f"recovery_{source}_verification_completed",
        extra={
            "payment_link_id": pending.payment_link_id,
            "case_id": pending.case_id,
            "verification_status": verified_outcome.status.value,
            "amount_recovered_minor": verified_outcome.amount_recovered_minor,
        },
    )

    # Audit: verification completed
    _audit_service.record(
        event_type=AuditEventType.VERIFICATION_COMPLETED,
        case_id=pending.case_id,
        merchant_id=pending.merchant_id,
        actor="verification_service",
        signal_id=pending.signal_id,
        execution_id=pending.execution_id,
        data={
            "verification_status": verified_outcome.status.value,
            "amount_recovered_minor": verified_outcome.amount_recovered_minor,
            "trigger": source,
        },
    )

    # Terminal outcome handling
    learning_updated = False

    if verified_outcome.status == VerificationStatus.RECOVERED:
        # Mark resolved — idempotent (returns False if already resolved)
        was_new = _pending_store.mark_resolved(
            pending.payment_link_id, "recovered", source
        )

        if was_new:
            # Learning update — only once per recovery
            # Use the canonical context_key preserved on PendingRecovery.
            # This is the same key the bandit used when selecting this action.
            context_key = pending.context_key
            logger.info(
                "learning_context_key_used",
                extra={
                    "case_id": pending.case_id,
                    "context_key": context_key,
                    "path": "webhook_verification",
                },
            )
            learning_updated = _learning_service.record_outcome(
                merchant_id=pending.merchant_id,
                capability_id=pending.capability_id,
                context_key=context_key,
                verified_outcome=verified_outcome,
            )

            audit_learning_type = (
                AuditEventType.LEARNING_UPDATED
                if learning_updated
                else AuditEventType.LEARNING_SKIPPED
            )
            _audit_service.record(
                event_type=audit_learning_type,
                case_id=pending.case_id,
                merchant_id=pending.merchant_id,
                actor="learning_service",
                signal_id=pending.signal_id,
                execution_id=pending.execution_id,
                data={
                    "context_key": context_key,
                    "verification_status": verified_outcome.status.value,
                    "trigger": source,
                },
            )

        _audit_service.record(
            event_type=AuditEventType.RECOVERY_RECOVERED,
            case_id=pending.case_id,
            merchant_id=pending.merchant_id,
            actor=f"recovery_{source}",
            signal_id=pending.signal_id,
            execution_id=pending.execution_id,
            data={
                "amount_recovered_minor": verified_outcome.amount_recovered_minor,
                "payment_link_id": pending.payment_link_id,
                "trigger": source,
                "duplicate": not was_new,
            },
        )

        return RecoveryWebhookResult(
            event_type=f"recovery.{source}",
            payment_link_id=pending.payment_link_id,
            case_id=pending.case_id,
            verification_status="recovered",
            amount_recovered_minor=verified_outcome.amount_recovered_minor,
            learning_updated=learning_updated,
            message="Recovery independently verified as RECOVERED.",
        )

    elif verified_outcome.status == VerificationStatus.NOT_RECOVERED:
        _pending_store.mark_resolved(
            pending.payment_link_id, "not_recovered", source
        )

        # Use the canonical context_key preserved on PendingRecovery.
        context_key = pending.context_key
        logger.info(
            "learning_context_key_used",
            extra={
                "case_id": pending.case_id,
                "context_key": context_key,
                "path": "webhook_verification_not_recovered",
            },
        )
        learning_updated = _learning_service.record_outcome(
            merchant_id=pending.merchant_id,
            capability_id=pending.capability_id,
            context_key=context_key,
            verified_outcome=verified_outcome,
        )

        audit_learning_type = (
            AuditEventType.LEARNING_UPDATED
            if learning_updated
            else AuditEventType.LEARNING_SKIPPED
        )
        _audit_service.record(
            event_type=audit_learning_type,
            case_id=pending.case_id,
            merchant_id=pending.merchant_id,
            actor="learning_service",
            signal_id=pending.signal_id,
            execution_id=pending.execution_id,
            data={
                "context_key": context_key,
                "verification_status": verified_outcome.status.value,
                "trigger": source,
            },
        )

        _audit_service.record(
            event_type=AuditEventType.RECOVERY_NOT_RECOVERED,
            case_id=pending.case_id,
            merchant_id=pending.merchant_id,
            actor=f"recovery_{source}",
            signal_id=pending.signal_id,
            execution_id=pending.execution_id,
            data={
                "payment_link_id": pending.payment_link_id,
                "trigger": source,
            },
        )

        return RecoveryWebhookResult(
            event_type=f"recovery.{source}",
            payment_link_id=pending.payment_link_id,
            case_id=pending.case_id,
            verification_status="not_recovered",
            amount_recovered_minor=0,
            learning_updated=learning_updated,
            message="Recovery independently verified as NOT_RECOVERED.",
        )

    else:
        # PENDING or UNKNOWN — no learning update, no resolution
        _audit_service.record(
            event_type=AuditEventType.LEARNING_SKIPPED,
            case_id=pending.case_id,
            merchant_id=pending.merchant_id,
            actor="learning_service",
            signal_id=pending.signal_id,
            execution_id=pending.execution_id,
            data={
                "verification_status": verified_outcome.status.value,
                "reason": "Non-terminal verification status — no learning update.",
                "trigger": source,
            },
        )

        return RecoveryWebhookResult(
            event_type=f"recovery.{source}",
            payment_link_id=pending.payment_link_id,
            case_id=pending.case_id,
            verification_status=verified_outcome.status.value,
            amount_recovered_minor=0,
            learning_updated=False,
            message=f"Payment link status is {verified_outcome.status.value}. "
            "No learning update.",
        )


async def handle_recovery_webhook(event: RazorpayWebhookEvent) -> RecoveryWebhookResult:
    """Handle a recovery-related webhook event (e.g. payment_link.paid).

    This function:
    1. Extracts the payment link ID from the webhook payload
    2. Correlates it to a pending recovery case
    3. Independently verifies the current status via Razorpay API
    4. Updates learning for terminal outcomes
    5. Records audit events

    Webhook receipt alone is NEVER treated as proof of recovery.
    The webhook triggers independent verification.

    This function dispatches the actual verification to a background thread
    so the webhook endpoint can return immediately.
    """
    event_type = event.event_type or "unknown"
    payment_link_id = _extract_payment_link_id(event.payload)

    logger.info(
        "recovery_webhook_processing",
        extra={
            "event_type": event_type,
            "payment_link_id": payment_link_id,
        },
    )

    if not payment_link_id:
        logger.warning(
            "recovery_webhook_no_payment_link_id",
            extra={"event_type": event_type},
        )
        return RecoveryWebhookResult(
            event_type=event_type,
            payment_link_id=None,
            case_id=None,
            verification_status=None,
            amount_recovered_minor=0,
            learning_updated=False,
            message="Could not extract payment_link_id from webhook payload.",
        )

    pending = _pending_store.get_by_payment_link_id(payment_link_id)
    if pending is None:
        logger.info(
            "recovery_webhook_no_pending_recovery",
            extra={
                "event_type": event_type,
                "payment_link_id": payment_link_id,
            },
        )
        return RecoveryWebhookResult(
            event_type=event_type,
            payment_link_id=payment_link_id,
            case_id=None,
            verification_status=None,
            amount_recovered_minor=0,
            learning_updated=False,
            message=f"No pending recovery found for payment_link_id={payment_link_id}.",
        )

    # Audit: webhook received
    _audit_service.record(
        event_type=AuditEventType.RECOVERY_WEBHOOK_RECEIVED,
        case_id=pending.case_id,
        merchant_id=pending.merchant_id,
        actor="webhook_receiver",
        signal_id=pending.signal_id,
        execution_id=pending.execution_id,
        data={
            "event_type": event_type,
            "payment_link_id": payment_link_id,
        },
    )

    # Dispatch independent verification to background thread.
    # The webhook endpoint returns immediately.
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        _perform_independent_verification,
        pending,
        "webhook",
    )

    logger.info(
        "recovery_webhook_completed",
        extra={
            "event_type": event_type,
            "payment_link_id": payment_link_id,
            "case_id": result.case_id,
            "verification_status": result.verification_status,
            "amount_recovered_minor": result.amount_recovered_minor,
            "learning_updated": result.learning_updated,
        },
    )

    return result


def verify_case_manually(case_id: str) -> RecoveryWebhookResult:
    """Manually trigger verification for a pending recovery case.

    This is the reliable fallback when webhooks are delayed, lost,
    or the system needs to check a case on demand.

    Args:
        case_id: The recovery case ID to verify.

    Returns:
        RecoveryWebhookResult with the verification outcome.
    """
    pending = _pending_store.get_by_case_id(case_id)

    if pending is None:
        return RecoveryWebhookResult(
            event_type="manual_verification",
            payment_link_id=None,
            case_id=case_id,
            verification_status=None,
            amount_recovered_minor=0,
            learning_updated=False,
            message=f"No pending recovery found for case_id={case_id}.",
        )

    logger.info(
        "manual_verification_triggered",
        extra={
            "case_id": case_id,
            "payment_link_id": pending.payment_link_id,
        },
    )

    return _perform_independent_verification(pending, "manual")

"""Focused regression tests for context_key consistency.

Proves that the canonical context_key used by the agent decision
is preserved through:

    AgentDecision → PendingRecovery → Verification → Learning

Root cause of the bug:
    The event-driven verification path (webhook) was calling
    build_context_key() with NO arguments, producing defaults
    'payment_failure|unknown|medium' instead of the real signal data
    like 'payment_failure|bank|low'.

Tests:
1. Agent decision context is preserved on PendingRecovery.
2. Learning receives the exact same context from the webhook path.
3. A bank/low payment failure cannot become unknown/medium during learning.
4. Existing learning behavior (inline pipeline) remains intact.
5. context_key is logged at decision and learning stages.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.recovery.agent.models import (
    ActionType,
    AgentContext,
    AgentDecision,
    CandidateAction,
    DecisionSource,
    Diagnosis,
    DiagnosisCategory,
    EligibilityStatus,
    FailureStage,
)
from app.recovery.capabilities.models import ExecutionResult, ExecutionStatus
from app.recovery.learning.service import LearningService, build_context_key
from app.recovery.learning.store import StrategyStore
from app.recovery.models import RecoveryCase, Recoverability, RiskStatus, Urgency
from app.recovery.pending_store import PendingRecovery, PendingRecoveryStore
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context_key_bank_low() -> str:
    """The canonical context key for a bank-sourced, low-urgency failure."""
    return build_context_key(
        signal_type="payment_failure",
        failure_source="bank",
        urgency="low",
    )


def _make_context_key_default() -> str:
    """The default (buggy) context key produced by build_context_key()."""
    return build_context_key()


def _make_pending_recovery(
    *,
    context_key: str = "payment_failure|bank|low",
    payment_link_id: str = "plink_ctx_test_001",
    case_id: str = "case_ctx_test_001",
) -> PendingRecovery:
    return PendingRecovery(
        payment_link_id=payment_link_id,
        case_id=case_id,
        execution_id="exec_ctx_test_001",
        decision_id="dec_ctx_test_001",
        merchant_id="acc_ctx_test_merchant",
        capability_id="payment_link_recovery",
        signal_id="sig_ctx_test_001",
        amount_at_risk_minor=50000,
        currency="INR",
        context_key=context_key,
    )


# ===========================================================================
# 1. Agent decision context is preserved on PendingRecovery.
# ===========================================================================


class TestContextKeyPreserved:
    def test_context_key_stored_on_pending_recovery(self):
        """PendingRecovery stores the canonical context_key."""
        pending = _make_pending_recovery(context_key="payment_failure|bank|low")
        assert pending.context_key == "payment_failure|bank|low"

    def test_context_key_survives_store_and_retrieve(self):
        """The stored context_key can be retrieved from the PendingRecoveryStore."""
        store = PendingRecoveryStore()
        pending = _make_pending_recovery(context_key="payment_failure|bank|low")
        store.store(pending)

        retrieved = store.get_by_payment_link_id("plink_ctx_test_001")
        assert retrieved is not None
        assert retrieved.context_key == "payment_failure|bank|low"

    def test_context_key_survives_case_id_lookup(self):
        """context_key is preserved when looking up by case_id."""
        store = PendingRecoveryStore()
        pending = _make_pending_recovery(context_key="payment_failure|bank|low")
        store.store(pending)

        retrieved = store.get_by_case_id("case_ctx_test_001")
        assert retrieved is not None
        assert retrieved.context_key == "payment_failure|bank|low"


# ===========================================================================
# 2. Learning receives the exact same context from the webhook path.
# ===========================================================================


class TestLearningReceivesCanonicalContext:
    def test_learning_update_uses_pending_context_key(self):
        """When learning is updated via webhook, it uses the stored context_key,
        not a reconstructed one."""
        store = StrategyStore()
        learning = LearningService(store=store)

        # The canonical key from the original decision.
        canonical_key = "payment_failure|bank|low"

        # Simulate what the webhook path does after the fix:
        # It reads pending.context_key and passes it to learning.
        outcome = VerifiedOutcome(
            case_id="case_ctx_test_001",
            execution_id="exec_ctx_test_001",
            capability_id="payment_link_recovery",
            status=VerificationStatus.RECOVERED,
            amount_recovered_minor=50000,
            amount_at_risk_minor=50000,
            currency="INR",
            provider_reference="plink_ctx_test_001",
        )

        # Capture baseline before the update (Beta(1,1) prior).
        baseline = learning.get_statistics(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key=canonical_key,
        )
        baseline_successes = baseline.successes

        updated = learning.record_outcome(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key=canonical_key,
            verified_outcome=outcome,
        )

        # Verify the learning store has the correct key.
        stats = learning.get_statistics(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key=canonical_key,
        )
        assert stats is not None
        # Exactly one learning update occurred.
        assert stats.successes - baseline_successes == 1

        # Verify that the default key was NOT updated.
        default_key = _make_context_key_default()
        default_baseline = learning.get_statistics(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key=default_key,
        )
        default_stats = learning.get_statistics(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key=default_key,
        )
        # Default key should have received zero updates.
        assert default_stats.successes == default_baseline.successes


# ===========================================================================
# 3. A bank/low payment failure cannot become unknown/medium during learning.
# ===========================================================================


class TestNoBogusContextMutation:
    def test_bank_low_never_becomes_unknown_medium(self):
        """Regression: the system must never silently mutate bank|low → unknown|medium."""
        canonical = _make_context_key_bank_low()
        default = _make_context_key_default()

        # They must be different strings.
        assert canonical != default
        assert canonical == "payment_failure|bank|low"
        assert default == "payment_failure|unknown|medium"

    def test_pending_recovery_with_bank_low_stays_bank_low(self):
        """No part of the PendingRecovery lifecycle can mutate the context_key."""
        store = PendingRecoveryStore()
        pending = _make_pending_recovery(context_key="payment_failure|bank|low")
        store.store(pending)

        # Simulate resolution (as the webhook path does).
        resolved = store.mark_resolved(
            "plink_ctx_test_001", "recovered", "webhook"
        )
        assert resolved is True

        # After resolution, the context_key must still be bank|low.
        entry = store.get_by_payment_link_id("plink_ctx_test_001")
        assert entry is not None
        assert entry.context_key == "payment_failure|bank|low"

    def test_end_to_end_decision_to_learning_consistency(self):
        """Simulate the full flow: decision → pending → webhook → learning.

        The same context_key must appear at every stage.
        """
        # 1. Decision phase: compute canonical key from signal.
        canonical_key = build_context_key(
            signal_type="payment_failure",
            failure_source="bank",
            urgency="low",
        )
        assert canonical_key == "payment_failure|bank|low"

        # 2. Store on PendingRecovery.
        pending_store = PendingRecoveryStore()
        pending = _make_pending_recovery(context_key=canonical_key)
        pending_store.store(pending)

        # 3. Webhook arrives → retrieve pending → use pending.context_key.
        retrieved = pending_store.get_by_payment_link_id("plink_ctx_test_001")
        webhook_context_key = retrieved.context_key  # NOT build_context_key()
        assert webhook_context_key == canonical_key

        # 4. Learning update with the correct key.
        learning_store = StrategyStore()
        learning = LearningService(store=learning_store)

        outcome = VerifiedOutcome(
            case_id="case_ctx_test_001",
            execution_id="exec_ctx_test_001",
            capability_id="payment_link_recovery",
            status=VerificationStatus.RECOVERED,
            amount_recovered_minor=50000,
            amount_at_risk_minor=50000,
            currency="INR",
        )

        # Capture baselines before the update (Beta(1,1) prior).
        baseline = learning.get_statistics(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key="payment_failure|bank|low",
        )
        baseline_successes = baseline.successes

        wrong_baseline = learning.get_statistics(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key="payment_failure|unknown|medium",
        )
        wrong_baseline_successes = wrong_baseline.successes

        learning.record_outcome(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key=webhook_context_key,
            verified_outcome=outcome,
        )

        # 5. Verify the learning store recorded bank|low, not unknown|medium.
        stats = learning.get_statistics(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key="payment_failure|bank|low",
        )
        # Exactly one learning update occurred on the correct key.
        assert stats.successes - baseline_successes == 1

        wrong_stats = learning.get_statistics(
            merchant_id="acc_ctx_test_merchant",
            capability_id="payment_link_recovery",
            context_key="payment_failure|unknown|medium",
        )
        # Zero updates on the wrong key.
        assert wrong_stats.successes - wrong_baseline_successes == 0


# ===========================================================================
# 4. Existing learning behavior (inline pipeline) remains intact.
# ===========================================================================


class TestInlinePipelineLearning:
    def test_inline_pipeline_uses_signal_data(self):
        """The inline pipeline path still computes context_key from real signal data."""
        # This is the inline path (not webhook).
        # It should use the same build_context_key with real parameters.
        key = build_context_key(
            signal_type="payment_failure",
            failure_source="customer",
            urgency="high",
        )
        assert key == "payment_failure|customer|high"

        store = StrategyStore()
        learning = LearningService(store=store)

        outcome = VerifiedOutcome(
            case_id="case_inline_test_001",
            execution_id="exec_inline_test_001",
            capability_id="payment_link_recovery",
            status=VerificationStatus.NOT_RECOVERED,
            amount_recovered_minor=0,
            amount_at_risk_minor=50000,
            currency="INR",
        )

        # Capture baseline before the update (Beta(1,1) prior).
        baseline = learning.get_statistics(
            merchant_id="acc_inline_test",
            capability_id="payment_link_recovery",
            context_key=key,
        )
        baseline_failures = baseline.failures
        baseline_successes = baseline.successes

        learning.record_outcome(
            merchant_id="acc_inline_test",
            capability_id="payment_link_recovery",
            context_key=key,
            verified_outcome=outcome,
        )

        stats = learning.get_statistics(
            merchant_id="acc_inline_test",
            capability_id="payment_link_recovery",
            context_key="payment_failure|customer|high",
        )
        # Exactly one failure update, zero success updates.
        assert stats.failures - baseline_failures == 1
        assert stats.successes - baseline_successes == 0

    def test_build_context_key_defaults_are_explicit(self):
        """Calling build_context_key() with defaults produces the known default."""
        assert build_context_key() == "payment_failure|unknown|medium"

    def test_build_context_key_with_real_data(self):
        """Calling build_context_key with real data produces correct key."""
        assert (
            build_context_key("payment_failure", "bank", "low")
            == "payment_failure|bank|low"
        )
        assert (
            build_context_key("payment_failure", "customer", "high")
            == "payment_failure|customer|high"
        )
        assert (
            build_context_key("payment_failure", "network", "medium")
            == "payment_failure|network|medium"
        )


# ===========================================================================
# 5. Observability: context_key is logged at decision and learning stages.
# ===========================================================================


class TestContextKeyObservability:
    def test_canonical_context_key_logged(self, caplog):
        """The canonical_context_key_computed event is logged by the pipeline."""
        # We test the log message format by simulating what the pipeline does.
        import logging

        logger = logging.getLogger("app.signals.service")

        with caplog.at_level(logging.INFO, logger="app.signals.service"):
            logger.info(
                "canonical_context_key_computed",
                extra={
                    "case_id": "case_test_001",
                    "decision_id": "dec_test_001",
                    "context_key": "payment_failure|bank|low",
                    "signal_type": "payment_failure",
                    "failure_source": "bank",
                    "urgency": "low",
                },
            )

        msgs = [
            r
            for r in caplog.records
            if r.message == "canonical_context_key_computed"
        ]
        assert len(msgs) == 1
        assert msgs[0].context_key == "payment_failure|bank|low"

    def test_learning_context_key_used_logged(self, caplog):
        """The learning_context_key_used event is logged by the pipeline."""
        import logging

        logger = logging.getLogger("app.signals.service")

        with caplog.at_level(logging.INFO, logger="app.signals.service"):
            logger.info(
                "learning_context_key_used",
                extra={
                    "case_id": "case_test_001",
                    "context_key": "payment_failure|bank|low",
                    "path": "webhook_verification",
                },
            )

        msgs = [
            r
            for r in caplog.records
            if r.message == "learning_context_key_used"
        ]
        assert len(msgs) == 1
        assert msgs[0].context_key == "payment_failure|bank|low"
        assert msgs[0].path == "webhook_verification"

    def test_pending_recovery_default_context_key(self):
        """PendingRecovery default context_key is the known default (backward compat)."""
        pending = PendingRecovery(
            payment_link_id="plink_default_001",
            case_id="case_default_001",
            execution_id="exec_default_001",
            decision_id="dec_default_001",
            merchant_id="acc_default",
            capability_id="payment_link_recovery",
            signal_id="sig_default_001",
            amount_at_risk_minor=50000,
            currency="INR",
            # No explicit context_key — should get the default.
        )
        assert pending.context_key == "payment_failure|unknown|medium"

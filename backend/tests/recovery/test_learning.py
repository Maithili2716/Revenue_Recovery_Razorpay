"""Focused tests for the Learning Layer.

Tests:
1. RECOVERED → increments successes
2. NOT_RECOVERED → increments failures
3. UNKNOWN → does NOT update statistics
4. PENDING → does NOT update statistics
5. merchant A learning does not affect merchant B
6. different context keys remain isolated
7. Thompson sample returns float in [0, 1]
8. RECOVERED updates success (explicit)
9. NOT_RECOVERED updates failure (explicit)
"""

from __future__ import annotations

from unittest.mock import patch

from app.recovery.learning.models import StrategyStatistics
from app.recovery.learning.service import LearningService, build_context_key
from app.recovery.learning.store import StrategyStore
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outcome(
    status: VerificationStatus,
    case_id: str = "case_test_001",
) -> VerifiedOutcome:
    return VerifiedOutcome(
        case_id=case_id,
        execution_id="exec_test_001",
        capability_id="payment_link_recovery",
        provider="razorpay",
        provider_reference="plink_test_123",
        status=status,
        amount_at_risk_minor=10000,
        amount_recovered_minor=10000 if status == VerificationStatus.RECOVERED else 0,
        currency="INR",
        reason="test",
    )


# ===========================================================================
# 1. RECOVERED increments successes
# ===========================================================================


class TestRecoveredOutcome:
    def test_recovered_increments_successes(self):
        store = StrategyStore()
        service = LearningService(store=store)

        updated = service.record_outcome(
            merchant_id="merchant_A",
            capability_id="payment_link_recovery",
            context_key="payment_failure|bank|medium",
            verified_outcome=_make_outcome(VerificationStatus.RECOVERED),
        )

        assert updated is True
        stats = service.get_statistics(
            "merchant_A", "payment_link_recovery", "payment_failure|bank|medium"
        )
        assert stats.successes == 2  # 1 prior + 1 success
        assert stats.failures == 1  # 1 prior


# ===========================================================================
# 2. NOT_RECOVERED increments failures
# ===========================================================================


class TestNotRecoveredOutcome:
    def test_not_recovered_increments_failures(self):
        store = StrategyStore()
        service = LearningService(store=store)

        updated = service.record_outcome(
            merchant_id="merchant_A",
            capability_id="payment_link_recovery",
            context_key="payment_failure|bank|medium",
            verified_outcome=_make_outcome(VerificationStatus.NOT_RECOVERED),
        )

        assert updated is True
        stats = service.get_statistics(
            "merchant_A", "payment_link_recovery", "payment_failure|bank|medium"
        )
        assert stats.successes == 1  # 1 prior
        assert stats.failures == 2  # 1 prior + 1 failure


# ===========================================================================
# 3. UNKNOWN does NOT change statistics
# ===========================================================================


class TestUnknownOutcome:
    def test_unknown_does_not_change_stats(self):
        store = StrategyStore()
        service = LearningService(store=store)

        store.record_success("merchant_A", "payment_link_recovery", "ctx")
        before = service.get_statistics("merchant_A", "payment_link_recovery", "ctx")
        before_s = before.successes
        before_f = before.failures

        updated = service.record_outcome(
            merchant_id="merchant_A",
            capability_id="payment_link_recovery",
            context_key="ctx",
            verified_outcome=_make_outcome(VerificationStatus.UNKNOWN),
        )

        assert updated is False
        after = service.get_statistics("merchant_A", "payment_link_recovery", "ctx")
        assert after.successes == before_s
        assert after.failures == before_f


# ===========================================================================
# 4. PENDING does NOT update statistics  (CRITICAL NEW TEST)
# ===========================================================================


class TestPendingOutcome:
    def test_pending_does_not_change_stats(self):
        """PENDING must NEVER update the learning store."""
        store = StrategyStore()
        service = LearningService(store=store)

        updated = service.record_outcome(
            merchant_id="merchant_A",
            capability_id="payment_link_recovery",
            context_key="ctx",
            verified_outcome=_make_outcome(VerificationStatus.PENDING),
        )

        assert updated is False
        stats = service.get_statistics("merchant_A", "payment_link_recovery", "ctx")
        assert stats.successes == 1  # only prior
        assert stats.failures == 1  # only prior

    def test_pending_does_not_add_failure(self):
        """Regression: PENDING was previously mapped to NOT_RECOVERED and
        falsely incremented failures. This must never happen."""
        store = StrategyStore()
        service = LearningService(store=store)

        # Record a PENDING outcome.
        service.record_outcome(
            merchant_id="merchant_A",
            capability_id="payment_link_recovery",
            context_key="payment_failure|bank|low",
            verified_outcome=_make_outcome(VerificationStatus.PENDING),
        )

        stats = service.get_statistics(
            "merchant_A", "payment_link_recovery", "payment_failure|bank|low"
        )

        # Failures must remain at the prior (1), NOT incremented.
        assert stats.failures == 1
        assert stats.total_verified_trials == 0


# ===========================================================================
# 5. Merchant A does not affect merchant B
# ===========================================================================


class TestMerchantIsolation:
    def test_merchants_isolated(self):
        store = StrategyStore()
        service = LearningService(store=store)

        service.record_outcome(
            merchant_id="merchant_A",
            capability_id="payment_link_recovery",
            context_key="ctx",
            verified_outcome=_make_outcome(VerificationStatus.RECOVERED),
        )

        stats_a = service.get_statistics("merchant_A", "payment_link_recovery", "ctx")
        stats_b = service.get_statistics("merchant_B", "payment_link_recovery", "ctx")

        assert stats_a.successes == 2  # 1 prior + 1
        assert stats_b.successes == 1  # only prior (untouched)


# ===========================================================================
# 6. Different context keys remain isolated
# ===========================================================================


class TestContextKeyIsolation:
    def test_context_keys_isolated(self):
        store = StrategyStore()
        service = LearningService(store=store)

        service.record_outcome(
            merchant_id="merchant_A",
            capability_id="payment_link_recovery",
            context_key="payment_failure|bank|high",
            verified_outcome=_make_outcome(VerificationStatus.RECOVERED),
        )

        stats_high = service.get_statistics(
            "merchant_A", "payment_link_recovery", "payment_failure|bank|high"
        )
        stats_low = service.get_statistics(
            "merchant_A", "payment_link_recovery", "payment_failure|bank|low"
        )

        assert stats_high.successes == 2
        assert stats_low.successes == 1  # only prior


# ===========================================================================
# 7. Thompson sample returns float in [0, 1]
# ===========================================================================


class TestThompsonSampling:
    def test_sample_score_returns_float_in_range(self):
        store = StrategyStore()
        store.record_success("merchant_A", "cap_1", "ctx")
        store.record_success("merchant_A", "cap_1", "ctx")
        store.record_failure("merchant_A", "cap_1", "ctx")

        score = store.sample_score("merchant_A", "cap_1", "ctx")
        assert 0.0 <= score <= 1.0

    def test_sample_with_deterministic_mock(self):
        store = StrategyStore()
        store.record_success("merchant_A", "cap_1", "ctx")

        with patch("app.recovery.learning.store.random.betavariate", return_value=0.75):
            score = store.sample_score("merchant_A", "cap_1", "ctx")

        assert score == 0.75


# ===========================================================================
# 8. RECOVERED updates success (explicit test)
# ===========================================================================


class TestRecoveredUpdatesSuccess:
    def test_multiple_successes(self):
        store = StrategyStore()
        service = LearningService(store=store)

        for _ in range(3):
            service.record_outcome(
                merchant_id="m",
                capability_id="c",
                context_key="k",
                verified_outcome=_make_outcome(VerificationStatus.RECOVERED),
            )

        stats = service.get_statistics("m", "c", "k")
        assert stats.successes == 4  # 1 prior + 3
        assert stats.failures == 1  # 1 prior


# ===========================================================================
# 9. NOT_RECOVERED updates failure (explicit test)
# ===========================================================================


class TestNotRecoveredUpdatesFailure:
    def test_multiple_failures(self):
        store = StrategyStore()
        service = LearningService(store=store)

        for _ in range(3):
            service.record_outcome(
                merchant_id="m",
                capability_id="c",
                context_key="k",
                verified_outcome=_make_outcome(VerificationStatus.NOT_RECOVERED),
            )

        stats = service.get_statistics("m", "c", "k")
        assert stats.successes == 1  # 1 prior
        assert stats.failures == 4  # 1 prior + 3


# ===========================================================================
# Additional utility tests
# ===========================================================================


class TestBuildContextKey:
    def test_default_context_key(self):
        key = build_context_key()
        assert key == "payment_failure|unknown|medium"

    def test_custom_context_key(self):
        key = build_context_key("payment_failure", "bank", "high")
        assert key == "payment_failure|bank|high"


class TestStrategyStatisticsProperties:
    def test_total_verified_trials_excludes_prior(self):
        stats = StrategyStatistics(
            merchant_id="m", capability_id="c", context_key="k",
            successes=3, failures=2,
        )
        assert stats.total_verified_trials == 3  # (3+2) - 2 prior

    def test_empirical_success_rate(self):
        stats = StrategyStatistics(
            merchant_id="m", capability_id="c", context_key="k",
            successes=3, failures=1,
        )
        assert stats.empirical_success_rate == 0.75

    def test_fresh_prior_stats(self):
        stats = StrategyStatistics(
            merchant_id="m", capability_id="c", context_key="k",
        )
        assert stats.successes == 1
        assert stats.failures == 1
        assert stats.total_verified_trials == 0
        assert stats.empirical_success_rate == 0.5

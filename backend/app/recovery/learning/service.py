"""Learning service — consumes verified outcomes to update strategy.

CRITICAL RULE:
    This service ONLY accepts VerifiedOutcome.
    It NEVER receives LLM claims, execution status, or API success.

Reward mapping:
    RECOVERED     → successes += 1
    NOT_RECOVERED → failures  += 1
    PENDING       → no update (customer has not yet acted)
    UNKNOWN       → no update (provider may be temporarily unavailable)
"""

from __future__ import annotations

import logging

from app.recovery.learning.models import StrategyStatistics
from app.recovery.learning.store import StrategyStore
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome

logger = logging.getLogger(__name__)


def build_context_key(
    signal_type: str = "payment_failure",
    failure_source: str = "unknown",
    urgency: str = "medium",
) -> str:
    """Build a deterministic context key for learning.

    Format: signal_type|failure_source|urgency

    Small and deterministic — suitable for hackathon MVP.
    """
    return f"{signal_type}|{failure_source}|{urgency}"


class LearningService:
    """Orchestrates learning from verified outcomes."""

    def __init__(self, store: StrategyStore) -> None:
        self._store = store

    def record_outcome(
        self,
        merchant_id: str,
        capability_id: str,
        context_key: str,
        verified_outcome: VerifiedOutcome,
    ) -> bool:
        """Record a verified outcome into the strategy store.

        Returns True if the learning state was updated, False if skipped.

        UNKNOWN outcomes are explicitly skipped — the provider may be
        temporarily unavailable and we must not penalize the capability.
        """
        if verified_outcome.status == VerificationStatus.UNKNOWN:
            logger.info(
                "learning_outcome_skipped",
                extra={
                    "merchant_id": merchant_id,
                    "capability_id": capability_id,
                    "context_key": context_key,
                    "reason": "verification_status_unknown",
                    "case_id": verified_outcome.case_id,
                },
            )
            return False

        if verified_outcome.status == VerificationStatus.PENDING:
            logger.info(
                "learning_outcome_skipped",
                extra={
                    "merchant_id": merchant_id,
                    "capability_id": capability_id,
                    "context_key": context_key,
                    "reason": "verification_status_pending",
                    "case_id": verified_outcome.case_id,
                },
            )
            return False

        if verified_outcome.status == VerificationStatus.RECOVERED:
            stats = self._store.record_success(
                merchant_id, capability_id, context_key
            )
            logger.info(
                "learning_outcome_recorded",
                extra={
                    "merchant_id": merchant_id,
                    "capability_id": capability_id,
                    "context_key": context_key,
                    "reward": "success",
                    "successes": stats.successes,
                    "failures": stats.failures,
                    "empirical_success_rate": round(
                        stats.empirical_success_rate, 4
                    ),
                    "case_id": verified_outcome.case_id,
                },
            )
            return True

        if verified_outcome.status == VerificationStatus.NOT_RECOVERED:
            stats = self._store.record_failure(
                merchant_id, capability_id, context_key
            )
            logger.info(
                "learning_outcome_recorded",
                extra={
                    "merchant_id": merchant_id,
                    "capability_id": capability_id,
                    "context_key": context_key,
                    "reward": "failure",
                    "successes": stats.successes,
                    "failures": stats.failures,
                    "empirical_success_rate": round(
                        stats.empirical_success_rate, 4
                    ),
                    "case_id": verified_outcome.case_id,
                },
            )
            return True

        # Defensive: unexpected status — do not update.
        logger.warning(
            "learning_outcome_unexpected_status",
            extra={
                "merchant_id": merchant_id,
                "capability_id": capability_id,
                "status": verified_outcome.status.value,
                "case_id": verified_outcome.case_id,
            },
        )
        return False

    def get_statistics(
        self,
        merchant_id: str,
        capability_id: str,
        context_key: str,
    ) -> StrategyStatistics:
        """Get current strategy statistics for a merchant/capability/context."""
        return self._store.get_or_create(
            merchant_id, capability_id, context_key
        )

    def sample_score(
        self,
        merchant_id: str,
        capability_id: str,
        context_key: str,
    ) -> float:
        """Thompson sample a score for bandit selection."""
        return self._store.sample_score(
            merchant_id, capability_id, context_key
        )

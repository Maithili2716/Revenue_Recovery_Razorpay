"""Strategy store — in-memory merchant-specific learning state.

Maintains per-(merchant, capability, context_key) Beta-Bernoulli statistics.

For this hackathon MVP, the store is in-memory.
A future block can persist to a database.

Thread safety: this is a single-process application; the store is safe
because background tasks run in a thread-pool executor but the GIL
protects dict mutations.  For production, use proper locking.
"""

from __future__ import annotations

import logging
import random

from app.recovery.learning.models import StrategyStatistics

logger = logging.getLogger(__name__)


def _make_store_key(
    merchant_id: str, capability_id: str, context_key: str
) -> str:
    """Build a composite key for the store."""
    return f"{merchant_id}|{capability_id}|{context_key}"


class StrategyStore:
    """In-memory store for merchant-specific strategy statistics."""

    def __init__(self) -> None:
        self._store: dict[str, StrategyStatistics] = {}

    def get_or_create(
        self,
        merchant_id: str,
        capability_id: str,
        context_key: str,
    ) -> StrategyStatistics:
        """Get existing statistics or create with Beta(1,1) prior."""
        key = _make_store_key(merchant_id, capability_id, context_key)
        if key not in self._store:
            self._store[key] = StrategyStatistics(
                merchant_id=merchant_id,
                capability_id=capability_id,
                context_key=context_key,
                successes=1,
                failures=1,
            )
        return self._store[key]

    def record_success(
        self,
        merchant_id: str,
        capability_id: str,
        context_key: str,
    ) -> StrategyStatistics:
        """Record a verified successful recovery."""
        stats = self.get_or_create(merchant_id, capability_id, context_key)
        stats.successes += 1
        return stats

    def record_failure(
        self,
        merchant_id: str,
        capability_id: str,
        context_key: str,
    ) -> StrategyStatistics:
        """Record a verified failed recovery."""
        stats = self.get_or_create(merchant_id, capability_id, context_key)
        stats.failures += 1
        return stats

    def sample_score(
        self,
        merchant_id: str,
        capability_id: str,
        context_key: str,
    ) -> float:
        """Thompson Sampling: sample from Beta(successes, failures).

        Returns a score in [0, 1] drawn from the posterior distribution.
        Used by the bandit to select among competing capabilities.
        """
        stats = self.get_or_create(merchant_id, capability_id, context_key)
        return random.betavariate(stats.successes, stats.failures)

    def get_all_for_merchant(
        self, merchant_id: str
    ) -> list[StrategyStatistics]:
        """Return all strategy statistics for a merchant."""
        return [
            stats
            for stats in self._store.values()
            if stats.merchant_id == merchant_id
        ]

    def get_all(self) -> list[StrategyStatistics]:
        """Return all strategy statistics (for diagnostics/demo)."""
        return list(self._store.values())

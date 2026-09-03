"""Learning models.

Defines:
- StrategyStatistics — per merchant/capability/context success/failure counts
- LearningContextKey — deterministic context key for merchant-specific learning
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StrategyStatistics(BaseModel):
    """Per-arm statistics for the contextual bandit.

    Uses Beta-Bernoulli representation:
        Beta(successes, failures)

    Initialized with Beta(1, 1) prior (uniform).
    """

    merchant_id: str
    capability_id: str
    context_key: str

    successes: int = Field(
        default=1,
        ge=0,
        description="Number of verified successful recoveries + prior.",
    )
    failures: int = Field(
        default=1,
        ge=0,
        description="Number of verified failed recoveries + prior.",
    )

    @property
    def total_verified_trials(self) -> int:
        """Total verified trials (excluding the initial prior of 2)."""
        return max(0, self.successes + self.failures - 2)

    @property
    def empirical_success_rate(self) -> float:
        """Empirical success rate based on verified trials.

        Returns the posterior mean: successes / (successes + failures).
        With the Beta(1,1) prior this is a Laplace-smoothed estimate.
        """
        total = self.successes + self.failures
        if total == 0:
            return 0.0
        return self.successes / total

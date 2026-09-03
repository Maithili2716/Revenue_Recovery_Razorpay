"""Contextual bandit — action selection component.

Selects among candidate recovery ACTIONS (not prompts, not natural language).
Uses the AgentContext as the context for selection.

STRATEGIES:
1. Deterministic priority (fallback) — used when:
   - no learning history exists
   - only one eligible candidate
   - learning store is not available

2. Thompson Sampling — used when:
   - multiple eligible candidates exist
   - learning statistics are available from the strategy store

IMPORTANT:
- With a single candidate, decision is effectively deterministic.
- Do not claim meaningful adaptation until multiple capabilities compete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.recovery.agent.models import (
    AgentContext,
    CandidateAction,
    EligibilityStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BanditSelection:
    """The result of the bandit's action selection."""

    selected: CandidateAction
    """The action chosen by the bandit."""

    selection_reason: str
    """Human-readable reason for why this action was selected."""


class ContextualBandit:
    """Contextual bandit for recovery action selection.

    Supports two strategies:
    1. Deterministic priority (default fallback)
    2. Thompson Sampling via learned merchant-specific statistics

    The learning_store is optional — when absent or when statistics are
    not available, falls back to deterministic priority.
    """

    def __init__(self, learning_store=None) -> None:
        """Initialize with optional learning store.

        Args:
            learning_store: A StrategyStore instance for Thompson Sampling.
                            None means deterministic-only mode.
        """
        self._learning_store = learning_store

    def select(
        self,
        candidates: list[CandidateAction],
        context: AgentContext,
    ) -> BanditSelection | None:
        """Choose the best action from the candidate list.

        Args:
            candidates: Ordered list of candidate actions.
            context: The agent context.

        Returns:
            A BanditSelection, or None if no eligible candidate exists.
        """
        eligible = [
            c for c in candidates
            if c.eligibility == EligibilityStatus.ELIGIBLE
        ]

        if not eligible:
            logger.warning(
                "bandit_no_eligible_candidates",
                extra={"case_id": context.case_id, "total_candidates": len(candidates)},
            )
            return None

        # Build context key for learning (must match learning service).
        context_key = self._build_context_key(context)

        # Try Thompson Sampling if learning store is available and
        # there are multiple eligible candidates.
        if self._learning_store is not None and len(eligible) > 1:
            return self._thompson_select(eligible, context, context_key)

        # Fallback: deterministic priority.
        return self._deterministic_select(eligible, context, context_key)

    def _deterministic_select(
        self,
        eligible: list[CandidateAction],
        context: AgentContext,
        context_key: str,
    ) -> BanditSelection:
        """Pick the highest-priority eligible candidate."""
        selected = eligible[0]

        strategy = "deterministic_priority"
        reason = (
            f"Selected {selected.capability_id} (priority={selected.priority}) "
            f"from {len(eligible)} eligible candidate(s) using {strategy}."
        )

        self._log_selection(
            context=context,
            selected=selected,
            eligible=eligible,
            context_key=context_key,
            strategy=strategy,
            learning_available=False,
        )

        return BanditSelection(selected=selected, selection_reason=reason)

    def _thompson_select(
        self,
        eligible: list[CandidateAction],
        context: AgentContext,
        context_key: str,
    ) -> BanditSelection:
        """Thompson Sampling: sample scores and pick the highest."""
        scores = {}
        for candidate in eligible:
            score = self._learning_store.sample_score(
                merchant_id=context.merchant_id,
                capability_id=candidate.capability_id,
                context_key=context_key,
            )
            scores[candidate.capability_id] = score

        # Pick the candidate with the highest sampled score.
        best = max(eligible, key=lambda c: scores[c.capability_id])

        strategy = "thompson_sampling"
        reason = (
            f"Selected {best.capability_id} "
            f"(sampled_score={scores[best.capability_id]:.4f}) "
            f"from {len(eligible)} eligible candidate(s) using {strategy}. "
            f"Scores: {', '.join(f'{k}={v:.4f}' for k, v in scores.items())}."
        )

        self._log_selection(
            context=context,
            selected=best,
            eligible=eligible,
            context_key=context_key,
            strategy=strategy,
            learning_available=True,
            scores=scores,
        )

        return BanditSelection(selected=best, selection_reason=reason)

    @staticmethod
    def _build_context_key(context: AgentContext) -> str:
        """Build the context key matching the learning service format."""
        signal_type = context.signal_type or "payment_failure"
        failure_source = context.failure_source or "unknown"
        urgency = context.urgency or "medium"
        return f"{signal_type}|{failure_source}|{urgency}"

    @staticmethod
    def _log_selection(
        *,
        context: AgentContext,
        selected: CandidateAction,
        eligible: list[CandidateAction],
        context_key: str,
        strategy: str,
        learning_available: bool,
        scores: dict | None = None,
    ) -> None:
        """Emit structured selection log."""
        extra = {
            "case_id": context.case_id,
            "merchant_id": context.merchant_id,
            "context_key": context_key,
            "selected_capability_id": selected.capability_id,
            "selected_action_type": selected.action_type.value,
            "eligible_candidates": [c.capability_id for c in eligible],
            "eligible_count": len(eligible),
            "strategy": strategy,
            "learning_available": learning_available,
        }
        if scores:
            extra["sampled_scores"] = {
                k: round(v, 4) for k, v in scores.items()
            }

        logger.info("bandit_action_selected", extra=extra)

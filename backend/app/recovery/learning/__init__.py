"""Learning layer package.

Consumes VERIFIED outcomes to build merchant-specific recovery
strategy statistics for the contextual bandit.

    VerifiedOutcome → LearningService → strategy store update

CRITICAL:
    Learning ONLY consumes verified outcomes.
    LLM confidence, execution status, or API success are NOT rewards.
    UNKNOWN outcomes do NOT update the strategy store.
"""

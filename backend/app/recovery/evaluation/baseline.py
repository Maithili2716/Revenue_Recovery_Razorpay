"""Fixed-strategy baseline for the held-out simulation."""


def select_baseline_strategy() -> str:
    """The baseline intentionally never adapts."""
    return "payment_link_recovery"

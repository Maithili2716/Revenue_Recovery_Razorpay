"""Verification engine package.

Determines whether a recovery action resulted in actual financial recovery
by independently querying the provider (Razorpay).

    ExecutionResult → VerificationService → VerifiedOutcome

CRITICAL:
    execution_status == EXECUTED  ≠  money recovered
    Only the verification engine establishes financial truth.
"""

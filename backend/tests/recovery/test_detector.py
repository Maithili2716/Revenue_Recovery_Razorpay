"""Block 3B tests — deterministic risk detector.

Tests:
1.  positive payment failure creates RecoveryCase
2.  amount is preserved in minor units
3.  risk_status is AT_RISK
4.  customer_id is propagated from signal
5.  recoverability is LIKELY when failure_source is a normal recoverable source
6.  recoverability is UNKNOWN when failure_source is missing/None
7.  urgency LOW below ₹1,000 (< 100_000 paise)
8.  urgency MEDIUM from ₹1,000 through ₹9,999.99
9.  urgency HIGH at ₹10,000 and above (>= 1_000_000 paise)
10. reason_codes are deterministic and ordered
11. same signal always produces same case_id
12. zero amount → None (no RecoveryCase)
13. negative amount → None (no RecoveryCase)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:5432/revenue_recovery_test",
)
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")

from app.recovery.detector import detect_recovery_case
from app.recovery.models import Recoverability, RiskStatus, Urgency, build_case_id
from app.signals.models import RevenueSignal, SignalStatus, SignalType

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_OCCURRED_AT = datetime(2026, 9, 2, 8, 0, 0, tzinfo=timezone.utc)


def _make_signal(**overrides) -> RevenueSignal:
    """Return a minimal but normalizer-compliant RevenueSignal."""
    defaults = dict(
        signal_id="sig_test_det_001",
        merchant_id="acc_TestDetector",
        customer_id=None,
        signal_type=SignalType.PAYMENT_FAILURE,
        status=SignalStatus.FAILED,
        amount_minor=49900,          # ₹499
        currency="INR",
        provider="razorpay",
        provider_event_id="evt_det_001",
        provider_entity_id="pay_det_001",
        reason="Payment failed (test).",
        failure_source="customer",
        failure_step="payment_authorization",
        occurred_at=_OCCURRED_AT,
        raw_event_type="payment.failed",
        metadata={"method": "card"},
    )
    defaults.update(overrides)
    return RevenueSignal(**defaults)


# ---------------------------------------------------------------------------
# 1. Positive payment failure creates RecoveryCase
# ---------------------------------------------------------------------------


def test_positive_payment_failure_creates_recovery_case() -> None:
    case = detect_recovery_case(_make_signal())
    assert case is not None


# ---------------------------------------------------------------------------
# 2. Amount is preserved in minor units
# ---------------------------------------------------------------------------


def test_amount_at_risk_minor_matches_signal_amount() -> None:
    case = detect_recovery_case(_make_signal(amount_minor=75000))
    assert case is not None
    assert case.amount_at_risk_minor == 75000


# ---------------------------------------------------------------------------
# 3. risk_status is AT_RISK
# ---------------------------------------------------------------------------


def test_risk_status_is_at_risk() -> None:
    case = detect_recovery_case(_make_signal())
    assert case is not None
    assert case.risk_status == RiskStatus.AT_RISK


# ---------------------------------------------------------------------------
# 4. customer_id is propagated from signal
# ---------------------------------------------------------------------------


def test_customer_id_is_none_when_signal_has_none() -> None:
    case = detect_recovery_case(_make_signal(customer_id=None))
    assert case is not None
    assert case.customer_id is None


def test_customer_id_is_propagated_when_present() -> None:
    case = detect_recovery_case(_make_signal(customer_id="cust_xyz"))
    assert case is not None
    assert case.customer_id == "cust_xyz"


# ---------------------------------------------------------------------------
# 5. recoverability LIKELY for normal recoverable sources
# ---------------------------------------------------------------------------


def test_recoverability_is_likely_for_customer_source() -> None:
    case = detect_recovery_case(_make_signal(failure_source="customer"))
    assert case is not None
    assert case.recoverability == Recoverability.LIKELY


def test_recoverability_is_likely_for_bank_source() -> None:
    # "bank" is not in the unrecoverable set; retry may still succeed.
    case = detect_recovery_case(_make_signal(failure_source="bank"))
    assert case is not None
    assert case.recoverability == Recoverability.LIKELY


# ---------------------------------------------------------------------------
# 6. recoverability UNKNOWN when failure_source is missing
# ---------------------------------------------------------------------------


def test_recoverability_is_unknown_when_failure_source_is_none() -> None:
    case = detect_recovery_case(_make_signal(failure_source=None))
    assert case is not None
    assert case.recoverability == Recoverability.UNKNOWN


def test_recoverability_is_unknown_when_failure_source_is_empty_string() -> None:
    case = detect_recovery_case(_make_signal(failure_source=""))
    assert case is not None
    assert case.recoverability == Recoverability.UNKNOWN


# ---------------------------------------------------------------------------
# 7. urgency LOW below ₹1,000
# ---------------------------------------------------------------------------


def test_urgency_low_for_small_amount() -> None:
    case = detect_recovery_case(_make_signal(amount_minor=5000))   # ₹50
    assert case is not None
    assert case.urgency == Urgency.LOW


def test_urgency_low_at_threshold_boundary() -> None:
    case = detect_recovery_case(_make_signal(amount_minor=99_999))  # just under ₹1,000
    assert case is not None
    assert case.urgency == Urgency.LOW


# ---------------------------------------------------------------------------
# 8. urgency MEDIUM from ₹1,000 through ₹9,999.99
# ---------------------------------------------------------------------------


def test_urgency_medium_at_lower_boundary() -> None:
    case = detect_recovery_case(_make_signal(amount_minor=100_000))  # exactly ₹1,000
    assert case is not None
    assert case.urgency == Urgency.MEDIUM


def test_urgency_medium_in_middle() -> None:
    case = detect_recovery_case(_make_signal(amount_minor=500_000))  # ₹5,000
    assert case is not None
    assert case.urgency == Urgency.MEDIUM


def test_urgency_medium_at_upper_boundary() -> None:
    case = detect_recovery_case(_make_signal(amount_minor=999_999))  # just under ₹10,000
    assert case is not None
    assert case.urgency == Urgency.MEDIUM


# ---------------------------------------------------------------------------
# 9. urgency HIGH at ₹10,000 and above
# ---------------------------------------------------------------------------


def test_urgency_high_at_lower_boundary() -> None:
    case = detect_recovery_case(_make_signal(amount_minor=1_000_000))  # exactly ₹10,000
    assert case is not None
    assert case.urgency == Urgency.HIGH


def test_urgency_high_for_large_amount() -> None:
    case = detect_recovery_case(_make_signal(amount_minor=5_000_000))  # ₹50,000
    assert case is not None
    assert case.urgency == Urgency.HIGH


# ---------------------------------------------------------------------------
# 10. reason_codes are deterministic and ordered
# ---------------------------------------------------------------------------


def test_reason_codes_always_start_with_payment_failed() -> None:
    case = detect_recovery_case(_make_signal())
    assert case is not None
    assert case.reason_codes[0] == "payment_failed"


def test_reason_codes_include_failure_source_code_when_present() -> None:
    case = detect_recovery_case(_make_signal(failure_source="customer"))
    assert case is not None
    assert "failure_source:customer" in case.reason_codes


def test_reason_codes_do_not_include_source_code_when_absent() -> None:
    case = detect_recovery_case(_make_signal(failure_source=None))
    assert case is not None
    assert case.reason_codes == ["payment_failed"]


def test_reason_codes_ordering_is_deterministic() -> None:
    sig = _make_signal(failure_source="gateway")
    case1 = detect_recovery_case(sig)
    case2 = detect_recovery_case(sig)
    assert case1 is not None and case2 is not None
    assert case1.reason_codes == case2.reason_codes


# ---------------------------------------------------------------------------
# 11. Same signal always produces same case_id
# ---------------------------------------------------------------------------


def test_case_id_is_deterministic_for_same_signal() -> None:
    sig = _make_signal()
    case1 = detect_recovery_case(sig)
    case2 = detect_recovery_case(sig)
    assert case1 is not None and case2 is not None
    assert case1.case_id == case2.case_id


def test_case_id_matches_build_case_id_helper() -> None:
    sig = _make_signal(signal_id="sig_known_001")
    case = detect_recovery_case(sig)
    assert case is not None
    assert case.case_id == build_case_id("sig_known_001")


# ---------------------------------------------------------------------------
# 12 & 13. Zero / negative amount → no RecoveryCase
# ---------------------------------------------------------------------------


def test_zero_amount_returns_none() -> None:
    assert detect_recovery_case(_make_signal(amount_minor=0)) is None


def test_negative_amount_returns_none() -> None:
    assert detect_recovery_case(_make_signal(amount_minor=-100)) is None

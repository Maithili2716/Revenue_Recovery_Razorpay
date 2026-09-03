"""Tests for Block 3A: RecoveryCase model contract.

Verifies:
- a valid RecoveryCase can be constructed
- enums serialize to their string values
- customer_id can be None
- amount_at_risk_minor is stored as an integer (no float conversion)
- case_id generation is deterministic for the same signal_id
- two different signal IDs produce different case IDs
- invalid enum values are rejected by Pydantic
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:5432/revenue_recovery_test",
)
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")

from pydantic import ValidationError

from app.recovery.models import (
    RecoveryCase,
    Recoverability,
    RiskStatus,
    Urgency,
    build_case_id,
)

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 2, 8, 0, 0, tzinfo=timezone.utc)


def _make_case(**overrides) -> RecoveryCase:
    defaults = dict(
        case_id=build_case_id("sig_abc123"),
        signal_id="sig_abc123",
        merchant_id="acc_TestMerchant",
        customer_id=None,
        amount_at_risk_minor=49900,
        currency="INR",
        risk_status=RiskStatus.AT_RISK,
        recoverability=Recoverability.LIKELY,
        urgency=Urgency.HIGH,
        reason_codes=["insufficient_funds"],
        created_at=_NOW,
    )
    defaults.update(overrides)
    return RecoveryCase(**defaults)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_valid_recovery_case_can_be_constructed() -> None:
    case = _make_case()
    assert isinstance(case, RecoveryCase)


def test_all_required_fields_are_present() -> None:
    case = _make_case()
    assert case.signal_id == "sig_abc123"
    assert case.merchant_id == "acc_TestMerchant"
    assert case.amount_at_risk_minor == 49900
    assert case.currency == "INR"
    assert case.created_at == _NOW


# ---------------------------------------------------------------------------
# Enum serialization
# ---------------------------------------------------------------------------


def test_risk_status_serializes_to_string() -> None:
    case = _make_case(risk_status=RiskStatus.AT_RISK)
    assert case.model_dump()["risk_status"] == "at_risk"


def test_recoverability_serializes_to_string() -> None:
    case = _make_case(recoverability=Recoverability.LIKELY)
    assert case.model_dump()["recoverability"] == "likely"


def test_urgency_serializes_to_string() -> None:
    case = _make_case(urgency=Urgency.HIGH)
    assert case.model_dump()["urgency"] == "high"


def test_all_risk_status_values_are_valid() -> None:
    for value in RiskStatus:
        _make_case(risk_status=value)


def test_all_recoverability_values_are_valid() -> None:
    for value in Recoverability:
        _make_case(recoverability=value)


def test_all_urgency_values_are_valid() -> None:
    for value in Urgency:
        _make_case(urgency=value)


# ---------------------------------------------------------------------------
# Nullable customer_id
# ---------------------------------------------------------------------------


def test_customer_id_defaults_to_none() -> None:
    case = _make_case()
    assert case.customer_id is None


def test_customer_id_can_be_set() -> None:
    case = _make_case(customer_id="cust_xyz")
    assert case.customer_id == "cust_xyz"


# ---------------------------------------------------------------------------
# Amount stays integer
# ---------------------------------------------------------------------------


def test_amount_at_risk_minor_is_integer() -> None:
    case = _make_case(amount_at_risk_minor=49900)
    assert isinstance(case.amount_at_risk_minor, int)
    assert case.amount_at_risk_minor == 49900


# ---------------------------------------------------------------------------
# case_id determinism
# ---------------------------------------------------------------------------


def test_build_case_id_is_deterministic() -> None:
    assert build_case_id("sig_abc123") == build_case_id("sig_abc123")


def test_different_signal_ids_produce_different_case_ids() -> None:
    assert build_case_id("sig_aaa") != build_case_id("sig_bbb")


def test_case_id_has_case_prefix() -> None:
    assert build_case_id("sig_abc123").startswith("case_")


def test_case_id_stored_on_model() -> None:
    cid = build_case_id("sig_abc123")
    case = _make_case(case_id=cid, signal_id="sig_abc123")
    assert case.case_id == cid


# ---------------------------------------------------------------------------
# Invalid enum values are rejected
# ---------------------------------------------------------------------------


def test_invalid_risk_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_case(risk_status="definitely_at_risk")


def test_invalid_recoverability_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_case(recoverability="maybe")


def test_invalid_urgency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_case(urgency="extreme")

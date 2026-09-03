"""Deterministic synthetic, held-out evaluation data only.

Invoice contexts below are simulations; production signal and capability layers
remain payment-failure-only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.recovery.evaluation.models import EvaluationCase
from app.recovery.models import Recoverability, RecoveryCase, RiskStatus, Urgency
from app.signals.models import RevenueSignal, SignalStatus, SignalType

_MERCHANTS = ("merchant_eval_a", "merchant_eval_b", "merchant_eval_c", "merchant_eval_d", "merchant_eval_e")

# context, source, urgency, amount, has pending link, held-out effectiveness
_PROFILES = (
    ("new_payment_failure", "bank", Urgency.MEDIUM, 125_000, False, {"payment_link_recovery": 76, "payment_link_reminder": 0, "invoice_recovery": 0}),
    ("new_payment_failure", "gateway", Urgency.HIGH, 320_000, False, {"payment_link_recovery": 71, "payment_link_reminder": 0, "invoice_recovery": 0}),
    ("new_payment_failure", "customer", Urgency.LOW, 85_000, False, {"payment_link_recovery": 67, "payment_link_reminder": 0, "invoice_recovery": 0}),
    ("new_payment_failure", "bank", Urgency.HIGH, 410_000, False, {"payment_link_recovery": 73, "payment_link_reminder": 0, "invoice_recovery": 0}),
    ("existing_payment_link", "customer", Urgency.MEDIUM, 185_000, True, {"payment_link_recovery": 49, "payment_link_reminder": 78, "invoice_recovery": 0}),
    ("existing_payment_link", "bank", Urgency.HIGH, 260_000, True, {"payment_link_recovery": 45, "payment_link_reminder": 74, "invoice_recovery": 0}),
    ("existing_payment_link", "gateway", Urgency.LOW, 115_000, True, {"payment_link_recovery": 51, "payment_link_reminder": 71, "invoice_recovery": 0}),
    ("overdue_invoice", "invoice", Urgency.HIGH, 560_000, False, {"payment_link_recovery": 31, "payment_link_reminder": 0, "invoice_recovery": 82}),
    ("overdue_invoice", "invoice", Urgency.MEDIUM, 475_000, False, {"payment_link_recovery": 35, "payment_link_reminder": 0, "invoice_recovery": 76}),
    ("overdue_invoice", "invoice", Urgency.LOW, 230_000, False, {"payment_link_recovery": 38, "payment_link_reminder": 0, "invoice_recovery": 69}),
)

_MERCHANT_OFFSETS = {"merchant_eval_a": 3, "merchant_eval_b": -4, "merchant_eval_c": 1, "merchant_eval_d": 5, "merchant_eval_e": -2}


def build_held_out_dataset() -> list[EvaluationCase]:
    """Build 50 stable, non-production cases across three recovery contexts."""
    cases: list[EvaluationCase] = []
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for merchant_index, merchant_id in enumerate(_MERCHANTS):
        for profile_index, (context, source, urgency, amount, has_pending_link, effectiveness) in enumerate(_PROFILES):
            sequence = merchant_index * len(_PROFILES) + profile_index
            is_invoice = context == "overdue_invoice"
            signal = RevenueSignal(
                signal_id=f"eval_signal_{sequence:03d}", merchant_id=merchant_id,
                signal_type=SignalType.PAYMENT_FAILURE, status=SignalStatus.FAILED,
                amount_minor=amount, currency="INR", provider="simulated_evaluation",
                provider_event_id=f"eval_event_{sequence:03d}", provider_entity_id=f"eval_entity_{sequence:03d}",
                failure_source=source, occurred_at=start + timedelta(minutes=sequence),
                raw_event_type="simulated.invoice.overdue" if is_invoice else "simulated.payment.failed",
                metadata={"evaluation_context": context, "simulated": True},
            )
            recovery_case = RecoveryCase(
                case_id=f"eval_case_{sequence:03d}", signal_id=signal.signal_id,
                merchant_id=merchant_id, amount_at_risk_minor=amount, currency="INR",
                risk_status=RiskStatus.AT_RISK, recoverability=Recoverability.LIKELY,
                urgency=urgency, reason_codes=["simulated_held_out", f"evaluation_context:{context}"],
                created_at=signal.occurred_at,
            )
            cases.append(EvaluationCase(
                signal=signal, recovery_case=recovery_case,
                pending_payment_link_id=f"eval_pending_link_{sequence:03d}" if has_pending_link else None,
                recovery_context=context,
                strategy_effectiveness=_held_out_effectiveness(effectiveness, merchant_id),
            ))
    return cases


def evaluation_calibration_profiles() -> dict[str, tuple[tuple[str, str, dict[str, int]], ...]]:
    """Separate simulated historical calibration keyed by merchant and context."""
    profiles: dict[str, tuple[tuple[str, str, dict[str, int]], ...]] = {}
    for merchant_id in _MERCHANTS:
        profiles[merchant_id] = tuple(
            (source, urgency.value, _calibration_effectiveness(effectiveness, merchant_id))
            for _, source, urgency, _, _, effectiveness in _PROFILES
        )
    return profiles


def _held_out_effectiveness(effectiveness: dict[str, int], merchant_id: str) -> dict[str, int]:
    return {strategy: _bounded(rate + _MERCHANT_OFFSETS[merchant_id]) if rate else 0 for strategy, rate in effectiveness.items()}


def _calibration_effectiveness(effectiveness: dict[str, int], merchant_id: str) -> dict[str, int]:
    return {strategy: _bounded(rate + _MERCHANT_OFFSETS[merchant_id] - 6) if rate else 0 for strategy, rate in effectiveness.items()}


def _bounded(rate: int) -> int:
    return max(0, min(100, rate))

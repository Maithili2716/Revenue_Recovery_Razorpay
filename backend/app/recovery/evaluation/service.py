"""In-memory service for running and retrieving held-out evaluation results."""

from __future__ import annotations

from app.recovery.evaluation.dataset import build_held_out_dataset
from app.recovery.evaluation.evaluator import evaluate
from app.recovery.evaluation.models import EvaluationResult


class EvaluationService:
    """Keeps the latest explicitly-run simulated evaluation in memory."""

    def __init__(self) -> None:
        self._latest: EvaluationResult | None = None

    def run(self) -> EvaluationResult:
        self._latest = evaluate(build_held_out_dataset())
        return self._latest

    def latest(self) -> EvaluationResult | None:
        return self._latest


_evaluation_service = EvaluationService()


def get_evaluation_service() -> EvaluationService:
    return _evaluation_service

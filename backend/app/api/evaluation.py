"""Read and run the held-out simulated evaluation benchmark."""

from fastapi import APIRouter, HTTPException, status

from app.recovery.evaluation.service import get_evaluation_service

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


@router.get("/latest")
def latest_evaluation() -> dict[str, object]:
    """Return the latest run; no result exists until the benchmark is run."""
    result = get_evaluation_service().latest()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No held-out evaluation has been run.",
        )
    return result.as_dict()


@router.post("/run")
def run_evaluation() -> dict[str, object]:
    """Run the deterministic, simulated held-out benchmark."""
    return get_evaluation_service().run().as_dict()

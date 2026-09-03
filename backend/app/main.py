import logging

from fastapi import FastAPI

from app.api.dashboard import router as dashboard_router
from app.api.demo import router as demo_router
from app.api.evaluation import router as evaluation_router
from app.api.razorpay_webhooks import router as razorpay_webhooks_router
from app.api.recovery import router as recovery_router

# Standard LogRecord attributes — excluded when rendering extra fields so
# only the application-supplied extras appear in the log line.
_STANDARD_LOG_ATTRS = frozenset(
    {
        "args", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs",
        "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


class _DevFormatter(logging.Formatter):
    """Development formatter that appends extra fields as key=value pairs.

    Standard usage of ``logger.info(msg, extra={...})`` passes fields into
    the LogRecord as top-level attributes alongside the standard ones.  The
    default Formatter ignores them unless explicitly named in the format
    string.  This formatter appends all application-supplied extras so that
    structured log records (e.g. revenue_signal_normalized) are fully
    human-readable in the terminal.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_LOG_ATTRS and not k.startswith("_")
        }
        if not extras:
            return base
        fields = "  ".join(f"{k}={v!r}" for k, v in extras.items())
        return f"{base}  {fields}"


def _configure_logging() -> None:
    """Configure the root logger for development.

    Uses _DevFormatter so that application INFO records (including the
    revenue_signal_normalized structured fields) are visible in the
    Uvicorn terminal.  Uvicorn's own access/error handlers are unaffected
    because Uvicorn configures its loggers directly.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        _DevFormatter("%(asctime)s %(levelname)-8s %(name)s  %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Only add the handler once (guard against re-import during --reload).
    if not root.handlers:
        root.addHandler(handler)


_configure_logging()

app = FastAPI()

app.include_router(razorpay_webhooks_router)
app.include_router(recovery_router)
app.include_router(dashboard_router)
app.include_router(demo_router)
app.include_router(evaluation_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

from threading import Lock
from typing import Protocol


class WebhookIdempotencyStore(Protocol):
    def record(self, event_id: str) -> bool:
        """Return True when the event is new, False when it is a duplicate."""


class InMemoryWebhookIdempotencyStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = Lock()

    def record(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._seen:
                return False
            self._seen.add(event_id)
            return True

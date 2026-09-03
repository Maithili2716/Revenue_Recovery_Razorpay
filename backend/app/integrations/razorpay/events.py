from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RazorpayWebhookEvent:
    event_type: str | None
    event_id: str | None
    raw_body: bytes
    payload: dict[str, Any]


def extract_event_type(payload: dict[str, Any]) -> str | None:
    event_type = payload.get("event")
    return event_type if isinstance(event_type, str) else None


def extract_event_id(
    payload: dict[str, Any],
    headers: Mapping[str, str],
) -> str | None:
    header_event_id = headers.get("x-razorpay-event-id") or headers.get(
        "X-Razorpay-Event-Id"
    )
    if isinstance(header_event_id, str) and header_event_id:
        return header_event_id

    top_level_id = payload.get("id")
    if isinstance(top_level_id, str) and top_level_id:
        return top_level_id

    event_type = extract_event_type(payload)
    nested_payload = payload.get("payload")
    if not isinstance(nested_payload, dict):
        return None

    for value in nested_payload.values():
        if not isinstance(value, dict):
            continue
        entity = value.get("entity")
        if isinstance(entity, dict):
            entity_id = entity.get("id")
            if isinstance(entity_id, str) and entity_id:
                if event_type:
                    return f"{event_type}:{entity_id}"
                return entity_id

    return None

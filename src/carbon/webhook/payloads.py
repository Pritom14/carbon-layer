"""Razorpay-style webhook payload builder."""

from __future__ import annotations

import time
from typing import Any, Iterable


ENTITY_STATE_TO_EVENT: dict[tuple[str, str], str] = {
    ("payment", "authorized"): "payment.authorized",
    ("payment", "captured"): "payment.captured",
    ("payment", "failed"): "payment.failed",
    ("dispute", "open"): "payment.dispute.created",
    ("refund", "processed"): "refund.processed",
    ("refund", "failed"): "refund.failed",
}


def _normalized_state(state: str | None) -> str:
    return (state or "").strip().lower()


def entity_to_event_type(entity_type: str, state: str | None) -> str | None:
    """Map entity+state to Razorpay event type."""
    key = (entity_type.strip().lower(), _normalized_state(state))
    return ENTITY_STATE_TO_EVENT.get(key)


def build_event_payload(
    *,
    event_type: str,
    entity_type: str,
    remote_id: str,
    metadata: dict | None,
    account_id: str = "acc_carbon",
) -> dict[str, Any]:
    """
    Build a minimal Razorpay-like webhook payload:
    - entity: event
    - account_id
    - event
    - created_at
    - contains: [entity_type]
    - payload: {<entity_type>: {entity: {...}}}
    """
    created_at = int(time.time())
    entity_obj = dict(metadata or {})
    if remote_id and "id" not in entity_obj:
        entity_obj["id"] = remote_id
    if "entity" not in entity_obj:
        entity_obj["entity"] = entity_type
    return {
        "entity": "event",
        "account_id": account_id,
        "event": event_type,
        "created_at": created_at,
        "contains": [entity_type],
        "payload": {entity_type: {"entity": entity_obj}},
    }


def build_events_from_entity_map(
    entity_map: dict,
    *,
    account_id: str = "acc_carbon",
) -> list[dict[str, Any]]:
    """Build webhook events for all entities in entity_map with a known mapping."""
    events: list[dict[str, Any]] = []
    for local_id, info in entity_map.items():
        entity_type = (info.get("entity_type") or "").strip().lower()
        state = info.get("state")
        event_type = entity_to_event_type(entity_type, state)
        if not event_type:
            continue
        remote_id = info.get("remote_id") or ""
        metadata = info.get("metadata") or {}
        events.append(
            {
                "event_type": event_type,
                "entity_type": entity_type,
                "local_id": local_id,
                "remote_id": remote_id,
                "payload": build_event_payload(
                    event_type=event_type,
                    entity_type=entity_type,
                    remote_id=remote_id,
                    metadata=metadata,
                    account_id=account_id,
                ),
            }
        )
    return events


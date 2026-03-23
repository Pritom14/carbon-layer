"""Provider-aware webhook payload builder."""

from __future__ import annotations

import time
import uuid
from typing import Any


# ---------------------------------------------------------------------------
# Razorpay event mapping & payload format
# ---------------------------------------------------------------------------

RAZORPAY_ENTITY_STATE_TO_EVENT: dict[tuple[str, str], str] = {
    ("payment", "authorized"): "payment.authorized",
    ("payment", "captured"): "payment.captured",
    ("payment", "failed"): "payment.failed",
    ("dispute", "open"): "payment.dispute.created",
    ("refund", "processed"): "refund.processed",
    ("refund", "failed"): "refund.failed",
}


def _build_razorpay_payload(
    *,
    event_type: str,
    entity_type: str,
    remote_id: str,
    metadata: dict | None,
    account_id: str,
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Stripe event mapping & payload format
# ---------------------------------------------------------------------------

STRIPE_ENTITY_STATE_TO_EVENT: dict[tuple[str, str], str] = {
    ("payment", "authorized"): "payment_intent.amount_capturable_updated",
    ("payment", "captured"): "payment_intent.succeeded",
    ("payment", "failed"): "payment_intent.payment_failed",
    ("dispute", "open"): "charge.dispute.created",
    ("refund", "processed"): "charge.refunded",
    ("refund", "failed"): "refund.failed",
}

# Map entity_type to Stripe object type
_STRIPE_OBJECT_TYPE: dict[str, str] = {
    "payment": "payment_intent",
    "dispute": "dispute",
    "refund": "refund",
}


def _build_stripe_payload(
    *,
    event_type: str,
    entity_type: str,
    remote_id: str,
    metadata: dict | None,
    account_id: str,
) -> dict[str, Any]:
    created = int(time.time())
    obj = dict(metadata or {})
    if remote_id and "id" not in obj:
        obj["id"] = remote_id
    obj_type = _STRIPE_OBJECT_TYPE.get(entity_type, entity_type)
    if "object" not in obj:
        obj["object"] = obj_type
    return {
        "id": f"evt_{uuid.uuid4().hex[:24]}",
        "object": "event",
        "account": account_id if account_id != "acc_carbon" else None,
        "api_version": "2025-03-31.basil",
        "created": created,
        "data": {
            "object": obj,
            "previous_attributes": None,
        },
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": f"req_{uuid.uuid4().hex[:14]}", "idempotency_key": None},
        "type": event_type,
    }


# ---------------------------------------------------------------------------
# Cashfree event mapping & payload format
# ---------------------------------------------------------------------------

CASHFREE_ENTITY_STATE_TO_EVENT: dict[tuple[str, str], str] = {
    ("payment", "authorized"): "PAYMENT_SUCCESS_WEBHOOK",
    ("payment", "captured"): "PAYMENT_SUCCESS_WEBHOOK",
    ("payment", "failed"): "PAYMENT_FAILED_WEBHOOK",
    ("dispute", "open"): "DISPUTE_CREATED",
    ("refund", "processed"): "REFUND_STATUS_WEBHOOK",
    ("refund", "failed"): "REFUND_STATUS_WEBHOOK",
}

# Map entity_type to Cashfree data key
_CASHFREE_DATA_KEY: dict[str, str] = {
    "payment": "payment",
    "dispute": "dispute",
    "refund": "refund",
}

# Map internal state to Cashfree payment/refund status
_CASHFREE_STATUS: dict[tuple[str, str], str] = {
    ("payment", "authorized"): "SUCCESS",
    ("payment", "captured"): "SUCCESS",
    ("payment", "failed"): "FAILED",
    ("refund", "processed"): "SUCCESS",
    ("refund", "failed"): "FAILED",
}


def _build_cashfree_payload(
    *,
    event_type: str,
    entity_type: str,
    remote_id: str,
    metadata: dict | None,
    account_id: str,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    event_time = datetime.now(timezone.utc).isoformat()
    obj = dict(metadata or {})

    if entity_type == "payment":
        cf_payment_id = remote_id or f"cf_{uuid.uuid4().hex[:12]}"
        order_id = obj.pop("order_id", f"order_{uuid.uuid4().hex[:8]}")
        amount = obj.pop("amount", 100)
        # Convert paise to rupees if needed
        if isinstance(amount, int) and amount > 100:
            amount = amount / 100
        status_key = ("payment", obj.get("status", "captured").lower())
        payment_status = _CASHFREE_STATUS.get(status_key, "SUCCESS")

        data = {
            "order": {
                "order_id": order_id,
                "order_amount": float(amount),
                "order_currency": obj.get("currency", "INR"),
                "order_tags": None,
            },
            "payment": {
                "cf_payment_id": cf_payment_id,
                "payment_status": payment_status,
                "payment_amount": float(amount),
                "payment_currency": obj.get("currency", "INR"),
                "payment_message": "Payment successful" if payment_status == "SUCCESS" else "Payment failed",
                "payment_time": event_time,
                "bank_reference": f"ref_{uuid.uuid4().hex[:10]}",
                "payment_group": "credit_card",
                **{k: v for k, v in obj.items() if k not in ("currency", "status")},
            },
            "customer_details": {
                "customer_name": None,
                "customer_id": obj.get("customer_id", "carbon_test_customer"),
                "customer_email": obj.get("email"),
                "customer_phone": obj.get("phone", "9999999999"),
            },
        }
        if payment_status == "FAILED":
            data["payment"]["error_details"] = {
                "error_code": obj.get("error_code", "TRANSACTION_DECLINED"),
                "error_description": obj.get("error_description", "Transaction declined by bank"),
                "error_reason": obj.get("error_reason", "transaction_declined"),
                "error_source": "bank",
            }

    elif entity_type == "dispute":
        data = {
            "dispute": {
                "dispute_id": int(remote_id) if remote_id and remote_id.isdigit() else hash(remote_id) % 100000,
                "dispute_type": "CHARGEBACK",
                "reason_code": obj.get("reason_code", "general"),
                "reason_description": obj.get("reason_description", "General dispute"),
                "dispute_amount": obj.get("amount", 100),
                "dispute_amount_currency": obj.get("currency", "INR"),
                "created_at": event_time,
                "respond_by": None,
                "dispute_status": "CHARGEBACK_CREATED",
            },
            "order_details": {
                "order_id": obj.get("order_id"),
                "order_currency": obj.get("currency", "INR"),
                "order_amount": obj.get("amount", 100),
            },
        }

    elif entity_type == "refund":
        status_key = ("refund", obj.get("status", "processed").lower())
        refund_status = _CASHFREE_STATUS.get(status_key, "SUCCESS")
        data = {
            "refund": {
                "cf_refund_id": remote_id or f"cfr_{uuid.uuid4().hex[:12]}",
                "refund_id": obj.get("refund_id", f"rfnd_{uuid.uuid4().hex[:8]}"),
                "order_id": obj.get("order_id"),
                "refund_amount": obj.get("amount", 100),
                "refund_currency": obj.get("currency", "INR"),
                "refund_status": refund_status,
                "refund_type": "MERCHANT_INITIATED",
                "created_at": event_time,
            },
        }

    else:
        data = {"entity_type": entity_type, "id": remote_id, **obj}

    return {
        "data": data,
        "event_time": event_time,
        "type": event_type,
    }


# ---------------------------------------------------------------------------
# Juspay event mapping & payload format
# ---------------------------------------------------------------------------

JUSPAY_ENTITY_STATE_TO_EVENT: dict[tuple[str, str], str] = {
    ("payment", "authorized"): "ORDER_SUCCEEDED",
    ("payment", "captured"): "ORDER_SUCCEEDED",
    ("payment", "failed"): "ORDER_FAILED",
    ("dispute", "open"): "ORDER_FAILED",  # Juspay has no dispute webhook — map to order failure
    ("refund", "processed"): "ORDER_REFUNDED",
    ("refund", "failed"): "ORDER_FAILED",
}


def _build_juspay_payload(
    *,
    event_type: str,
    entity_type: str,
    remote_id: str,
    metadata: dict | None,
    account_id: str,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    event_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    obj = dict(metadata or {})

    order_id = obj.pop("order_id", remote_id or f"order_{uuid.uuid4().hex[:8]}")
    amount = obj.pop("amount", 100)
    if isinstance(amount, int) and amount > 100:
        amount = amount / 100
    currency = obj.pop("currency", "INR")

    # Juspay status mapping
    status_map = {
        "ORDER_SUCCEEDED": "CHARGED",
        "ORDER_FAILED": "AUTHENTICATION_FAILED",
        "ORDER_REFUNDED": "AUTO_REFUNDED",
    }

    order_obj = {
        "order_id": order_id,
        "txn_id": obj.get("txn_id", f"txn_{uuid.uuid4().hex[:12]}"),
        "status": status_map.get(event_type, "NEW"),
        "amount": float(amount),
        "currency": currency,
        "customer_id": obj.get("customer_id", "carbon_test_customer"),
        "customer_email": obj.get("customer_email", "test@carbon.dev"),
        "customer_phone": obj.get("customer_phone", "9999999999"),
        "date_created": event_time,
        "last_updated": event_time,
    }

    if event_type == "ORDER_REFUNDED":
        order_obj["refunds"] = [{
            "unique_request_id": obj.get("unique_request_id", f"rfnd_{uuid.uuid4().hex[:8]}"),
            "amount": float(amount),
            "status": "SUCCESS",
            "created": event_time,
        }]

    return {
        "id": f"evt_{uuid.uuid4().hex[:24]}",
        "date_created": event_time,
        "event_name": event_type,
        "content": {
            "order": order_obj,
        },
    }


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------

_EVENT_MAPS: dict[str, dict[tuple[str, str], str]] = {
    "razorpay": RAZORPAY_ENTITY_STATE_TO_EVENT,
    "stripe": STRIPE_ENTITY_STATE_TO_EVENT,
    "cashfree": CASHFREE_ENTITY_STATE_TO_EVENT,
    "juspay": JUSPAY_ENTITY_STATE_TO_EVENT,
}

_PAYLOAD_BUILDERS = {
    "razorpay": _build_razorpay_payload,
    "stripe": _build_stripe_payload,
    "cashfree": _build_cashfree_payload,
    "juspay": _build_juspay_payload,
}

# Backward-compat alias
ENTITY_STATE_TO_EVENT = RAZORPAY_ENTITY_STATE_TO_EVENT


def _normalized_state(state: str | None) -> str:
    return (state or "").strip().lower()


def entity_to_event_type(entity_type: str, state: str | None, provider: str = "razorpay") -> str | None:
    """Map entity+state to event type for a given provider."""
    key = (entity_type.strip().lower(), _normalized_state(state))
    event_map = _EVENT_MAPS.get(provider, RAZORPAY_ENTITY_STATE_TO_EVENT)
    return event_map.get(key)


def build_event_payload(
    *,
    event_type: str,
    entity_type: str,
    remote_id: str,
    metadata: dict | None,
    account_id: str = "acc_carbon",
    provider: str = "razorpay",
) -> dict[str, Any]:
    """Build a webhook payload in the format of the given provider."""
    builder = _PAYLOAD_BUILDERS.get(provider, _build_razorpay_payload)
    return builder(
        event_type=event_type,
        entity_type=entity_type,
        remote_id=remote_id,
        metadata=metadata,
        account_id=account_id,
    )


def build_events_from_entity_map(
    entity_map: dict,
    *,
    account_id: str = "acc_carbon",
    provider: str = "razorpay",
) -> list[dict[str, Any]]:
    """Build webhook events for all entities in entity_map with a known mapping."""
    events: list[dict[str, Any]] = []
    for local_id, info in entity_map.items():
        entity_type = (info.get("entity_type") or "").strip().lower()
        state = info.get("state")
        event_type = entity_to_event_type(entity_type, state, provider=provider)
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
                    provider=provider,
                ),
            }
        )
    return events

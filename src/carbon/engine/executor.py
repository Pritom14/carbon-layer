"""Execute plan: run steps, call adapter, persist to DB."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any, List

from carbon.adapters.base import PaymentAdapter
from carbon.compiler.models import ExecutionPlan, Step
from carbon.storage.db import get_connection, init_db
from carbon.storage.repo import (
    get_actions_for_run,
    get_entity_map,
    insert_action,
    insert_entity_mappings_bulk,
    set_run_started,
    update_action_result,
    update_run_status,
)
from carbon.webhook.payloads import CARBON_META_KEY, is_followup


# Error fields for failed payments, in Razorpay's payment entity format
_FAILURE_FIELDS = {
    "generic": {
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Payment failed",
        "error_source": "bank",
        "error_reason": "payment_failed",
    },
    # Razorpay UPI error list: payment_timed_out, "Customer Exceeded Payment Time Limit"
    # (https://razorpay.com/docs/errors/payments/upi)
    "payment_timed_out": {
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Customer Exceeded Payment Time Limit",
        "error_source": "customer",
        "error_reason": "payment_timed_out",
    },
}


def _entity_row(local_id: str, entity_type: str, remote_id: str, state: str | None, metadata: dict | None) -> dict:
    return {"local_id": local_id, "entity_type": entity_type, "remote_id": remote_id, "state": state, "metadata": metadata or {}}


def _entity_amount(entity_map: dict, ref: str) -> int | None:
    """Amount (paise) of a payment, falling back to its order's amount."""
    for local_id in (ref, ref[len("payment_"):] if ref.startswith("payment_") else None):
        if not local_id:
            continue
        meta = (entity_map.get(local_id) or {}).get("metadata") or {}
        amount = meta.get("amount") or meta.get("amount_paise")
        if isinstance(amount, (int, float)) and amount > 0:
            return int(amount)
    return None


async def _run_step(
    step: Step,
    run_id: str,
    provider: str,
    adapter: PaymentAdapter,
    entity_map: dict,
    conn: Any,
) -> None:
    """Execute a single step; collect entity rows and bulk insert."""
    params = dict(step.params)
    for_each = params.pop("for_each", None) or params.pop("on", None) or params.pop(True, None)
    count_param = params.get("count")
    refs: list[str] = []
    if for_each:
        type_map = {
            "orders": "order",
            "successful_payments": "payment",
            "captured_payments": "payment",
            "failed_payments": "payment",
        }
        etype = type_map.get(for_each, for_each.rstrip("s") if for_each.endswith("s") else for_each)
        allowed_states: tuple[str, ...] | None = None
        if for_each == "successful_payments":
            allowed_states = ("authorized", "captured")
        elif for_each == "captured_payments":
            allowed_states = ("captured",)
        elif for_each == "failed_payments":
            allowed_states = ("failed",)
        for local_id, info in entity_map.items():
            if info.get("entity_type") != etype or is_followup(info):
                continue
            if allowed_states is not None:
                s = (info.get("state") or "").lower().strip()
                if s not in allowed_states:
                    continue
            refs.append(local_id)
        refs = sorted(refs)
        if not refs and for_each == "captured_payments":
            for local_id, info in entity_map.items():
                if info.get("entity_type") == "payment":
                    refs.append(local_id)
            refs = sorted(refs)
        if count_param is not None and step.action_type == "create_disputes":
            n = min(int(count_param), len(refs))
            refs = list(random.sample(refs, n)) if refs else []
        if count_param is not None and step.action_type == "create_refunds":
            n = min(int(count_param), len(refs))
            refs = list(random.sample(refs, n)) if refs else []
    if step.action_type == "create_disputes" and not refs and entity_map:
        payment_refs = [lid for lid, info in entity_map.items() if info.get("entity_type") == "payment"]
        if payment_refs:
            n = min(int(step.params.get("count", 1)), len(payment_refs))
            refs = list(random.sample(payment_refs, n))

    if step.action_type == "duplicate_payments" and refs:
        n = max(0, min(len(refs), int(round(len(refs) * float(params.get("rate", 1.0))))))
        refs = sorted(random.sample(refs, n))

    if step.action_type == "late_events" and refs:
        # These late events arrive *before* the original instead (delivery reordered)
        n = max(0, min(len(refs), int(round(len(refs) * float(params.get("reorder_rate", 0.0))))))
        params["_before_refs"] = set(random.sample(refs, n))

    if step.action_type == "create_payments" and refs:
        # Exact success count (random which ones) so rate-based validations aren't flaky
        rate = float(params.get("success_rate", 1.0))
        n_success = max(0, min(len(refs), int(round(len(refs) * rate))))
        params["_success_refs"] = set(random.sample(refs, n_success))

    all_rows: List[dict] = []
    if refs:
        # Run adapter calls in parallel (batched to avoid overwhelming)
        BATCH = 100
        for i in range(0, len(refs), BATCH):
            batch_refs = refs[i : i + BATCH]
            results = await asyncio.gather(
                *[_execute_one(step, run_id, provider, adapter, params, ref, entity_map) for ref in batch_refs]
            )
            for r in results:
                if r:
                    all_rows.extend(r)
    else:
        result = await _execute_one(step, run_id, provider, adapter, params, None, entity_map)
        if result:
            all_rows.extend(result)
    if all_rows:
        await insert_entity_mappings_bulk(conn, run_id, provider, all_rows)


async def _execute_one(
    step: Step,
    run_id: str,
    provider: str,
    adapter: PaymentAdapter,
    params: dict,
    ref: str | None,
    entity_map: dict,
) -> List[dict] | None:
    """Return entity row(s) to bulk insert, or None."""
    action_type = step.action_type
    if action_type == "create_orders":
        count = int(params.get("count", 1))
        amount_paise = int(params.get("amount_paise", 250000))
        rows: List[dict] = []
        for i in range(count):
            out = await adapter.create_order(
                {"amount": amount_paise, "amount_paise": amount_paise, "receipt": f"carbon_{run_id}_{i}"}
            )
            rows.append(_entity_row(f"order_{i}", "order", out.get("id", ""), "created", out))
        return rows

    if action_type == "create_payments" and ref:
        order_remote_id = entity_map.get(ref, {}).get("remote_id") or ref
        if "_success_refs" in params:
            success = ref in params["_success_refs"]
        else:
            success = random.random() < float(params.get("success_rate", 1.0))
        out = await adapter.create_payment(order_remote_id, {"success": success})
        if params.get("method"):
            out = {**out, "method": params["method"]}
        if not success and params.get("failure_reason"):
            out = {**out, **_FAILURE_FIELDS.get(params["failure_reason"], {"error_reason": params["failure_reason"]})}
        return [_entity_row(f"payment_{ref}", "payment", out.get("id", ""), out.get("status", "authorized"), out)]

    if action_type == "duplicate_payments" and ref:
        # A second payment for the same order: a double click, or the client retrying checkout
        info = entity_map.get(ref, {})
        order_id = (info.get("metadata") or {}).get("order_id") or ""
        amount = _entity_amount(entity_map, ref) or 250000
        created = await adapter.create_payment(order_id, {"success": True})
        out = await adapter.capture_payment(created.get("id", ""), amount)
        out = {**created, **(out or {}), "order_id": order_id}
        return [_entity_row(f"{ref}_second", "payment", out.get("id", ""), "captured", out)]

    if action_type == "late_events" and ref:
        # Another webhook about the same payment, with a different state, delivered later
        info = entity_map.get(ref, {})
        state = str(params.get("state", "failed")).lower()
        meta = {k: v for k, v in (info.get("metadata") or {}).items() if not k.startswith("error_")}
        meta.update(status=state, captured=state == "captured")
        if state == "failed":
            meta.update(_FAILURE_FIELDS.get(params.get("failure_reason", ""), _FAILURE_FIELDS["generic"]))
        stage = 0 if ref in params.get("_before_refs", ()) else int(params.get("stage", 2))
        meta[CARBON_META_KEY] = {"stage": stage, "followup_of": ref}
        return [_entity_row(f"late_{state}_{ref}", "payment", info.get("remote_id") or "", state, meta)]

    if action_type == "capture_payments" and ref:
        payment_remote_id = entity_map.get(ref, {}).get("remote_id") or ref
        # Capture the full amount of the payment (i.e. its order) unless the step overrides it
        amount = params.get("amount_paise") or _entity_amount(entity_map, ref) or 250000
        out = await adapter.capture_payment(payment_remote_id, amount)
        return [_entity_row(ref, "payment", payment_remote_id, "captured", out)]

    if action_type == "create_disputes" and ref:
        payment_remote_id = entity_map.get(ref, {}).get("remote_id") or ref
        out = await adapter.create_dispute(payment_remote_id, {"reason": "chargeback"})
        return [_entity_row(f"dispute_{ref}", "dispute", out.get("id", ""), "open", out)]

    if action_type == "create_refunds" and ref:
        payment_remote_id = entity_map.get(ref, {}).get("remote_id") or ref
        amount_paise = params.get("amount_paise")
        refund_params = {}
        if amount_paise is not None:
            refund_params["amount"] = int(amount_paise)
        out = await adapter.create_refund(payment_remote_id, refund_params)
        return [_entity_row(f"refund_{ref}", "refund", out.get("id", ""), out.get("status", "processed"), out)]
    return None


async def run_plan(
    plan: ExecutionPlan,
    run_id: str,
    provider: str,
    adapter: PaymentAdapter,
) -> None:
    await init_db()
    conn = await get_connection()
    try:
        await set_run_started(run_id, conn=conn)
        entity_map: dict = {}
        for step in plan.steps:
            entity_map = await get_entity_map(run_id, conn=conn)
            action_id = await insert_action(
                run_id, step.phase, step.action_type, step.params, None, "running", conn=conn
            )
            try:
                await _run_step(step, run_id, provider, adapter, entity_map, conn)
                await update_action_result(action_id, result={"ok": True}, conn=conn)
            except Exception as e:
                await update_action_result(action_id, error=str(e), conn=conn)
                raise
        actions = await get_actions_for_run(run_id)
        summary = {"steps": len(actions), "completed": sum(1 for a in actions if a["status"] == "completed")}
        await update_run_status(run_id, "completed", datetime.utcnow(), summary, conn=conn)
    finally:
        await conn.close()

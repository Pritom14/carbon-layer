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


def _entity_row(local_id: str, entity_type: str, remote_id: str, state: str | None, metadata: dict | None) -> dict:
    return {"local_id": local_id, "entity_type": entity_type, "remote_id": remote_id, "state": state, "metadata": metadata or {}}


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
        type_map = {"orders": "order", "successful_payments": "payment", "captured_payments": "payment"}
        etype = type_map.get(for_each, for_each.rstrip("s") if for_each.endswith("s") else for_each)
        allowed_states: tuple[str, ...] | None = None
        if for_each == "successful_payments":
            allowed_states = ("authorized", "captured")
        elif for_each == "captured_payments":
            allowed_states = ("captured",)
        for local_id, info in entity_map.items():
            if info.get("entity_type") != etype:
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
        success_rate = float(params.get("success_rate", 1.0))
        success = random.random() < success_rate
        out = await adapter.create_payment(order_remote_id, {"success": success})
        return [_entity_row(f"payment_{ref}", "payment", out.get("id", ""), out.get("status", "authorized"), out)]

    if action_type == "capture_payments" and ref:
        payment_remote_id = entity_map.get(ref, {}).get("remote_id") or ref
        amount = params.get("amount_paise") or 250000
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

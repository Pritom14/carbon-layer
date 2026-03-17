"""Resolve entity refs from entity_map for for_each steps."""

from carbon.storage.repo import get_entity_map


async def resolve_refs(run_id: str, for_each: str) -> list[str]:
    """
    Map for_each (e.g. 'orders', 'successful_payments', 'captured_payments') to list of local_ids.
    entity_map stores local_id -> remote_id; we need all local_ids of the given type (or derived).
    """
    entity_map = await get_entity_map(run_id)
    # Normalize: orders -> order, successful_payments -> payment (captured), captured_payments -> payment
    type_map = {
        "orders": "order",
        "successful_payments": "payment",
        "captured_payments": "payment",
        "payments": "payment",
    }
    entity_type = type_map.get(for_each, for_each.rstrip("s") if for_each.endswith("s") else for_each)
    refs = []
    for local_id, info in entity_map.items():
        if info.get("entity_type") == entity_type:
            # For captured_payments/successful_payments only include captured ones
            if "payment" in for_each and "captured" in for_each:
                if info.get("state") != "captured":
                    continue
            refs.append(local_id)
    return sorted(refs)

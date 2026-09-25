"""Order/item agent: the order row and the item rows that belong to its timeline."""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..state import NOT_FOUND, CaseState, SpecialistResult
from ..trace import TraceWriter
from .base import fetch_evidence
from .timeline import money, parse_time

ACTOR = "order-agent"


async def run(state: CaseState, gateway: EvidenceGateway, trace: TraceWriter) -> SpecialistResult:
    result = SpecialistResult(case_id=state.case_id, actor=ACTOR)
    order = await fetch_evidence(
        gateway,
        trace,
        actor=ACTOR,
        case_id=state.case_id,
        tool_name="get_order",
        order_id=state.order_id,
    )
    if order is None or not isinstance(order.data, dict):
        result.status = NOT_FOUND
        return result
    result.evidence.append(order)
    result.facts.update(order_facts(order.data))

    items = await fetch_evidence(
        gateway,
        trace,
        actor=ACTOR,
        case_id=state.case_id,
        tool_name="get_order_items",
        order_id=state.order_id,
    )
    if items is None or not isinstance(items.data, list):
        result.warnings.append("order items unavailable")
        return result
    result.evidence.append(items)
    result.facts.update(item_facts(items.data, result.facts))

    sellers = await fetch_evidence(
        gateway,
        trace,
        actor=ACTOR,
        case_id=state.case_id,
        tool_name="get_sellers",
        order_id=state.order_id,
    )
    if sellers is not None:
        result.evidence.append(sellers)

    products = await fetch_evidence(
        gateway,
        trace,
        actor=ACTOR,
        case_id=state.case_id,
        tool_name="get_product_context",
        order_id=state.order_id,
    )
    if products is not None:
        result.evidence.append(products)
    return result


def order_facts(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": row.get("order_id"),
        "order_status": row.get("order_status"),
        "purchased_at": parse_time(row.get("order_purchase_timestamp")),
        "approved_at": parse_time(row.get("order_approved_at")),
        "estimated_delivery_at": parse_time(row.get("order_estimated_delivery_date")),
        "delivered_carrier_at": parse_time(row.get("order_delivered_carrier_date")),
        "delivered_customer_at": parse_time(row.get("order_delivered_customer_date")),
    }


def item_facts(rows: list[dict[str, Any]], order: dict[str, Any]) -> dict[str, Any]:
    """Keep one row per item, preferring rows whose shipping limit sits on the order timeline.

    Rows dated outside purchase..estimated delivery are stale copies, not the order's items.
    """
    purchased, estimated = order.get("purchased_at"), order.get("estimated_delivery_at")
    on_timeline = [
        row for row in rows if _within(row.get("shipping_limit_date"), purchased, estimated)
    ]
    chosen = on_timeline or rows
    items: dict[str, dict[str, Any]] = {}
    for row in chosen:
        items.setdefault(row["order_item_id"], row)
    unique = list(items.values())
    return {
        "item_ids": [row["order_item_id"] for row in unique],
        "seller_ids": list(dict.fromkeys(row["seller_id"] for row in unique)),
        "order_total": round(
            sum(money(row.get("price")) + money(row.get("freight_value")) for row in unique), 2
        ),
        "freight_total": round(sum(money(row.get("freight_value")) for row in unique), 2),
        "shipping_limit_at": min(
            (parse_time(row["shipping_limit_date"]) for row in unique), default=None
        ),
        "stale_item_rows": len(rows) - len(unique),
    }


def _within(value: str | None, start: Any, end: Any) -> bool:
    moment = parse_time(value)
    return bool(moment and start and end and start <= moment <= end)

"""order-agent: order row, items and sellers (domains order, item, seller)."""

from __future__ import annotations

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .protocol import fetch
from .rules import in_window, money, parse_ts
from .types import AgentResult, CaseContext

ACTOR = "order-agent"


async def run(ctx: CaseContext, gateway: EvidenceGateway, trace: TraceWriter) -> AgentResult:
    result = AgentResult(actor=ACTOR, ok=False)
    if not ctx.order_id:
        result.notes_code = "ORDER_NOT_RESOLVED"
        return result

    order_ev = await fetch(gateway, trace, ctx, ACTOR, "get_order", order_id=ctx.order_id)
    if order_ev is None or not isinstance(order_ev.data, dict):
        result.notes_code = "ORDER_NOT_RESOLVED"
        return result
    order = order_ev.data
    result.evidence.append(order_ev)
    result.ok = True

    start = parse_ts(order.get("order_purchase_timestamp"))
    end = parse_ts(ctx.opened_at)
    items_ev = await fetch(gateway, trace, ctx, ACTOR, "get_order_items", order_id=ctx.order_id)
    items = items_ev.data if items_ev and isinstance(items_ev.data, list) else []
    if items_ev:
        result.evidence.append(items_ev)
    current = [i for i in items if in_window(i.get("shipping_limit_date"), start, end)]
    scoped = current or items

    sellers_ev = await fetch(gateway, trace, ctx, ACTOR, "get_sellers", order_id=ctx.order_id)
    if sellers_ev:
        result.evidence.append(sellers_ev)

    result.entities = {
        "order_ids": [order.get("order_id") or ctx.order_id],
        "item_ids": list(
            dict.fromkeys(str(i["order_item_id"]) for i in scoped if i.get("order_item_id"))
        ),
        "seller_ids": list(
            dict.fromkeys(str(i["seller_id"]) for i in scoped if i.get("seller_id"))
        ),
    }
    result.findings = {
        "order_status": order.get("order_status"),
        "purchase_at": order.get("order_purchase_timestamp"),
        "order_total": round(
            sum(money(i.get("price")) + money(i.get("freight_value")) for i in scoped), 2
        ),
        "freight_total": round(sum(money(i.get("freight_value")) for i in scoped), 2),
        "shipping_limit_at": min(
            (i["shipping_limit_date"] for i in scoped if i.get("shipping_limit_date")),
            default=None,
        ),
    }
    return result

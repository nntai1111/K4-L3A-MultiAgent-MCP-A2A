"""shipment-agent: delivery timeline and late-delivery attribution (domain shipment)."""

from __future__ import annotations

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .protocol import fetch
from .rules import in_window, parse_ts
from .types import AgentResult, CaseContext

ACTOR = "shipment-agent"


async def run(
    ctx: CaseContext, gateway: EvidenceGateway, trace: TraceWriter, order: AgentResult
) -> AgentResult:
    result = AgentResult(actor=ACTOR, ok=False)
    ev = await fetch(gateway, trace, ctx, ACTOR, "get_shipment_summary", order_id=ctx.order_id)
    if ev is None or not isinstance(ev.data, dict):
        result.notes_code = "SHIPMENT_NOT_FOUND"
        return result
    result.ok = True
    result.evidence.append(ev)
    data = ev.data
    start = parse_ts(order.findings.get("purchase_at"))
    end = parse_ts(ctx.opened_at)

    carrier_at = parse_ts(data.get("delivered_carrier_at"))
    customer_at = parse_ts(data.get("delivered_customer_at"))
    estimated_at = parse_ts(data.get("estimated_delivery_at"))

    # Shipment `events` include decoy `delivered_late` rows (even inside the case window),
    # so lateness and attribution come from the authoritative timestamps only.
    limits = [
        parse_ts(s.get("shipping_limit_at"))
        for s in data.get("shipping_limits") or []
        if in_window(s.get("shipping_limit_at"), start, end)
    ]
    limit_at = min((x for x in limits if x), default=None)
    # Late if the promised date had passed when the case was opened and delivery came after it.
    shippable = order.findings.get("order_status") not in ("canceled", "unavailable")
    promise_passed = estimated_at is not None and (end is None or estimated_at <= end)
    customer_late = (
        shippable and promise_passed and (customer_at is None or customer_at > estimated_at)
    )
    seller_breach = carrier_at is not None and limit_at is not None and carrier_at > limit_at

    result.findings = {
        "seller_late": customer_late and seller_breach,
        "logistics_late": customer_late and not seller_breach,
        "timeline_incomplete": customer_at is None or estimated_at is None,
    }
    return result

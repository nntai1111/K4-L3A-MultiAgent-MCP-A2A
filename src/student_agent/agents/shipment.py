"""Shipment agent: was the delivery late, and was the seller's handoff late."""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..state import NOT_FOUND, CaseState, SpecialistResult
from ..trace import TraceWriter
from .base import fetch_evidence
from .timeline import parse_time

ACTOR = "shipment-agent"


async def run(state: CaseState, gateway: EvidenceGateway, trace: TraceWriter) -> SpecialistResult:
    result = SpecialistResult(case_id=state.case_id, actor=ACTOR)
    summary = await fetch_evidence(
        gateway,
        trace,
        actor=ACTOR,
        case_id=state.case_id,
        tool_name="get_shipment_summary",
        order_id=state.order_id,
    )
    if summary is None or not isinstance(summary.data, dict):
        result.status = NOT_FOUND
        return result
    result.evidence.append(summary)
    result.facts.update(shipment_facts(summary.data, state.facts("order-agent")))
    return result


def shipment_facts(summary: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    """Lateness comes from timestamps; free-text shipment events are not trusted over them."""
    purchased = order.get("purchased_at")
    estimated = parse_time(summary.get("estimated_delivery_at")) or order.get(
        "estimated_delivery_at"
    )
    delivered = parse_time(summary.get("delivered_customer_at"))
    handed_to_carrier = parse_time(summary.get("delivered_carrier_at"))
    limits = [
        moment
        for limit in summary.get("shipping_limits", [])
        if (moment := parse_time(limit.get("shipping_limit_at")))
        and purchased
        and estimated
        and purchased <= moment <= estimated
    ]
    shipping_limit = min(limits, default=order.get("shipping_limit_at"))
    delivered_late = bool(delivered and estimated and delivered > estimated)
    return {
        "delivered_late": delivered_late,
        "seller_handoff_late": bool(
            handed_to_carrier and shipping_limit and handed_to_carrier > shipping_limit
        ),
        "delivered_customer_at": delivered,
        "estimated_delivery_at": estimated,
    }

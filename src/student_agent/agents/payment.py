"""Payment agent: charges captured on the order's own timeline, and refund lifecycle."""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..state import NOT_FOUND, CaseState, SpecialistResult
from ..trace import TraceWriter
from .base import fetch_evidence
from .timeline import money, parse_time

ACTOR = "payment-agent"


async def run(
    state: CaseState, gateway: EvidenceGateway, trace: TraceWriter, *, check_refunds: bool
) -> SpecialistResult:
    result = SpecialistResult(case_id=state.case_id, actor=ACTOR)
    order = state.facts("order-agent")
    timeline = await fetch_evidence(
        gateway,
        trace,
        actor=ACTOR,
        case_id=state.case_id,
        tool_name="get_payment_timeline",
        order_id=state.order_id,
    )
    if timeline is None or not isinstance(timeline.data, dict):
        result.status = NOT_FOUND
        return result
    result.evidence.append(timeline)
    result.facts.update(payment_facts(timeline.data.get("events", []), order))

    payments = await fetch_evidence(
        gateway,
        trace,
        actor=ACTOR,
        case_id=state.case_id,
        tool_name="get_order_payments",
        order_id=state.order_id,
    )
    if payments is not None:
        result.evidence.append(payments)

    if check_refunds:
        refunds = await fetch_evidence(
            gateway,
            trace,
            actor=ACTOR,
            case_id=state.case_id,
            tool_name="get_refund_timeline",
            order_id=state.order_id,
        )
        if refunds is not None and isinstance(refunds.data, dict):
            result.evidence.append(refunds)
            result.facts.update(
                refund_facts(refunds.data.get("events", []), order, state.case.get("opened_at"))
            )
        else:
            result.facts["refund_status"] = None
            result.warnings.append("no refund record")
    return result


def payment_facts(events: list[dict[str, Any]], order: dict[str, Any]) -> dict[str, Any]:
    """Payment events belong to the order when they happen on its purchase day.

    Events dated on other days are stale copies and are counted, never used.
    """
    purchased = order.get("purchased_at")
    same_day = [
        event
        for event in events
        if purchased
        and (moment := parse_time(event.get("event_at")))
        and moment.date() == purchased.date()
    ]
    captures = [money(e.get("amount_brl")) for e in same_day if e.get("event_type") == "captured"]
    mismatches = [e for e in same_day if e.get("event_type") == "reconciliation_mismatch"]
    return {
        "captures": captures,
        "captured_total": round(sum(captures), 2),
        "has_reconciliation_mismatch": bool(mismatches),
        "stale_payment_events": len(events) - len(same_day),
    }


def refund_facts(
    events: list[dict[str, Any]], order: dict[str, Any], opened_at: str | None
) -> dict[str, Any]:
    purchased, opened = order.get("purchased_at"), parse_time(opened_at)
    in_window = sorted(
        (
            event
            for event in events
            if (moment := parse_time(event.get("event_at")))
            and purchased
            and opened
            and purchased <= moment <= opened
        ),
        key=lambda event: event["event_at"],
    )
    latest = in_window[-1] if in_window else None
    return {
        "refund_status": latest.get("status") if latest else None,
        "refund_amount": money(latest.get("amount_brl")) if latest else 0.0,
        "stale_refund_events": len(events) - len(in_window),
    }

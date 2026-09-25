"""payment-agent: captures, reconciliation and refunds (domains payment, refund)."""

from __future__ import annotations

from collections import Counter

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .protocol import fetch
from .rules import close, in_window, money, parse_ts
from .types import AgentResult, CaseContext

ACTOR = "payment-agent"


async def run(
    ctx: CaseContext, gateway: EvidenceGateway, trace: TraceWriter, order: AgentResult
) -> AgentResult:
    result = AgentResult(actor=ACTOR, ok=False)
    start = parse_ts(order.findings.get("purchase_at"))
    end = parse_ts(ctx.opened_at)

    pay_ev = await fetch(gateway, trace, ctx, ACTOR, "get_payment_timeline", order_id=ctx.order_id)
    if pay_ev is None or not isinstance(pay_ev.data, dict):
        result.notes_code = "PAYMENT_NOT_FOUND"
        return result
    result.ok = True
    result.evidence.append(pay_ev)
    events = [
        e for e in pay_ev.data.get("events") or [] if in_window(e.get("event_at"), start, end)
    ]
    captures = [
        money(e.get("amount_brl"))
        for e in events
        if e.get("event_type") == "captured" and e.get("status") == "confirmed"
    ]
    captured_total = round(sum(captures), 2)
    order_total = order.findings.get("order_total", 0.0)
    mismatch = any(e.get("event_type") == "reconciliation_mismatch" for e in events)
    split_valid = len(captures) >= 2 and order_total > 0 and close(captured_total, order_total)
    repeated = any(count >= 2 for count in Counter(captures).values())
    duplicate = repeated and not split_valid

    refund_ev = await fetch(
        gateway, trace, ctx, ACTOR, "get_refund_timeline", order_id=ctx.order_id
    )
    refunds = []
    if refund_ev is not None and isinstance(refund_ev.data, dict):
        result.evidence.append(refund_ev)
        refunds = [
            e
            for e in refund_ev.data.get("events") or []
            if in_window(e.get("event_at"), start, end)
        ]
    pending = [money(e.get("amount_brl")) for e in refunds if e.get("status") == "pending"]
    failed = [money(e.get("amount_brl")) for e in refunds if e.get("status") == "failed"]

    result.findings = {
        "captured_total": captured_total,
        "capture_count": len(captures),
        "mismatch": mismatch,
        "split_valid": split_valid,
        "duplicate": duplicate,
        "refund_pending": bool(pending),
        "refund_failed": bool(failed),
        "pending_amount": round(sum(pending), 2),
        "failed_amount": round(sum(failed), 2),
    }
    return result

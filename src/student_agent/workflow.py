from __future__ import annotations

import asyncio
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .agents import order_agent, payment_agent, policy_agent, shipment_agent, verifier
from .agents.protocol import assign, handoff
from .agents.types import AgentResult, CaseContext
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: order-agent first, then shipment + payment in parallel, then policy and
    verifier. Each actor runs at most once per case, so the A2A flow cannot loop."""
    ctx = CaseContext.from_input(case)

    assign(trace, ctx, "order-agent")
    order = await order_agent.run(ctx, gateway, trace)

    if order.ok:
        handoff(trace, ctx, "order-agent", "shipment-agent", order.evidence_refs)
        handoff(trace, ctx, "order-agent", "payment-agent", order.evidence_refs)
        shipment, payment = await asyncio.gather(
            shipment_agent.run(ctx, gateway, trace, order),
            payment_agent.run(ctx, gateway, trace, order),
        )
    else:
        # No authoritative order: skip dependent lookups instead of guessing.
        shipment = AgentResult("shipment-agent", ok=False, notes_code="SKIPPED_NO_ORDER")
        payment = AgentResult("payment-agent", ok=False, notes_code="SKIPPED_NO_ORDER")
        handoff(trace, ctx, "order-agent", "policy-agent", found=False)

    for specialist in (shipment, payment):
        if order.ok:
            handoff(
                trace,
                ctx,
                specialist.actor,
                "policy-agent",
                specialist.evidence_refs,
                found=specialist.ok,
                notes_code=specialist.notes_code,
            )

    decision = await policy_agent.run(ctx, gateway, trace, order, shipment, payment)
    handoff(trace, ctx, "policy-agent", "verifier", decision["evidence_refs"])

    refund = decision["refund"]
    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": decision["primary_issue"],
            "case_status": decision["case_status"],
            "confidence": decision["confidence"],
        },
        "affected_entities": {
            "order_ids": order.entities.get("order_ids", []),
            "item_ids": order.entities.get("item_ids", []),
            "seller_ids": order.entities.get("seller_ids", []),
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": decision["claim_assessments"],
        "root_cause_analysis": {
            "ranked_causes": decision["ranked_causes"],
            "responsible_parties": decision["responsible_parties"],
        },
        "evidence_refs": decision["evidence_refs"],
        "data_conflicts": decision["conflicts"],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": (
                [
                    {
                        "reason_code": decision["actions"][0],
                        "amount_brl": refund,
                        "entity_id": ctx.order_id,
                    }
                ]
                if refund > 0
                else []
            ),
        },
        "resolution_actions": decision["actions"],
    }
    fetched = {ref for r in (order, shipment, payment) for ref in r.evidence_refs}
    fetched |= set(decision["evidence_refs"])
    return verifier.run(ctx, trace, output, fetched)

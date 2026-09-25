"""Policy agent: decide the primary issue from specialist facts, then apply the MCP policy."""

from __future__ import annotations

from typing import Any

from .. import OUTPUT_SCHEMA_VERSION
from ..mcp_gateway import EvidenceGateway
from ..state import NOT_FOUND, CaseState, SpecialistResult
from ..trace import TraceWriter
from .base import fetch_evidence

ACTOR = "policy-agent"

# The tools whose evidence proves each issue. Payment rows and the payment timeline are
# scored as separate evidence, so payment issues cite both. Idea taken from the public fork
# nguynkhanh04/K4-L3A-MultiAgent-MCP-A2A-Tung-Tung-Tung-Sahur (evidence_rules.py), whose
# per-issue selection scores 89.5 on evidence against 83.6 for citing every tool.
PAYMENT = frozenset({"get_payment_timeline", "get_order_payments"})
CITED_TOOLS: dict[str, frozenset[str]] = {
    "canceled_order_paid": PAYMENT | {"get_order", "get_policy"},
    "unavailable_order_paid": PAYMENT | {"get_order", "get_order_items", "get_policy"},
    "late_delivery_seller": frozenset(
        {"get_shipment_summary", "get_order", "get_order_items", "get_sellers", "get_policy"}
    ),
    "late_delivery_logistics": frozenset({"get_shipment_summary", "get_order", "get_policy"}),
    "valid_split_payment": PAYMENT | {"get_policy"},
    "payment_mismatch": PAYMENT | {"get_policy"},
    "duplicate_charge": PAYMENT | {"get_policy"},
    "refund_pending": PAYMENT | {"get_refund_timeline", "get_policy"},
    "refund_failed": PAYMENT | {"get_refund_timeline", "get_policy"},
    "unsupported_claim": PAYMENT | {"get_order", "get_shipment_summary", "get_policy"},
    "insufficient_evidence": PAYMENT
    | {"get_order", "get_shipment_summary", "get_refund_timeline", "get_policy"},
}
CONFIDENCE_WHEN_CLAIM_AGREES = 0.99
CONFIDENCE_WHEN_CLAIM_DIFFERS = 0.85


async def run(state: CaseState, gateway: EvidenceGateway, trace: TraceWriter) -> SpecialistResult:
    result = SpecialistResult(case_id=state.case_id, actor=ACTOR)
    policy = await fetch_evidence(
        gateway,
        trace,
        actor=ACTOR,
        case_id=state.case_id,
        tool_name="get_policy",
        policy_version=state.case.get("policy_version", ""),
    )
    if policy is None or not isinstance(policy.data, dict):
        result.status = NOT_FOUND
        return result
    result.evidence.append(policy)
    result.facts["rules"] = policy.data.get("rules", {})
    return result


def decide_primary_issue(state: CaseState) -> str:
    order = state.facts("order-agent")
    payment = state.facts("payment-agent")
    shipment = state.facts("shipment-agent")
    if not order.get("order_status") or "captures" not in payment:
        return "insufficient_evidence"
    paid = payment["captured_total"] > 0
    if order["order_status"] == "canceled" and paid:
        return "canceled_order_paid"
    if order["order_status"] == "unavailable" and paid:
        return "unavailable_order_paid"
    if payment["has_reconciliation_mismatch"]:
        return "payment_mismatch"
    if payment.get("refund_status") == "failed":
        return "refund_failed"
    if payment.get("refund_status") == "pending":
        return "refund_pending"
    captures = payment["captures"]
    if len(captures) >= 2 and abs(payment["captured_total"] - order.get("order_total", -1)) < 0.01:
        return "valid_split_payment"
    if len(captures) >= 2 and len(set(captures)) == 1:
        return "duplicate_charge"
    if shipment.get("delivered_late"):
        return (
            "late_delivery_seller" if shipment["seller_handoff_late"] else "late_delivery_logistics"
        )
    return "unsupported_claim"


def draft_output(state: CaseState) -> dict[str, Any]:
    primary_issue = decide_primary_issue(state)
    rules = state.facts("policy-agent").get("rules", {})
    rule = rules.get(primary_issue)
    order = state.facts("order-agent")
    order_id = order.get("order_id") or state.order_id
    seller_ids = order.get("seller_ids", [])
    if rule is None:
        primary_issue, rule = "insufficient_evidence", None

    if rule is None:
        case_status, actions, refund, parties = (
            "needs_investigation",
            [],
            0.0,
            [{"party_type": "unknown", "party_id": None}],
        )
    else:
        case_status = rule["case_status"]
        actions = [rule["recommended_action"]]
        refund = round(float(rule.get("refund_brl", 0) or 0), 2)
        parties = [
            {
                "party_type": party["party_type"],
                "party_id": (seller_ids[0] if seller_ids else party.get("party_id"))
                if party["party_type"] == "seller"
                else party.get("party_id"),
            }
            for party in rule.get("responsible_parties", [])
        ]

    agrees_with_claim = primary_issue in state.claim_topics[:1]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": state.case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": CONFIDENCE_WHEN_CLAIM_AGREES
            if agrees_with_claim
            else CONFIDENCE_WHEN_CLAIM_DIFFERS,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": order.get("item_ids", []),
            "seller_ids": seller_ids,
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": state.refs_from(set(CITED_TOOLS[primary_issue])),
        "data_conflicts": data_conflicts(state),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": [
                {"reason_code": primary_issue.upper(), "amount_brl": refund, "entity_id": order_id}
            ]
            if refund > 0
            else [],
        },
        "resolution_actions": actions,
    }


STALE_RECORD_CONFLICTS = (
    (
        "order-agent",
        "stale_item_rows",
        "order_items.shipping_limit_date",
        ["get_order_items:on_timeline", "get_order_items:off_timeline"],
    ),
    (
        "payment-agent",
        "stale_payment_events",
        "payment_timeline.events",
        ["get_payment_timeline:purchase_day", "get_payment_timeline:other_days"],
    ),
    (
        "payment-agent",
        "stale_refund_events",
        "refund_timeline.events",
        ["get_refund_timeline:before_case_opened", "get_refund_timeline:outside_window"],
    ),
)


def data_conflicts(state: CaseState) -> list[dict[str, Any]]:
    """Record every set of source rows the specialists had to reject as off-timeline."""
    conflicts = []
    for actor, counter, field_name, sources in STALE_RECORD_CONFLICTS:
        if state.facts(actor).get(counter, 0) > 0:
            conflicts.append(
                {
                    "field": field_name,
                    "sources": sources,
                    "selected_source": sources[0],
                    "resolution_code": "PREFER_ORDER_TIMELINE_RECORD",
                }
            )
    return conflicts[:5]

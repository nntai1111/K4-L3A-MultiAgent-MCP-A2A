"""policy-agent: pick primary_issue from specialist findings and apply the MCP policy."""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .protocol import fetch
from .rules import CAUSE_CODES, ISSUE_DOMAINS, candidate_issues, choose_issue, money
from .types import AgentResult, CaseContext, Evidence

ACTOR = "policy-agent"


def _confidence(issue: str, claimed: str | None, shipment: AgentResult) -> float:
    if issue == "insufficient_evidence":
        return 0.3
    if issue == "unsupported_claim":
        return 0.8
    if issue != claimed:
        return 0.7
    if issue.startswith("late_delivery") and shipment.findings.get("timeline_incomplete"):
        return 0.55
    return 0.9


async def run(
    ctx: CaseContext,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    order: AgentResult,
    shipment: AgentResult,
    payment: AgentResult,
) -> dict[str, Any]:
    policy_ev: Evidence | None = None
    if ctx.policy_version:
        policy_ev = await fetch(
            gateway, trace, ctx, ACTOR, "get_policy", policy_version=ctx.policy_version
        )
    rules = (policy_ev.data or {}).get("rules", {}) if policy_ev else {}
    claimed = ctx.issue_claim.topic if ctx.issue_claim else None

    if not order.ok:
        issue = "insufficient_evidence"
    else:
        candidates = candidate_issues(order.findings, shipment.findings, payment.findings)
        issue = choose_issue(candidates, claimed)

    rule = rules.get(issue) or {}
    seller_ids = order.entities.get("seller_ids", [])
    if issue == "insufficient_evidence":
        case_status, refund, action = "needs_investigation", 0.0, "escalate_missing_evidence"
        parties: list[dict[str, Any]] = [{"party_type": "unknown", "party_id": None}]
    else:
        case_status = rule.get("case_status") or "needs_investigation"
        refund = money(rule.get("refund_brl"))
        action = rule.get("recommended_action") or "review_case"
        parties = []
        for party in rule.get("responsible_parties") or [{"party_type": "unknown"}]:
            party_type = party.get("party_type") or "unknown"
            party_id = party.get("party_id")
            if party_type == "seller":
                # Policy lists a sample seller; responsibility must point at this order's seller.
                party_id = seller_ids[0] if seller_ids else None
            parties.append({"party_type": party_type, "party_id": party_id})

    # Cite only the domains that support this conclusion.
    wanted = set(ISSUE_DOMAINS[issue])
    if issue == "unsupported_claim" and claimed in ISSUE_DOMAINS:
        # Cite the evidence that contradicts the customer's alleged issue.
        wanted |= set(ISSUE_DOMAINS[claimed])
    evidence = [*order.evidence, *shipment.evidence, *payment.evidence]
    if policy_ev:
        evidence.append(policy_ev)
    cited = list(dict.fromkeys(ev.ref for ev in evidence if ev.domain in wanted))

    confidence = _confidence(issue, claimed, shipment)
    claim_assessments = []
    for claim in ctx.claims:
        if issue == "insufficient_evidence":
            verdict, claim_conf = "insufficient_evidence", 0.3
        elif claim.topic == "requested_full_refund":
            captured = payment.findings.get("captured_total", 0.0)
            if refund <= 0:
                verdict = "unsupported"
            elif captured and refund + 0.05 >= captured:
                verdict = "supported"
            else:
                verdict = "partially_supported"
            claim_conf = min(confidence, 0.8)
        else:
            verdict = "supported" if claim.topic == issue else "unsupported"
            claim_conf = confidence
        claim_assessments.append(
            {
                "claim_id": claim.claim_id,
                "verdict": verdict,
                "confidence": claim_conf,
                "evidence_refs": cited,
            }
        )

    decision = {
        "primary_issue": issue,
        "case_status": case_status,
        "confidence": confidence,
        "ranked_causes": [{"cause_code": CAUSE_CODES[issue], "rank": 1}],
        "responsible_parties": parties,
        "refund": refund,
        "actions": [action],
        "evidence_refs": cited,
        "claim_assessments": claim_assessments[:5],
        "conflicts": [*order.conflicts, *shipment.conflicts, *payment.conflicts][:5],
    }
    trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor=ACTOR,
        decision_code=issue,
        evidence_refs=cited[:20] or None,
        attributes={"case_status": case_status, "refund_brl": refund},
    )
    return decision

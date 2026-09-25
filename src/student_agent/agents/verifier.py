"""verifier: enforce cross-field invariants before the coordinator returns the output."""

from __future__ import annotations

import re
from typing import Any

from ..trace import TraceWriter
from .rules import CAUSE_CODES
from .types import CaseContext

ACTOR = "verifier"
REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


def run(
    ctx: CaseContext, trace: TraceWriter, output: dict[str, Any], fetched_refs: set[str]
) -> dict[str, Any]:
    fixes: list[str] = []
    assessment = output["assessment"]
    finance = output["financial_resolution"]
    entities = output["affected_entities"]

    refs = [r for r in output["evidence_refs"] if REF_PATTERN.fullmatch(r) and r in fetched_refs]
    if len(refs) != len(output["evidence_refs"]):
        fixes.append("DROP_FOREIGN_REF")
    output["evidence_refs"] = refs[:30]
    for claim in output.get("claim_assessments", []):
        claim["evidence_refs"] = [r for r in claim["evidence_refs"] if r in refs]

    if not refs and assessment["primary_issue"] != "insufficient_evidence":
        fixes.append("NO_EVIDENCE_DOWNGRADE")
        assessment.update(
            primary_issue="insufficient_evidence", case_status="needs_investigation", confidence=0.3
        )
        output["root_cause_analysis"] = {
            "ranked_causes": [{"cause_code": CAUSE_CODES["insufficient_evidence"], "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        }
        finance.update(recommended_refund_brl=0.0, refund_lines=[])

    if assessment["case_status"] == "no_action" and finance["recommended_refund_brl"] > 0:
        fixes.append("NO_ACTION_ZERO_REFUND")
        finance.update(recommended_refund_brl=0.0, refund_lines=[])

    total = round(sum(line["amount_brl"] for line in finance["refund_lines"]), 2)
    if abs(total - finance["recommended_refund_brl"]) > 0.01:
        fixes.append("REFUND_LINES_REBALANCED")
        finance["recommended_refund_brl"] = total

    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in entities["seller_ids"]:
            fixes.append("SELLER_NOT_IN_SCOPE")
            party.update(party_type="unknown", party_id=None)

    output["resolution_actions"] = list(dict.fromkeys(output["resolution_actions"]))[:8]
    assessment["confidence"] = min(max(float(assessment["confidence"]), 0.0), 1.0)

    trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor=ACTOR,
        decision_code="PASS" if not fixes else "FIXED",
        evidence_refs=refs[:20] or None,
        attributes={"fixes": ",".join(fixes) or None},
    )
    return output

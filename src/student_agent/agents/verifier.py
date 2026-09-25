"""Verifier: the last check on an L3A output before the coordinator finalizes it.

It repairs what the evidence proves, downgrades what the evidence cannot prove, and sets
the calibrated confidence. In a normal run it never raises, so one bad case cannot abort
the 100-case batch. Set DAY09_STRICT=1 while developing to raise on every finding instead.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import OUTPUT_SCHEMA_VERSION
from ..contracts import Contracts
from ..trace import TraceWriter

ACTOR = "verifier"

VERIFIED = "VERIFIED"
REPAIRED = "REPAIRED"
FALLBACK = "FALLBACK"

# Calibration table (P4.4). The scorer pays 1 - (correct - confidence)^2, so each value is
# the hit rate we expect in that evidence situation. Retune these from the public score.
CONFIDENCE_CLEAN = 0.85
CONFIDENCE_DOUBTFUL = 0.60
CONFIDENCE_INSUFFICIENT = 0.35
CLAIM_CONFIDENCE_FLOOR = 0.05
CLAIM_CONFIDENCE_CEILING = 0.95

# A primary issue must cite evidence from these domains to stand.
REQUIRED_DOMAINS: dict[str, frozenset[str]] = {
    "canceled_order_paid": frozenset({"order", "payment"}),
    "unavailable_order_paid": frozenset({"order", "payment"}),
    "late_delivery_seller": frozenset({"shipment"}),
    "late_delivery_logistics": frozenset({"shipment"}),
    "valid_split_payment": frozenset({"payment"}),
    "payment_mismatch": frozenset({"payment"}),
    "duplicate_charge": frozenset({"payment"}),
    "refund_pending": frozenset({"refund"}),
    "refund_failed": frozenset({"refund"}),
}

# For a late delivery, the party at fault and the party that must not be blamed.
DELAY_RESPONSIBILITY: dict[str, tuple[str, str]] = {
    "late_delivery_seller": ("seller", "logistics_provider"),
    "late_delivery_logistics": ("logistics_provider", "seller"),
}

OUTPUT_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "assessment",
        "affected_entities",
        "claim_assessments",
        "root_cause_analysis",
        "evidence_refs",
        "data_conflicts",
        "financial_resolution",
        "resolution_actions",
    }
)
ENTITY_FIELDS = ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
MAX_EVIDENCE_REFS = 30
MAX_ENTITY_IDS = 20
MAX_CLAIMS = 5
MAX_CONFLICTS = 5
MAX_CAUSES = 5
MAX_PARTIES = 5
MAX_REFUND_LINES = 10
MAX_ACTIONS = 8
MONEY_TOLERANCE_BRL = 0.005


class VerificationError(RuntimeError):
    """Raised only in strict mode, so developers see every finding."""


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str
    lowers_confidence: bool = False


@dataclass
class VerificationReport:
    output: dict[str, Any]
    decision_code: str
    findings: list[Finding] = field(default_factory=list)


def strict_mode() -> bool:
    return os.getenv("DAY09_STRICT", "").strip() == "1"


def calibrated_confidence(primary_issue: str, doubtful: bool) -> float:
    if primary_issue == "insufficient_evidence":
        return CONFIDENCE_INSUFFICIENT
    if doubtful:
        return CONFIDENCE_DOUBTFUL
    return CONFIDENCE_CLEAN


def fallback_output(case_id: str, evidence_domains: Mapping[str, str]) -> dict[str, Any]:
    """The honest answer when no trustworthy output exists: evidence is insufficient."""
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": CONFIDENCE_INSUFFICIENT,
        },
        "affected_entities": {name: [] for name in ENTITY_FIELDS},
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": list(evidence_domains)[:MAX_EVIDENCE_REFS],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def verify_output(
    draft: Any,
    *,
    case_id: str,
    evidence_domains: Mapping[str, str],
    contracts: Contracts | None = None,
    strict: bool | None = None,
) -> VerificationReport:
    """Return a schema-valid, internally consistent output for ``case_id``.

    ``evidence_domains`` maps each evidence_ref consumed for this case, meaning one
    ``tool_result_consumed`` event was emitted for it, to the domain MCP returned with it.
    """
    strict = strict_mode() if strict is None else strict
    findings: list[Finding] = []
    try:
        output = _repair(draft, case_id, evidence_domains, findings)
        if contracts is not None:
            contracts.validate_output(output, f"verifier:{case_id}")
    except Exception as error:
        if strict:
            raise
        findings.append(Finding("FALLBACK_USED", type(error).__name__, lowers_confidence=True))
        return VerificationReport(fallback_output(case_id, evidence_domains), FALLBACK, findings)
    if strict and findings:
        summary = "; ".join(f"{finding.code}: {finding.detail}" for finding in findings)
        raise VerificationError(f"{case_id}: {summary}")
    return VerificationReport(output, REPAIRED if findings else VERIFIED, findings)


def verify_and_trace(
    draft: Any,
    *,
    case_id: str,
    evidence_domains: Mapping[str, str],
    trace: TraceWriter,
    contracts: Contracts | None = None,
) -> dict[str, Any]:
    report = verify_output(
        draft, case_id=case_id, evidence_domains=evidence_domains, contracts=contracts
    )
    finding_codes = sorted({finding.code for finding in report.findings})
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=ACTOR,
        target="coordinator",
        decision_code=report.decision_code,
        attributes={
            "finding_count": len(report.findings),
            "finding_codes": ",".join(finding_codes) or None,
            "confidence": report.output["assessment"]["confidence"],
        },
    )
    return report.output


def _repair(
    draft: Any, case_id: str, evidence_domains: Mapping[str, str], findings: list[Finding]
) -> dict[str, Any]:
    if not isinstance(draft, dict):
        raise TypeError("draft output is not a JSON object")
    output = copy.deepcopy(draft)
    _repair_identity(output, case_id, findings)
    _repair_evidence(output, evidence_domains, findings)
    _remove_duplicates(output, findings)
    _repair_money_and_status(output, findings)
    _repair_responsibility(output, findings)
    _set_confidence(output, findings)
    return output


def _repair_identity(output: dict[str, Any], case_id: str, findings: list[Finding]) -> None:
    unknown_fields = sorted(set(output) - OUTPUT_FIELDS)
    for name in unknown_fields:
        del output[name]
    if unknown_fields:
        findings.append(Finding("UNKNOWN_FIELDS_DROPPED", ",".join(unknown_fields)))
    if output.get("case_id") != case_id:
        findings.append(Finding("CASE_ID_REPAIRED", case_id, lowers_confidence=True))
        output["case_id"] = case_id
    if output.get("schema_version") != OUTPUT_SCHEMA_VERSION:
        findings.append(Finding("SCHEMA_VERSION_REPAIRED", OUTPUT_SCHEMA_VERSION))
        output["schema_version"] = OUTPUT_SCHEMA_VERSION


def _repair_evidence(
    output: dict[str, Any], evidence_domains: Mapping[str, str], findings: list[Finding]
) -> None:
    cited = _keep_owned_refs(output["evidence_refs"], evidence_domains, "evidence_refs", findings)

    for claim in output.get("claim_assessments", []):
        claim_refs = _keep_owned_refs(
            claim["evidence_refs"], evidence_domains, f"claim {claim['claim_id']}", findings
        )
        claim["evidence_refs"] = claim_refs
        if not claim_refs and claim["verdict"] != "insufficient_evidence":
            findings.append(
                Finding("CLAIM_WITHOUT_EVIDENCE", str(claim["claim_id"]), lowers_confidence=True)
            )
            claim["verdict"] = "insufficient_evidence"
        uncited_claim_refs = [ref for ref in claim_refs if ref not in cited]
        if uncited_claim_refs:
            findings.append(Finding("CLAIM_EVIDENCE_MERGED", str(claim["claim_id"])))
            cited.extend(uncited_claim_refs)

    assessment = output["assessment"]
    cited_domains = {evidence_domains[ref] for ref in cited}
    for domain in sorted(REQUIRED_DOMAINS.get(assessment["primary_issue"], frozenset())):
        if domain in cited_domains:
            continue
        consumed_in_domain = [ref for ref, owner in evidence_domains.items() if owner == domain]
        if consumed_in_domain:
            findings.append(Finding("REQUIRED_EVIDENCE_CITED", domain))
            cited.extend(ref for ref in consumed_in_domain if ref not in cited)
        else:
            findings.append(Finding("REQUIRED_EVIDENCE_MISSING", domain, lowers_confidence=True))

    if not cited and assessment["primary_issue"] != "insufficient_evidence":
        findings.append(
            Finding(
                "CONCLUSION_WITHOUT_EVIDENCE", assessment["primary_issue"], lowers_confidence=True
            )
        )
        assessment["primary_issue"] = "insufficient_evidence"

    if len(cited) > MAX_EVIDENCE_REFS:
        findings.append(Finding("EVIDENCE_REFS_TRUNCATED", str(len(cited))))
    output["evidence_refs"] = cited[:MAX_EVIDENCE_REFS]


def _keep_owned_refs(
    refs: list[str], evidence_domains: Mapping[str, str], where: str, findings: list[Finding]
) -> list[str]:
    foreign = [ref for ref in refs if ref not in evidence_domains]
    if foreign:
        findings.append(
            Finding("FOREIGN_EVIDENCE_DROPPED", f"{where}: {len(foreign)}", lowers_confidence=True)
        )
    owned = [ref for ref in refs if ref in evidence_domains]
    kept = list(dict.fromkeys(owned))
    if len(kept) != len(owned):
        findings.append(Finding("DUPLICATE_EVIDENCE_DROPPED", where))
    return kept


def _remove_duplicates(output: dict[str, Any], findings: list[Finding]) -> None:
    actions = [action.strip() for action in output["resolution_actions"]]
    unique_actions: dict[str, str] = {}
    for action in actions:
        if action:
            unique_actions.setdefault(action.casefold(), action)
    if len(unique_actions) != len(output["resolution_actions"]):
        findings.append(Finding("DUPLICATE_ACTIONS_DROPPED", str(len(actions))))
    output["resolution_actions"] = list(unique_actions.values())[:MAX_ACTIONS]

    entities = output["affected_entities"]
    for name in ENTITY_FIELDS:
        unique_ids = list(dict.fromkeys(entities[name]))[:MAX_ENTITY_IDS]
        if unique_ids != entities[name]:
            findings.append(Finding("DUPLICATE_ENTITY_IDS_DROPPED", name))
        entities[name] = unique_ids

    analysis = output["root_cause_analysis"]
    best_rank_by_cause: dict[str, int] = {}
    for cause in analysis["ranked_causes"]:
        code, rank = cause["cause_code"], cause["rank"]
        best_rank_by_cause[code] = min(rank, best_rank_by_cause.get(code, rank))
    ordered_codes = sorted(best_rank_by_cause, key=best_rank_by_cause.__getitem__)
    renumbered = [
        {"cause_code": code, "rank": rank}
        for rank, code in enumerate(ordered_codes[:MAX_CAUSES], start=1)
    ]
    if renumbered != analysis["ranked_causes"]:
        findings.append(Finding("CAUSE_RANKS_RENUMBERED", str(len(renumbered))))
    analysis["ranked_causes"] = renumbered

    parties = analysis["responsible_parties"]
    unique_parties = list(
        {(party["party_type"], party["party_id"]): party for party in parties}.values()
    )[:MAX_PARTIES]
    if len(unique_parties) != len(parties):
        findings.append(Finding("DUPLICATE_PARTIES_DROPPED", str(len(parties))))
    analysis["responsible_parties"] = unique_parties

    for name, limit in (("claim_assessments", MAX_CLAIMS), ("data_conflicts", MAX_CONFLICTS)):
        if len(output.get(name, [])) > limit:
            findings.append(Finding("LIST_TRUNCATED", name))
            output[name] = output[name][:limit]


def _repair_money_and_status(output: dict[str, Any], findings: list[Finding]) -> None:
    assessment = output["assessment"]
    money = output["financial_resolution"]
    lines = money["refund_lines"]

    if money.get("currency") != "BRL":
        findings.append(Finding("CURRENCY_REPAIRED", "BRL"))
        money["currency"] = "BRL"

    if assessment["primary_issue"] == "insufficient_evidence":
        if lines or money["recommended_refund_brl"]:
            findings.append(Finding("REFUND_CLEARED_WITHOUT_EVIDENCE", "insufficient_evidence"))
        lines.clear()
        money["recommended_refund_brl"] = 0
        if assessment["case_status"] != "needs_investigation":
            findings.append(Finding("STATUS_SET_NEEDS_INVESTIGATION", "insufficient_evidence"))
            assessment["case_status"] = "needs_investigation"

    if not lines and money["recommended_refund_brl"] > 0:
        order_ids = output["affected_entities"]["order_ids"]
        lines.append(
            {
                "reason_code": "UNITEMIZED_REFUND",
                "amount_brl": money["recommended_refund_brl"],
                "entity_id": order_ids[0] if order_ids else None,
            }
        )
        findings.append(Finding("REFUND_LINE_ADDED", "UNITEMIZED_REFUND"))
    if len(lines) > MAX_REFUND_LINES:
        findings.append(Finding("LIST_TRUNCATED", "refund_lines"))
        del lines[MAX_REFUND_LINES:]

    line_total = round(sum(float(line["amount_brl"]) for line in lines), 2)
    if abs(line_total - money["recommended_refund_brl"]) > MONEY_TOLERANCE_BRL:
        findings.append(Finding("REFUND_TOTAL_REPAIRED", f"{line_total:.2f}"))
    money["recommended_refund_brl"] = line_total

    actions = output["resolution_actions"]
    if line_total > 0 and assessment["case_status"] != "action_required":
        findings.append(Finding("STATUS_SET_ACTION_REQUIRED", assessment["case_status"]))
        assessment["case_status"] = "action_required"
    elif assessment["case_status"] == "action_required" and line_total == 0 and not actions:
        findings.append(Finding("STATUS_SET_NEEDS_INVESTIGATION", "nothing_to_act_on"))
        assessment["case_status"] = "needs_investigation"
    if assessment["case_status"] == "no_action" and actions:
        findings.append(Finding("NO_ACTION_WITH_ACTIONS", str(len(actions))))


def _repair_responsibility(output: dict[str, Any], findings: list[Finding]) -> None:
    responsibility = DELAY_RESPONSIBILITY.get(output["assessment"]["primary_issue"])
    if responsibility is None:
        return
    at_fault, not_at_fault = responsibility
    analysis = output["root_cause_analysis"]
    parties = [
        party for party in analysis["responsible_parties"] if party["party_type"] != not_at_fault
    ]
    if len(parties) != len(analysis["responsible_parties"]):
        findings.append(Finding("WRONG_PARTY_BLAMED", not_at_fault, lowers_confidence=True))
    if not any(party["party_type"] == at_fault for party in parties):
        seller_ids = output["affected_entities"]["seller_ids"]
        party_id = seller_ids[0] if at_fault == "seller" and len(seller_ids) == 1 else None
        parties.insert(0, {"party_type": at_fault, "party_id": party_id})
        findings.append(Finding("RESPONSIBLE_PARTY_ADDED", at_fault))
    analysis["responsible_parties"] = parties[:MAX_PARTIES]


def _set_confidence(output: dict[str, Any], findings: list[Finding]) -> None:
    assessment = output["assessment"]
    doubtful = bool(output["data_conflicts"]) or any(
        finding.lowers_confidence for finding in findings
    )
    assessment["confidence"] = calibrated_confidence(assessment["primary_issue"], doubtful)
    for claim in output.get("claim_assessments", []):
        confidence = min(
            max(float(claim["confidence"]), CLAIM_CONFIDENCE_FLOOR), CLAIM_CONFIDENCE_CEILING
        )
        if claim["verdict"] == "insufficient_evidence":
            confidence = min(confidence, CONFIDENCE_INSUFFICIENT)
        claim["confidence"] = confidence

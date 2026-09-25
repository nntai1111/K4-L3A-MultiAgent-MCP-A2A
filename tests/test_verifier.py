from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.agents import verifier
from student_agent.agents.verifier import (
    CONFIDENCE_CLEAN,
    CONFIDENCE_DOUBTFUL,
    CONFIDENCE_INSUFFICIENT,
    FALLBACK,
    REPAIRED,
    VERIFIED,
    VerificationError,
    calibrated_confidence,
    fallback_output,
    verify_and_trace,
    verify_output,
)
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")

CASE_ID = "L3A_CASE_001"
ORDER_REF = "ev_order_aaaaaaaaaaaaaaaaaaaa"
PAYMENT_REF = "ev_payment_bbbbbbbbbbbbbbbbbbbb"
SHIPMENT_REF = "ev_shipment_cccccccccccccccccccc"
REFUND_REF = "ev_refund_dddddddddddddddddddd"
OTHER_CASE_REF = "ev_othercase_eeeeeeeeeeeeeeeeeeee"
LEDGER = {
    ORDER_REF: "order",
    PAYMENT_REF: "payment",
    SHIPMENT_REF: "shipment",
    REFUND_REF: "refund",
}


def clean_output() -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": CASE_ID,
        "assessment": {
            "primary_issue": "duplicate_charge",
            "case_status": "action_required",
            "confidence": 0.9,
        },
        "affected_entities": {
            "order_ids": ["order-1"],
            "item_ids": ["item-1"],
            "seller_ids": ["seller-1"],
            "payment_references": ["pay-1", "pay-2"],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": "C1",
                "verdict": "supported",
                "confidence": 0.9,
                "evidence_refs": [PAYMENT_REF],
            }
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "DUPLICATE_CAPTURE", "rank": 1}],
            "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
        },
        "evidence_refs": [ORDER_REF, PAYMENT_REF],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 49.9,
            "refund_lines": [
                {"reason_code": "DUPLICATE_CHARGE", "amount_brl": 49.9, "entity_id": "pay-2"}
            ],
        },
        "resolution_actions": ["refund_duplicate_charge"],
    }


def late_delivery_output(primary_issue: str) -> dict[str, Any]:
    output = clean_output()
    output["assessment"]["primary_issue"] = primary_issue
    output["claim_assessments"] = []
    output["evidence_refs"] = [SHIPMENT_REF]
    output["financial_resolution"]["refund_lines"] = []
    output["financial_resolution"]["recommended_refund_brl"] = 0
    output["resolution_actions"] = ["notify_responsible_party"]
    return output


def verify(draft: Any, ledger: dict[str, str] | None = None) -> verifier.VerificationReport:
    report = verify_output(
        draft,
        case_id=CASE_ID,
        evidence_domains=LEDGER if ledger is None else ledger,
        contracts=CONTRACTS,
        strict=False,
    )
    CONTRACTS.validate_output(report.output, "verified output")
    return report


def codes(report: verifier.VerificationReport) -> set[str]:
    return {finding.code for finding in report.findings}


def test_clean_output_passes_and_gets_the_clean_confidence() -> None:
    draft = clean_output()
    report = verify(draft)
    assert report.decision_code == VERIFIED
    assert report.findings == []
    assert report.output["assessment"]["confidence"] == CONFIDENCE_CLEAN
    assert draft == clean_output(), "the draft must not be mutated"


def test_evidence_from_another_case_is_dropped_and_lowers_confidence() -> None:
    draft = clean_output()
    draft["evidence_refs"].append(OTHER_CASE_REF)
    draft["claim_assessments"][0]["evidence_refs"].append(OTHER_CASE_REF)
    report = verify(draft)
    assert OTHER_CASE_REF not in report.output["evidence_refs"]
    assert OTHER_CASE_REF not in report.output["claim_assessments"][0]["evidence_refs"]
    assert "FOREIGN_EVIDENCE_DROPPED" in codes(report)
    assert report.output["assessment"]["confidence"] == CONFIDENCE_DOUBTFUL


def test_claim_left_without_evidence_becomes_insufficient() -> None:
    draft = clean_output()
    draft["claim_assessments"][0]["evidence_refs"] = [OTHER_CASE_REF]
    report = verify(draft)
    claim = report.output["claim_assessments"][0]
    assert claim["verdict"] == "insufficient_evidence"
    assert claim["confidence"] <= CONFIDENCE_INSUFFICIENT


def test_claim_evidence_is_merged_into_output_evidence() -> None:
    draft = clean_output()
    draft["claim_assessments"][0]["evidence_refs"] = [PAYMENT_REF, REFUND_REF]
    report = verify(draft)
    assert REFUND_REF in report.output["evidence_refs"]
    assert "CLAIM_EVIDENCE_MERGED" in codes(report)


def test_required_domain_is_cited_from_consumed_evidence() -> None:
    draft = clean_output()
    draft["evidence_refs"] = [ORDER_REF]
    draft["claim_assessments"] = []
    report = verify(draft)
    assert PAYMENT_REF in report.output["evidence_refs"]
    assert "REQUIRED_EVIDENCE_CITED" in codes(report)
    assert report.output["assessment"]["confidence"] == CONFIDENCE_CLEAN


def test_required_domain_never_consumed_lowers_confidence() -> None:
    draft = clean_output()
    draft["claim_assessments"] = []
    draft["evidence_refs"] = [ORDER_REF]
    report = verify(draft, ledger={ORDER_REF: "order"})
    assert "REQUIRED_EVIDENCE_MISSING" in codes(report)
    assert report.output["assessment"]["primary_issue"] == "duplicate_charge"
    assert report.output["assessment"]["confidence"] == CONFIDENCE_DOUBTFUL


def test_conclusion_without_any_evidence_is_downgraded() -> None:
    draft = clean_output()
    draft["assessment"]["primary_issue"] = "unsupported_claim"
    draft["claim_assessments"] = []
    draft["evidence_refs"] = []
    report = verify(draft)
    assessment = report.output["assessment"]
    assert assessment["primary_issue"] == "insufficient_evidence"
    assert assessment["case_status"] == "needs_investigation"
    assert assessment["confidence"] == CONFIDENCE_INSUFFICIENT
    assert report.output["financial_resolution"]["recommended_refund_brl"] == 0


def test_refund_total_is_the_sum_of_refund_lines() -> None:
    draft = clean_output()
    draft["financial_resolution"]["refund_lines"].append(
        {"reason_code": "SHIPPING_FEE", "amount_brl": 10.1, "entity_id": "order-1"}
    )
    report = verify(draft)
    assert report.output["financial_resolution"]["recommended_refund_brl"] == 60.0
    assert "REFUND_TOTAL_REPAIRED" in codes(report)


def test_float_noise_in_the_total_is_not_a_finding() -> None:
    draft = clean_output()
    draft["financial_resolution"]["refund_lines"] = [
        {"reason_code": "A", "amount_brl": 0.1, "entity_id": None},
        {"reason_code": "B", "amount_brl": 0.2, "entity_id": None},
    ]
    draft["financial_resolution"]["recommended_refund_brl"] = 0.3
    report = verify(draft)
    assert report.decision_code == VERIFIED
    assert report.output["financial_resolution"]["recommended_refund_brl"] == 0.3


def test_refund_without_lines_keeps_the_amount_as_one_line() -> None:
    draft = clean_output()
    draft["financial_resolution"]["refund_lines"] = []
    report = verify(draft)
    money = report.output["financial_resolution"]
    assert money["recommended_refund_brl"] == 49.9
    assert money["refund_lines"] == [
        {"reason_code": "UNITEMIZED_REFUND", "amount_brl": 49.9, "entity_id": "order-1"}
    ]


def test_a_refund_requires_action_required_status() -> None:
    draft = clean_output()
    draft["assessment"]["case_status"] = "no_action"
    report = verify(draft)
    assert report.output["assessment"]["case_status"] == "action_required"
    assert "STATUS_SET_ACTION_REQUIRED" in codes(report)


def test_action_required_with_nothing_to_do_needs_investigation() -> None:
    draft = clean_output()
    draft["financial_resolution"]["refund_lines"] = []
    draft["financial_resolution"]["recommended_refund_brl"] = 0
    draft["resolution_actions"] = []
    report = verify(draft)
    assert report.output["assessment"]["case_status"] == "needs_investigation"


def test_no_action_with_actions_is_flagged_but_kept() -> None:
    draft = clean_output()
    draft["assessment"]["case_status"] = "no_action"
    draft["financial_resolution"]["refund_lines"] = []
    draft["financial_resolution"]["recommended_refund_brl"] = 0
    report = verify(draft)
    assert "NO_ACTION_WITH_ACTIONS" in codes(report)
    assert report.output["resolution_actions"] == ["refund_duplicate_charge"]


def test_insufficient_evidence_never_carries_a_refund() -> None:
    draft = clean_output()
    draft["assessment"]["primary_issue"] = "insufficient_evidence"
    report = verify(draft)
    money = report.output["financial_resolution"]
    assert money["recommended_refund_brl"] == 0
    assert money["refund_lines"] == []
    assert report.output["assessment"]["case_status"] == "needs_investigation"


def test_seller_delay_never_blames_logistics() -> None:
    draft = late_delivery_output("late_delivery_seller")
    draft["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "logistics_provider", "party_id": "carrier-1"}
    ]
    report = verify(draft)
    parties = report.output["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "seller", "party_id": "seller-1"}]
    assert "WRONG_PARTY_BLAMED" in codes(report)


def test_logistics_delay_never_blames_the_seller() -> None:
    draft = late_delivery_output("late_delivery_logistics")
    draft["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "seller", "party_id": "seller-1"},
        {"party_type": "logistics_provider", "party_id": "carrier-1"},
    ]
    report = verify(draft)
    parties = report.output["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "logistics_provider", "party_id": "carrier-1"}]


def test_duplicate_actions_are_dropped() -> None:
    draft = clean_output()
    draft["resolution_actions"] = ["refund_duplicate_charge", " Refund_Duplicate_Charge ", ""]
    report = verify(draft)
    assert report.output["resolution_actions"] == ["refund_duplicate_charge"]
    assert "DUPLICATE_ACTIONS_DROPPED" in codes(report)


def test_cause_ranks_are_renumbered_without_duplicates() -> None:
    draft = clean_output()
    draft["root_cause_analysis"]["ranked_causes"] = [
        {"cause_code": "LATE_HANDOFF", "rank": 3},
        {"cause_code": "DUPLICATE_CAPTURE", "rank": 2},
        {"cause_code": "LATE_HANDOFF", "rank": 5},
    ]
    report = verify(draft)
    assert report.output["root_cause_analysis"]["ranked_causes"] == [
        {"cause_code": "DUPLICATE_CAPTURE", "rank": 1},
        {"cause_code": "LATE_HANDOFF", "rank": 2},
    ]


def test_a_data_conflict_caps_confidence() -> None:
    draft = clean_output()
    draft["data_conflicts"] = [
        {
            "field": "order_status",
            "sources": ["order", "payment"],
            "selected_source": "payment",
            "resolution_code": "PREFER_PAYMENT_LIFECYCLE",
        }
    ]
    report = verify(draft)
    assert report.output["assessment"]["confidence"] == CONFIDENCE_DOUBTFUL


def test_case_id_and_unknown_fields_are_repaired() -> None:
    draft = clean_output()
    draft["case_id"] = "L3A_CASE_999"
    draft["debug_notes"] = "left over"
    report = verify(draft)
    assert report.output["case_id"] == CASE_ID
    assert "debug_notes" not in report.output
    assert {"CASE_ID_REPAIRED", "UNKNOWN_FIELDS_DROPPED"} <= codes(report)
    assert report.decision_code == REPAIRED


def broken_drafts() -> list[Any]:
    missing_assessment = clean_output()
    del missing_assessment["assessment"]
    negative_refund = clean_output()
    negative_refund["financial_resolution"]["refund_lines"][0]["amount_brl"] = -5
    wrong_types = clean_output()
    wrong_types["resolution_actions"] = [None]
    unknown_issue = clean_output()
    unknown_issue["assessment"]["primary_issue"] = "made_up_issue"
    return [None, [], {}, missing_assessment, negative_refund, wrong_types, unknown_issue]


@pytest.mark.parametrize("draft", broken_drafts())
def test_broken_drafts_fall_back_without_raising(draft: Any) -> None:
    report = verify(draft)
    assert report.decision_code == FALLBACK
    assert report.output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert report.output["case_id"] == CASE_ID
    assert set(report.output["evidence_refs"]) <= set(LEDGER)


def test_fallback_output_is_schema_valid() -> None:
    CONTRACTS.validate_output(fallback_output(CASE_ID, LEDGER), "fallback")
    CONTRACTS.validate_output(fallback_output(CASE_ID, {}), "fallback without evidence")


def test_strict_mode_raises_instead_of_repairing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAY09_STRICT", "1")
    draft = clean_output()
    draft["evidence_refs"].append(OTHER_CASE_REF)
    with pytest.raises(VerificationError, match="FOREIGN_EVIDENCE_DROPPED"):
        verify_output(draft, case_id=CASE_ID, evidence_domains=LEDGER, contracts=CONTRACTS)
    with pytest.raises(TypeError):
        verify_output(None, case_id=CASE_ID, evidence_domains=LEDGER)


def test_calibration_never_reports_certainty() -> None:
    for primary_issue in ("duplicate_charge", "insufficient_evidence"):
        for doubtful in (False, True):
            assert 0 < calibrated_confidence(primary_issue, doubtful) < 1
    assert calibrated_confidence("duplicate_charge", doubtful=True) < CONFIDENCE_CLEAN


def test_verify_and_trace_emits_verification_completed(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    draft = copy.deepcopy(clean_output())
    output = verify_and_trace(
        draft,
        case_id=CASE_ID,
        evidence_domains=LEDGER,
        trace=TraceWriter(trace_path, CONTRACTS),
        contracts=CONTRACTS,
    )
    (event,) = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert event["event_type"] == "verification_completed"
    assert event["actor"] == "verifier"
    assert event["target"] == "coordinator"
    assert event["decision_code"] == VERIFIED
    assert event["attributes"]["confidence"] == output["assessment"]["confidence"]

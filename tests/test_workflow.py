from __future__ import annotations

import asyncio
import json
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from student_agent.agents.base import GatewayUnavailable
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import ToolError
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
ORDER_ID = "order0000000000000000000000000001"
SELLER_ID = "seller-000000000001"
POLICY = {
    "currency": "BRL",
    "policy_version": "TEST_POLICY",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_id": None, "party_type": "platform"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-from-example", "party_type": "seller"}],
        },
        "valid_split_payment": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
        "refund_pending": {
            "case_status": "needs_investigation",
            "recommended_action": "monitor_refund",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "unsupported_claim": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    },
}
DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_sellers": "seller",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_policy": "policy",
}


def order_row(status: str, delivered: str | None) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "customer_id": "customer-row-1",
        "order_status": status,
        "order_purchase_timestamp": "2018-03-01T09:00:00-03:00",
        "order_approved_at": "2018-03-01T10:00:00-03:00",
        "order_delivered_carrier_date": "2018-03-03T09:00:00-03:00",
        "order_delivered_customer_date": delivered,
        "order_estimated_delivery_date": "2018-03-10T09:00:00-03:00",
    }


def item(limit: str, freight: str) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "order_item_id": "item-1",
        "product_id": "product-1",
        "seller_id": SELLER_ID,
        "shipping_limit_date": limit,
        "price": "79.00",
        "freight_value": freight,
    }


def captured(at: str, amount: str) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "event_at": at,
        "event_type": "captured",
        "amount_brl": amount,
        "status": "confirmed",
    }


def scenario(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A case input plus the MCP data for one family, each with a planted stale record."""
    stale_item = item("2018-07-20T09:00:00-03:00", "18.00")
    stale_capture = captured("2018-06-11T10:00:00-03:00", "52.00")
    data: dict[str, Any] = {
        "get_order": order_row("delivered", "2018-03-08T09:00:00-03:00"),
        "get_order_items": [item("2018-03-04T09:00:00-03:00", "10.00"), stale_item],
        "get_payment_timeline": {
            "order_id": ORDER_ID,
            "payments": [],
            "events": [captured("2018-03-01T10:00:00-03:00", "89.00"), stale_capture],
        },
        "get_shipment_summary": {
            "order_id": ORDER_ID,
            "order_status": "delivered",
            "delivered_carrier_at": "2018-03-03T09:00:00-03:00",
            "delivered_customer_at": "2018-03-08T09:00:00-03:00",
            "estimated_delivery_at": "2018-03-10T09:00:00-03:00",
            "shipping_limits": [
                {
                    "order_item_id": "item-1",
                    "seller_id": SELLER_ID,
                    "shipping_limit_at": "2018-03-04T09:00:00-03:00",
                }
            ],
            "events": [
                {
                    "order_id": ORDER_ID,
                    "event_at": "2018-06-01T09:00:00-03:00",
                    "event_type": "delivered_late",
                    "actor": "logistics_provider",
                    "status": "confirmed",
                }
            ],
        },
        "get_policy": POLICY,
        "get_sellers": [
            {
                "seller_id": SELLER_ID,
                "seller_zip_code_prefix": "01001",
                "seller_city": "sao_paulo",
                "seller_state": "SP",
            }
        ],
        "get_order_payments": [
            {
                "order_id": ORDER_ID,
                "payment_sequential": "1",
                "payment_type": "credit_card",
                "payment_installments": "1",
                "payment_value": "89.00",
            }
        ],
    }
    topic = name
    if name == "canceled_order_paid":
        data["get_order"] = order_row("canceled", None)
        data["get_payment_timeline"]["events"][0] = captured("2018-03-01T10:00:00-03:00", "79.00")
    elif name == "late_delivery_seller":
        data["get_shipment_summary"]["delivered_carrier_at"] = "2018-03-06T09:00:00-03:00"
        data["get_shipment_summary"]["delivered_customer_at"] = "2018-03-12T09:00:00-03:00"
    elif name == "valid_split_payment":
        data["get_payment_timeline"]["events"][:1] = [
            captured("2018-03-01T10:00:00-03:00", "44.50"),
            captured("2018-03-01T11:00:00-03:00", "44.50"),
        ]
    elif name == "refund_pending":
        data["get_refund_timeline"] = {
            "order_id": ORDER_ID,
            "events": [
                {
                    "order_id": ORDER_ID,
                    "event_at": "2018-03-19T09:00:00-03:00",
                    "event_type": "refund_requested",
                    "amount_brl": "89.00",
                    "status": "pending",
                }
            ],
        }
    case = {
        "case_id": "L3A_TEST_001",
        "opened_at": "2018-03-20T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "test",
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "c-a", "topic": topic},
                {"claim_id": "c-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "TEST_POLICY",
    }
    return case, data


class FakeGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []
        self.tool_of_ref: dict[str, str] = {}
        self._ids = count(1)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append(tool_name)
        if tool_name not in self.data:
            raise ToolError(f"MCP tool {tool_name} failed: Error executing tool")
        evidence_ref = f"ev_test_{next(self._ids):04d}_xxxxxxxxxxxxxxxx"
        self.tool_of_ref[evidence_ref] = tool_name
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": evidence_ref,
            "result_hash": "sha256:" + "0" * 64,
            "domain": DOMAINS[tool_name],
            "data": self.data[tool_name],
        }


def run(name: str, tmp_path: Path) -> tuple[dict[str, Any], FakeGateway, list[dict[str, Any]]]:
    case, data = scenario(name)
    gateway = FakeGateway(data)
    trace_path = tmp_path / "trace.jsonl"
    output = asyncio.run(solve_case(case, gateway, TraceWriter(trace_path, CONTRACTS)))
    CONTRACTS.validate_output(output, name)
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    return output, gateway, events


@pytest.mark.parametrize(
    ("name", "status", "refund"),
    [
        ("canceled_order_paid", "action_required", 79.0),
        ("late_delivery_seller", "action_required", 18.0),
        ("valid_split_payment", "no_action", 0),
        ("refund_pending", "needs_investigation", 0),
        ("unsupported_claim", "no_action", 0),
    ],
)
def test_each_family_is_decided_from_evidence(
    name: str, status: str, refund: float, tmp_path: Path
) -> None:
    output, _, _ = run(name, tmp_path)
    assert output["assessment"]["primary_issue"] == name
    assert output["assessment"]["case_status"] == status
    assert output["financial_resolution"]["recommended_refund_brl"] == refund


def test_stale_records_are_ignored_and_recorded_as_conflicts(tmp_path: Path) -> None:
    output, _, _ = run("unsupported_claim", tmp_path)
    assert output["affected_entities"]["item_ids"] == ["item-1"]
    fields = {conflict["field"] for conflict in output["data_conflicts"]}
    assert {"order_items.shipping_limit_date", "payment_timeline.events"} <= fields


def test_seller_party_names_this_cases_seller(tmp_path: Path) -> None:
    output, _, _ = run("late_delivery_seller", tmp_path)
    parties = output["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "seller", "party_id": SELLER_ID}]


def test_refund_history_is_only_requested_for_refund_claims(tmp_path: Path) -> None:
    _, gateway, _ = run("canceled_order_paid", tmp_path)
    assert "get_refund_timeline" not in gateway.calls
    _, gateway, _ = run("refund_pending", tmp_path / "second")
    assert gateway.calls.count("get_refund_timeline") == 1


def test_cited_evidence_is_consumed_and_never_product_or_customer(tmp_path: Path) -> None:
    output, gateway, events = run("canceled_order_paid", tmp_path)
    consumed = {
        ref
        for e in events
        if e["event_type"] == "tool_result_consumed"
        for ref in e["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    cited_tools = {gateway.tool_of_ref[ref] for ref in output["evidence_refs"]}
    assert cited_tools == {
        "get_order",
        "get_order_items",
        "get_sellers",
        "get_payment_timeline",
        "get_order_payments",
        "get_shipment_summary",
        "get_policy",
    }


def test_trace_covers_every_lifecycle_step_with_real_actors(tmp_path: Path) -> None:
    _, _, events = run("late_delivery_seller", tmp_path)
    types = [e["event_type"] for e in events]
    for required in (
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
    ):
        assert required in types
    assert {e["actor"] for e in events} == {
        "coordinator",
        "order-agent",
        "payment-agent",
        "shipment-agent",
        "policy-agent",
        "verifier",
    }


def test_a_missing_order_becomes_insufficient_evidence(tmp_path: Path) -> None:
    case, data = scenario("canceled_order_paid")
    del data["get_order"]
    gateway = FakeGateway(data)
    output = asyncio.run(solve_case(case, gateway, TraceWriter(tmp_path / "t.jsonl", CONTRACTS)))
    CONTRACTS.validate_output(output, "missing order")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"


class DroppedConnectionGateway(FakeGateway):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        raise ConnectionResetError("connection dropped")


def test_a_dropped_connection_is_retried_not_answered(tmp_path: Path) -> None:
    case, data = scenario("canceled_order_paid")
    gateway = DroppedConnectionGateway(data)
    with pytest.raises(GatewayUnavailable):
        asyncio.run(solve_case(case, gateway, TraceWriter(tmp_path / "t.jsonl", CONTRACTS)))

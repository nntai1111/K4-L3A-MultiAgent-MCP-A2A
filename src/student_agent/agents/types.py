"""Shared handoff types. Change only when the whole team agrees."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACTORS = (
    "coordinator",
    "order-agent",
    "shipment-agent",
    "payment-agent",
    "policy-agent",
    "verifier",
)


@dataclass(frozen=True)
class Claim:
    claim_id: str
    topic: str


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    opened_at: str
    order_id: str | None
    policy_version: str | None
    claims: tuple[Claim, ...]

    @classmethod
    def from_input(cls, case: dict[str, Any]) -> CaseContext:
        request = case.get("customer_request") or {}
        claims = tuple(
            Claim(str(item["claim_id"]), str(item["topic"]))
            for item in request.get("claims") or []
            if isinstance(item, dict) and item.get("claim_id") and item.get("topic")
        )
        return cls(
            case_id=case["case_id"],
            opened_at=case.get("opened_at") or "",
            order_id=request.get("claimed_order_id") or None,
            policy_version=case.get("policy_version") or None,
            claims=claims,
        )

    @property
    def issue_claim(self) -> Claim | None:
        """The claim naming the customer's alleged issue (not ground truth)."""
        return next((c for c in self.claims if c.topic != "requested_full_refund"), None)


@dataclass(frozen=True)
class Evidence:
    """One validated MCP envelope. `ref` is copied verbatim from the gateway."""

    tool: str
    domain: str
    ref: str
    data: Any


@dataclass
class AgentResult:
    actor: str
    ok: bool
    evidence: list[Evidence] = field(default_factory=list)
    entities: dict[str, list[str]] = field(default_factory=dict)
    findings: dict[str, Any] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    notes_code: str = "OK"

    @property
    def evidence_refs(self) -> list[str]:
        return [item.ref for item in self.evidence]

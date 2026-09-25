"""Shared data passed between agents for one case. Created per case, never reused."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

OK = "ok"
NOT_FOUND = "not_found"
ERROR = "error"


@dataclass(frozen=True)
class Evidence:
    evidence_ref: str
    tool_name: str
    domain: str
    data: Any


@dataclass
class SpecialistResult:
    case_id: str
    actor: str
    status: str = OK
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CaseState:
    case: dict[str, Any]
    results: dict[str, SpecialistResult] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    @property
    def order_id(self) -> str:
        return self.case["customer_request"]["claimed_order_id"]

    @property
    def claim_topics(self) -> list[str]:
        return [claim["topic"] for claim in self.case["customer_request"].get("claims", [])]

    def facts(self, actor: str) -> dict[str, Any]:
        result = self.results.get(actor)
        return result.facts if result else {}

    def evidence_domains(self) -> dict[str, str]:
        """Every evidence_ref consumed for this case, mapped to its MCP domain."""
        return {
            item.evidence_ref: item.domain
            for result in self.results.values()
            for item in result.evidence
        }

    def refs_in(self, domains: set[str]) -> list[str]:
        return [
            item.evidence_ref
            for result in self.results.values()
            for item in result.evidence
            if item.domain in domains
        ]

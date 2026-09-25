"""The one way agents reach MCP: allowlisted, case-scoped, bounded retry, traced."""

from __future__ import annotations

import asyncio
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..state import Evidence
from ..trace import TraceWriter

TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "coordinator": frozenset(),
    "order-agent": frozenset({"get_order", "get_order_items", "get_sellers"}),
    "payment-agent": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "policy-agent": frozenset({"get_policy"}),
    "verifier": frozenset(),
}

CALL_TIMEOUT_SECONDS = 90.0
TIMEOUT_RETRIES = 2


class ToolNotAllowed(PermissionError):
    pass


async def fetch_evidence(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    actor: str,
    case_id: str,
    tool_name: str,
    **arguments: str,
) -> Evidence | None:
    """Call one MCP tool for this case and record the evidence it returned.

    A tool error means the source has no record; it is never retried, because a
    repeated failing call is still a failing call in the audit. Only transport
    timeouts are retried, a bounded number of times.
    """
    if tool_name not in TOOL_ALLOWLIST.get(actor, frozenset()):
        raise ToolNotAllowed(f"{actor} may not call {tool_name}")
    response: dict[str, Any] | None = None
    for attempt in range(TIMEOUT_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                gateway.call(tool_name, case_id=case_id, **arguments), CALL_TIMEOUT_SECONDS
            )
            break
        except TimeoutError:
            if attempt == TIMEOUT_RETRIES:
                return None
            await asyncio.sleep(1.5 * (attempt + 1))
        except (RuntimeError, ValueError):
            return None
    if response is None:
        return None
    evidence = Evidence(
        evidence_ref=response["evidence_ref"],
        tool_name=tool_name,
        domain=response["domain"],
        data=response["data"],
    )
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence.evidence_ref],
        attributes={"domain": evidence.domain},
    )
    return evidence

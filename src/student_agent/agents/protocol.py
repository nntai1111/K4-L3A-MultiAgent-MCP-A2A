"""MCP access and A2A trace helpers shared by every actor."""

from __future__ import annotations

import asyncio
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .types import CaseContext, Evidence

MAX_ATTEMPTS = 3  # first call + 2 idempotent retries on transport failures


async def fetch(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ctx: CaseContext,
    actor: str,
    tool: str,
    **arguments: str,
) -> Evidence | None:
    """Call one MCP tool for this case and emit `tool_result_consumed` on success.

    A tool-level error (RuntimeError, e.g. not found / out of scope) is not retried and
    returns None: missing evidence stays missing, it is never replaced by a guess.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            envelope = await gateway.call(tool, case_id=ctx.case_id, **arguments)
        except RuntimeError:
            return None
        except Exception:
            if attempt == MAX_ATTEMPTS - 1:
                return None
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        evidence = Evidence(tool, envelope["domain"], envelope["evidence_ref"], envelope["data"])
        trace.emit(
            case_id=ctx.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[evidence.ref],
        )
        return evidence
    return None


def assign(trace: TraceWriter, ctx: CaseContext, target: str) -> None:
    trace.emit(case_id=ctx.case_id, event_type="task_assigned", actor="coordinator", target=target)


def handoff(
    trace: TraceWriter,
    ctx: CaseContext,
    actor: str,
    target: str,
    refs: list[str] | None = None,
    **attributes: Any,
) -> None:
    trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=actor,
        target=target,
        evidence_refs=list(dict.fromkeys(refs))[:20] if refs else None,
        attributes=attributes or None,
    )

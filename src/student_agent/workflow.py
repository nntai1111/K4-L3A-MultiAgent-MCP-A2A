from __future__ import annotations

from typing import Any

from .agents import order, payment, policy, shipment
from .agents.verifier import fallback_output, verify_and_trace
from .mcp_gateway import EvidenceGateway
from .state import CaseState, SpecialistResult
from .trace import TraceWriter

COORDINATOR = "coordinator"
REFUND_TOPICS = frozenset({"refund_pending", "refund_failed"})


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: route the case through the specialists, the policy agent and the verifier.

    Any failure inside the pipeline becomes an honest insufficient_evidence answer for this
    case only, so one case can never abort the batch.
    """
    state = CaseState(case)
    try:
        await _assign(
            state, trace, order.ACTOR, "COLLECT_ORDER_EVIDENCE", order.run(state, gateway, trace)
        )
        await _assign(
            state,
            trace,
            payment.ACTOR,
            "COLLECT_PAYMENT_EVIDENCE",
            payment.run(state, gateway, trace, check_refunds=_needs_refund_check(state)),
        )
        await _assign(
            state,
            trace,
            shipment.ACTOR,
            "COLLECT_SHIPMENT_EVIDENCE",
            shipment.run(state, gateway, trace),
        )

        trace.emit(
            case_id=state.case_id,
            event_type="task_assigned",
            actor=COORDINATOR,
            target=policy.ACTOR,
            decision_code="DECIDE_RESOLUTION",
        )
        state.results[policy.ACTOR] = await policy.run(state, gateway, trace)
        draft = policy.draft_output(state)
        trace.emit(
            case_id=state.case_id,
            event_type="policy_decided",
            actor=policy.ACTOR,
            target="verifier",
            decision_code=draft["assessment"]["primary_issue"],
            attributes={
                "case_status": draft["assessment"]["case_status"],
                "refund_brl": draft["financial_resolution"]["recommended_refund_brl"],
            },
        )
        trace.emit(
            case_id=state.case_id,
            event_type="handoff",
            actor=policy.ACTOR,
            target="verifier",
            decision_code=state.results[policy.ACTOR].status.upper(),
        )
    except Exception:  # noqa: BLE001 - the coordinator's safety net for this case only
        draft = fallback_output(state.case_id, state.evidence_domains())

    return verify_and_trace(
        draft, case_id=state.case_id, evidence_domains=state.evidence_domains(), trace=trace
    )


async def _assign(state: CaseState, trace: TraceWriter, actor: str, task: str, work) -> None:
    trace.emit(
        case_id=state.case_id,
        event_type="task_assigned",
        actor=COORDINATOR,
        target=actor,
        decision_code=task,
    )
    result: SpecialistResult = await work
    if result.case_id != state.case_id:
        raise ValueError(f"{actor} returned a result for another case")
    state.results[actor] = result
    trace.emit(
        case_id=state.case_id,
        event_type="handoff",
        actor=actor,
        target=COORDINATOR,
        decision_code=result.status.upper(),
        attributes={"evidence_count": len(result.evidence)},
    )


def _needs_refund_check(state: CaseState) -> bool:
    """Only ask for refund history when the customer's claims are about a refund.

    Orders without refunds make the refund tool fail, and failed calls are audited.
    """
    return bool(REFUND_TOPICS & set(state.claim_topics))

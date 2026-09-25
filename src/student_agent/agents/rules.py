"""Pure business rules shared by specialists, policy and verifier.

Observed in MCP data: every order carries decoy rows (items, payment/refund/shipment events)
dated before the purchase or after the case was opened. Only rows inside the
[order_purchase_timestamp, opened_at] window describe the dispute.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

MONEY_TOLERANCE = 0.05

# Checked in this order; the customer's claimed topic wins only among issues evidence supports.
ISSUE_PRIORITY = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "refund_pending",
    "refund_failed",
    "duplicate_charge",
    "payment_mismatch",
    "valid_split_payment",
    "late_delivery_seller",
    "late_delivery_logistics",
)

CAUSE_CODES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_CAPTURE",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_CAPTURE",
    "late_delivery_seller": "SELLER_SHIPPING_LIMIT_BREACH",
    "late_delivery_logistics": "CARRIER_TRANSIT_DELAY",
    "payment_mismatch": "PAYMENT_SUM_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "valid_split_payment": "SPLIT_PAYMENT_RECONCILED",
    "refund_pending": "REFUND_NOT_SETTLED",
    "refund_failed": "REFUND_ATTEMPT_FAILED",
    "unsupported_claim": "CLAIM_CONTRADICTS_EVIDENCE",
    "insufficient_evidence": "MISSING_REQUIRED_EVIDENCE",
}

# Evidence domains that support each conclusion (cited in the output).
ISSUE_DOMAINS = {
    "canceled_order_paid": ("order", "payment", "policy"),
    "unavailable_order_paid": ("order", "payment", "item", "seller", "policy"),
    "late_delivery_seller": ("order", "shipment", "item", "seller", "policy"),
    "late_delivery_logistics": ("order", "shipment", "policy"),
    "valid_split_payment": ("order", "payment", "policy"),
    "payment_mismatch": ("order", "payment", "policy"),
    "duplicate_charge": ("order", "payment", "policy"),
    "refund_pending": ("order", "payment", "refund", "policy"),
    "refund_failed": ("order", "payment", "refund", "policy"),
    "unsupported_claim": ("order", "policy"),
    "insufficient_evidence": ("order", "policy"),
}


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def in_window(value: Any, start: datetime | None, end: datetime | None) -> bool:
    moment = parse_ts(value)
    if moment is None:
        return False
    if start is not None and moment < start:
        return False
    return not (end is not None and moment > end)


def close(a: float, b: float) -> bool:
    return abs(a - b) <= MONEY_TOLERANCE


def candidate_issues(order: dict[str, Any], shipment: dict[str, Any], payment: dict[str, Any]):
    """Issues supported by in-window evidence, in priority order."""
    status = order.get("order_status")
    captured = payment.get("captured_total", 0.0)
    found = {
        "canceled_order_paid": status == "canceled" and captured > 0,
        "unavailable_order_paid": status == "unavailable" and captured > 0,
        "refund_pending": payment.get("refund_pending", False),
        "refund_failed": payment.get("refund_failed", False),
        "duplicate_charge": payment.get("duplicate", False),
        "payment_mismatch": payment.get("mismatch", False),
        "valid_split_payment": payment.get("split_valid", False),
        "late_delivery_seller": shipment.get("seller_late", False),
        "late_delivery_logistics": shipment.get("logistics_late", False),
    }
    return [issue for issue in ISSUE_PRIORITY if found[issue]]


def choose_issue(candidates: list[str], claimed_topic: str | None) -> str:
    if claimed_topic in candidates:
        return claimed_topic
    return candidates[0] if candidates else "unsupported_claim"

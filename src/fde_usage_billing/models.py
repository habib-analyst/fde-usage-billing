"""Core domain models. All money and units are Decimal, never float."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class UsageEvent:
    tenant: str
    meter: str
    units: Decimal
    ts: datetime
    idempotency_key: str

    def __post_init__(self):
        if not self.tenant:
            raise ValueError("tenant is required")
        if not self.meter:
            raise ValueError("meter is required")
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")
        if self.ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        if self.units < 0:
            raise ValueError("units must be >= 0")


@dataclass(frozen=True)
class Adjustment:
    """A credit or adjustment, applied as a negative (or correcting) line."""

    description: str
    amount: Decimal


@dataclass
class InvoiceLine:
    line_id: str
    kind: str  # "meter" | "adjustment"
    description: str
    amount: Decimal
    meter: str | None = None
    plan: str | None = None
    quantity: Decimal = Decimal("0")        # total units recorded in segment
    allowance_applied: Decimal = Decimal("0")
    billable_units: Decimal = Decimal("0")
    segment_start: datetime | None = None
    segment_end: datetime | None = None
    tier_breakdown: list[dict] = field(default_factory=list)


@dataclass
class Invoice:
    invoice_id: str
    tenant: str
    period_start: datetime
    period_end: datetime
    state: str  # "draft" | "finalized"
    pricebook_version: str
    lines: list[InvoiceLine]
    subtotal: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    total: Decimal
    created_at: datetime
    finalized_at: datetime | None = None
    reference_invoice_id: str | None = None

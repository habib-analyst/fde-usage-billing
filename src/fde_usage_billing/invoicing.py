"""Billing service: ingestion, aggregation, invoicing, proration.

Invoice lifecycle: draft -> finalized. A finalized invoice is immutable:
re-rating, adding lines or changing totals on it raises
ImmutableInvoiceError and leaves the stored invoice untouched. A
correction is issued as a NEW adjustment invoice that references the
finalized original.

Money rules:
- All amounts are Decimal.
- Subtotal = sum of meter lines + adjustment lines (adjustments negative).
- Tax is a stub: one configured rate applied to the subtotal, rounded
  HALF-UP to cents at the invoice level only (never per line).
- Total = subtotal + tax.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .clock import Clock, SystemClock
from .models import Adjustment, Invoice, InvoiceLine, UsageEvent
from .pricebook import PriceBook, default_pricebook
from .rating import prorate_allowance, rate_units
from .storage import Storage

CENTS = Decimal("0.01")
DEFAULT_TAX_RATE = Decimal("0.10")


class ImmutableInvoiceError(RuntimeError):
    pass


class InvoiceNotFoundError(KeyError):
    pass


@dataclass(frozen=True)
class PlanSegment:
    """One plan's slice of a billing period (proration building block)."""

    plan: str
    start: datetime
    end: datetime


def compute_tax(subtotal: Decimal, tax_rate: Decimal) -> Decimal:
    """The single rounding point in the system: invoice-level tax, half-up."""
    if subtotal <= 0:
        return Decimal("0.00")
    return (subtotal * tax_rate).quantize(CENTS, rounding=ROUND_HALF_UP)


def _totals(lines: list[InvoiceLine], tax_rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    subtotal = sum((line.amount for line in lines), Decimal("0"))
    tax = compute_tax(subtotal, tax_rate)
    return subtotal, tax, subtotal + tax


def _invoice_to_payload(inv: Invoice) -> dict:
    return {
        "invoice_id": inv.invoice_id,
        "tenant": inv.tenant,
        "period_start": inv.period_start.isoformat(),
        "period_end": inv.period_end.isoformat(),
        "state": inv.state,
        "pricebook_version": inv.pricebook_version,
        "lines": [{
            "line_id": ln.line_id,
            "kind": ln.kind,
            "description": ln.description,
            "amount": str(ln.amount),
            "meter": ln.meter,
            "plan": ln.plan,
            "quantity": str(ln.quantity),
            "allowance_applied": str(ln.allowance_applied),
            "billable_units": str(ln.billable_units),
            "segment_start": ln.segment_start.isoformat() if ln.segment_start else None,
            "segment_end": ln.segment_end.isoformat() if ln.segment_end else None,
            "tier_breakdown": ln.tier_breakdown,
        } for ln in inv.lines],
        "subtotal": str(inv.subtotal),
        "tax_rate": str(inv.tax_rate),
        "tax_amount": str(inv.tax_amount),
        "total": str(inv.total),
        "created_at": inv.created_at.isoformat(),
        "finalized_at": inv.finalized_at.isoformat() if inv.finalized_at else None,
        "reference_invoice_id": inv.reference_invoice_id,
    }


def _payload_to_invoice(p: dict) -> Invoice:
    lines = [InvoiceLine(
        line_id=ln["line_id"], kind=ln["kind"], description=ln["description"],
        amount=Decimal(ln["amount"]), meter=ln["meter"], plan=ln["plan"],
        quantity=Decimal(ln["quantity"]),
        allowance_applied=Decimal(ln["allowance_applied"]),
        billable_units=Decimal(ln["billable_units"]),
        segment_start=datetime.fromisoformat(ln["segment_start"]) if ln["segment_start"] else None,
        segment_end=datetime.fromisoformat(ln["segment_end"]) if ln["segment_end"] else None,
        tier_breakdown=ln["tier_breakdown"],
    ) for ln in p["lines"]]
    return Invoice(
        invoice_id=p["invoice_id"], tenant=p["tenant"],
        period_start=datetime.fromisoformat(p["period_start"]),
        period_end=datetime.fromisoformat(p["period_end"]),
        state=p["state"], pricebook_version=p["pricebook_version"],
        lines=lines, subtotal=Decimal(p["subtotal"]),
        tax_rate=Decimal(p["tax_rate"]), tax_amount=Decimal(p["tax_amount"]),
        total=Decimal(p["total"]),
        created_at=datetime.fromisoformat(p["created_at"]),
        finalized_at=datetime.fromisoformat(p["finalized_at"]) if p["finalized_at"] else None,
        reference_invoice_id=p["reference_invoice_id"],
    )


class BillingService:
    def __init__(self, storage: Storage | None = None,
                 pricebook: PriceBook | None = None,
                 clock: Clock | None = None,
                 tax_rate: Decimal = DEFAULT_TAX_RATE):
        self.storage = storage or Storage()
        self.pricebook = pricebook or default_pricebook()
        self.clock = clock or SystemClock()
        self.tax_rate = tax_rate
        self._seq = 0

    # ---- ingestion ------------------------------------------------------
    def ingest(self, event: UsageEvent) -> bool:
        """True if stored; False if absorbed as a duplicate idempotency key."""
        return self.storage.insert_event(event)

    def ingest_many(self, events: list[UsageEvent]) -> tuple[int, int]:
        """Returns (accepted, duplicates)."""
        accepted = sum(1 for e in events if self.ingest(e))
        return accepted, len(events) - accepted

    def aggregate(self, tenant: str, start: datetime, end: datetime) -> dict[str, Decimal]:
        return self.storage.aggregate(tenant, start, end)

    # ---- invoice generation ----------------------------------------------
    def _next_id(self) -> str:
        self._seq += 1
        return f"inv_{self._seq:06d}"

    def _meter_lines(self, tenant: str, segments: list[PlanSegment],
                     period_start: datetime, period_end: datetime,
                     version_id: str) -> list[InvoiceLine]:
        version = self.pricebook.version_by_id(version_id)
        period_seconds = int((period_end - period_start).total_seconds())
        lines: list[InvoiceLine] = []
        n = 0
        for seg in segments:
            plan = version.plan(seg.plan)
            seg_seconds = int((seg.end - seg.start).total_seconds())
            prorated = seg_seconds != period_seconds
            totals = self.storage.aggregate(tenant, seg.start, seg.end)
            for meter in sorted(totals):
                if meter not in plan.meters:
                    raise KeyError(f"meter {meter!r} is not priced for plan {seg.plan!r}")
                pricing = plan.meters[meter]
                allowance = (prorate_allowance(pricing.allowance, seg_seconds, period_seconds)
                             if prorated else pricing.allowance)
                result = rate_units(totals[meter], pricing, allowance_override=allowance)
                n += 1
                lines.append(InvoiceLine(
                    line_id=f"line_{n:03d}", kind="meter",
                    description=f"{meter} ({seg.plan})",
                    amount=result.amount, meter=meter, plan=seg.plan,
                    quantity=result.quantity,
                    allowance_applied=result.allowance_applied,
                    billable_units=result.billable_units,
                    segment_start=seg.start, segment_end=seg.end,
                    tier_breakdown=result.tier_breakdown,
                ))
        return lines

    def generate_invoice(self, tenant: str, period_start: datetime, period_end: datetime,
                         plan: str,
                         adjustments: list[Adjustment] | None = None) -> Invoice:
        """Single-plan invoice for a whole period (price book pinned at start)."""
        segments = [PlanSegment(plan, period_start, period_end)]
        return self._build_invoice(tenant, period_start, period_end, segments, adjustments)

    def generate_prorated_invoice(self, tenant: str, period_start: datetime,
                                  period_end: datetime, old_plan: str, new_plan: str,
                                  change_at: datetime,
                                  adjustments: list[Adjustment] | None = None) -> Invoice:
        """Plan change mid-period: the period splits at the change instant.

        Each segment is rated under its own plan, with that plan's included
        allowance prorated by the segment's share of the period
        (see rating.prorate_allowance for the rounding rule). Usage is
        split by event timestamp, so events before the change bill under
        the old plan and events at/after it under the new plan.
        """
        if not (period_start < change_at < period_end):
            raise ValueError("change_at must lie strictly inside the period")
        segments = [
            PlanSegment(old_plan, period_start, change_at),
            PlanSegment(new_plan, change_at, period_end),
        ]
        return self._build_invoice(tenant, period_start, period_end, segments, adjustments)

    def _build_invoice(self, tenant: str, period_start: datetime, period_end: datetime,
                       segments: list[PlanSegment],
                       adjustments: list[Adjustment] | None) -> Invoice:
        version = self.pricebook.version_for_period(period_start)
        lines = self._meter_lines(tenant, segments, period_start, period_end,
                                  version.version_id)
        for adj in adjustments or []:
            lines.append(InvoiceLine(
                line_id=f"line_{len(lines) + 1:03d}", kind="adjustment",
                description=adj.description, amount=adj.amount,
            ))
        subtotal, tax, total = _totals(lines, self.tax_rate)
        inv = Invoice(
            invoice_id=self._next_id(), tenant=tenant,
            period_start=period_start, period_end=period_end,
            state="draft", pricebook_version=version.version_id,
            lines=lines, subtotal=subtotal, tax_rate=self.tax_rate,
            tax_amount=tax, total=total, created_at=self.clock.now(),
        )
        self.storage.save_invoice(_invoice_to_payload(inv))
        return inv

    # ---- lifecycle ---------------------------------------------------------
    def get_invoice(self, tenant: str, invoice_id: str) -> Invoice:
        payload = self.storage.load_invoice_payload(invoice_id)
        if payload is None or payload["tenant"] != tenant:
            # Another tenant's invoice behaves exactly like a missing one:
            # existence never leaks across tenants.
            raise InvoiceNotFoundError(invoice_id)
        return _payload_to_invoice(payload)

    def list_invoices(self, tenant: str) -> list[Invoice]:
        return [self.get_invoice(tenant, iid)
                for iid in self.storage.invoice_ids_for_tenant(tenant)]

    def finalize(self, tenant: str, invoice_id: str) -> Invoice:
        inv = self.get_invoice(tenant, invoice_id)
        if inv.state == "finalized":
            return inv
        inv.state = "finalized"
        inv.finalized_at = self.clock.now()
        self.storage.save_invoice(_invoice_to_payload(inv))
        return inv

    def add_adjustment(self, tenant: str, invoice_id: str,
                       adjustment: Adjustment) -> Invoice:
        """Draft-only mutation. Finalized invoices reject every change."""
        inv = self.get_invoice(tenant, invoice_id)
        if inv.state == "finalized":
            raise ImmutableInvoiceError(
                f"invoice {invoice_id} is finalized; issue an adjustment invoice instead")
        inv.lines.append(InvoiceLine(
            line_id=f"line_{len(inv.lines) + 1:03d}", kind="adjustment",
            description=adjustment.description, amount=adjustment.amount,
        ))
        inv.subtotal, inv.tax_amount, inv.total = _totals(inv.lines, inv.tax_rate)
        self.storage.save_invoice(_invoice_to_payload(inv))
        return inv

    def rerate(self, tenant: str, invoice_id: str) -> Invoice:
        """Recompute a draft from current events; finalized invoices refuse."""
        inv = self.get_invoice(tenant, invoice_id)
        if inv.state == "finalized":
            raise ImmutableInvoiceError(
                f"invoice {invoice_id} is finalized and cannot be re-rated")
        plans = {ln.plan for ln in inv.lines if ln.kind == "meter" and ln.plan}
        if len(plans) != 1:
            raise ValueError("rerate supports single-plan draft invoices")
        fresh = self.generate_invoice(tenant, inv.period_start, inv.period_end,
                                      plans.pop())
        fresh.invoice_id = inv.invoice_id
        fresh.created_at = inv.created_at
        self.storage.save_invoice(_invoice_to_payload(fresh))
        return fresh

    def create_adjustment_invoice(self, tenant: str, original_id: str,
                                  adjustments: list[Adjustment]) -> Invoice:
        """The correction path for a finalized invoice: a new draft invoice
        carrying only the correcting lines, referencing the original."""
        original = self.get_invoice(tenant, original_id)
        lines = [InvoiceLine(
            line_id=f"line_{i + 1:03d}", kind="adjustment",
            description=adj.description, amount=adj.amount,
        ) for i, adj in enumerate(adjustments)]
        subtotal, tax, total = _totals(lines, self.tax_rate)
        inv = Invoice(
            invoice_id=self._next_id(), tenant=tenant,
            period_start=original.period_start, period_end=original.period_end,
            state="draft", pricebook_version=original.pricebook_version,
            lines=lines, subtotal=subtotal, tax_rate=self.tax_rate,
            tax_amount=tax, total=total, created_at=self.clock.now(),
            reference_invoice_id=original.invoice_id,
        )
        self.storage.save_invoice(_invoice_to_payload(inv))
        return inv

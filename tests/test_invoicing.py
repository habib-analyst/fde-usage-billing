from datetime import datetime, timezone
from decimal import Decimal

import pytest

from conftest import FEB1, JAN1, ingest_units
from fde_usage_billing import Adjustment, ImmutableInvoiceError, compute_tax

TS = datetime(2026, 1, 10, tzinfo=timezone.utc)


def _seed_two_meters(service, tenant="tenant-a"):
    ingest_units(service, tenant, "api_calls", "12000", TS, "api-1")
    ingest_units(service, tenant, "feature_units", "1100", TS, "feat-1")


def test_invoice_math_two_meters(service):
    _seed_two_meters(service)
    inv = service.generate_invoice("tenant-a", JAN1, FEB1, "starter")
    amounts = {ln.meter: ln.amount for ln in inv.lines if ln.kind == "meter"}
    assert amounts == {"api_calls": Decimal("21.00"),
                       "feature_units": Decimal("50.00")}
    assert inv.subtotal == Decimal("71.00")
    assert inv.tax_rate == Decimal("0.10")
    assert inv.tax_amount == Decimal("7.10")
    assert inv.total == Decimal("78.10")
    assert inv.state == "draft"


def test_credit_adjustment_is_a_negative_line(service):
    _seed_two_meters(service)
    inv = service.generate_invoice(
        "tenant-a", JAN1, FEB1, "starter",
        adjustments=[Adjustment("Goodwill credit", Decimal("-10.00"))])
    adj = [ln for ln in inv.lines if ln.kind == "adjustment"]
    assert len(adj) == 1 and adj[0].amount == Decimal("-10.00")
    assert inv.subtotal == Decimal("61.00")
    assert inv.tax_amount == Decimal("6.10")
    assert inv.total == Decimal("67.10")


def test_tax_rounding_half_up_at_invoice_level(service):
    # Enterprise api_calls: allowance 20000, flat 0.001/unit.
    # 30050 units -> 10050 billable -> subtotal exactly 10.05.
    # Tax 10% = 1.005 -> HALF-UP to 1.01 (banker's rounding would give 1.00).
    ingest_units(service, "tenant-a", "api_calls", "30050", TS, "ent-1")
    inv = service.generate_invoice("tenant-a", JAN1, FEB1, "enterprise")
    assert inv.subtotal == Decimal("10.05")
    assert inv.tax_amount == Decimal("1.01")
    assert inv.total == Decimal("11.06")
    assert compute_tax(Decimal("23.25"), Decimal("0.10")) == Decimal("2.33")
    assert compute_tax(Decimal("10.04"), Decimal("0.10")) == Decimal("1.00")


def test_credit_only_invoice_has_no_tax(service):
    inv = service.generate_invoice(
        "tenant-a", JAN1, FEB1, "starter",
        adjustments=[Adjustment("Credit", Decimal("-5.00"))])
    assert inv.subtotal == Decimal("-5.00")
    assert inv.tax_amount == Decimal("0.00")
    assert inv.total == Decimal("-5.00")


def test_finalize_transitions_and_is_idempotent(service):
    _seed_two_meters(service)
    inv = service.generate_invoice("tenant-a", JAN1, FEB1, "starter")
    done = service.finalize("tenant-a", inv.invoice_id)
    assert done.state == "finalized"
    assert done.finalized_at is not None
    again = service.finalize("tenant-a", inv.invoice_id)
    assert again.state == "finalized"
    assert again.total == Decimal("78.10")


def test_finalized_invoice_is_immutable(service):
    _seed_two_meters(service)
    inv = service.generate_invoice("tenant-a", JAN1, FEB1, "starter")
    service.finalize("tenant-a", inv.invoice_id)
    # New usage arrives and a re-rate / adjustment is attempted: both refuse.
    ingest_units(service, "tenant-a", "api_calls", "50000", TS, "late-1")
    with pytest.raises(ImmutableInvoiceError):
        service.rerate("tenant-a", inv.invoice_id)
    with pytest.raises(ImmutableInvoiceError):
        service.add_adjustment("tenant-a", inv.invoice_id,
                               Adjustment("Late credit", Decimal("-1.00")))
    reloaded = service.get_invoice("tenant-a", inv.invoice_id)
    assert reloaded.subtotal == Decimal("71.00")
    assert reloaded.tax_amount == Decimal("7.10")
    assert reloaded.total == Decimal("78.10")
    assert len(reloaded.lines) == 2


def test_draft_rerate_picks_up_new_usage(service):
    ingest_units(service, "tenant-a", "api_calls", "2000", TS, "e1")
    inv = service.generate_invoice("tenant-a", JAN1, FEB1, "starter")
    assert inv.subtotal == Decimal("2.00")
    ingest_units(service, "tenant-a", "api_calls", "1000", TS, "e2")
    fresh = service.rerate("tenant-a", inv.invoice_id)
    assert fresh.invoice_id == inv.invoice_id
    assert fresh.subtotal == Decimal("4.00")  # 2000 billable @ 0.002


def test_corrections_go_through_a_new_adjustment_invoice(service):
    _seed_two_meters(service)
    inv = service.generate_invoice("tenant-a", JAN1, FEB1, "starter")
    service.finalize("tenant-a", inv.invoice_id)
    correction = service.create_adjustment_invoice(
        "tenant-a", inv.invoice_id,
        [Adjustment("Correction: overbilled feature_units", Decimal("-5.00"))])
    assert correction.state == "draft"
    assert correction.reference_invoice_id == inv.invoice_id
    assert correction.subtotal == Decimal("-5.00")
    assert correction.total == Decimal("-5.00")
    original = service.get_invoice("tenant-a", inv.invoice_id)
    assert original.total == Decimal("78.10")  # untouched

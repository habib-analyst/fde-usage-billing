from datetime import datetime, timezone
from decimal import Decimal

import pytest

from conftest import JAN1, JAN16, JAN31, ingest_units
from fde_usage_billing import prorate_allowance

DAY = 86400


def test_prorate_allowance_half_period():
    assert prorate_allowance(Decimal("1000"), 15 * DAY, 30 * DAY) == Decimal("500")
    assert prorate_allowance(Decimal("5000"), 15 * DAY, 30 * DAY) == Decimal("2500")


def test_prorate_allowance_rounds_down_to_whole_units():
    # 1000 * 10/30 = 333.33... -> floor to 333, never more than the share.
    assert prorate_allowance(Decimal("1000"), 10 * DAY, 30 * DAY) == Decimal("333")


def test_prorate_allowance_full_and_zero_segments():
    assert prorate_allowance(Decimal("1000"), 30 * DAY, 30 * DAY) == Decimal("1000")
    assert prorate_allowance(Decimal("1000"), 0, 30 * DAY) == Decimal("0")


def test_mid_period_upgrade_pinned_numbers(service):
    # Period Jan 1 -> Jan 31 (30 days, price book v1). Upgrade
    # starter -> growth at Jan 16 00:00, exactly halfway.
    # 8000 api_calls in each half.
    before = datetime(2026, 1, 10, tzinfo=timezone.utc)
    after = datetime(2026, 1, 20, tzinfo=timezone.utc)
    ingest_units(service, "tenant-a", "api_calls", "8000", before, "pre")
    ingest_units(service, "tenant-a", "api_calls", "8000", after, "post")

    inv = service.generate_prorated_invoice(
        "tenant-a", JAN1, JAN31, "starter", "growth", JAN16)
    assert inv.pricebook_version == "v1"

    assert len(inv.lines) == 2
    first, second = inv.lines
    # Segment 1 (starter): allowance 1000 prorated 15/30 -> 500.
    # Billable 7500, all in tier 1 @ 0.002 -> 15.00.
    assert first.plan == "starter"
    assert first.quantity == Decimal("8000")
    assert first.allowance_applied == Decimal("500")
    assert first.billable_units == Decimal("7500")
    assert first.amount == Decimal("15.00")
    # Segment 2 (growth): allowance 5000 prorated 15/30 -> 2500.
    # Billable 5500 @ 0.0015 -> 8.25.
    assert second.plan == "growth"
    assert second.quantity == Decimal("8000")
    assert second.allowance_applied == Decimal("2500")
    assert second.billable_units == Decimal("5500")
    assert second.amount == Decimal("8.25")
    # Invoice: subtotal 23.25, tax 10% = 2.325 -> half-up 2.33, total 25.58.
    assert inv.subtotal == Decimal("23.25")
    assert inv.tax_amount == Decimal("2.33")
    assert inv.total == Decimal("25.58")


def test_change_at_must_be_inside_period(service):
    with pytest.raises(ValueError):
        service.generate_prorated_invoice(
            "tenant-a", JAN1, JAN31, "starter", "growth", JAN1)
    with pytest.raises(ValueError):
        service.generate_prorated_invoice(
            "tenant-a", JAN1, JAN31, "starter", "growth", JAN31)

from datetime import datetime, timezone
from decimal import Decimal

from conftest import JAN1, ingest_units

JAN15 = datetime(2026, 1, 15, tzinfo=timezone.utc)
FEB15 = datetime(2026, 2, 15, tzinfo=timezone.utc)
MAR1 = datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_version_effective_at_boundaries(service):
    book = service.pricebook
    assert book.version_effective_at(JAN1).version_id == "v1"
    assert book.version_effective_at(JAN15).version_id == "v1"
    feb1 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert book.version_effective_at(feb1).version_id == "v2"


def test_period_starting_before_v2_uses_v1_even_when_v2_lands_mid_period(service):
    # Period Jan 15 -> Feb 15. v2 takes effect Feb 1, mid-period, but the
    # pinned rule rates the whole period at the version effective at its
    # start: v1. 2000 api_calls on starter: 1000 billable @ 0.002 = 2.00.
    ts = datetime(2026, 1, 20, tzinfo=timezone.utc)
    ingest_units(service, "tenant-a", "api_calls", "2000", ts, "e1")
    inv = service.generate_invoice("tenant-a", JAN15, FEB15, "starter")
    assert inv.pricebook_version == "v1"
    assert inv.subtotal == Decimal("2.00")


def test_period_starting_at_v2_uses_v2_rates(service):
    # Same usage in the Feb period: v2 starter rate 0.003 -> 3.00.
    feb1 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    ts = datetime(2026, 2, 10, tzinfo=timezone.utc)
    ingest_units(service, "tenant-a", "api_calls", "2000", ts, "e1")
    inv = service.generate_invoice("tenant-a", feb1, MAR1, "starter")
    assert inv.pricebook_version == "v2"
    assert inv.subtotal == Decimal("3.00")

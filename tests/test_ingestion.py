from datetime import datetime, timezone
from decimal import Decimal

from conftest import JAN1, FEB1, ingest_units


def test_volume_dedupe_1000_events_10_percent_duplicated(service):
    # 1,000 ingest calls: 900 unique keys, then 100 repeats of the first
    # 100 keys. Every event carries 2 api_calls units.
    events_ts = datetime(2026, 1, 10, tzinfo=timezone.utc)
    accepted = duplicates = 0
    for i in range(1000):
        key = f"evt-{i}" if i < 900 else f"evt-{i - 900}"
        if ingest_units(service, "tenant-a", "api_calls", "2", events_ts, key):
            accepted += 1
        else:
            duplicates += 1
    assert accepted == 900
    assert duplicates == 100
    assert service.storage.event_count("tenant-a") == 900
    totals = service.aggregate("tenant-a", JAN1, FEB1)
    assert totals == {"api_calls": Decimal("1800")}


def test_ingest_many_reports_accepted_and_duplicates(service):
    ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    from fde_usage_billing import UsageEvent
    events = [UsageEvent("tenant-a", "api_calls", Decimal("1"), ts, f"k-{i}")
              for i in range(10)]
    events += events[:3]  # 3 duplicated keys
    accepted, duplicates = service.ingest_many(events)
    assert (accepted, duplicates) == (10, 3)


def test_duplicate_key_absorbs_even_with_different_units(service):
    ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert ingest_units(service, "tenant-a", "api_calls", "5", ts, "same-key")
    # Retry with a different payload under the same key: absorbed, not added.
    assert not ingest_units(service, "tenant-a", "api_calls", "999", ts, "same-key")
    assert service.aggregate("tenant-a", JAN1, FEB1)["api_calls"] == Decimal("5")


def test_events_outside_period_are_not_aggregated(service):
    inside = datetime(2026, 1, 15, tzinfo=timezone.utc)
    outside = datetime(2026, 2, 15, tzinfo=timezone.utc)
    ingest_units(service, "tenant-a", "api_calls", "10", inside, "in")
    ingest_units(service, "tenant-a", "api_calls", "70", outside, "out")
    assert service.aggregate("tenant-a", JAN1, FEB1)["api_calls"] == Decimal("10")

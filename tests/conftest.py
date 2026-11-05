from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fde_usage_billing import BillingService, FixedClock, UsageEvent

JAN1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
JAN16 = datetime(2026, 1, 16, tzinfo=timezone.utc)
JAN31 = datetime(2026, 1, 31, tzinfo=timezone.utc)
FEB1 = datetime(2026, 2, 1, tzinfo=timezone.utc)


@pytest.fixture
def service():
    return BillingService(clock=FixedClock(JAN1))


def ingest_units(svc, tenant, meter, units, ts, key):
    return svc.ingest(UsageEvent(
        tenant=tenant, meter=meter, units=Decimal(units),
        ts=ts, idempotency_key=key))

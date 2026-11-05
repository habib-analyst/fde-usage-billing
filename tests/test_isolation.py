from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from conftest import FEB1, JAN1, ingest_units
from fde_usage_billing.invoicing import InvoiceNotFoundError

TS = datetime(2026, 1, 10, tzinfo=timezone.utc)


def test_same_idempotency_key_is_scoped_per_tenant(service):
    assert ingest_units(service, "tenant-a", "api_calls", "10", TS, "shared-key")
    assert ingest_units(service, "tenant-b", "api_calls", "30", TS, "shared-key")
    assert service.aggregate("tenant-a", JAN1, FEB1)["api_calls"] == Decimal("10")
    assert service.aggregate("tenant-b", JAN1, FEB1)["api_calls"] == Decimal("30")


def test_invoices_do_not_leak_across_tenants(service):
    ingest_units(service, "tenant-a", "api_calls", "12000", TS, "a1")
    ingest_units(service, "tenant-b", "api_calls", "2000", TS, "b1")
    inv_a = service.generate_invoice("tenant-a", JAN1, FEB1, "starter")
    inv_b = service.generate_invoice("tenant-b", JAN1, FEB1, "starter")
    assert inv_a.subtotal == Decimal("21.00")
    assert inv_b.subtotal == Decimal("2.00")
    assert [i.invoice_id for i in service.list_invoices("tenant-a")] == [inv_a.invoice_id]
    assert [i.invoice_id for i in service.list_invoices("tenant-b")] == [inv_b.invoice_id]
    with pytest.raises(InvoiceNotFoundError):
        service.get_invoice("tenant-b", inv_a.invoice_id)
    with pytest.raises(InvoiceNotFoundError):
        service.finalize("tenant-b", inv_a.invoice_id)


def test_api_tenant_isolation_and_auth(service):
    from fde_usage_billing.app import create_app
    from fde_usage_billing import BillingService
    client = TestClient(create_app(BillingService()))
    ha = {"X-Tenant-Id": "tenant-a", "X-Api-Key": "fixture-key-tenant-a"}
    hb = {"X-Tenant-Id": "tenant-b", "X-Api-Key": "fixture-key-tenant-b"}

    r = client.post("/usage-events", headers=ha, json={
        "meter": "api_calls", "units": "12000",
        "ts": "2026-01-10T00:00:00Z", "idempotency_key": "api-1"})
    assert r.status_code == 200 and r.json()["stored"] is True
    # Same key replayed for tenant A: absorbed. For tenant B: its own event.
    r = client.post("/usage-events", headers=ha, json={
        "meter": "api_calls", "units": "12000",
        "ts": "2026-01-10T00:00:00Z", "idempotency_key": "api-1"})
    assert r.json()["duplicate"] is True

    r = client.post("/invoices", headers=ha, json={
        "period_start": "2026-01-01T00:00:00Z",
        "period_end": "2026-02-01T00:00:00Z", "plan": "starter"})
    assert r.status_code == 200
    inv_id = r.json()["invoice_id"]
    assert Decimal(r.json()["total"]) == Decimal("23.10")  # 21.00 + 2.10 tax

    # Tenant B cannot see tenant A's invoice (404, existence does not leak).
    r = client.get(f"/invoices/{inv_id}", headers=hb)
    assert r.status_code == 404
    # Wrong fixture key for the tenant: rejected.
    r = client.get(f"/invoices/{inv_id}", headers={
        "X-Tenant-Id": "tenant-a", "X-Api-Key": "fixture-key-tenant-b"})
    assert r.status_code == 401
    # Tenant B's aggregate is empty.
    r = client.get("/usage/aggregate", headers=hb, params={
        "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"})
    assert r.json() == {}

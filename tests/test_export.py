from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from conftest import FEB1, JAN1, ingest_units
from fde_usage_billing import (StripeFixtureExporter, create_fixture_app,
                               invoice_item_payload, meter_event_payload)

TS = datetime(2026, 1, 10, tzinfo=timezone.utc)


def _finalized_invoice(service, tenant="tenant-a"):
    ingest_units(service, tenant, "api_calls", "12000", TS, "api-1")
    ingest_units(service, tenant, "feature_units", "1100", TS, "feat-1")
    inv = service.generate_invoice(tenant, JAN1, FEB1, "starter")
    return service.finalize(tenant, inv.invoice_id)


def _exporter(service):
    fixture = create_fixture_app()
    client = TestClient(fixture)
    return fixture, client, StripeFixtureExporter(service.storage, client)


def test_payload_shapes(service):
    inv = _finalized_invoice(service)
    lines = {ln.meter: ln for ln in inv.lines}
    me = meter_event_payload(inv, lines["api_calls"])
    assert me["event_name"] == "api_calls"
    assert me["payload"]["value"] == "11000"  # billable units, as string
    assert me["payload"]["stripe_customer_id"] == "cus_test_tenant-a"
    assert me["identifier"].endswith(":meter_event")

    ii = invoice_item_payload(inv, lines["api_calls"])
    assert ii["customer"] == "cus_test_tenant-a"
    assert ii["amount"] == 2100  # 21.00 -> integer cents
    assert ii["currency"] == "usd"
    ii_feature = invoice_item_payload(inv, lines["feature_units"])
    assert ii_feature["amount"] == 5000


def test_export_posts_each_line_once(service):
    inv = _finalized_invoice(service)
    fixture, client, exporter = _exporter(service)
    result = exporter.export_invoice(inv)
    assert result.complete and result.sent == 4 and result.failed == 0
    stats = client.get("/v1/fixture/stats").json()
    assert stats["meter_events_created"] == 2
    assert stats["invoice_items_created"] == 2
    assert stats["request_counts"] == {"meter_events": 2, "invoice_items": 2}
    assert stats["livemode"] is False
    records = exporter.export_status(inv.invoice_id)
    assert len(records) == 4
    assert all(r["status"] == "sent" for r in records)
    ids = {r["response_id"] for r in records}
    assert any(i.startswith("bme_test_") for i in ids)
    assert any(i.startswith("ii_test_") for i in ids)


def test_reexport_sends_nothing_new(service):
    inv = _finalized_invoice(service)
    fixture, client, exporter = _exporter(service)
    exporter.export_invoice(inv)
    before = client.get("/v1/fixture/stats").json()
    second = exporter.export_invoice(inv)
    assert second.sent == 0 and second.skipped == 4 and second.failed == 0
    after = client.get("/v1/fixture/stats").json()
    assert after == before  # zero new requests, zero new objects


def test_fixture_dedupes_by_idempotency_key_directly(service):
    fixture = create_fixture_app()
    client = TestClient(fixture)
    payload = {"event_name": "api_calls",
               "payload": {"value": "5", "stripe_customer_id": "cus_test_x"},
               "identifier": "inv_x:line_001:meter_event"}
    headers = {"Idempotency-Key": "inv_x:line_001:meter_event"}
    r1 = client.post("/v1/billing/meter_events", json=payload, headers=headers)
    r2 = client.post("/v1/billing/meter_events", json=payload, headers=headers)
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["livemode"] is False
    stats = client.get("/v1/fixture/stats").json()
    assert stats["meter_events_created"] == 1      # deduped server-side
    assert stats["request_counts"]["meter_events"] == 2


def test_failure_leaves_retryable_state_and_retry_never_double_posts(service):
    inv = _finalized_invoice(service)
    fixture, client, exporter = _exporter(service)
    client.post("/v1/fixture/fail_next", json={"count": 1})
    first = exporter.export_invoice(inv)
    assert first.failed == 1 and first.sent == 3
    records = {r["idempotency_key"]: r
               for r in exporter.export_status(inv.invoice_id)}
    failed = [r for r in records.values() if r["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["attempts"] == 1

    retry = exporter.export_invoice(inv)
    assert retry.failed == 0 and retry.sent == 1 and retry.skipped == 3
    stats = client.get("/v1/fixture/stats").json()
    # Exactly one object per item despite the failed attempt + retry.
    assert stats["meter_events_created"] == 2
    assert stats["invoice_items_created"] == 2
    records = exporter.export_status(inv.invoice_id)
    assert all(r["status"] == "sent" for r in records)
    retried = [r for r in records if r["attempts"] == 2]
    assert len(retried) == 1


def test_draft_invoice_cannot_be_exported(service):
    ingest_units(service, "tenant-a", "api_calls", "12000", TS, "api-1")
    draft = service.generate_invoice("tenant-a", JAN1, FEB1, "starter")
    _, _, exporter = _exporter(service)
    with pytest.raises(ValueError):
        exporter.export_invoice(draft)


def test_fixture_rejects_non_fixture_keys(service):
    fixture = create_fixture_app()
    client = TestClient(fixture)
    r = client.post("/v1/billing/meter_events",
                    json={"event_name": "api_calls", "payload": {}},
                    headers={"Authorization": "Bearer sk_live_not_a_fixture"})
    assert r.status_code == 401

"""Stripe-shaped export adapter.

For a FINALIZED invoice, builds the payload shapes a Stripe usage-based
billing integration would send — a billing meter event and an invoice
item per meter line — and POSTs them to the local fixture server that
mimics the Stripe API surface in test mode. Nothing here can reach the
real Stripe API: the client is injected, the base "URL" is the fixture
app, and the only credential is the synthetic fixture key.

Idempotency, two layers:
1. Local export ledger (SQLite): a line already recorded as sent is
   never POSTed again — re-exporting a fully exported invoice sends
   zero requests.
2. Every POST carries an Idempotency-Key, and the fixture server
   dedupes on it, so even a crash between POST and ledger write cannot
   create a duplicate object server-side.

Failure handling: a 5xx from the fixture marks that item `failed`
(retryable). The export returns a partial result; a later retry sends
only the items that are not yet `sent`. Nothing is ever marked sent
without a 2xx response.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .fixture_stripe import FIXTURE_API_KEY
from .models import Invoice
from .storage import Storage

CENTS = Decimal("1")


@dataclass
class ExportResult:
    invoice_id: str
    sent: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def complete(self) -> bool:
        return self.failed == 0


def meter_event_payload(invoice: Invoice, line) -> dict:
    """2024-style billing meter event shape."""
    return {
        "event_name": line.meter,
        "payload": {
            "value": str(line.billable_units),
            "stripe_customer_id": f"cus_test_{invoice.tenant}",
        },
        "timestamp": invoice.period_end.isoformat(),
        "identifier": f"{invoice.invoice_id}:{line.line_id}:meter_event",
    }


def invoice_item_payload(invoice: Invoice, line) -> dict:
    """Invoice item shape; amount is integer cents, rounded half-up once."""
    amount_cents = int((line.amount * 100).quantize(CENTS, rounding=ROUND_HALF_UP))
    return {
        "customer": f"cus_test_{invoice.tenant}",
        "amount": amount_cents,
        "currency": "usd",
        "description": line.description,
        "invoice": f"in_test_{invoice.invoice_id}",
        "metadata": {
            "meter": line.meter or "",
            "plan": line.plan or "",
            "billable_units": str(line.billable_units),
        },
    }


class StripeFixtureExporter:
    """`client` is any object with .post(path, json=..., headers=...)
    returning a response with .status_code and .json() — in practice a
    fastapi TestClient bound to the fixture app."""

    def __init__(self, storage: Storage, client, api_key: str = FIXTURE_API_KEY):
        self.storage = storage
        self.client = client
        self.api_key = api_key

    def _headers(self, idempotency_key: str) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }

    def export_invoice(self, invoice: Invoice) -> ExportResult:
        if invoice.state != "finalized":
            raise ValueError("only finalized invoices can be exported")
        result = ExportResult(invoice_id=invoice.invoice_id)
        jobs: list[tuple[str, str, dict]] = []  # (kind, key, payload)
        for line in invoice.lines:
            if line.kind != "meter":
                continue
            jobs.append(("meter_event",
                         f"{invoice.invoice_id}:{line.line_id}:meter_event",
                         meter_event_payload(invoice, line)))
            jobs.append(("invoice_item",
                         f"{invoice.invoice_id}:{line.line_id}:invoice_item",
                         invoice_item_payload(invoice, line)))
        paths = {"meter_event": "/v1/billing/meter_events",
                 "invoice_item": "/v1/invoiceitems"}
        for kind, key, payload in jobs:
            existing = self.storage.get_export(key)
            if existing and existing["status"] == "sent":
                result.skipped += 1
                continue
            response = self.client.post(paths[kind], json=payload,
                                        headers=self._headers(key))
            if 200 <= response.status_code < 300:
                body = response.json()
                self.storage.record_export_attempt(
                    key, invoice.invoice_id, kind, "sent", body.get("id"))
                result.sent += 1
            else:
                self.storage.record_export_attempt(
                    key, invoice.invoice_id, kind, "failed", None)
                result.failed += 1
        return result

    def export_status(self, invoice_id: str) -> list[dict]:
        return self.storage.exports_for_invoice(invoice_id)

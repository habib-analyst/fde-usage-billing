"""Billing service API (FastAPI).

Fixture-grade auth: callers present the tenant id and the synthetic
fixture key for that tenant (`fixture-key-<tenant>`). The key decides
identity; a key for another tenant is rejected. This mirrors the shape
of API-key tenant auth without any real credential anywhere.

Run: PYTHONPATH=src uvicorn fde_usage_billing.app:app --port 8001
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import Body, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .invoicing import BillingService, InvoiceNotFoundError
from .models import Adjustment, UsageEvent

service = BillingService()


class EventIn(BaseModel):
    meter: str
    units: str
    ts: datetime
    idempotency_key: str


class InvoiceIn(BaseModel):
    period_start: datetime
    period_end: datetime
    plan: str
    adjustments: list[dict] = []


def create_app(svc: BillingService | None = None) -> FastAPI:
    svc = svc or service
    app = FastAPI(title="fde-usage-billing")

    def tenant_for(x_tenant_id: str, x_api_key: str) -> str:
        if x_api_key != f"fixture-key-{x_tenant_id}":
            raise HTTPException(status_code=401, detail="invalid fixture key for tenant")
        return x_tenant_id

    def invoice_out(inv) -> dict:
        return {
            "invoice_id": inv.invoice_id, "tenant": inv.tenant,
            "state": inv.state, "pricebook_version": inv.pricebook_version,
            "period_start": inv.period_start.isoformat(),
            "period_end": inv.period_end.isoformat(),
            "lines": [{
                "line_id": ln.line_id, "kind": ln.kind,
                "description": ln.description, "amount": str(ln.amount),
                "meter": ln.meter, "plan": ln.plan,
                "quantity": str(ln.quantity),
                "allowance_applied": str(ln.allowance_applied),
                "billable_units": str(ln.billable_units),
            } for ln in inv.lines],
            "subtotal": str(inv.subtotal), "tax_rate": str(inv.tax_rate),
            "tax_amount": str(inv.tax_amount), "total": str(inv.total),
            "reference_invoice_id": inv.reference_invoice_id,
        }

    @app.post("/usage-events")
    def ingest(event: EventIn = Body(...), x_tenant_id: str = Header(...),
               x_api_key: str = Header(...)):
        tenant = tenant_for(x_tenant_id, x_api_key)
        stored = svc.ingest(UsageEvent(
            tenant=tenant, meter=event.meter, units=Decimal(event.units),
            ts=event.ts, idempotency_key=event.idempotency_key))
        return {"stored": stored, "duplicate": not stored}

    @app.get("/usage/aggregate")
    def aggregate(start: datetime, end: datetime,
                  x_tenant_id: str = Header(...), x_api_key: str = Header(...)):
        tenant = tenant_for(x_tenant_id, x_api_key)
        totals = svc.aggregate(tenant, start, end)
        return {meter: str(units) for meter, units in totals.items()}

    @app.post("/invoices")
    def create_invoice(body: InvoiceIn = Body(...),
                       x_tenant_id: str = Header(...),
                       x_api_key: str = Header(...)):
        tenant = tenant_for(x_tenant_id, x_api_key)
        adjustments = [Adjustment(a["description"], Decimal(a["amount"]))
                       for a in body.adjustments]
        inv = svc.generate_invoice(tenant, body.period_start, body.period_end,
                                   body.plan, adjustments)
        return invoice_out(inv)

    @app.get("/invoices/{invoice_id}")
    def get_invoice(invoice_id: str, x_tenant_id: str = Header(...),
                    x_api_key: str = Header(...)):
        tenant = tenant_for(x_tenant_id, x_api_key)
        try:
            return invoice_out(svc.get_invoice(tenant, invoice_id))
        except InvoiceNotFoundError:
            raise HTTPException(status_code=404, detail="invoice not found") from None

    @app.post("/invoices/{invoice_id}/finalize")
    def finalize(invoice_id: str, x_tenant_id: str = Header(...),
                 x_api_key: str = Header(...)):
        tenant = tenant_for(x_tenant_id, x_api_key)
        try:
            return invoice_out(svc.finalize(tenant, invoice_id))
        except InvoiceNotFoundError:
            raise HTTPException(status_code=404, detail="invoice not found") from None

    return app


app = create_app()

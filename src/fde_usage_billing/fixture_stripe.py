"""Local fixture server that mimics a Stripe-shaped API in test mode.

This is NOT Stripe and never talks to Stripe. It implements the small
2024-style surface the export adapter uses:

    POST /v1/billing/meter_events   (usage records)
    POST /v1/invoiceitems           (invoice line items)
    GET  /v1/fixture/stats          (fixture introspection for tests)
    POST /v1/fixture/fail_next      (failure injection: {"count": n})

Behaviour mirrored from the real API's test mode:
- Responses carry "livemode": false and test-style ids (bme_test_..., ii_test_...).
- Idempotency: a repeated Idempotency-Key returns the originally created
  object and does NOT create a second one.
- Failure injection returns a Stripe-shaped 500 error body.

The only key string anywhere near this module is the clearly synthetic
fixture key below, accepted (and ignored beyond a prefix check) so client
code exercises the same header path it would against a real test mode.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

FIXTURE_API_KEY = "sk_test_fixture_000000000000000000000000"


def create_fixture_app() -> FastAPI:
    app = FastAPI(title="Fixture Stripe-shaped API (test mode, local only)")
    app.state.meter_events = {}       # idempotency_key -> response body
    app.state.invoice_items = {}      # idempotency_key -> response body
    app.state.request_counts = {"meter_events": 0, "invoice_items": 0}
    app.state.fail_next = 0
    app.state.seq = 0

    def _maybe_fail() -> JSONResponse | None:
        if app.state.fail_next > 0:
            app.state.fail_next -= 1
            return JSONResponse(status_code=500, content={
                "error": {
                    "type": "api_error",
                    "code": "fixture_injected_failure",
                    "message": "Fixture server injected failure (test mode).",
                }
            })
        return None

    def _check_key(authorization: str | None) -> JSONResponse | None:
        if authorization is not None and not authorization.startswith(
                "Bearer sk_test_fixture"):
            return JSONResponse(status_code=401, content={
                "error": {
                    "type": "authentication_error",
                    "message": "Fixture server accepts only the synthetic fixture key.",
                }
            })
        return None

    @app.post("/v1/billing/meter_events")
    async def create_meter_event(request: Request,
                                 idempotency_key: str | None = Header(default=None),
                                 authorization: str | None = Header(default=None)):
        app.state.request_counts["meter_events"] += 1
        if (resp := _check_key(authorization)) is not None:
            return resp
        if (resp := _maybe_fail()) is not None:
            return resp
        body = await request.json()
        key = idempotency_key or body.get("identifier")
        if key in app.state.meter_events:
            return app.state.meter_events[key]
        app.state.seq += 1
        created = {
            "id": f"bme_test_{app.state.seq:06d}",
            "object": "billing.meter_event",
            "livemode": False,
            "event_name": body.get("event_name"),
            "payload": body.get("payload", {}),
            "timestamp": body.get("timestamp"),
            "identifier": body.get("identifier"),
        }
        app.state.meter_events[key] = created
        return created

    @app.post("/v1/invoiceitems")
    async def create_invoice_item(request: Request,
                                  idempotency_key: str | None = Header(default=None),
                                  authorization: str | None = Header(default=None)):
        app.state.request_counts["invoice_items"] += 1
        if (resp := _check_key(authorization)) is not None:
            return resp
        if (resp := _maybe_fail()) is not None:
            return resp
        body = await request.json()
        key = idempotency_key
        if key in app.state.invoice_items:
            return app.state.invoice_items[key]
        app.state.seq += 1
        created = {
            "id": f"ii_test_{app.state.seq:06d}",
            "object": "invoiceitem",
            "livemode": False,
            "customer": body.get("customer"),
            "amount": body.get("amount"),
            "currency": body.get("currency"),
            "description": body.get("description"),
            "invoice": body.get("invoice"),
            "metadata": body.get("metadata", {}),
        }
        app.state.invoice_items[key] = created
        return created

    @app.get("/v1/fixture/stats")
    def stats():
        return {
            "meter_events_created": len(app.state.meter_events),
            "invoice_items_created": len(app.state.invoice_items),
            "request_counts": dict(app.state.request_counts),
            "livemode": False,
        }

    @app.post("/v1/fixture/fail_next")
    async def fail_next(request: Request):
        body = await request.json()
        app.state.fail_next = int(body.get("count", 0))
        return {"fail_next": app.state.fail_next}

    return app


# Uvicorn entry point: uvicorn fde_usage_billing.fixture_stripe:app
app = create_fixture_app()

# fde-usage-billing

A usage metering and billing engine for a multi-tenant SaaS: idempotent
usage ingestion, a versioned price book, period aggregation, rating,
Decimal-exact invoice generation, mid-period proration, and a
Stripe-shaped export adapter that runs against a local fixture server in
test mode.

**How this differs from fde-multitenant-saas (P1).** P1 is the tenant
platform: tenant isolation, analytics, and a billing *view* that prices
metered events with a simple rate card. This project is the billing
*engine itself*, built as its own system: a versioned price book with
per-plan allowances and tiers, idempotent ingestion that stays exact at
volume, invoice lifecycle rules (draft, finalized, corrections by
adjustment invoice), proration for mid-period plan changes, and an
export layer that speaks the payload shapes of a usage-based billing
integration. The two share no code.

## What it demonstrates

- **Idempotent ingestion at volume.** Every usage event carries an
  idempotency key, enforced by a `UNIQUE (tenant, idempotency_key)`
  constraint in SQLite, so a duplicate key is absorbed by the database,
  not by a racy read-then-write check. Pinned by test: 1,000 ingest
  calls with 10% duplicated keys store exactly 900 events and aggregate
  to exactly 1,800 units.
- **A versioned price book.** Plans (starter / growth / enterprise
  fixtures) price each meter with an included allowance and tiered
  per-unit rates. Rating a period uses the price-book version effective
  at the **period start**; a version published mid-period never
  re-prices a period already begun. That rule is pinned by tests
  (a Jan 15 to Feb 15 period rates at v1 even though v2 lands Feb 1).
- **Decimal-exact invoicing.** All money and units are `Decimal` end to
  end; floats never enter the pipeline. Tax is a stub: one configured
  rate applied to the subtotal and rounded HALF-UP to cents at the
  invoice level only, never per line.
- **Invoice lifecycle with immutability.** Invoices move draft to
  finalized. A finalized invoice rejects re-rating and new lines, and
  later usage never changes it; corrections are issued as a new
  adjustment invoice that references the original.
- **Proration for mid-period plan changes.** The period splits at the
  change instant, usage is split by event timestamp, each segment rates
  under its own plan, and allowances are prorated by elapsed fraction,
  rounded down to whole units (proration never grants more than the
  proportional share).
- **A Stripe-shaped export that cannot touch Stripe.** For a finalized
  invoice the adapter builds billing meter event and invoice item
  payloads and posts them to a local FastAPI fixture that mimics the
  2024-style Stripe API surface in test mode (`livemode: false`,
  `bme_test_...` / `ii_test_...` ids). Export is idempotent at two
  layers, and a fixture 500 leaves the export retryable, never
  double-posted.

## Architecture

```
 usage events ──► Ingestion (SQLite, UNIQUE tenant+idempotency_key)
                       │
                       ▼
                 Aggregation (per tenant, per period, per meter)
                       │
        Price book ───► Rating (allowance first, tiers after, Decimal)
        (versioned,           │
         pinned at            ▼
         period start)   Invoice (draft ──► finalized, immutable)
                              │  corrections = new adjustment invoice
                              ▼
                 Export adapter ──► Fixture "Stripe" server (local,
                 meter events +     test mode only, dedupes by
                 invoice items,     Idempotency-Key, failure
                 idempotency keys)  injection for retry tests)
```

An injected clock supplies period-boundary and finalization times, so
tests are fully deterministic.

## The price book (v1 fixtures)

| Plan | Meter | Allowance / period | Tiers (billable units) |
|---|---|---|---|
| starter | api_calls | 1,000 | 10,000 @ $0.002, then $0.001 |
| starter | feature_units | 100 | 1,000 @ $0.05, then $0.03 |
| growth | api_calls | 5,000 | 50,000 @ $0.0015, then $0.0008 |
| growth | feature_units | 500 | 5,000 @ $0.04, then $0.02 |
| enterprise | api_calls | 20,000 | flat $0.001 |
| enterprise | feature_units | 2,000 | flat $0.025 |

Version v2 (effective 2026-02-01 in the fixtures) raises the starter
api_calls rates to $0.003 / $0.0015 and carries everything else over.
Rating example, starter api_calls at 12,000 units: 1,000 allowance,
11,000 billable = 10,000 x $0.002 + 1,000 x $0.001 = **$21.00**.

## Invoicing rules

- One line item per meter (per plan segment when prorated), showing
  quantity, allowance applied, billable units, tier breakdown, amount.
- Credits and adjustments are lines with negative amounts.
- Subtotal = meter lines + adjustment lines.
- Tax = subtotal x configured rate (10% in the fixtures), rounded
  HALF-UP to cents, computed once at the invoice level. Credit-only
  invoices (subtotal <= 0) carry no tax.
- Worked example (starter, January, price book v1): 12,000 api_calls
  ($21.00) + 1,100 feature_units ($50.00) = subtotal $71.00, tax $7.10,
  **total $78.10**. With a $10.00 goodwill credit line: subtotal $61.00,
  tax $6.10, total $67.10.
- Rounding proof: an enterprise invoice with subtotal exactly $10.05
  has tax $1.01 (1.005 rounds half-up; banker's rounding would give
  $1.00) and total $11.06.

## Proration, pinned example

Period Jan 1 to Jan 31 (30 days), upgrade starter to growth at Jan 16
00:00 (exactly halfway), 8,000 api_calls in each half:

| Segment | Plan | Allowance (prorated) | Billable | Amount |
|---|---|---|---|---|
| Jan 1–16 | starter | 1,000 x 15/30 = 500 | 7,500 @ $0.002 | $15.00 |
| Jan 16–31 | growth | 5,000 x 15/30 = 2,500 | 5,500 @ $0.0015 | $8.25 |

Subtotal $23.25, tax 10% = $2.325, rounded half-up to $2.33,
**total $25.58**. Prorated allowances round DOWN to whole units:
a 1,000-unit allowance over 10 of 30 days grants 333 units, not 334.

## The export adapter and fixture server

Per meter line of a finalized invoice, the adapter sends two requests:

- `POST /v1/billing/meter_events` with
  `{event_name, payload: {value: "<billable units>", stripe_customer_id: "cus_test_<tenant>"}, timestamp, identifier}`
- `POST /v1/invoiceitems` with
  `{customer: "cus_test_<tenant>", amount: <integer cents>, currency: "usd", description, invoice: "in_test_<invoice id>", metadata}`

Both carry an `Idempotency-Key` header (`<invoice>:<line>:<kind>`) and
the synthetic fixture key `sk_test_fixture_...` as a bearer token, the
same header path a real test-mode integration would use.

Idempotency holds at two layers: the adapter's local export ledger
records each item as sent only after a 2xx, so re-exporting a fully
exported invoice sends **zero** new requests; and the fixture server
dedupes on the idempotency key, so a crash between POST and ledger
write still creates exactly one object. Failure handling: the fixture
can be told to fail the next N requests with a Stripe-shaped 500; the
affected item is recorded `failed` with its attempt count and a retry
sends only that item. The failure test asserts the fixture ends with
exactly 2 meter events and 2 invoice items created, never 3.

## Run it

```bash
pip install -r requirements.txt

# Full test suite (38 tests)
python3 -m pytest tests/ -q

# Fixture "Stripe" server (local test mode only)
PYTHONPATH=src uvicorn fde_usage_billing.fixture_stripe:app --port 8002

# Billing API (SQLite, fixture-key tenant auth: fixture-key-<tenant>)
PYTHONPATH=src uvicorn fde_usage_billing.app:app --port 8001

curl -X POST http://127.0.0.1:8001/usage-events \
  -H "X-Tenant-Id: tenant-a" -H "X-Api-Key: fixture-key-tenant-a" \
  -H "Content-Type: application/json" \
  -d '{"meter":"api_calls","units":"12000","ts":"2026-01-10T00:00:00Z","idempotency_key":"api-1"}'
curl -X POST http://127.0.0.1:8001/invoices \
  -H "X-Tenant-Id: tenant-a" -H "X-Api-Key: fixture-key-tenant-a" \
  -H "Content-Type: application/json" \
  -d '{"period_start":"2026-01-01T00:00:00Z","period_end":"2026-02-01T00:00:00Z","plan":"starter"}'
```

## Measured results

From `python3 -m pytest tests/ -q` in this build: **38 passed**.

- Ingestion: 1,000 events, 100 duplicate keys, 900 stored, aggregate
  exactly 1,800 units; a duplicate key with different units is absorbed
  with the original payload.
- Rating: allowance boundary (1,000 units bills $0; 1,001 bills
  $0.002), tier boundary (11,000 bills exactly $20.00; 11,001 bills
  $20.001), second tier and feature-meter tiers all pinned exactly.
- Price book: v1 vs v2 selection at period start pinned ($2.00 vs
  $3.00 for identical usage in adjacent periods).
- Invoicing: $71.00 / $7.10 / $78.10 worked example; $10.05 subtotal
  tax-rounding proof ($1.01); finalized-invoice immutability verified
  after late usage arrives; correction issued as a referencing
  adjustment invoice with the original untouched.
- Proration: the pinned upgrade case above, $23.25 / $2.33 / $25.58,
  with prorated allowances 500 and 2,500 asserted per segment.
- Export: 2-line invoice produces 4 fixture requests and 4 created
  objects; re-export sends 0; injected 500 then retry ends with
  exactly 2 + 2 objects and one item showing 2 attempts.
- Isolation: meters, aggregates, and invoices are per-tenant; the same
  idempotency key under two tenants is two events; another tenant's
  invoice id returns 404 through the API.

## Scope honesty

- The "Stripe" server is a **local fixture** that mimics the API
  surface and test-mode response shapes. This project makes **no calls
  to the real Stripe API**, holds **no real API keys** (the only key
  string is the clearly synthetic `sk_test_fixture_...`), processes
  **no payments**, and handles **no card data of any kind**. It is
  metering, invoice math, and a test-mode-shaped adapter against
  fixtures.
- Plans, prices, tenants, and customers are fixtures. Tax is a single
  configured-rate stub, not a tax engine. Storage is SQLite, chosen so
  the whole system runs and tests anywhere; the storage layer is the
  only component that would change behind a production database.
- The billing API's tenant auth is a fixture key check
  (`fixture-key-<tenant>`) that mirrors API-key tenant auth shape; it
  is not production authentication.

# Launch note — fde-usage-billing (20-point pre-launch checklist)

1. **Tests green** — PASS: `python3 -m pytest tests/ -q` → 38 passed in this build session.
2. **No real secrets** — PASS: the only key strings are clearly synthetic fixtures (`sk_test_fixture_0000...` for the fixture server, `fixture-key-<tenant>` for the billing API); no realistic credential literal anywhere in the tree.
3. **No calls to the real Stripe API** — PASS: the export adapter's client is injected and bound in tests to the local fixture app; no Stripe SDK, no stripe.com URL, and no network client pointed anywhere else exists in the code (grep-verified).
4. **No real payments, no card data** — PASS: there is no payment, charge, payment-method, or card code path at all; the system stops at metering, invoice math, and test-mode-shaped export payloads.
5. **No private/personal data** — PASS: tenants (`tenant-a/b`), customers (`cus_test_...`), plans, and usage are invented fixtures.
6. **Free tier only / $0 cost** — PASS: Python stdlib (SQLite, Decimal) plus already-installed open-source packages (FastAPI, pydantic, pytest, httpx); no paid service is used or required.
7. **No network calls at runtime or in tests** — PASS: the fixture server is exercised in-process via TestClient; nothing leaves the machine.
8. **Deterministic runs** — PASS: an injected FixedClock drives period/finalize times in tests; ids are sequential; expected totals are fixed constants.
9. **Money math is Decimal-exact** — PASS: all money and units are `Decimal` (stored in SQLite as strings); no `float(` appears in `src/` (grep-verified).
10. **Ingestion idempotency tested at volume** — PASS: 1,000 events with 10% duplicated keys store exactly 900 and aggregate to exactly 1,800 units; dedupe is enforced by a UNIQUE (tenant, idempotency_key) constraint, not a read-then-write check.
11. **Rating boundaries pinned** — PASS: allowance edge (1,000 → $0; 1,001 → $0.002), tier edge (11,000 → $20.00; 11,001 → $20.001), deep second tier, and feature-meter tiers asserted exactly.
12. **Price-book version rule pinned** — PASS: the version effective at period start rates the whole period, proven by a period straddling the v2 effective date ($2.00 at v1 vs $3.00 at v2 for identical usage).
13. **Invoice math incl. tax rounding pinned** — PASS: $71.00/$7.10/$78.10 worked example, credit-line case $61.00/$6.10/$67.10, and half-up proof on a $10.05 subtotal (tax $1.01).
14. **Finalized-invoice immutability tested** — PASS: re-rate and line additions on a finalized invoice raise, late usage does not change it, and the correction path (new referencing adjustment invoice) is tested with the original untouched.
15. **Proration pinned** — PASS: mid-period starter→growth upgrade asserts per-segment prorated allowances (500 / 2,500), amounts ($15.00 / $8.25), and totals ($23.25 / $2.33 / $25.58); the floor-to-whole-units rounding rule is documented in code and README.
16. **Export idempotency + failure recovery tested** — PASS: re-export sends zero new requests (fixture request counts asserted unchanged); an injected fixture 500 leaves one retryable `failed` record, the retry sends only that item, and the fixture ends with exactly 2 + 2 created objects.
17. **Cross-tenant isolation tested** — PASS: aggregates, the same idempotency key under two tenants, invoice listing/lookup, and the API layer (another tenant's invoice → 404, wrong fixture key → 401) are all covered.
18. **README verified against reality** — PASS: every number in the README (38 tests, $78.10 invoice, $25.58 prorated total, 900/1,800 ingestion figures, 4 export requests) was produced by the test suite in this build session.
19. **README free of internal process language** — PASS: public-facing wording only; it describes the system, its design rules, and its scope limits.
20. **Reproducible from a clean checkout** — PASS: `pip install -r requirements.txt` plus the documented pytest/uvicorn commands reproduce everything locally; no database server, account, or network access is needed.

Verdict: all items PASS. Cleared for the ship lane.

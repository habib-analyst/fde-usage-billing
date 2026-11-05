"""SQLite storage: usage events, invoices, export records.

Decimals are stored as strings so no precision is ever lost to floats.
Usage events carry a UNIQUE(tenant, idempotency_key) constraint: a
duplicate key is absorbed by the database itself, not by a read-then-write
check, so dedupe holds under retries and volume.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from .models import UsageEvent


class Storage:
    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False: the FastAPI app serves requests on a
        # worker thread; access is serialized by the service layer's calls.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                tenant TEXT NOT NULL,
                meter TEXT NOT NULL,
                units TEXT NOT NULL,
                ts TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                UNIQUE (tenant, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts
                ON usage_events (tenant, ts);
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exports (
                idempotency_key TEXT PRIMARY KEY,
                invoice_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,          -- pending | sent | failed
                attempts INTEGER NOT NULL DEFAULT 0,
                response_id TEXT
            );
            """
        )
        self._conn.commit()

    # ---- usage events -------------------------------------------------
    def insert_event(self, event: UsageEvent) -> bool:
        """True if stored, False if the idempotency key already existed."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO usage_events (tenant, meter, units, ts, idempotency_key)"
            " VALUES (?, ?, ?, ?, ?)",
            (event.tenant, event.meter, str(event.units),
             event.ts.isoformat(), event.idempotency_key),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def aggregate(self, tenant: str, start: datetime, end: datetime) -> dict[str, Decimal]:
        """Total units per meter for one tenant in [start, end)."""
        rows = self._conn.execute(
            "SELECT meter, units FROM usage_events"
            " WHERE tenant = ? AND ts >= ? AND ts < ?",
            (tenant, start.isoformat(), end.isoformat()),
        ).fetchall()
        totals: dict[str, Decimal] = {}
        for row in rows:
            totals[row["meter"]] = totals.get(row["meter"], Decimal("0")) + Decimal(row["units"])
        return totals

    def event_count(self, tenant: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM usage_events WHERE tenant = ?", (tenant,)
        ).fetchone()
        return int(row["n"])

    # ---- invoices -------------------------------------------------------
    def save_invoice(self, payload: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO invoices (invoice_id, tenant, state, payload)"
            " VALUES (?, ?, ?, ?)",
            (payload["invoice_id"], payload["tenant"], payload["state"],
             json.dumps(payload)),
        )
        self._conn.commit()

    def load_invoice_payload(self, invoice_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def invoice_ids_for_tenant(self, tenant: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT invoice_id FROM invoices WHERE tenant = ? ORDER BY invoice_id",
            (tenant,),
        ).fetchall()
        return [r["invoice_id"] for r in rows]

    # ---- export records ---------------------------------------------------
    def get_export(self, idempotency_key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM exports WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return dict(row) if row else None

    def record_export_attempt(self, idempotency_key: str, invoice_id: str,
                              kind: str, status: str, response_id: str | None) -> None:
        existing = self.get_export(idempotency_key)
        attempts = (existing["attempts"] + 1) if existing else 1
        self._conn.execute(
            "INSERT OR REPLACE INTO exports (idempotency_key, invoice_id, kind, status,"
            " attempts, response_id) VALUES (?, ?, ?, ?, ?, ?)",
            (idempotency_key, invoice_id, kind, status, attempts, response_id),
        )
        self._conn.commit()

    def exports_for_invoice(self, invoice_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM exports WHERE invoice_id = ? ORDER BY idempotency_key",
            (invoice_id,),
        ).fetchall()
        return [dict(r) for r in rows]

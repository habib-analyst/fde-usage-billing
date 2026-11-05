"""Injected clock: period boundaries never read the wall clock directly."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()


class FixedClock:
    """Deterministic clock for tests and period-boundary control."""

    def __init__(self, instant: datetime):
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant

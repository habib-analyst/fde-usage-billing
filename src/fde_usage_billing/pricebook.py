"""Versioned price book.

Plans are fixtures (starter / growth / enterprise). Each plan prices each
meter with an included allowance (units per billing period) and tiered
per-unit rates applied to billable units AFTER the allowance.

Rating always uses the price-book version effective at the PERIOD START.
A version published mid-period never re-prices a period already begun;
that rule is pinned by tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class Tier:
    """up_to is the cumulative billable-unit ceiling of this tier (None = open)."""

    up_to: Decimal | None
    rate: Decimal


@dataclass(frozen=True)
class MeterPricing:
    allowance: Decimal
    tiers: tuple[Tier, ...]


@dataclass(frozen=True)
class PlanPricing:
    name: str
    meters: dict[str, MeterPricing]


@dataclass(frozen=True)
class PriceBookVersion:
    version_id: str
    effective_from: datetime
    plans: dict[str, PlanPricing]

    def plan(self, name: str) -> PlanPricing:
        try:
            return self.plans[name]
        except KeyError:
            raise KeyError(f"unknown plan {name!r} in price book {self.version_id}") from None


def _d(text: str) -> Decimal:
    return Decimal(text)


def _v1_plans() -> dict[str, PlanPricing]:
    return {
        "starter": PlanPricing("starter", {
            "api_calls": MeterPricing(_d("1000"), (
                Tier(_d("10000"), _d("0.002")),
                Tier(None, _d("0.001")),
            )),
            "feature_units": MeterPricing(_d("100"), (
                Tier(_d("1000"), _d("0.05")),
                Tier(None, _d("0.03")),
            )),
        }),
        "growth": PlanPricing("growth", {
            "api_calls": MeterPricing(_d("5000"), (
                Tier(_d("50000"), _d("0.0015")),
                Tier(None, _d("0.0008")),
            )),
            "feature_units": MeterPricing(_d("500"), (
                Tier(_d("5000"), _d("0.04")),
                Tier(None, _d("0.02")),
            )),
        }),
        "enterprise": PlanPricing("enterprise", {
            "api_calls": MeterPricing(_d("20000"), (
                Tier(None, _d("0.001")),
            )),
            "feature_units": MeterPricing(_d("2000"), (
                Tier(None, _d("0.025")),
            )),
        }),
    }


def _v2_plans() -> dict[str, PlanPricing]:
    plans = _v1_plans()
    # v2 raises only the starter api_calls rates; everything else carries over.
    starter = plans["starter"]
    plans["starter"] = PlanPricing("starter", {
        **starter.meters,
        "api_calls": MeterPricing(_d("1000"), (
            Tier(_d("10000"), _d("0.003")),
            Tier(None, _d("0.0015")),
        )),
    })
    return plans


class PriceBook:
    def __init__(self, versions: list[PriceBookVersion]):
        if not versions:
            raise ValueError("price book needs at least one version")
        self._versions = sorted(versions, key=lambda v: v.effective_from)

    @property
    def versions(self) -> list[PriceBookVersion]:
        return list(self._versions)

    def version_by_id(self, version_id: str) -> PriceBookVersion:
        for v in self._versions:
            if v.version_id == version_id:
                return v
        raise KeyError(f"unknown price book version {version_id!r}")

    def version_effective_at(self, instant: datetime) -> PriceBookVersion:
        chosen = None
        for v in self._versions:
            if v.effective_from <= instant:
                chosen = v
            else:
                break
        if chosen is None:
            raise KeyError(f"no price book version effective at {instant.isoformat()}")
        return chosen

    def version_for_period(self, period_start: datetime) -> PriceBookVersion:
        """The pinned rule: the version effective at the period start."""
        return self.version_effective_at(period_start)


def default_pricebook() -> PriceBook:
    return PriceBook([
        PriceBookVersion("v1", datetime(2026, 1, 1, tzinfo=timezone.utc), _v1_plans()),
        PriceBookVersion("v2", datetime(2026, 2, 1, tzinfo=timezone.utc), _v2_plans()),
    ])

"""Rating: turn units into money. Allowance first, tiers after. Decimal only."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .pricebook import MeterPricing


@dataclass
class RatingResult:
    quantity: Decimal
    allowance_applied: Decimal
    billable_units: Decimal
    amount: Decimal
    tier_breakdown: list[dict]


def rate_units(units: Decimal, pricing: MeterPricing,
               allowance_override: Decimal | None = None) -> RatingResult:
    """Rate `units` against a meter pricing.

    The included allowance absorbs units first (allowance_applied is the
    portion of the allowance actually consumed). Remaining billable units
    walk the tiers in order; tier ceilings are cumulative billable units.
    Amounts are exact Decimal products — no rounding happens in rating.
    Rounding exists in exactly one place: invoice-level tax.
    """
    allowance = pricing.allowance if allowance_override is None else allowance_override
    allowance_applied = min(units, allowance)
    billable = units - allowance_applied
    remaining = billable
    consumed = Decimal("0")
    amount = Decimal("0")
    breakdown: list[dict] = []
    for tier in pricing.tiers:
        if remaining <= 0:
            break
        if tier.up_to is None:
            take = remaining
        else:
            capacity = tier.up_to - consumed
            take = min(remaining, capacity)
        if take <= 0:
            continue
        tier_amount = take * tier.rate
        breakdown.append({
            "units": str(take),
            "rate": str(tier.rate),
            "amount": str(tier_amount),
        })
        amount += tier_amount
        consumed += take
        remaining -= take
    if remaining > 0:
        raise ValueError("tier list does not cover billable units (last tier must be open)")
    return RatingResult(
        quantity=units,
        allowance_applied=allowance_applied,
        billable_units=billable,
        amount=amount,
        tier_breakdown=breakdown,
    )


def prorate_allowance(full_allowance: Decimal, segment_seconds: int,
                      period_seconds: int) -> Decimal:
    """Prorated allowance for part of a period.

    Rounding rule (documented, pinned by tests): the proportional share
    full_allowance * segment / period, rounded DOWN to a whole unit
    (ROUND_FLOOR). Proration never grants more than the proportional
    share of the allowance.
    """
    if period_seconds <= 0:
        raise ValueError("period must be positive")
    if segment_seconds < 0 or segment_seconds > period_seconds:
        raise ValueError("segment must lie inside the period")
    share = full_allowance * Decimal(segment_seconds) / Decimal(period_seconds)
    return share.quantize(Decimal("1"), rounding=ROUND_FLOOR)

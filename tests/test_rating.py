from decimal import Decimal

from fde_usage_billing import default_pricebook, rate_units

BOOK = default_pricebook()
STARTER_API = BOOK.version_by_id("v1").plan("starter").meters["api_calls"]
STARTER_FEATURE = BOOK.version_by_id("v1").plan("starter").meters["feature_units"]


def test_allowance_absorbs_first_units():
    r = rate_units(Decimal("999"), STARTER_API)
    assert r.allowance_applied == Decimal("999")
    assert r.billable_units == Decimal("0")
    assert r.amount == Decimal("0")


def test_exactly_at_allowance_bills_nothing():
    r = rate_units(Decimal("1000"), STARTER_API)
    assert r.billable_units == Decimal("0")
    assert r.amount == Decimal("0")


def test_one_unit_over_allowance():
    r = rate_units(Decimal("1001"), STARTER_API)
    assert r.billable_units == Decimal("1")
    assert r.amount == Decimal("0.002")


def test_first_tier_boundary_exact():
    # 1000 allowance + 10000 billable fills tier 1 exactly: 10000 * 0.002.
    r = rate_units(Decimal("11000"), STARTER_API)
    assert r.billable_units == Decimal("10000")
    assert r.amount == Decimal("20.00")


def test_one_unit_into_second_tier():
    r = rate_units(Decimal("11001"), STARTER_API)
    assert r.amount == Decimal("20.001")
    assert [t["units"] for t in r.tier_breakdown] == ["10000", "1"]


def test_deep_second_tier():
    r = rate_units(Decimal("12000"), STARTER_API)
    assert r.billable_units == Decimal("11000")
    assert r.amount == Decimal("21.00")  # 10000*0.002 + 1000*0.001


def test_feature_units_tiers():
    r = rate_units(Decimal("1100"), STARTER_FEATURE)
    assert r.allowance_applied == Decimal("100")
    assert r.billable_units == Decimal("1000")
    assert r.amount == Decimal("50.00")  # 1000 * 0.05
    r2 = rate_units(Decimal("1101"), STARTER_FEATURE)
    assert r2.amount == Decimal("50.03")  # + 1 * 0.03 in tier 2


def test_zero_units():
    r = rate_units(Decimal("0"), STARTER_API)
    assert r.amount == Decimal("0")
    assert r.tier_breakdown == []

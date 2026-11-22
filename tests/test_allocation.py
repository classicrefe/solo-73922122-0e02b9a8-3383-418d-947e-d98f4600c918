"""归因算法单元测试。"""

from decimal import Decimal

from curtailment.allocation import (
    Interval,
    Order,
    allocate,
    aggregate_days,
    effective_caps,
    split_interval,
    validate_order,
)


def iv(slot: int, available: str, actual: str, start: str = "2026-01-01T00:00:00Z") -> Interval:
    return Interval(slot, start, Decimal(available), Decimal(actual))


def test_no_cap_all_unsplit():
    losses, unsplit = split_interval(iv(1, "10", "4"), {})
    assert losses == {}
    assert unsplit == Decimal("6.000")


def test_no_curtailment():
    losses, unsplit = split_interval(iv(1, "5", "5"), {"A": Decimal("1")})
    assert losses == {}
    assert unsplit == Decimal("0.000")


def test_single_cap_covering_whole_range():
    # 上限 4 <= 段下端 4：整个 4..10 都归 A。
    losses, unsplit = split_interval(iv(1, "10", "4"), {"A": Decimal("4")})
    assert losses == {"A": Decimal("6.000")}
    assert unsplit == Decimal("0.000")


def test_cap_above_segment_gets_nothing():
    # 段 4..8 无人认领（A 上限 8 > 段下端 4）；段 8..10 才归 A。
    losses, unsplit = split_interval(iv(1, "10", "4"), {"A": Decimal("8")})
    assert losses == {"A": Decimal("2.000")}
    assert unsplit == Decimal("4.000")


def test_two_caps_split_by_segments():
    # 段 4..6: A(6)>4、B(8)>4 → 待查 2
    # 段 6..8: A(6)<=6 → A 得 2
    # 段 8..10: A、B 都 <= 8 → 各得 1
    losses, unsplit = split_interval(iv(1, "10", "4"), {"A": Decimal("6"), "B": Decimal("8")})
    assert losses == {"A": Decimal("3.000"), "B": Decimal("1.000")}
    assert unsplit == Decimal("2.000")


def test_equal_split_between_eligible_causes():
    losses, unsplit = split_interval(iv(1, "10", "4"), {"A": Decimal("5"), "B": Decimal("5")})
    # 段 4..5 无人认领；段 5..10 两端都可认领，各 2.5
    assert losses == {"A": Decimal("2.500"), "B": Decimal("2.500")}
    assert unsplit == Decimal("1.000")


def test_lowest_cap_per_cause_wins():
    interval = iv(1, "10", "4")
    orders = [
        Order("O1", "A", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", Decimal("9")),
        Order("O2", "A", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", Decimal("5")),
    ]
    caps = effective_caps(interval, orders)
    assert caps == {"A": Decimal("5")}


def test_rounding_conserves_total():
    interval = iv(1, "10.000", "4.000")
    losses, unsplit = split_interval(
        interval,
        {"A": Decimal("5"), "B": Decimal("7"), "C": Decimal("9")},
    )
    total = sum(losses.values()) + unsplit
    assert total == Decimal("6.000")
    assert set(losses) <= {"A", "B", "C"}


def test_rounding_three_way_equal():
    interval = iv(1, "10", "4")
    losses, unsplit = split_interval(
        interval, {"A": Decimal("2"), "B": Decimal("3"), "C": Decimal("4")}
    )
    assert sum(losses.values()) + unsplit == Decimal("6.000")


def test_conflict_when_actual_exceeds_cap():
    interval = iv(1, "10", "7")
    order = Order("O1", "A", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", Decimal("6"))
    hits = validate_order(order, [interval])
    assert [hit.order_no for hit in hits] == ["O1"]
    assert hits[0].start_utc == "2026-01-01T00:00:00Z"


def test_no_conflict_at_exact_cap():
    interval = iv(1, "10", "6")
    order = Order("O1", "A", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", Decimal("6"))
    assert validate_order(order, [interval]) == []


def test_interval_end_is_exclusive():
    interval = iv(2, "10", "4", start="2026-01-01T01:00:00Z")
    order = Order("O1", "A", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", Decimal("1"))
    assert effective_caps(interval, [order]) == {}


def test_aggregate_days_splits_utc_dates():
    results = allocate(
        [
            iv(1, "10", "4", "2026-01-01T23:45:00Z"),
            iv(2, "10", "4", "2026-01-02T00:00:00Z"),
        ],
        [Order("O1", "A", "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z", Decimal("4"))],
    )
    days = aggregate_days(results)
    assert [day.date for day in days] == ["2026-01-01", "2026-01-02"]
    assert days[0].losses == {"A": Decimal("6.000")}
    assert days[1].losses == {"A": Decimal("6.000")}


def test_actual_above_available_no_negative_loss():
    interval = iv(1, "4", "5")
    losses, unsplit = split_interval(interval, {"A": Decimal("4")})
    assert losses == {}
    assert unsplit == Decimal("0.000")
    assert interval.curtailment == Decimal("0.000")

"""限发损失归因算法（纯计算，不依赖 Flask/SQLite）。

规则摘要
--------
* 计量表按 15 分钟一格，含 UTC 时刻、可发量、实发量。
* 启用的限发单覆盖某格 ``start <= 格时刻 < end``；同一格同一原因取最低上限。
* 在 ``[实发量, 可发量]`` 间用该格所有上限切段；每段均分给
  “上限不高于该段下端”的原因（每个原因每段至多一份）。
* 没有原因认领的段计入待查。
* 某单覆盖格内 ``实发量 > 上限`` 即冲突：整单退回。

电量统一量化到 0.001，并按最大余数法分摊舍入尾差，保证每格
``各原因损失之和 + 待查 == 可发量 - 实发量``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence

QUANT = Decimal("0.001")
MILL = 1000
ZERO = Decimal("0")


def q(value: Decimal) -> Decimal:
    """量化到 0.001（四舍五入）。"""
    return Decimal(value).quantize(QUANT, rounding=ROUND_HALF_UP)


def to_decimal(value) -> Decimal:
    """把表单/数据库中的值转为有限 Decimal，非法时抛 ValueError。"""
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError(f"不是有效数字：{value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"不是有限数字：{value!r}")
    return result


def clamp_curtailment(available: Decimal, actual: Decimal) -> Decimal:
    """实发量高于可发量时视为该格无损失（不允许负损失）。"""
    return q(max(ZERO, available - actual))


@dataclass(frozen=True)
class Interval:
    """计量表的一格。"""

    slot: int
    start_utc: str
    available: Decimal
    actual: Decimal

    @property
    def curtailment(self) -> Decimal:
        return clamp_curtailment(self.available, self.actual)

    @property
    def date(self) -> str:
        return self.start_utc[:10]


@dataclass(frozen=True)
class Order:
    """一张限发单的某个修订版。"""

    order_no: str
    cause: str
    start_utc: str
    end_utc: str
    cap: Decimal
    revision: int = 1

    def covers(self, interval: Interval) -> bool:
        return self.start_utc <= interval.start_utc < self.end_utc


@dataclass
class SlotResult:
    slot: int
    start_utc: str
    available: Decimal
    actual: Decimal
    losses: dict[str, Decimal] = field(default_factory=dict)
    unsplit: Decimal = ZERO

    @property
    def date(self) -> str:
        return self.start_utc[:10]

    @property
    def curtailment(self) -> Decimal:
        return clamp_curtailment(self.available, self.actual)

    @property
    def attributed(self) -> Decimal:
        return q(sum(self.losses.values(), ZERO))


@dataclass
class DayResult:
    date: str
    losses: dict[str, Decimal] = field(default_factory=dict)
    unsplit: Decimal = ZERO
    unsplit_slots: list[dict] = field(default_factory=list)
    slots: list[SlotResult] = field(default_factory=list)


@dataclass(frozen=True)
class Conflict:
    order_no: str
    slot: int
    start_utc: str
    available: Decimal
    actual: Decimal
    cap: Decimal

    def as_dict(self) -> dict:
        return {
            "order_no": self.order_no,
            "slot": self.slot,
            "start_utc": self.start_utc,
            "available": str(q(self.available)),
            "actual": str(q(self.actual)),
            "cap": str(q(self.cap)),
        }


def effective_caps(interval: Interval, orders: Iterable[Order]) -> dict[str, Decimal]:
    """该格每个原因的最低上限。"""
    caps: dict[str, Decimal] = {}
    for order in orders:
        if not order.covers(interval):
            continue
        current = caps.get(order.cause)
        if current is None or order.cap < current:
            caps[order.cause] = order.cap
    return caps


def _split_points(
    actual: Decimal, available: Decimal, caps: Mapping[str, Decimal]
) -> list[Decimal]:
    points = {actual, available}
    for cap in caps.values():
        cap = q(cap)
        if actual < cap < available:
            points.add(cap)
    return sorted(points)


def _largest_remainder(exact: Sequence[Decimal], total_mills: int) -> list[int]:
    """把 ``total_mills`` 毫单位按 ``exact`` 比例分摊为整数毫单位。

    先取下整，再按小数余数从大到小逐个 ±1 调整，保证合计恰为 ``total_mills``。
    由于 Decimal 除法可能带极微小的舍入，正负两个方向都要能调整。
    """
    if not exact:
        return []
    scaled = [max(ZERO, value) * MILL for value in exact]
    floors = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in scaled]
    result = list(floors)
    remainder = total_mills - sum(result)
    fractions = [value - Decimal(floor) for value, floor in zip(scaled, floors)]

    if remainder > 0:
        order = sorted(range(len(exact)), key=lambda i: (-fractions[i], i))
        index = 0
        while remainder > 0:
            result[order[index % len(order)]] += 1
            remainder -= 1
            index += 1
    elif remainder < 0:
        # 极少数情况下理论合计略有超出：从余数最小且可减的桶里扣回。
        order = sorted(range(len(exact)), key=lambda i: (fractions[i], i))
        index = 0
        guard = 0
        while remainder < 0 and guard < len(order) * 4:
            i = order[index % len(order)]
            if result[i] > 0:
                result[i] -= 1
                remainder += 1
            index += 1
            guard += 1
        if remainder < 0:  # 兜底：全部归零
            result = [0] * len(exact)
            remainder = 0
    return result


def split_interval(
    interval: Interval, caps: Mapping[str, Decimal]
) -> tuple[dict[str, Decimal], Decimal]:
    """对一格归因，返回 ``(原因 -> 损失, 待查)``。

    段 [lo, hi] 的归属：所有 ``上限 <= lo`` 的原因平分；无人认领则整段待查。
    量化到 0.001 后按最大余数法分配尾差，保证
    ``各原因损失之和 + 待查 == 可发量 - 实发量``。
    """
    actual = q(interval.actual)
    available = q(interval.available)
    if available <= actual:
        return {}, ZERO

    total_mills = int((available - actual) * MILL)
    points = _split_points(actual, available, caps)

    exact_losses: dict[str, Decimal] = {cause: ZERO for cause in caps}
    exact_unsplit = ZERO

    for lo, hi in zip(points, points[1:]):
        width = hi - lo
        if width <= ZERO:
            continue
        eligible = sorted(cause for cause, cap in caps.items() if q(cap) <= lo)
        if not eligible:
            exact_unsplit += width
            continue
        share = width / Decimal(len(eligible))
        for cause in eligible:
            exact_losses[cause] += share

    keys: list[str] = [cause for cause, value in exact_losses.items() if value > ZERO]
    exact: list[Decimal] = [exact_losses[cause] for cause in keys]
    if exact_unsplit > ZERO:
        keys.append("")
        exact.append(exact_unsplit)

    mills = _largest_remainder(exact, total_mills)

    losses: dict[str, Decimal] = {}
    unsplit_mills = 0
    for key, value in zip(keys, mills):
        if key == "":
            unsplit_mills += value
        elif value > 0:
            losses[key] = Decimal(value) / MILL

    # 尾差（含四舍五入归零的小额）并入待查，保证不变量成立。
    allocated_mills = sum(mills) - unsplit_mills
    unsplit_mills = total_mills - allocated_mills
    if unsplit_mills < 0:  # 理论上不会发生；兜底从最大损失里扣回
        overflow = -unsplit_mills
        for cause in sorted(losses, key=lambda c: losses[c], reverse=True):
            take = min(overflow, int(losses[cause] * MILL))
            losses[cause] -= Decimal(take) / MILL
            overflow -= take
            if overflow == 0:
                break
        losses = {cause: value for cause, value in losses.items() if value > ZERO}
        unsplit_mills = 0

    return dict(sorted(losses.items())), q(Decimal(unsplit_mills) / MILL)


def find_conflicts(interval: Interval, orders: Iterable[Order]) -> list[Conflict]:
    """该格中 ``实发量 > 上限`` 的冲突。"""
    found: list[Conflict] = []
    actual = q(interval.actual)
    for order in orders:
        if not order.covers(interval):
            continue
        if actual > q(order.cap):
            found.append(
                Conflict(
                    order_no=order.order_no,
                    slot=interval.slot,
                    start_utc=interval.start_utc,
                    available=q(interval.available),
                    actual=actual,
                    cap=q(order.cap),
                )
            )
    return found


def validate_order(order: Order, intervals: Sequence[Interval]) -> list[Conflict]:
    """整单冲突检查；返回非空即应退回整单。"""
    return [
        conflict
        for interval in intervals
        if order.covers(interval)
        for conflict in find_conflicts(interval, [order])
    ]


def allocate(
    intervals: Sequence[Interval], orders: Sequence[Order]
) -> list[SlotResult]:
    """对给定格集合归因（orders 应已排除冲突单）。"""
    results: list[SlotResult] = []
    for interval in intervals:
        caps = effective_caps(interval, orders)
        losses, unsplit = split_interval(interval, caps)
        results.append(
            SlotResult(
                slot=interval.slot,
                start_utc=interval.start_utc,
                available=q(interval.available),
                actual=q(interval.actual),
                losses=losses,
                unsplit=unsplit,
            )
        )
    return results


def aggregate_days(results: Sequence[SlotResult]) -> list[DayResult]:
    days: dict[str, DayResult] = {}
    for res in results:
        day = days.setdefault(res.date, DayResult(date=res.date))
        day.slots.append(res)
        for cause, value in res.losses.items():
            day.losses[cause] = day.losses.get(cause, ZERO) + value
        if res.unsplit > ZERO:
            day.unsplit += res.unsplit
            day.unsplit_slots.append(
                {
                    "slot": res.slot,
                    "start_utc": res.start_utc,
                    "amount": q(res.unsplit),
                }
            )
    for day in days.values():
        day.losses = {
            cause: q(value) for cause, value in sorted(day.losses.items()) if q(value) > ZERO
        }
        day.unsplit = q(day.unsplit)
        day.unsplit_slots.sort(key=lambda item: item["start_utc"])
        day.slots.sort(key=lambda res: res.start_utc)
    return [days[key] for key in sorted(days)]

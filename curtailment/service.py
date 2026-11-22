"""业务服务层：导入计量表、提交/更正限发单、按日重算归因。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from .allocation import (
    Conflict,
    DayResult,
    Interval,
    Order,
    allocate,
    aggregate_days,
    clamp_curtailment,
    q,
    to_decimal,
    validate_order,
)
from .db import tx
from .parsing import (
    MeterRow,
    canonical,
    day_bounds,
    expected_slots,
    next_date,
    parse_order_time,
    parse_utc,
)

FMT = "%Y-%m-%dT%H:%M:%SZ"
MAX_NAME = 64


def validate_token(value: str, *, field: str) -> str:
    """单号/风机名会出现在 URL 里：拒绝斜杠与控制字符。"""
    text = (value or "").strip()
    if not text:
        raise ValidationError(f"{field}不能为空")
    if len(text) > MAX_NAME:
        raise ValidationError(f"{field}不能超过 {MAX_NAME} 个字符")
    if "/" in text or "\\" in text or any(ord(ch) < 32 for ch in text):
        raise ValidationError(f"{field}不能包含斜杠或控制字符：{text!r}")
    return text

class ValidationError(ValueError):
    """表单/数据校验失败。"""

    def __init__(self, message: str, errors: Optional[list[str]] = None):
        super().__init__(message)
        self.errors = errors if errors else [message]


class OrderConflict(ValidationError):
    """限发单冲突：整单退回。"""

    def __init__(self, order_no: str, conflicts: Sequence[Conflict]):
        self.order_no = order_no
        self.conflicts = list(conflicts)
        super().__init__(
            f"单据 {order_no} 有 {len(self.conflicts)} 个冲突格（实发量超过每格上限），"
            "已退回整单，原单据与归因结果保持不变。",
            [
                f"{item.start_utc} 实发 {q(item.actual)} > 上限 {q(item.cap)}"
                for item in self.conflicts
            ],
        )


@dataclass
class OrderInput:
    order_no: str
    cause: str
    start_utc: str
    end_utc: str
    cap: object

    @classmethod
    def from_form(cls, form: dict) -> "OrderInput":
        def get(key: str) -> str:
            value = form.get(key)
            return "" if value is None else str(value).strip()

        errors: list[str] = []
        order_no = get("order_no")
        cause = get("cause")
        try:
            order_no = validate_token(order_no, field="单号")
        except ValidationError as exc:
            errors.append(str(exc))
        if not cause:
            errors.append("原因不能为空")
        elif len(cause) > MAX_NAME:
            errors.append(f"原因不能超过 {MAX_NAME} 个字符")

        start_dt = end_dt = None
        try:
            start_dt = parse_order_time(get("start_utc") or get("start"), field="开始时刻")
        except ValueError as exc:
            errors.append(str(exc))
        try:
            end_dt = parse_order_time(get("end_utc") or get("end"), field="结束时刻")
        except ValueError as exc:
            errors.append(str(exc))

        cap = None
        cap_text = get("cap")
        if cap_text == "":
            errors.append("每格电量上限不能为空")
        else:
            try:
                cap = to_decimal(cap_text.replace(",", ""))
                if cap < 0:
                    errors.append("每格电量上限不能为负数")
            except ValueError as exc:
                errors.append(str(exc))

        if start_dt and end_dt:
            if end_dt <= start_dt:
                errors.append("结束时刻必须晚于开始时刻")
            else:
                try:
                    expected_slots(start_dt, end_dt)
                except ValueError as exc:
                    errors.append(str(exc))

        if errors:
            raise ValidationError("单据校验失败", errors)
        if start_dt is None or end_dt is None or cap is None:
            raise ValidationError("单据校验失败")

        return cls(
            order_no=order_no,
            cause=cause,
            start_utc=canonical(start_dt),
            end_utc=canonical(end_dt),
            cap=q(cap),
        )


@dataclass
class SubmitResult:
    order_no: str
    revision: int
    corrected: bool
    dates: list[str]
    days: list[DayResult]


@dataclass
class ImportResult:
    turbine: str
    inserted: int
    updated: int
    dates: list[str]
    warnings: list[str] = field(default_factory=list)
    order_conflicts: dict[str, list[Conflict]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _row_to_interval(row: sqlite3.Row) -> Interval:
    return Interval(
        slot=row["id"],
        start_utc=row["start_utc"],
        available=to_decimal(row["available"]),
        actual=to_decimal(row["actual"]),
    )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        order_no=row["order_no"],
        cause=row["cause"],
        start_utc=row["start_utc"],
        end_utc=row["end_utc"],
        cap=to_decimal(row["cap"]),
        revision=row["revision"],
    )


def load_intervals(
    conn: sqlite3.Connection,
    turbine: str,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None,
) -> list[Interval]:
    sql = "SELECT id, start_utc, available, actual FROM meters WHERE turbine = ?"
    params: list = [turbine]
    if start_utc is not None:
        sql += " AND start_utc >= ?"
        params.append(start_utc)
    if end_utc is not None:
        sql += " AND start_utc < ?"
        params.append(end_utc)
    sql += " ORDER BY start_utc"
    return [_row_to_interval(row) for row in conn.execute(sql, params)]


def load_all_intervals(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> list[Interval]:
    """区间内所有风机的格（冲突检查用：限发单是全风场口径）。"""
    return [
        _row_to_interval(row)
        for row in conn.execute(
            "SELECT id, start_utc, available, actual FROM meters "
            "WHERE start_utc >= ? AND start_utc < ? ORDER BY turbine, start_utc",
            (start_utc, end_utc),
        )
    ]


def load_orders(conn: sqlite3.Connection, *, include_conflicted: bool = False) -> list[Order]:
    """启用且未因导入冲突被暂停的限发单。"""
    sql = "SELECT * FROM orders WHERE active = 1"
    if not include_conflicted:
        sql += " AND conflicted = 0"
    sql += " ORDER BY order_no"
    return [_row_to_order(row) for row in conn.execute(sql)]


def load_order(conn: sqlite3.Connection, order_no: str) -> Optional[Order]:
    row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    return _row_to_order(row) if row else None


def order_dates(start_utc: str, end_utc: str) -> list[str]:
    """单据覆盖（含边界日）的 UTC 日期；end 为开区间。"""
    start = parse_utc(start_utc, field="开始时刻")
    end = parse_utc(end_utc, field="结束时刻")
    if end <= start:
        return []
    last = (end - timedelta(microseconds=1)).date()
    current = start.date()
    dates: list[str] = []
    while current <= last:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def order_conflicts(conn: sqlite3.Connection, order: Order) -> list[Conflict]:
    return validate_order(
        order, load_all_intervals(conn, order.start_utc, order.end_utc)
    )


# ---------------------------------------------------------------------------
# 落库与重算
# ---------------------------------------------------------------------------


def _turbines_with_meters(conn: sqlite3.Connection, date: str) -> list[str]:
    start, end = day_bounds(date)
    return [
        row["turbine"]
        for row in conn.execute(
            "SELECT DISTINCT turbine FROM meters WHERE start_utc >= ? AND start_utc < ? "
            "ORDER BY turbine",
            (start, end),
        )
    ]


def _write_day(
    conn: sqlite3.Connection, turbine: str, date: str, day: DayResult, results
) -> None:
    now = datetime.now(timezone.utc).strftime(FMT)
    conn.execute(
        "INSERT OR REPLACE INTO attribution_days (turbine, date, computed_at) VALUES (?, ?, ?)",
        (turbine, date, now),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO attribution_slots "
        "(turbine, start_utc, date, available, actual, unsplit) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                turbine,
                res.start_utc,
                date,
                str(q(res.available)),
                str(q(res.actual)),
                str(q(res.unsplit)),
            )
            for res in results
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO attribution_losses "
        "(turbine, start_utc, date, cause, amount) VALUES (?, ?, ?, ?, ?)",
        [
            (turbine, res.start_utc, date, cause, str(q(amount)))
            for res in results
            for cause, amount in res.losses.items()
        ],
    )


def recompute_day(conn: sqlite3.Connection, turbine: str, date: str) -> Optional[DayResult]:
    """重算并落库某风机某日的归因；该日无计量时清空旧结果并返回 None。

    归因只取该日的格与覆盖该日的启用单，因此重算严格限于日期，不影响其他日期。
    """
    start, end = day_bounds(date)

    conn.execute("DELETE FROM attribution_losses WHERE turbine = ? AND date = ?", (turbine, date))
    conn.execute("DELETE FROM attribution_slots WHERE turbine = ? AND date = ?", (turbine, date))
    conn.execute("DELETE FROM attribution_days WHERE turbine = ? AND date = ?", (turbine, date))

    intervals = load_intervals(conn, turbine, start, end)
    if not intervals:
        return None

    orders = [
        order
        for order in load_orders(conn)
        if order.start_utc < end and order.end_utc > start
    ]
    results = allocate(intervals, orders)
    day = aggregate_days(results)[0]
    _write_day(conn, turbine, date, day, results)
    return day


def recompute_dates(conn: sqlite3.Connection, dates: Iterable[str]) -> list[DayResult]:
    """只重算给定日期：这些日期中有计量数据的每台风机各重算整日。"""
    days: list[DayResult] = []
    for date in sorted(set(dates)):
        for turbine in _turbines_with_meters(conn, date):
            result = recompute_day(conn, turbine, date)
            if result is not None:
                days.append(result)
    return days


def _refresh_conflicted_orders(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> tuple[list[str], set[str]]:
    """导入计量后复核与该区间重叠的单据；冲突单整单暂停（conflicted=1）。

    返回 ``(状态发生变化的单号, 仍处于冲突的单号)``。
    """
    changed: list[str] = []
    conflicted: set[str] = set()
    rows = conn.execute(
        "SELECT * FROM orders WHERE active = 1 AND start_utc < ? AND end_utc > ?",
        (end_utc, start_utc),
    ).fetchall()
    for row in rows:
        order = _row_to_order(row)
        hits = order_conflicts(conn, order)
        flag = 1 if hits else 0
        if flag:
            conflicted.add(order.order_no)
        if flag != row["conflicted"]:
            conn.execute(
                "UPDATE orders SET conflicted = ? WHERE order_no = ?",
                (flag, order.order_no),
            )
            changed.append(order.order_no)
    return changed, conflicted


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def import_meter(
    conn: sqlite3.Connection,
    turbine: str,
    rows: Sequence[MeterRow],
    *,
    replace: bool = False,
) -> ImportResult:
    """导入/更新计量行，并在同一事务内重算受影响日期。"""
    turbine = validate_token(turbine, field="风机编号")
    if not rows:
        raise ValidationError("没有可导入的数据行")

    seen: set[str] = set()
    for row in rows:
        if row.start_utc in seen:
            raise ValidationError(f"文件内时刻重复：{row.start_utc}")
        seen.add(row.start_utc)

    dates = sorted({row.start_utc[:10] for row in rows})
    inserted = updated = 0
    range_start = f"{dates[0]}T00:00:00Z"
    range_end = f"{next_date(dates[-1])}T00:00:00Z"

    with tx(conn):
        if replace:
            conn.execute(
                "DELETE FROM meters WHERE turbine = ? AND start_utc >= ? AND start_utc < ?",
                (turbine, range_start, range_end),
            )
        for row in rows:
            existing = conn.execute(
                "SELECT id FROM meters WHERE turbine = ? AND start_utc = ?",
                (turbine, row.start_utc),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE meters SET available = ?, actual = ?, imported_at = "
                    "(strftime('%Y-%m-%dT%H:%M:%SZ','now')) WHERE id = ?",
                    (str(q(row.available)), str(q(row.actual)), existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO meters (turbine, start_utc, available, actual) VALUES (?, ?, ?, ?)",
                    (turbine, row.start_utc, str(q(row.available)), str(q(row.actual))),
                )
                inserted += 1

        changed_orders, still_conflicted = _refresh_conflicted_orders(
            conn, range_start, range_end
        )

        # 重算导入日期；若某单的暂停状态变化，它覆盖的日期也要重算（日期口径）。
        recompute = set(dates)
        for order_no in changed_orders:
            row = conn.execute(
                "SELECT start_utc, end_utc FROM orders WHERE order_no = ?", (order_no,)
            ).fetchone()
            recompute |= set(order_dates(row["start_utc"], row["end_utc"]))
        recompute_dates(conn, recompute)

        detail: dict[str, list[Conflict]] = {}
        for order_no in sorted(still_conflicted):
            row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
            detail[order_no] = order_conflicts(conn, _row_to_order(row))

    return ImportResult(
        turbine=turbine,
        inserted=inserted,
        updated=updated,
        dates=dates,
        warnings=[],
        order_conflicts=detail,
    )


# ---------------------------------------------------------------------------
# 单据提交 / 更正
# ---------------------------------------------------------------------------


def submit_order(conn: sqlite3.Connection, data: OrderInput) -> SubmitResult:
    """新建或整单更正限发单。

    * 任意覆盖格 ``实发量 > 上限`` → 抛 :class:`OrderConflict`，整单退回；
      事务回滚，原单据与归因结果均不变。
    * 成功 → 单据与归因结果同一事务生效；只重算新旧单覆盖的日期，
      重算时所有启用单（含其他重叠单据/更正）一并参与。
    """
    with tx(conn):
        existing = load_order(conn, data.order_no)
        if existing is not None:
            revision = existing.revision + 1
            corrected = True
            old_dates = set(order_dates(existing.start_utc, existing.end_utc))
        else:
            revision = 1
            corrected = False
            old_dates = set()

        candidate = Order(
            order_no=data.order_no,
            cause=data.cause,
            start_utc=data.start_utc,
            end_utc=data.end_utc,
            cap=data.cap,
            revision=revision,
        )

        conflicts = order_conflicts(conn, candidate)
        if conflicts:
            raise OrderConflict(data.order_no, conflicts)

        now = datetime.now(timezone.utc).strftime(FMT)
        cap_text = str(q(candidate.cap))
        if corrected:
            conn.execute(
                "UPDATE orders SET cause = ?, start_utc = ?, end_utc = ?, cap = ?, "
                "revision = ?, active = 1, conflicted = 0, updated_at = ? WHERE order_no = ?",
                (
                    candidate.cause,
                    candidate.start_utc,
                    candidate.end_utc,
                    cap_text,
                    revision,
                    now,
                    candidate.order_no,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO orders (order_no, cause, start_utc, end_utc, cap, revision, "
                "active, conflicted, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                (
                    candidate.order_no,
                    candidate.cause,
                    candidate.start_utc,
                    candidate.end_utc,
                    cap_text,
                    revision,
                    now,
                    now,
                ),
            )
        conn.execute(
            "INSERT INTO order_revisions (order_no, revision, cause, start_utc, end_utc, "
            "cap, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (
                candidate.order_no,
                revision,
                candidate.cause,
                candidate.start_utc,
                candidate.end_utc,
                cap_text,
                now,
            ),
        )

        dates = set(order_dates(candidate.start_utc, candidate.end_utc)) | old_dates
        days = recompute_dates(conn, dates)

    return SubmitResult(
        order_no=candidate.order_no,
        revision=revision,
        corrected=corrected,
        dates=sorted(dates),
        days=sorted(days, key=lambda day: day.date),
    )


# ---------------------------------------------------------------------------
# 查询视图
# ---------------------------------------------------------------------------


def list_turbines(conn: sqlite3.Connection) -> list[str]:
    return [
        row["turbine"]
        for row in conn.execute("SELECT DISTINCT turbine FROM meters ORDER BY turbine")
    ]


def list_days(conn: sqlite3.Connection, turbine: str) -> list[dict]:
    """按日汇总：各原因损失、待查合计、待查格数。"""
    date_rows = conn.execute(
        "SELECT DISTINCT date FROM attribution_slots WHERE turbine = ? ORDER BY date",
        (turbine,),
    ).fetchall()
    days: list[dict] = []
    for row in date_rows:
        date = row["date"]
        losses: dict[str, object] = {}
        for item in conn.execute(
            "SELECT cause, amount FROM attribution_losses WHERE turbine = ? AND date = ?",
            (turbine, date),
        ):
            losses[item["cause"]] = losses.get(item["cause"], q(0)) + to_decimal(item["amount"])
        losses = {cause: q(amount) for cause, amount in sorted(losses.items()) if q(amount) > 0}

        unsplit = q(0)
        curtailment = q(0)
        unsplit_count = 0
        slot_count = 0
        for slot in conn.execute(
            "SELECT available, actual, unsplit FROM attribution_slots WHERE turbine = ? AND date = ?",
            (turbine, date),
        ):
            unsplit += to_decimal(slot["unsplit"])
            curtailment += clamp_curtailment(
                to_decimal(slot["available"]), to_decimal(slot["actual"])
            )
            slot_count += 1
            if to_decimal(slot["unsplit"]) > 0:
                unsplit_count += 1

        days.append(
            {
                "date": date,
                "losses": losses,
                "unsplit": q(unsplit),
                "unsplit_count": unsplit_count,
                "curtailment": q(curtailment),
                "slot_count": slot_count,
            }
        )
    return days


def get_day(conn: sqlite3.Connection, turbine: str, date: str) -> Optional[dict]:
    day_row = conn.execute(
        "SELECT * FROM attribution_days WHERE turbine = ? AND date = ?", (turbine, date)
    ).fetchone()
    if day_row is None:
        return None

    slot_rows = conn.execute(
        "SELECT start_utc, available, actual, unsplit FROM attribution_slots "
        "WHERE turbine = ? AND date = ? ORDER BY start_utc",
        (turbine, date),
    ).fetchall()
    if not slot_rows:
        return None

    losses_by_slot: dict[str, dict[str, object]] = {}
    for item in conn.execute(
        "SELECT start_utc, cause, amount FROM attribution_losses WHERE turbine = ? AND date = ?",
        (turbine, date),
    ):
        losses_by_slot.setdefault(item["start_utc"], {})[item["cause"]] = to_decimal(item["amount"])

    slots: list[dict] = []
    for row in slot_rows:
        available = to_decimal(row["available"])
        actual = to_decimal(row["actual"])
        losses = {
            cause: q(amount)
            for cause, amount in sorted(losses_by_slot.get(row["start_utc"], {}).items())
        }
        slots.append(
            {
                "start_utc": row["start_utc"],
                "available": q(available),
                "actual": q(actual),
                "curtailment": clamp_curtailment(available, actual),
                "losses": losses,
                "unsplit": q(to_decimal(row["unsplit"])),
            }
        )

    day_losses: dict[str, object] = {}
    for slot in slots:
        for cause, amount in slot["losses"].items():
            day_losses[cause] = day_losses.get(cause, q(0)) + amount
    day_losses = {cause: q(amount) for cause, amount in sorted(day_losses.items()) if q(amount) > 0}
    unsplit_slots = [slot for slot in slots if slot["unsplit"] > 0]
    unsplit_total = q(sum((slot["unsplit"] for slot in unsplit_slots), q(0)))
    curtailment = q(sum((slot["curtailment"] for slot in slots), q(0)))

    day_start, day_end = day_bounds(date)
    orders = [
        order
        for order in load_orders(conn, include_conflicted=True)
        if order.start_utc < day_end and order.end_utc > day_start
    ]
    paused: dict[str, list[Conflict]] = {}
    for order in orders:
        hits = order_conflicts(conn, order)
        if hits:
            paused[order.order_no] = hits

    return {
        "turbine": turbine,
        "date": date,
        "computed_at": day_row["computed_at"],
        "losses": day_losses,
        "unsplit": unsplit_total,
        "unsplit_slots": unsplit_slots,
        "slots": slots,
        "curtailment": curtailment,
        "orders": orders,
        "paused_orders": paused,
    }


def list_orders(conn: sqlite3.Connection) -> list[dict]:
    return [
        {
            "order_no": row["order_no"],
            "cause": row["cause"],
            "start_utc": row["start_utc"],
            "end_utc": row["end_utc"],
            "cap": to_decimal(row["cap"]),
            "revision": row["revision"],
            "active": bool(row["active"]),
            "conflicted": bool(row["conflicted"]),
            "updated_at": row["updated_at"],
        }
        for row in conn.execute("SELECT * FROM orders ORDER BY order_no")
    ]


def get_order(conn: sqlite3.Connection, order_no: str) -> Optional[dict]:
    current = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    if current is None:
        return None
    revisions = [
        {
            "revision": row["revision"],
            "cause": row["cause"],
            "start_utc": row["start_utc"],
            "end_utc": row["end_utc"],
            "cap": to_decimal(row["cap"]),
            "active": bool(row["active"]),
            "created_at": row["created_at"],
        }
        for row in conn.execute(
            "SELECT * FROM order_revisions WHERE order_no = ? ORDER BY revision DESC",
            (order_no,),
        )
    ]
    order = _row_to_order(current)
    return {
        "order_no": current["order_no"],
        "cause": current["cause"],
        "start_utc": current["start_utc"],
        "end_utc": current["end_utc"],
        "cap": to_decimal(current["cap"]),
        "revision": current["revision"],
        "active": bool(current["active"]),
        "conflicted": bool(current["conflicted"]),
        "updated_at": current["updated_at"],
        "revisions": revisions,
        "conflicts": order_conflicts(conn, order),
    }

"""时刻、数值与 CSV 解析。"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from .allocation import q

SLOT = timedelta(minutes=15)
CANONICAL = "%Y-%m-%dT%H:%M:%SZ"


def parse_utc(value: str, *, field: str = "时刻") -> datetime:
    """解析为 UTC datetime。无时区信息时按 UTC 处理。"""
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field}不能为空")
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field}格式无法识别：{value!r}（请用 ISO 8601，如 2026-01-01T00:00:00Z）") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(CANONICAL)


def parse_slot(value: str, *, field: str = "时刻") -> datetime:
    """解析计量格时刻，必须落在 15 分钟边界。"""
    dt = parse_utc(value, field=field)
    if dt.minute % 15 != 0 or dt.second != 0 or dt.microsecond != 0:
        raise ValueError(f"{field}必须对齐 15 分钟：{value!r}")
    return dt


def parse_order_time(value: str, *, field: str) -> datetime:
    """单据起止时刻：同样要求 15 分钟对齐（按格归属）。"""
    return parse_slot(value, field=field)


def next_date(date_text: str) -> str:
    return (datetime.strptime(date_text, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()


def day_bounds(date_text: str) -> tuple[str, str]:
    start = f"{date_text}T00:00:00Z"
    end = f"{next_date(date_text)}T00:00:00Z"
    return start, end


_HEADER_ALIASES = {
    "time": {
        "time",
        "timestamp",
        "startutc",
        "datetime",
        "utc",
        "时刻",
        "时间",
        "utc时刻",
        "时间utc",
        "起始时刻",
        "计量时刻",
        "时刻utc",
    },
    "available": {
        "available",
        "availablekwh",
        "可发量",
        "可发",
        "理论可发",
        "理论可发量",
        "可发电量",
    },
    "actual": {
        "actual",
        "actualkwh",
        "实发量",
        "实发",
        "实际发电",
        "实际发电量",
        "实发电量",
    },
    "turbine": {"turbine", "turbineid", "风机", "风机编号", "机组", "机组编号"},
}


def _norm_header(value: str) -> str:
    text = (value or "").replace("﻿", "").strip().lower()
    for ch in " _-()（）[]【】":
        text = text.replace(ch, "")
    return text


def _number(value: str, *, field: str) -> Decimal:
    text = str(value).strip().replace(",", "").replace("，", "")
    if text == "":
        raise ValueError(f"{field}为空")
    try:
        number = Decimal(text)
    except Exception as exc:  # noqa: BLE001 - Decimal 抛 InvalidOperation
        raise ValueError(f"{field}不是数字：{value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"{field}不是有限数字：{value!r}")
    if number < 0:
        raise ValueError(f"{field}不能为负数：{value!r}")
    return q(number)


@dataclass(frozen=True)
class MeterRow:
    start_utc: str
    available: Decimal
    actual: Decimal
    line: int
    turbine: Optional[str] = None


@dataclass
class ParsedMeter:
    rows: list[MeterRow]
    turbine: Optional[str]
    warnings: list[str]


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 编码无法识别，请使用 UTF-8 或 GB18030")


def parse_meter_csv(
    data: bytes, *, default_turbine: Optional[str] = None
) -> ParsedMeter:
    """解析 15 分钟计量表。

    支持带表头（中英文列名）或无表头（时刻、可发量、实发量）的 CSV。
    未写时区的时刻按 UTC 处理；带时区的会换算到 UTC。
    """
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    raw_rows = [
        (idx, row) for idx, row in enumerate(reader, start=1) if any(c.strip() for c in row)
    ]
    if not raw_rows:
        raise ValueError("CSV 为空")

    first = raw_rows[0][1]
    header_map: dict[str, int] = {}
    normalized = [_norm_header(cell) for cell in first]

    def match_header() -> bool:
        for index, cell in enumerate(normalized):
            for key, aliases in _HEADER_ALIASES.items():
                if cell in aliases and key not in header_map:
                    header_map[key] = index
        return "time" in header_map and "available" in header_map and "actual" in header_map

    has_header = match_header()
    if not has_header:
        if len(first) < 3:
            raise ValueError("CSV 需要至少 3 列：时刻、可发量、实发量（或提供表头）")
        header_map = {"time": 0, "available": 1, "actual": 2}
        if len(first) >= 4:
            header_map["turbine"] = 3
        data_rows = raw_rows
    else:
        data_rows = raw_rows[1:]

    if not data_rows:
        raise ValueError("CSV 没有数据行")

    rows: list[MeterRow] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}
    csv_turbines: set[str] = set()

    for line, cells in data_rows:
        def cell(key: str) -> str:
            index = header_map.get(key)
            if index is None or index >= len(cells):
                return ""
            return cells[index]

        try:
            start = parse_slot(cell("time"), field="时刻")
            available = _number(cell("available"), field="可发量")
            actual = _number(cell("actual"), field="实发量")
        except ValueError as exc:
            raise ValueError(f"第 {line} 行：{exc}") from exc

        start_utc = canonical(start)
        if start_utc in seen:
            raise ValueError(f"第 {line} 行与第 {seen[start_utc]} 行时刻重复：{start_utc}")
        seen[start_utc] = line

        if actual > available:
            warnings.append(
                f"第 {line} 行实发量 {actual} 大于可发量 {available}，该格按无损失处理"
            )

        row_turbine = cell("turbine").strip() or None
        if row_turbine:
            csv_turbines.add(row_turbine)

        rows.append(
            MeterRow(
                start_utc=start_utc,
                available=available,
                actual=actual,
                line=line,
                turbine=row_turbine,
            )
        )

    if len(csv_turbines) > 1:
        raise ValueError(f"一个文件只能包含一台风机，检测到：{', '.join(sorted(csv_turbines))}")
    csv_turbine = next(iter(csv_turbines), None)
    if csv_turbine is not None and "/" in csv_turbine:
        raise ValueError(f"风机编号不能包含斜杠：{csv_turbine!r}")

    form_turbine = (default_turbine or "").strip() or None
    if form_turbine and csv_turbine and form_turbine != csv_turbine:
        raise ValueError(f"表单风机 {form_turbine!r} 与 CSV 风机 {csv_turbine!r} 不一致")
    turbine = form_turbine or csv_turbine
    if not turbine:
        raise ValueError("请填写风机编号（或在 CSV 中提供风机列）")

    rows.sort(key=lambda row: row.start_utc)
    return ParsedMeter(rows=rows, turbine=turbine, warnings=warnings)


def expected_slots(start: datetime, end: datetime) -> int:
    total = end - start
    if total.total_seconds() <= 0:
        raise ValueError("结束时刻必须晚于开始时刻")
    if total % SLOT != timedelta(0):
        raise ValueError("起止时刻跨度必须是 15 分钟的整数倍")
    return int(total.total_seconds() // SLOT.total_seconds())

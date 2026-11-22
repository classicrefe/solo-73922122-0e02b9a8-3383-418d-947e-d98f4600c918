"""CSV 解析测试。"""

from decimal import Decimal

import pytest

from curtailment.parsing import parse_meter_csv, parse_slot, parse_utc


def test_header_chinese():
    data = (
        "时刻,可发量,实发量\n"
        "2026-01-01T00:00:00Z,10,4\n"
        "2026-01-01T00:15:00Z,12,4\n"
    ).encode("utf-8")
    parsed = parse_meter_csv(data, default_turbine="WT-1")
    assert parsed.turbine == "WT-1"
    assert len(parsed.rows) == 2
    assert parsed.rows[0].available == Decimal("10.000")
    assert parsed.rows[0].start_utc == "2026-01-01T00:00:00Z"


def test_header_english_and_bom():
    data = (
        "﻿time,available,actual,turbine\n"
        "2026-01-01T00:00:00Z,10,4,WT-9\n"
    ).encode("utf-8")
    parsed = parse_meter_csv(data)
    assert parsed.turbine == "WT-9"
    assert parsed.rows[0].actual == Decimal("4.000")


def test_headerless():
    data = b"2026-01-01T00:00:00Z,10,4\n2026-01-01T00:15:00Z,9,5\n"
    parsed = parse_meter_csv(data, default_turbine="WT-1")
    assert [row.start_utc for row in parsed.rows] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:15:00Z",
    ]


def test_naive_time_is_utc():
    data = b"2026-01-01 00:00,10,4\n"
    parsed = parse_meter_csv(data, default_turbine="WT-1")
    assert parsed.rows[0].start_utc == "2026-01-01T00:00:00Z"


def test_offset_converted_to_utc():
    data = b"2026-01-01T08:00:00+08:00,10,4\n"
    parsed = parse_meter_csv(data, default_turbine="WT-1")
    assert parsed.rows[0].start_utc == "2026-01-01T00:00:00Z"


def test_unsorted_input_sorted():
    data = b"2026-01-01T00:15:00Z,9,5\n2026-01-01T00:00:00Z,10,4\n"
    parsed = parse_meter_csv(data, default_turbine="WT-1")
    assert parsed.rows[0].start_utc == "2026-01-01T00:00:00Z"


def test_duplicate_slot_rejected():
    data = b"2026-01-01T00:00:00Z,10,4\n2026-01-01T00:00:00Z,9,5\n"
    with pytest.raises(ValueError, match="重复"):
        parse_meter_csv(data, default_turbine="WT-1")


def test_not_aligned_rejected():
    data = b"2026-01-01T00:07:00Z,10,4\n"
    with pytest.raises(ValueError, match="15 分钟"):
        parse_meter_csv(data, default_turbine="WT-1")


def test_negative_rejected():
    data = b"2026-01-01T00:00:00Z,-1,4\n"
    with pytest.raises(ValueError, match="负数"):
        parse_meter_csv(data, default_turbine="WT-1")


def test_multiple_turbines_rejected():
    data = b"turbine,time,available,actual\nWT-1,2026-01-01T00:00:00Z,10,4\nWT-2,2026-01-01T00:15:00Z,9,5\n"
    with pytest.raises(ValueError, match="一台风机"):
        parse_meter_csv(data, default_turbine="WT-1")


def test_turbine_mismatch_rejected():
    data = b"time,available,actual,turbine\n2026-01-01T00:00:00Z,10,4,WT-2\n"
    with pytest.raises(ValueError, match="不一致"):
        parse_meter_csv(data, default_turbine="WT-1")


def test_missing_turbine_rejected():
    data = b"2026-01-01T00:00:00Z,10,4\n"
    with pytest.raises(ValueError, match="风机编号"):
        parse_meter_csv(data)


def test_actual_above_available_warns():
    data = b"2026-01-01T00:00:00Z,4,5\n"
    parsed = parse_meter_csv(data, default_turbine="WT-1")
    assert parsed.warnings


def test_parse_utc_z():
    assert parse_utc("2026-01-01T00:00:00Z").utcoffset().total_seconds() == 0
    assert parse_slot("2026-01-01T00:15:00Z").minute == 15

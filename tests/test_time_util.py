"""time_util 纯逻辑单测：时区、日照时段、本地时间端点与日历工具。"""

from __future__ import annotations

from datetime import datetime, timezone

from AI聊天陪伴.services.narrative import time_util as t


def test_canonical_timezone_valid_and_invalid():
    assert t.canonical_timezone("Asia/Shanghai") == "Asia/Shanghai"
    assert t.canonical_timezone("UTC") == "UTC"
    assert t.canonical_timezone("Not/AZone") == t.DEFAULT_TIMEZONE
    assert t.canonical_timezone("") == t.DEFAULT_TIMEZONE


def test_period_boundaries():
    # 绝对断言：Shanghai = UTC+8，本地中午对应 UTC 04:00。
    assert t.period(datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc), "Asia/Shanghai") == "afternoon"
    assert t.period(datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc), "Asia/Shanghai") == "night"
    assert t.period(datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc), "Asia/Shanghai") == "morning"


def test_date_key_round_trip():
    assert t.date_key("2026-01-05") == "2026-01-05"
    assert t.date_key("2026-02-30") is None
    assert t.date_key("bad") is None
    assert t.date_key(None) is None
    assert t.date_key("2026-1-5") is None


def test_add_date_and_difference():
    assert t.add_date("2026-01-05", 3) == "2026-01-08"
    assert t.add_date("2026-01-05", -1) == "2026-01-04"
    assert t.date_difference("2026-01-05", "2026-01-08") == 3


def test_weekday_cl_matches_js_semantics():
    # 2026-01-05 是周一。JS getUTCDay(): 周一 -> 1 -> "Monday"。
    assert t.weekday_cl("2026-01-05") == "Monday"
    # 2026-01-04 是周日 -> "Sunday"。
    assert t.weekday_cl("2026-01-04") == "Sunday"


def test_local_endpoint_fields():
    value = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    ctx = t.local_endpoint(value, "Asia/Shanghai")
    assert ctx["date"] == "2026-01-01"
    assert ctx["weekday_en"] in t._WEEKDAYS_EN
    assert ctx["offset"].startswith("GMT+")
    assert ctx["period"] in ("morning", "afternoon", "evening", "night")
    assert "daylightExpectation" in ctx
    # UTC 04:00 = 上海本地 12:00。
    assert ctx["hour"] == 12


def test_iso_round_trip():
    value = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    text = t.iso(value)
    assert text.endswith("Z")
    assert t.parse_iso(text) == value
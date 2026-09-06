"""Schedule Preplan 日程预规划单测。"""

from __future__ import annotations

from datetime import datetime, timezone

from AI聊天陪伴.services.narrative import schedule as s


def test_resolve_config_clamps():
    config = s.resolve_schedule_preplan_config({"horizonDays": 99, "reviewAfterLocalHour": -5, "variationLevel": "weird"})
    assert config["horizonDays"] == 30
    assert config["reviewAfterLocalHour"] == 0
    assert config["variationLevel"] == "stable"


def test_normalize_record_round_trip():
    record = s.normalize_schedule_preplan_record(
        {
            "storyId": "story:1",
            "validFrom": "2026-01-01",
            "validThrough": "2026-01-14",
            "timezone": "Asia/Shanghai",
            "regimes": [],
            "exceptions": [],
            "materializedDays": [],
        }
    )
    assert record is not None
    assert record["storyId"] == "story:1"
    assert record["validFrom"] == "2026-01-01"


def _regime():
    return {
        "id": "school",
        "label": "上课",
        "from": "2026-01-01",
        "weekly": {
            "monday": [
                {"id": "class", "start": "09:00", "end": "12:00", "label": "上课", "kind": "fixed"}
            ]
        },
        "sourceEntryIds": [1],
    }


def test_materialize_includes_weekday_blocks():
    # 2026-01-05 是周一。
    days = s.materialize_schedule_preplan([_regime()], [], "2026-01-05", 7)
    assert len(days) == 7
    monday = days[0]
    assert monday["date"] == "2026-01-05"
    assert any(item["id"] == "class" for item in monday["blocks"])
    # 非周一不应有该 block。
    tuesday = days[1]
    assert all(item["id"] != "class" for item in tuesday["blocks"])


def test_materialize_exception_replace():
    exception = {
        "date": "2026-01-05",
        "mode": "replace",
        "reason": "放假",
        "blocks": [{"id": "trip", "start": "10:00", "end": "11:00", "label": "出行", "kind": "routine"}],
    }
    days = s.materialize_schedule_preplan([_regime()], [exception], "2026-01-05", 1)
    blocks = days[0]["blocks"]
    assert any(item["id"] == "trip" for item in blocks)
    assert all(item["id"] != "class" for item in blocks)


def test_window_returns_none_without_record():
    assert s.schedule_preplan_window(None, datetime(2026, 1, 5, tzinfo=timezone.utc), "Asia/Shanghai") is None


def test_next_transition_none_empty_record():
    assert s.next_schedule_preplan_transition(None, datetime(2026, 1, 5, tzinfo=timezone.utc), "Asia/Shanghai") is None
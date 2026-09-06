"""Agency Window 主体行动窗口单测。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from AI聊天陪伴.services.narrative import agency as g


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return g.iso(value)


def test_normalize_agency_window_state_valid():
    value = {
        "activityLoad": "free",
        "privacy": "private",
        "deviceAccess": "available",
        "validUntil": _iso(_now() + timedelta(hours=1)),
        "updatedAt": _iso(_now()),
        "basis": "刚刚有空",
        "sourceEntryIds": [1, 2],
    }
    state = g.normalize_agency_window_state(value)
    assert state is not None
    assert state["activityLoad"] == "free"
    assert state["sourceEntryIds"] == [1, 2]


def test_normalize_agency_window_state_invalid_enum():
    value = {
        "activityLoad": "nope",
        "privacy": "private",
        "deviceAccess": "available",
        "validUntil": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    assert g.normalize_agency_window_state(value) is None


def test_normalize_agency_window_draft_clamps_valid_until():
    config = g.resolve_agency_config()
    value = {
        "activityLoad": "free",
        "privacy": "private",
        "deviceAccess": "available",
        "validUntil": _iso(_now() + timedelta(days=1)),
        "basis": "basis",
        "sourceEntryIds": [1],
    }
    draft = g.normalize_agency_window_draft(value, _now(), config, {1})
    assert draft is not None
    max_until = _now() + timedelta(minutes=config["maxWindowMinutes"])
    assert g._to_datetime(draft["validUntil"]) <= max_until


def test_evaluate_agency_capacity_device_unavailable():
    config = g.resolve_agency_config()
    window = {
        "activityLoad": "free",
        "privacy": "private",
        "deviceAccess": "unavailable",
        "validUntil": _iso(_now() + timedelta(hours=1)),
        "basis": "b",
        "sourceEntryIds": [1],
        "updatedAt": _iso(_now()),
    }
    candidate = {
        "participantId": "p",
        "origin": "life-event",
        "disclosure": "ordinary",
        "sourceEntryIds": [1],
        "motive": "m",
    }
    result = g.evaluate_agency_capacity(window, candidate, _now(), config)
    assert result["allowed"] is False
    assert result["reason"] == "device-unavailable"


def test_evaluate_agency_capacity_available():
    config = g.resolve_agency_config()
    window = {
        "activityLoad": "free",
        "privacy": "private",
        "deviceAccess": "available",
        "validUntil": _iso(_now() + timedelta(hours=1)),
        "basis": "b",
        "sourceEntryIds": [1],
        "updatedAt": _iso(_now()),
    }
    candidate = {
        "participantId": "p",
        "origin": "life-event",
        "disclosure": "ordinary",
        "sourceEntryIds": [1],
        "motive": "m",
    }
    result = g.evaluate_agency_capacity(window, candidate, _now(), config)
    assert result["allowed"] is True


def test_proactive_candidate_fingerprint_deterministic():
    candidate = {"participantId": "p1", "origin": "promise", "sourceEntryIds": [2, 1]}
    assert g.proactive_candidate_fingerprint(candidate) == "p1|promise|1,2"
"""Alter System 情绪偏移状态机单测。"""

from __future__ import annotations

from datetime import datetime, timezone

from AI聊天陪伴.services.narrative import alter as a


def _now() -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_normalize_alter_value_clamps():
    assert a.normalize_alter_value(3.7) == 4
    assert a.normalize_alter_value(-5) == -5
    assert a.normalize_alter_value(99) == 5
    assert a.normalize_alter_value(-99) == -5
    assert a.normalize_alter_value(True) is None
    assert a.normalize_alter_value("3") is None
    assert a.normalize_alter_value(float("inf")) is None


def test_create_state_defaults():
    state = a.create_alter_system_state(_now())
    assert state["alterValue"] == 0
    assert state["alterWeight"] == 0
    assert state["history"] == []
    assert state["emotionalOffset"] is None


def test_advance_accumulates_and_threshold():
    config = dict(a.DEFAULT_ALTER_SYSTEM_CONFIG)
    config["baseThreshold"] = 10
    first = a.advance_alter_system(None, 3, "user-message", _now(), config)
    assert first["state"]["alterValue"] == 3
    assert first["thresholdReached"] is False

    second = a.advance_alter_system(first["state"], 8, "advance", _now(), config)
    assert second["state"]["alterValue"] == 11
    assert second["thresholdReached"] is True
    assert second["state"]["history"][-1]["turn"] == 2


def test_adjust_weight_directions():
    config = dict(a.DEFAULT_ALTER_SYSTEM_CONFIG)
    config["sameDirectionBoost"] = 0.1
    config["oppositeDecay"] = 0.2
    same = a.adjust_alter_weight(0.5, True, 2, config)
    opposite = a.adjust_alter_weight(0.5, False, 2, config)
    assert same > 0.5
    assert opposite < 0.5


def test_complete_analysis_resets_value():
    state = dict(a.create_alter_system_state(_now()))
    state["alterValue"] = 9
    result = a.complete_alter_analysis(state, "气氛转向严肃", 10, _now(), a.DEFAULT_ALTER_SYSTEM_CONFIG)
    assert result["alterValue"] == 0
    assert result["alterWeight"] == 1
    assert result["emotionalOffset"]["direction"] == "serious"


def test_emotional_offset_for_prompt_disabled():
    state = dict(a.create_alter_system_state(_now()))
    state["emotionalOffset"] = {"direction": "serious", "description": "x", "intensity": 1, "generatedAt": a.iso(_now())}
    assert a.emotional_offset_for_prompt(state, {"enabled": False}) is None
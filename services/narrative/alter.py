"""Alter System：情绪偏移累积、动态阈值与权重投影。

对应 HDS Interlude 的 ``src/alter.ts``。纯逻辑，不依赖框架；模型分析（生成
``emotionalOffset`` 文字描述）发生在 narrator 层，这里只负责状态机与阈值计算。
"""

from __future__ import annotations

import math
from typing import Any

from .time_util import valid_date, iso

HOUR_MS = 60 * 60 * 1000
HISTORY_LIMIT = 50
PHASES = ("advance", "conversation-follow-up", "user-message", "intent-due")

DEFAULT_ALTER_SYSTEM_CONFIG: dict[str, Any] = {
    "enabled": False,
    "baseThreshold": 10,
    "densityFactor": 0.3,
    "sameDirectionBoost": 0.05,
    "oppositeDecay": 0.15,
    "minWeight": 0.2,
    "maxIntensity": 2,
    "modelId": "",
    "providerId": "",
    "model": "",
    "temperature": 0.3,
    "topP": 1,
    "maxTokens": 400,
    "timeout": 30_000,
    "prompt": "",
}


def resolve_alter_system_config(value: dict | None = None) -> dict:
    config = dict(DEFAULT_ALTER_SYSTEM_CONFIG)
    if value:
        config.update(value)
    return config


def normalize_alter_value(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return max(-5, min(5, round(float(value))))


def create_alter_system_state(now=None) -> dict:
    timestamp = iso(now) if now else iso(_now())
    return {
        "alterValue": 0,
        "alterWeight": 0,
        "lastTriggerDirection": 0,
        "emotionalOffset": None,
        "history": [],
        "lastUpdatedAt": timestamp,
    }


def normalize_alter_system_state(value: Any) -> dict | None:
    if not _is_record(value):
        return None
    history: list[dict] = []
    raw_history = value.get("history")
    if isinstance(raw_history, list):
        for index, entry in enumerate(raw_history):
            if not _is_record(entry):
                continue
            alter_value = normalize_alter_value(entry.get("alter"))
            history.append({
                "turn": max(1, math.floor(_finite_number(entry.get("turn"), index + 1))),
                "phase": _normalize_phase(entry.get("phase")),
                "alter": alter_value if alter_value is not None else 0,
                "alterValue": _clamp(_finite_number(entry.get("alterValue"), 0), -1000, 1000),
                "timestamp": _normalized_iso(entry.get("timestamp")) or iso(_epoch()),
            })
    history = history[-HISTORY_LIMIT:]

    emotional_offset = None
    raw_offset = value.get("emotionalOffset")
    if _is_record(raw_offset) and isinstance(raw_offset.get("description"), str):
        emotional_offset = {
            "direction": "relaxed" if raw_offset.get("direction") == "relaxed" else "serious",
            "description": raw_offset["description"].strip()[:800],
            "intensity": _clamp(_finite_number(raw_offset.get("intensity"), 1), 0, 3),
            "generatedAt": _normalized_iso(raw_offset.get("generatedAt")) or iso(_epoch()),
        }

    legacy_direction = _sign(_finite_number(value.get("lastTriggerAlter"), 0))
    direction = _sign(_finite_number(value.get("lastTriggerDirection"), legacy_direction))

    return {
        "alterValue": _clamp(_finite_number(value.get("alterValue"), 0), -1000, 1000),
        "alterWeight": _clamp(_finite_number(value.get("alterWeight"), 0), 0, 1),
        "lastTriggerDirection": direction,
        "emotionalOffset": emotional_offset,
        "history": history,
        "lastUpdatedAt": _normalized_iso(value.get("lastUpdatedAt")) or iso(_epoch()),
        "lastAnalysisAttemptAt": _normalized_iso(value.get("lastAnalysisAttemptAt")),
    }


def calculate_alter_threshold(history: list[dict], config: dict, now=None) -> float:
    now_ms = _time_ms(now)
    one_hour_ago = now_ms - HOUR_MS
    turns = sum(
        1 for entry in history if _entry_ms(entry.get("timestamp")) >= one_hour_ago
    )
    density = min(turns / 10, 1)
    base = max(1, _finite_number(config.get("baseThreshold"), 10))
    factor = _clamp(_finite_number(config.get("densityFactor"), 0.3), 0, 1)
    return max(base * 0.5, base * (1 - density * factor))


def adjust_alter_weight(weight: float, same_direction: bool, magnitude: float, config: dict) -> float:
    rate = config.get("sameDirectionBoost") if same_direction else -config.get("oppositeDecay")
    return _clamp(weight + max(0, magnitude) * _finite_number(rate, 0), 0, 1)


def advance_alter_system(
    current: dict | None,
    alter: int,
    phase: str,
    now,
    config: dict,
) -> dict:
    """把一次叙事回合的情绪偏移计入状态机，返回 ``{state, threshold, offsetExpired, thresholdReached}``。"""
    if current:
        state = dict(current)
        state["history"] = list(current.get("history", []))
    else:
        state = create_alter_system_state(now)

    state["alterValue"] = _clamp(state.get("alterValue", 0) + alter, -1000, 1000)
    direction = _sign(alter)
    offset_expired = False
    if state.get("emotionalOffset") and direction:
        state["alterWeight"] = adjust_alter_weight(
            state.get("alterWeight", 0),
            direction == state.get("lastTriggerDirection", 0),
            abs(alter),
            config,
        )
        if state["alterWeight"] < config.get("minWeight", 0.2):
            state["emotionalOffset"] = None
            state["alterWeight"] = 0
            offset_expired = True

    previous_turn = state["history"][-1]["turn"] if state["history"] else 0
    state["history"].append({
        "turn": previous_turn + 1,
        "phase": phase,
        "alter": alter,
        "alterValue": state["alterValue"],
        "timestamp": iso(now),
    })
    state["history"] = state["history"][-HISTORY_LIMIT:]
    state["lastUpdatedAt"] = iso(now)

    threshold = calculate_alter_threshold(state["history"], config, now)
    return {
        "state": state,
        "threshold": threshold,
        "offsetExpired": offset_expired,
        "thresholdReached": abs(state["alterValue"]) >= threshold,
    }


def complete_alter_analysis(
    state: dict,
    description: str,
    threshold: float,
    now,
    config: dict,
) -> dict:
    trigger_value = state.get("alterValue", 0)
    direction = _sign(trigger_value)
    result = dict(state)
    result["alterValue"] = 0
    result["alterWeight"] = 1
    result["lastTriggerDirection"] = direction
    result["emotionalOffset"] = {
        "direction": "serious" if direction > 0 else "relaxed",
        "description": description.strip()[:800],
        "intensity": min(abs(trigger_value) / max(1, threshold), config.get("maxIntensity", 2)),
        "generatedAt": iso(now),
    }
    result["lastUpdatedAt"] = iso(now)
    return result


def emotional_offset_for_prompt(state: dict | None, config: dict) -> dict | None:
    if not config.get("enabled") or not state or not state.get("emotionalOffset"):
        return None
    if state.get("alterWeight", 0) < config.get("minWeight", 0.2):
        return None
    return {**state["emotionalOffset"], "weight": state.get("alterWeight", 0)}


def alter_analysis_cooling_down(state: dict, now=None, cooldown_ms: int = 5 * 60 * 1000) -> bool:
    last_attempt = valid_date(state.get("lastAnalysisAttemptAt")) if state else None
    return bool(last_attempt and _time_ms(now) - last_attempt.timestamp() * 1000 < cooldown_ms)


# --------------------------------------------------------------------------- helpers

def _normalize_phase(value: Any) -> str:
    return str(value) if str(value) in PHASES else "user-message"


def _normalized_iso(value: Any) -> str | None:
    parsed = valid_date(value)
    return iso(parsed) if parsed else None


def _entry_ms(value: Any) -> int:
    parsed = valid_date(value)
    return int(parsed.timestamp() * 1000) if parsed else 0


def _time_ms(value) -> int:
    if value is None:
        return _time_ms(_now())
    if isinstance(value, (int, float)):
        return int(value)
    parsed = valid_date(value)
    return int(parsed.timestamp() * 1000) if parsed else _time_ms(_now())


def _finite_number(value: Any, fallback: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else fallback


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sign(value: float) -> int:
    return 1 if value > 0 else (-1 if value < 0 else 0)


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _epoch():
    from datetime import datetime, timezone as _tz
    return datetime(1970, 1, 1, tzinfo=_tz.utc)


def _now():
    from .time_util import utc_now
    return utc_now()
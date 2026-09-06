"""群聊意愿层：一个刻意保持小型、无模型的意愿评分与概率决策。

对应 HDS Interlude 的 ``src/group-willingness.ts``。只作用于单个 HDSI 群聊，
绝不影响私聊回合、Agency Window、Alter、提示词或持久化故事状态。
"""

from __future__ import annotations

import random
from typing import Any

DEFAULT_GROUP_WILLINGNESS: dict[str, Any] = {
    "enabled": False,
    "maxScore": 1,
    "threshold": 0.24,
    "probabilityAmplifier": 1.3,
    "decayHalfLifeSeconds": 180,
    "replyCost": 0.55,
    "baseGain": 0.12,
    "quoteGain": 0.12,
    "keywordGain": 0.18,
    "keywords": [],
}


def resolve_group_willingness(config: dict | None = None) -> dict:
    merged = dict(DEFAULT_GROUP_WILLINGNESS)
    if config:
        merged.update(config)
    keywords = merged.get("keywords") or DEFAULT_GROUP_WILLINGNESS["keywords"]
    merged["keywords"] = [
        str(item).strip() for item in keywords if str(item).strip()
    ][:30]
    return merged


def evaluate_group_willingness(
    previous: dict | None,
    config_input: dict | None,
    input_data: dict,
) -> dict:
    """返回 ``{state, shouldCall, probability, reason}``。"""
    config = resolve_group_willingness(config_input)
    now = input_data.get("now") or 0
    state = _decay(previous, config, now)

    if not config["enabled"]:
        return {"state": state, "shouldCall": True, "probability": 1, "reason": "disabled"}

    content = str(input_data.get("content") or "")
    keyword_hit = any(keyword in content for keyword in config["keywords"])
    raw_gain = config["baseGain"] * max(1, min(3, input_data.get("messageCount") or 0)) \
        + (config["quoteGain"] if input_data.get("quotedBot") else 0) \
        + (config["keywordGain"] if keyword_hit else 0)
    marginal = 1 - min(1, state["score"] / max(config["maxScore"], 1e-9)) ** 2
    state["score"] = _clamp(state["score"] + raw_gain * max(0, marginal), 0, config["maxScore"])

    if input_data.get("mentionedBot"):
        return {"state": state, "shouldCall": True, "probability": 1, "reason": "forced-mention"}
    if state["score"] <= config["threshold"]:
        return {"state": state, "shouldCall": False, "probability": 0, "reason": "below-threshold"}

    probability = _clamp((state["score"] - config["threshold"]) * config["probabilityAmplifier"], 0, 1)
    roll = input_data.get("random")
    if roll is None:
        roll = random.random()
    return {
        "state": state,
        "shouldCall": roll < probability,
        "probability": probability,
        "reason": "probability-roll",
    }


def consume_group_willingness(
    previous: dict | None,
    config_input: dict | None,
    now: float,
) -> dict:
    config = resolve_group_willingness(config_input)
    state = _decay(previous, config, now)
    return {"score": max(0, state["score"] - config["replyCost"]), "updatedAt": now}


def _decay(previous: dict | None, config: dict, now: float) -> dict:
    score = previous.get("score", 0) if previous else 0
    elapsed_seconds = max(0, now - (previous.get("updatedAt", now) if previous else now)) / 1000
    factor = 0.5 ** (elapsed_seconds / max(1, config["decayHalfLifeSeconds"]))
    decayed = score * factor
    return {"score": 0 if decayed < 0.001 else decayed, "updatedAt": now}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
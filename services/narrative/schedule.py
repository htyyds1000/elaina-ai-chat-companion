"""Schedule Preplan：故事角色的日程预规划（recurring regimes + exceptions）。

对应 HDS Interlude 的 ``src/schedule-preplan.ts``。模型生成 regimes/exceptions
提案，本模块负责归一化、合并、物化（materialize）未来 N 天的日程，并投影出
进入主提示词的「未来 12 小时」窗口。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .time_util import (
    add_date,
    calendar_day_key,
    date_difference,
    date_key,
    local_clock_minutes,
    local_endpoint,
    weekday_cl,
    valid_date,
)

WEEKDAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
KINDS = ["fixed", "routine", "flexible", "open"]
PRIORITY = {"fixed": 4, "routine": 3, "flexible": 2, "open": 1}

DEFAULT_SCHEDULE_PREPLAN_CONFIG: dict[str, Any] = {
    "enabled": True,
    "horizonDays": 14,
    "reviewAfterLocalHour": 3,
    "anchorAutoAdvance": True,
    "variationLevel": "stable",
    "candidateActivationProbability": 0.25,
    "candidateRevealMinutes": 120,
}

_TIME_KEY_RE = re.compile(r"^(\d{2}):(\d{2})$")


def resolve_schedule_preplan_config(value: dict | None = None) -> dict:
    value = value or {}
    variation = value.get("variationLevel")
    variation = variation if variation in ("stable", "contextual", "granular") else "stable"
    return {
        "enabled": value.get("enabled") is not False,
        "horizonDays": _clamp_int(value.get("horizonDays"), 3, 30, 14),
        "reviewAfterLocalHour": _clamp_int(value.get("reviewAfterLocalHour"), 0, 23, 3),
        "anchorAutoAdvance": value.get("anchorAutoAdvance") is not False,
        "variationLevel": variation,
        "candidateActivationProbability": _clamp_number(value.get("candidateActivationProbability"), 0.05, 0.5, 0.25),
        "candidateRevealMinutes": _clamp_int(value.get("candidateRevealMinutes"), 15, 360, 120),
    }


def normalize_schedule_preplan_record(value: Any) -> dict | None:
    if not _is_record(value) or not isinstance(value.get("storyId"), str):
        return None
    valid_from = date_key(value.get("validFrom"))
    valid_through = date_key(value.get("validThrough"))
    if not valid_from or not valid_through:
        return None
    created_at = valid_date(value.get("createdAt")) or _epoch()
    updated_at = valid_date(value.get("updatedAt")) or _epoch()
    return {
        "storyId": value["storyId"],
        "revision": _clamp_int(value.get("revision"), 0, 1_000_000, 0),
        "timezone": _text(value.get("timezone"), 127) or "UTC",
        "validFrom": valid_from,
        "validThrough": valid_through,
        "lastReviewedLocalDate": date_key(value.get("lastReviewedLocalDate")) or "",
        "lastEvidenceEntryId": _clamp_int(value.get("lastEvidenceEntryId"), 0, 2**53, 0),
        "reviewReason": _text(value.get("reviewReason"), 500),
        "regimes": _normalize_regimes(value.get("regimes")),
        "exceptions": _normalize_exceptions(value.get("exceptions")),
        "materializedDays": _normalize_days(value.get("materializedDays")),
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def schedule_preplan_review_due(record: dict | None, now: datetime, timezone: str, config: dict) -> bool:
    if not config["enabled"]:
        return False
    today = calendar_day_key(now, timezone)
    if not record:
        return True
    if record.get("timezone") != timezone:
        return True
    return record.get("lastReviewedLocalDate") != today and \
        local_clock_minutes(now, timezone) >= config["reviewAfterLocalHour"] * 60


def schedule_preplan_needs_model(
    record: dict | None,
    evidence: list[dict],
    today: str,
    timezone: str,
    config: dict,
) -> bool:
    if not record or record.get("timezone") != timezone:
        return True
    if any(entry.get("id", 0) > record.get("lastEvidenceEntryId", 0) for entry in evidence):
        return True
    if not record.get("regimes"):
        return False
    coverage_target = add_date(today, max(1, config["horizonDays"] - 3))
    if any(
        regime.get("from") <= coverage_target and (not regime.get("to") or regime["to"] >= coverage_target)
        for regime in record.get("regimes", [])
    ):
        return False
    return record.get("validThrough", "") < coverage_target


def refresh_schedule_preplan(
    record: dict,
    today: str,
    timezone: str,
    config: dict,
    now: datetime,
    reason: str = "每日回顾未发现改变日程的证据。",
) -> dict:
    result = dict(record)
    result["timezone"] = timezone
    result["validFrom"] = today
    result["validThrough"] = add_date(today, config["horizonDays"] - 1)
    result["lastReviewedLocalDate"] = today
    result["reviewReason"] = reason
    result["materializedDays"] = materialize_schedule_preplan(
        record.get("regimes", []), record.get("exceptions", []), today, config["horizonDays"]
    )
    result["updatedAt"] = now
    return result


def apply_schedule_preplan_proposal(
    current: dict | None,
    proposal_value: Any,
    evidence: list[dict],
    today: str,
    timezone: str,
    config: dict,
    now: datetime,
    variation_level: str = "stable",
) -> dict | None:
    proposal = _normalize_proposal(proposal_value, {entry.get("id") for entry in evidence}, variation_level)
    if not proposal:
        return refresh_schedule_preplan(current, today, timezone, config, now, "提案无效，保留既有日程。") if current else None
    if proposal["outcome"] == "unchanged" and current:
        result = refresh_schedule_preplan(current, today, timezone, config, now, proposal["reason"])
        result["lastEvidenceEntryId"] = max(current.get("lastEvidenceEntryId", 0), max((entry.get("id", 0) for entry in evidence), default=0))
        return result

    regimes = list(current.get("regimes", [])) if current else []
    exceptions = list(current.get("exceptions", [])) if current else []
    if proposal["outcome"] == "replace" or not current:
        regimes = list(proposal.get("regimes") or [])
        exceptions = list(proposal.get("exceptions") or [])
    else:
        changes = proposal.get("regimes") or []
        regimes = _merge_by(regimes, changes, lambda item: item["id"])
        exception_changes = proposal.get("exceptions") or []
        exceptions = _merge_by(exceptions, exception_changes, lambda item: item["date"])

    if not regimes:
        if not current:
            return {
                "storyId": "", "revision": 1, "timezone": timezone,
                "validFrom": today, "validThrough": add_date(today, config["horizonDays"] - 1),
                "lastReviewedLocalDate": today,
                "lastEvidenceEntryId": max((entry.get("id", 0) for entry in evidence), default=0),
                "reviewReason": proposal["reason"],
                "regimes": [], "exceptions": [], "materializedDays": [],
                "createdAt": now, "updatedAt": now,
            }
        return refresh_schedule_preplan(current, today, timezone, config, now, "空提案忽略，保留既有日程。")

    valid_through = add_date(today, config["horizonDays"] - 1)
    return {
        "storyId": current.get("storyId", "") if current else "",
        "revision": (current.get("revision", 0) if current else 0) + 1,
        "timezone": timezone,
        "validFrom": today,
        "validThrough": valid_through,
        "lastReviewedLocalDate": today,
        "lastEvidenceEntryId": max(current.get("lastEvidenceEntryId", 0) if current else 0, max((entry.get("id", 0) for entry in evidence), default=0)),
        "reviewReason": proposal["reason"],
        "regimes": regimes[-6:],
        "exceptions": [item for item in exceptions if item["date"] >= add_date(today, -1)][-30:],
        "materializedDays": materialize_schedule_preplan(regimes, exceptions, today, config["horizonDays"]),
        "createdAt": current.get("createdAt", now) if current else now,
        "updatedAt": now,
    }


def materialize_schedule_preplan(
    regimes: list[dict],
    exceptions: list[dict],
    start_date: str,
    horizon_days: int,
) -> list[dict]:
    days = []
    for offset in range(max(1, horizon_days)):
        date = add_date(start_date, offset)
        matching = [
            regime for regime in regimes
            if regime.get("from") <= date and (not regime.get("to") or regime["to"] >= date)
        ]
        matching.sort(key=lambda item: item["from"], reverse=True)
        selected = matching[0] if matching else None
        weekday = weekday_cl(date).lower()
        blocks = [dict(block) for block in (selected.get("weekly", {}).get(weekday) or [])] if selected else []
        exception = next((item for item in exceptions if item["date"] == date), None)
        if exception and exception.get("mode") == "replace":
            blocks = [dict(block) for block in (exception.get("blocks") or [])]
        elif exception:
            removed = set(exception.get("removeBlockIds") or [])
            blocks = [block for block in blocks if block.get("id") not in removed] + \
                [dict(block) for block in (exception.get("blocks") or [])]
        days.append({"date": date, "blocks": _resolve_overlaps(blocks)[:12]})
    return days


def schedule_preplan_window(
    record: dict | None,
    now: datetime,
    timezone: str,
    hours: int = 12,
    config: dict | None = None,
) -> dict | None:
    config = config or DEFAULT_SCHEDULE_PREPLAN_CONFIG
    if not record or record.get("timezone") != timezone:
        return None
    local = local_endpoint(now, timezone)
    today = local["date"]
    start_minute = local["hour"] * 60 + int(local["time"][3:5])
    end_minute = start_minute + max(1, hours) * 60
    blocks: list[dict] = []
    for day in record.get("materializedDays", []):
        day_offset = date_difference(today, day["date"])
        if day_offset < 0 or day_offset > 1:
            continue
        for block in day["blocks"]:
            start = day_offset * 1440 + _time_minutes(block["start"])
            end = day_offset * 1440 + _time_minutes(block["end"])
            if end <= start:
                end += 1440
            if end <= start_minute or start >= end_minute:
                continue
            if block.get("tentative"):
                if not _is_tentative_block_active(record.get("storyId", ""), day["date"], block["id"], config["candidateActivationProbability"]):
                    continue
                minutes_until = start - start_minute
                if minutes_until > config["candidateRevealMinutes"]:
                    blocks.append({**block, "label": "可能的个人安排", "location": None, "date": day["date"], "tentative": True})
                    continue
            blocks.append({**block, "date": day["date"]})
    to_total = end_minute
    to_date = add_date(today, to_total // 1440)
    to_clock = _clock(to_total % 1440)
    return {
        "name": "Schedule Preplan",
        "timezone": timezone,
        "from": f"{today} {_clock(start_minute)}",
        "to": f"{to_date} {to_clock}",
        "plannedNotObserved": True,
        "revision": record.get("revision", 0),
        "blocks": blocks[:8],
    }


def next_schedule_preplan_transition(
    record: dict | None,
    now: datetime,
    timezone: str,
    max_hours: int = 12,
) -> datetime | None:
    window = schedule_preplan_window(record, now, timezone, max_hours)
    if not window:
        return None
    local = local_endpoint(now, timezone)
    current = local["hour"] * 60 + int(local["time"][3:5])
    candidates: list[int] = []
    for block in [item for item in window["blocks"] if item.get("kind") == "fixed"]:
        offset = date_difference(local["date"], block["date"]) * 1440
        start = offset + _time_minutes(block["start"])
        end = offset + _time_minutes(block["end"])
        if end <= start:
            end += 1440
        if start > current:
            candidates.append(start)
        if end > current:
            candidates.append(end)
    if not candidates:
        return None
    next_minute = min(candidates)
    return now + timedelta(minutes=(next_minute - current))


# --------------------------------------------------------------------------- proposal/block 归一化

def _normalize_proposal(value: Any, valid_evidence_ids: set[int] | None, variation_level: str) -> dict | None:
    if not _is_record(value) or str(value.get("outcome")) not in ("unchanged", "extend", "patch", "replace"):
        return None
    outcome = value["outcome"]
    reason = _text(value.get("reason"), 500)
    if not reason:
        return None
    source_entry_ids = [i for i in _ids(value.get("sourceEntryIds")) if valid_evidence_ids is None or i in valid_evidence_ids]
    allow_tentative = variation_level == "granular"
    regimes = _normalize_regimes(value.get("regimes"), valid_evidence_ids, allow_tentative)
    exceptions = _normalize_exceptions(value.get("exceptions"), valid_evidence_ids, allow_tentative)
    if (outcome in ("patch", "replace") and valid_evidence_ids and not source_entry_ids
            and not any(item.get("sourceEntryIds") for item in regimes)
            and not any(item.get("sourceEntryIds") for item in exceptions)):
        return None
    confidence = value.get("confidence")
    return {
        "outcome": outcome,
        "reason": reason,
        "confidence": None if confidence is None else _clamp_number(confidence, 0, 1, 0),
        "sourceEntryIds": source_entry_ids,
        "regimes": regimes,
        "exceptions": exceptions,
    }


def _normalize_regimes(value: Any, valid_evidence_ids: set[int] | None = None, allow_tentative: bool = True) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in (_normalize_regime(v, valid_evidence_ids, allow_tentative) for v in value) if item][:6]


def _normalize_regime(value: Any, valid_evidence_ids: set[int] | None = None, allow_tentative: bool = True) -> dict | None:
    if not _is_record(value) or not _is_record(value.get("weekly")):
        return None
    id_ = _slug(value.get("id"), 80)
    label = _text(value.get("label"), 120)
    from_date = date_key(value.get("from"))
    to_date = date_key(value.get("to"))
    if not id_ or not label or not from_date or (to_date and to_date < from_date):
        return None
    weekly = {}
    for weekday in WEEKDAYS:
        blocks = _normalize_blocks(value.get("weekly", {}).get(weekday), valid_evidence_ids, allow_tentative)
        if blocks:
            weekly[weekday] = blocks
    result = {"id": id_, "label": label, "from": from_date, "weekly": weekly, "sourceEntryIds": _evidence_ids(value.get("sourceEntryIds"), valid_evidence_ids)}
    if to_date:
        result["to"] = to_date
    return result


def _normalize_exceptions(value: Any, valid_evidence_ids: set[int] | None = None, allow_tentative: bool = True) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in (_normalize_exception(v, valid_evidence_ids, allow_tentative) for v in value) if item][:30]


def _normalize_exception(value: Any, valid_evidence_ids: set[int] | None = None, allow_tentative: bool = True) -> dict | None:
    if not _is_record(value):
        return None
    date = date_key(value.get("date"))
    mode = "replace" if value.get("mode") == "replace" else ("patch" if value.get("mode") == "patch" else None)
    reason = _text(value.get("reason"), 300)
    if not date or not mode or not reason:
        return None
    raw_remove = value.get("removeBlockIds")
    remove_block_ids = [_slug(item, 80) for item in raw_remove if _slug(item, 80)][:20] if isinstance(raw_remove, list) else []
    return {
        "date": date,
        "mode": mode,
        "reason": reason,
        "removeBlockIds": remove_block_ids,
        "blocks": _normalize_blocks(value.get("blocks"), valid_evidence_ids, allow_tentative),
        "sourceEntryIds": _evidence_ids(value.get("sourceEntryIds"), valid_evidence_ids),
    }


def _normalize_days(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if _is_record(item) and date_key(item.get("date")):
            result.append({"date": date_key(item["date"]), "blocks": _normalize_blocks(item.get("blocks"))})
    return result[:31]


def _normalize_blocks(value: Any, valid_evidence_ids: set[int] | None = None, allow_tentative: bool = True) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in (_normalize_block(v, valid_evidence_ids, allow_tentative) for v in value) if item][:20]


def _normalize_block(value: Any, valid_evidence_ids: set[int] | None = None, allow_tentative: bool = True) -> dict | None:
    if not _is_record(value):
        return None
    id_ = _slug(value.get("id"), 80)
    start = _time_key(value.get("start"))
    end = _time_key(value.get("end"))
    label = _text(value.get("label"), 160)
    kind = value.get("kind") if value.get("kind") in KINDS else None
    if not id_ or not start or not end or start == end or not label or not kind:
        return None
    location = _text(value.get("location"), 120)
    tentative = allow_tentative and value.get("tentative") is True and kind in ("flexible", "open")
    result = {"id": id_, "start": start, "end": end, "label": label, "kind": kind, "sourceEntryIds": _evidence_ids(value.get("sourceEntryIds"), valid_evidence_ids)}
    if location:
        result["location"] = location
    if tentative:
        result["tentative"] = True
    return result


# --------------------------------------------------------------------------- helpers

def _is_tentative_block_active(story_id: str, date: str, block_id: str, probability: float) -> bool:
    input_s = f"{story_id}|{date}|{block_id}"
    h = 2166136261
    for ch in input_s:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return (h / 0x1_0000_0000) < probability


def _resolve_overlaps(blocks: list[dict]) -> list[dict]:
    ordered = sorted(
        blocks,
        key=lambda item: (-PRIORITY[item["kind"]], _time_minutes(item["start"])),
    )
    chosen: list[dict] = []
    for candidate in ordered:
        start = _time_minutes(candidate["start"])
        end = _time_minutes(candidate["end"])
        if end <= start:
            end += 1440
        overlaps = any(_overlaps(start, end, block) for block in chosen)
        if not overlaps and not any(block.get("id") == candidate.get("id") for block in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda item: _time_minutes(item["start"]))


def _overlaps(start: int, end: int, block: dict) -> bool:
    other_start = _time_minutes(block["start"])
    other_end = _time_minutes(block["end"])
    if other_end <= other_start:
        other_end += 1440
    return start < other_end and end > other_start


def _evidence_ids(value: Any, valid: set[int] | None) -> list[int]:
    normalized = _ids(value)
    return [i for i in normalized if valid is None or i in valid] if valid is not None else normalized


def _ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result[:30]


def _merge_by(current: list[dict], changes: list[dict], key) -> list[dict]:
    merged = {key(item): item for item in current}
    for item in changes:
        merged[key(item)] = item
    return list(merged.values())


def _time_key(value: Any) -> str | None:
    raw = value.strip() if isinstance(value, str) else ""
    match = _TIME_KEY_RE.match(raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return raw


def _time_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _slug(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    slug = re.sub(r"[^\w-]+", "-", value.strip(), flags=re.UNICODE)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:limit]


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\r\n]+", " ", value).strip()[:limit]


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return max(low, min(high, int(number)))


def _clamp_number(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return max(low, min(high, number))


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _epoch() -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc)
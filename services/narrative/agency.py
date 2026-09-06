"""Agency Window：主体的外部行动能力窗口与主动联系决策。

对应 HDS Interlude 的 ``src/agency.ts``。模型负责生成窗口草稿与主动联系候选，
本模块负责合法性校验、容量评估与再检查时刻计算。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .time_util import valid_date, iso

MINUTE = 60_000
HOUR = 60 * MINUTE

ACTIVITY_LOADS = ("free", "occupied", "overloaded")
PRIVACIES = ("private", "shared", "public")
DEVICE_ACCESSES = ("available", "limited", "unavailable")
CONTACT_ORIGINS = ("life-event", "promise", "practical-update", "relationship-follow-up")
DISCLOSURES = ("ordinary", "personal")
OUTCOMES = ("send-now", "recheck-later", "let-go")

DEFAULT_AGENCY_CONFIG: dict[str, Any] = {
    "enabled": True,
    "maxWindowMinutes": 240,
    "minimumProactiveIntervalMinutes": 60,
    "maxCandidateHours": 24,
}


def resolve_agency_config(value: dict | None = None) -> dict:
    config = dict(DEFAULT_AGENCY_CONFIG)
    if value:
        config.update(value)
    return config


def normalize_agency_window_state(value: Any) -> dict | None:
    if not _is_record(value):
        return None
    if str(value.get("activityLoad")) not in ACTIVITY_LOADS:
        return None
    if str(value.get("privacy")) not in PRIVACIES:
        return None
    if str(value.get("deviceAccess")) not in DEVICE_ACCESSES:
        return None
    valid_until = _to_datetime(value.get("validUntil"))
    updated_at = _to_datetime(value.get("updatedAt"))
    if not valid_until or not updated_at:
        return None
    next_opportunity = _to_datetime(value.get("nextOpportunityAt"))
    return {
        "activityLoad": value["activityLoad"],
        "privacy": value["privacy"],
        "deviceAccess": value["deviceAccess"],
        "nextOpportunityAt": iso(next_opportunity) if next_opportunity else None,
        "validUntil": iso(valid_until),
        "basis": _text(value.get("basis"), 500),
        "sourceEntryIds": _positive_ids(value.get("sourceEntryIds"))[-20:],
        "updatedAt": iso(updated_at),
    }


def normalize_agency_window_draft(
    value: Any,
    now: datetime,
    config: dict,
    valid_source_entry_ids: set[int],
    fallback_source_entry_id: int | None = None,
) -> dict | None:
    if not _is_record(value):
        return None
    if str(value.get("activityLoad")) not in ACTIVITY_LOADS:
        return None
    if str(value.get("privacy")) not in PRIVACIES:
        return None
    if str(value.get("deviceAccess")) not in DEVICE_ACCESSES:
        return None
    maximum = now + timedelta(minutes=max(5, config["maxWindowMinutes"]))
    requested_until = _to_datetime(value.get("validUntil"))
    valid_until = requested_until if requested_until and requested_until > now else maximum
    if valid_until > maximum:
        valid_until = maximum
    requested_opportunity = _to_datetime(value.get("nextOpportunityAt"))
    next_opportunity = requested_opportunity if requested_opportunity and requested_opportunity > now else None
    if next_opportunity and next_opportunity > valid_until:
        next_opportunity = valid_until
    source_entry_ids = _grounded_ids(
        value.get("sourceEntryIds"), valid_source_entry_ids, fallback_source_entry_id
    )
    basis = _text(value.get("basis"), 500)
    if not basis or not source_entry_ids:
        return None
    return {
        "activityLoad": value["activityLoad"],
        "privacy": value["privacy"],
        "deviceAccess": value["deviceAccess"],
        "nextOpportunityAt": iso(next_opportunity) if next_opportunity else None,
        "validUntil": iso(valid_until),
        "basis": basis,
        "sourceEntryIds": source_entry_ids,
        "updatedAt": iso(now),
    }


def active_agency_window(value: Any, now: datetime | None = None) -> dict | None:
    if now is None:
        now = _now()
    state = normalize_agency_window_state(value)
    if state and _to_datetime(state["validUntil"]) > now:
        return state
    return None


def normalize_proactive_contact(
    value: Any,
    now: datetime,
    config: dict,
    permitted_participant_ids: set[str],
    valid_source_entry_ids: set[int],
    fallback_source_entry_id: int | None = None,
) -> dict | None:
    if not _is_record(value):
        return None
    if str(value.get("participantId")) not in permitted_participant_ids:
        return None
    if str(value.get("origin")) not in CONTACT_ORIGINS:
        return None
    if str(value.get("disclosure")) not in DISCLOSURES:
        return None
    if str(value.get("outcome")) not in OUTCOMES:
        return None
    motive = _text(value.get("motive"), 600)
    source_entry_ids = _grounded_ids(
        value.get("sourceEntryIds"), valid_source_entry_ids, fallback_source_entry_id
    )
    if not motive or not source_entry_ids:
        return None
    maximum_expiry = now + timedelta(hours=max(1, config["maxCandidateHours"]))
    requested_expiry = _to_datetime(value.get("expiresAt"))
    expires_at = requested_expiry if requested_expiry and requested_expiry > now else maximum_expiry
    if expires_at > maximum_expiry:
        expires_at = maximum_expiry
    requested_not_before = _to_datetime(value.get("notBefore"))
    not_before = iso(requested_not_before) if requested_not_before and requested_not_before > now and requested_not_before < expires_at else None
    willingness = value.get("willingness")
    return {
        "participantId": str(value["participantId"]),
        "origin": value["origin"],
        "motive": motive,
        "disclosure": value["disclosure"],
        "sourceEntryIds": source_entry_ids,
        "willingness": None if willingness is None else _clamp(float(willingness), 0, 1),
        "outcome": value["outcome"],
        "notBefore": not_before,
        "expiresAt": iso(expires_at),
    }


def evaluate_agency_capacity(
    window: dict | None,
    candidate: dict,
    now: datetime,
    config: dict,
    last_character_message_at: str | None = None,
) -> dict:
    if not window or _to_datetime(window["validUntil"]) <= now:
        return {"allowed": False, "reason": "agency-window-missing-or-expired"}
    next_opportunity_at = _future_datetime(window.get("nextOpportunityAt"), now)
    if window["deviceAccess"] == "unavailable":
        return {"allowed": False, "reason": "device-unavailable", "nextOpportunityAt": next_opportunity_at}
    if window["deviceAccess"] == "limited":
        return {"allowed": False, "reason": "device-limited", "nextOpportunityAt": next_opportunity_at}
    if window["activityLoad"] == "overloaded":
        return {"allowed": False, "reason": "schedule-overloaded", "nextOpportunityAt": next_opportunity_at}
    if candidate["disclosure"] == "personal" and window["privacy"] != "private":
        return {"allowed": False, "reason": "privacy-insufficient", "nextOpportunityAt": next_opportunity_at}

    last_contact = _to_datetime(last_character_message_at) if last_character_message_at else None
    minimum_interval = max(0, config["minimumProactiveIntervalMinutes"]) * MINUTE
    if candidate["origin"] != "promise" and last_contact and _time_ms(now) - _time_ms(last_contact) < minimum_interval:
        return {
            "allowed": False,
            "reason": "minimum-proactive-interval",
            "nextOpportunityAt": last_contact + timedelta(milliseconds=minimum_interval),
        }
    if window["activityLoad"] == "occupied" and candidate["origin"] not in ("promise", "practical-update"):
        return {"allowed": False, "reason": "schedule-occupied", "nextOpportunityAt": next_opportunity_at}
    return {"allowed": True, "reason": "capacity-available"}


def proactive_candidate_fingerprint(candidate: dict) -> str:
    return "|".join([
        candidate.get("participantId", ""),
        candidate.get("origin", ""),
        ",".join(str(i) for i in sorted(candidate.get("sourceEntryIds") or [])),
    ])


def proactive_recheck_at(
    candidate: dict,
    capacity: dict,
    window: dict,
    now: datetime,
) -> datetime:
    requested = _to_datetime(candidate.get("notBefore"))
    capacity_time = _future_datetime(capacity.get("nextOpportunityAt"), now)
    window_time = _future_datetime(window.get("nextOpportunityAt"), now)
    fallback = now + timedelta(minutes=30)
    candidates = [value for value in (requested, capacity_time, window_time) if value and value > now]
    selected = min(candidates) if candidates else fallback
    expiry = _to_datetime(candidate.get("expiresAt")) or (now + timedelta(hours=1))
    return min(selected, expiry)


def proactive_origin_bypasses_ordinary_interval(origin: str) -> bool:
    return origin == "promise"


# --------------------------------------------------------------------------- helpers

def _grounded_ids(value: Any, valid: set[int], fallback: int | None = None) -> list[int]:
    ids = [i for i in _positive_ids(value) if i in valid]
    if not ids and fallback and fallback > 0:
        ids.append(fallback)
    return list(dict.fromkeys(ids))[-20:]


def _positive_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.append(number)
    return result


def _future_datetime(value: Any, now: datetime) -> datetime | None:
    parsed = _to_datetime(value)
    return parsed if parsed and parsed > now else None


def _to_datetime(value: Any) -> datetime | None:
    return valid_date(value)


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _time_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _now() -> datetime:
    from .time_util import utc_now
    return utc_now()
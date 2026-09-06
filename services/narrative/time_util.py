"""时区校验、本地时间权威端点与日照时段。

对应 HDS Interlude 的 ``src/time.ts``：主叙事每次请求都会携带 UTC 与故事本地时间，
并以本地日期、时分秒、星期、时段与日照预期作为权威端点。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"
_UTC = timezone.utc
_DATE_KEY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_WEEKDAYS_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_WEEKDAYS_EN = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]
_WEEKDAYS_CL = [
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
]

_PERIOD_ZH = {"morning": "上午", "afternoon": "下午", "evening": "傍晚/晚上", "night": "夜间"}
_PERIOD_DAYLIGHT = {
    "morning": "通常为白天，除非当前天气、季节或场景明确另有说明",
    "afternoon": "通常为白天，除非当前天气、季节或场景明确另有说明",
    "evening": "正逐渐入夜，结合已确立的季节与场景",
    "night": "通常为黑夜，除非场景明确另有说明",
}

# candidate -> resolved IANA key 的负缓存，避免重复对非法时区抛异常。
_TZ_CACHE: dict[str, str] = {}


def canonical_timezone(value, default: str = DEFAULT_TIMEZONE) -> str:
    """校验并返回标准 IANA 时区；非法时区回退到默认值。"""
    name = str(value or "").strip() or default
    cached = _TZ_CACHE.get(name)
    if cached is not None:
        return cached
    resolved = default
    try:
        resolved = str(ZoneInfo(name).key)
    except Exception:
        pass
    _TZ_CACHE[name] = resolved
    return resolved


def utc_now() -> datetime:
    return datetime.now(_UTC)


def local_now(tz: str) -> datetime:
    return utc_now().astimezone(ZoneInfo(canonical_timezone(tz)))


def parse_iso(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed


def valid_date(value) -> datetime | None:
    parsed = parse_iso(value)
    return parsed


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def format_local(value: datetime, tz: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    local = value.astimezone(ZoneInfo(canonical_timezone(tz)))
    return local.strftime(fmt)


def format_log_time(value, tz: str) -> str:
    parsed = valid_date(value)
    if parsed is None:
        return "-"
    return format_local(parsed, tz, "%m-%d %H:%M:%S")


def format_story_display_time(value, tz: str) -> str:
    parsed = valid_date(value)
    if parsed is None:
        return "-"
    context = local_endpoint(parsed, tz)
    return f"{context['local']} {context['offset']}"


def gmt_offset(value: datetime, tz: str) -> str:
    zone = ZoneInfo(canonical_timezone(tz))
    offset = value.astimezone(zone).utcoffset() or timedelta()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"GMT{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def period(value: datetime, tz: str) -> str:
    """返回 HDS 的四段式时段标识：morning / afternoon / evening / night。"""
    hour = value.astimezone(ZoneInfo(canonical_timezone(tz))).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "night"


def period_zh(period_name: str) -> str:
    return _PERIOD_ZH.get(period_name, period_name)


def daylight_expectation(period_name: str) -> str:
    return _PERIOD_DAYLIGHT.get(period_name, "通常为白天")


def local_clock_minutes(value: datetime, tz: str) -> int:
    local = value.astimezone(ZoneInfo(canonical_timezone(tz)))
    return local.hour * 60 + local.minute


def _calendar_day_key(value: datetime, tz: str) -> str:
    local = value.astimezone(ZoneInfo(canonical_timezone(tz)))
    return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"


def calendar_day_key(value: datetime | None, tz: str) -> str:
    if value is None:
        value = utc_now()
    return _calendar_day_key(value, tz)


def date_key(value) -> str | None:
    """严格校验 ``YYYY-MM-DD`` 字符串并保证往返一致。"""
    raw = value.strip() if isinstance(value, str) else ""
    if not _DATE_KEY_RE.match(raw):
        return None
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    return raw if d.strftime("%Y-%m-%d") == raw else None


def add_date(value: str, days: int) -> str:
    d = datetime.strptime(value, "%Y-%m-%d").date()
    return (d + timedelta(days=int(days))).isoformat()


def date_difference(left: str, right: str) -> int:
    left_d = datetime.strptime(left, "%Y-%m-%d").date()
    right_d = datetime.strptime(right, "%Y-%m-%d").date()
    return (right_d - left_d).days


def weekday_cl(date: str) -> str:
    """按 JS ``getUTCDay()`` 语义返回 Sunday..Saturday。"""
    d = datetime.strptime(date, "%Y-%m-%d").date()
    return _WEEKDAYS_CL[d.isoweekday() % 7]


def local_endpoint(value: datetime | None, tz: str) -> dict:
    """构建主叙事请求的权威本地时间端点，字段对齐 HDS ``storyLocalTimeContext``。"""
    if value is None:
        value = utc_now()
    zone = ZoneInfo(canonical_timezone(tz))
    local = value.astimezone(zone)
    period_name = period(value, tz)
    date_str = f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
    time_str = f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}"
    return {
        "timezone": str(zone),
        "utc": iso(value),
        "local": f"{date_str} {time_str}",
        "date": date_str,
        "time": time_str,
        "hour": local.hour,
        "weekday": _WEEKDAYS_ZH[local.weekday()],
        "weekday_en": _WEEKDAYS_EN[local.weekday()],
        "offset": gmt_offset(value, tz),
        "period": period_name,
        "periodZh": period_zh(period_name),
        "daylightExpectation": daylight_expectation(period_name),
    }


def add_minutes(value: datetime, minutes: float) -> datetime:
    return value + timedelta(minutes=minutes)


def elapsed_minutes(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0
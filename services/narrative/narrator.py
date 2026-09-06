"""narrator 层：所有模型角色统一走中央 ``ai_llm``。

对应 HDS Interlude 的 ``src/narrator.ts`` 与 ``src/service.ts`` 中的模型调用部分。
本模块只负责：选择角色参数 → 组装系统提示 → 发送 JSON 载荷 → 宽容解析 JSON 结果。
具体请求载荷（interval/recentScript/intents 等）由 ``engine.py`` 组装后传入；这里保持
对载荷结构的透明，不依赖存储层，便于单独测试与替换实现。

模型角色约定（均走 ElainaBot 中央 AI LLM，通过 ``central.get_service()`` 获取）：
- ``main``      —— 主叙事（systemPrompt）
- ``alter``     —— Alter 侧端低频气氛分析
- ``compaction``—— 低成本连续性压缩
- ``timeline``  —— 时间导演（自动推进事件账本）
- ``preplan``   —— 日程预规划
- ``overlay``   —— Overlay 设定演化压缩
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import prompt as prompt_builder


class NarrativeModelError(RuntimeError):
    """叙事模型调用失败或无法解析为有效 JSON。"""


# ---------------------------------------------------------------------------
# 宽容 JSON 解析
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _strip_json(value: str) -> str:
    """去掉说明性文字与 Markdown 围栏，只保留最可能的 JSON 对象片段。"""
    text = str(value or "").strip()
    if not text:
        return ""
    fenced = _CODE_FENCE_RE.findall(text)
    if fenced:
        return max(fenced, key=len).strip()
    start = text.find("{")
    if start == -1:
        return ""
    # 从第一个 { 到最后一个 }，兼容前后夹杂说明文字。
    end = text.rfind("}")
    return text[start : end + 1] if end > start else text[start:]


def _balanced_truncation(text: str) -> str:
    """对截断的 JSON 输出做逐年收尾，找到能够闭合的最小前缀。"""
    for end in range(len(text), 0, -1):
        candidate = text[:end] + "}"
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return candidate
    return ""


def parse_json_object(value: str) -> dict:
    """把模型输出解析为 dict。依次尝试：直接解析、去围栏、平衡截断。"""
    text = _strip_json(value)
    if not text:
        raise NarrativeModelError("模型未返回 JSON 对象")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        repaired = _balanced_truncation(text)
        if repaired:
            result = json.loads(repaired)
        else:
            raise NarrativeModelError("模型返回的内容不是有效 JSON") from None
    if not isinstance(result, dict):
        raise NarrativeModelError("模型返回的 JSON 顶层不是对象")
    return result


def extract_text_field(value: str) -> str:
    """从不规范输出里兜底捞出 ``script`` / ``reply.content`` 等文本字段。"""
    try:
        return str(parse_json_object(value).get("script") or "")
    except NarrativeModelError:
        return str(value or "").strip()


# ---------------------------------------------------------------------------
# 角色参数解析
# ---------------------------------------------------------------------------

_ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "main": {"temperature": 0.8, "max_tokens": 8192},
    "alter": {"temperature": 0.3, "max_tokens": 400},
    "compaction": {"temperature": 0.2, "max_tokens": 4096},
    "timeline": {"temperature": 0.3, "max_tokens": 1200},
    "preplan": {"temperature": 0.2, "max_tokens": 2048},
    "overlay": {"temperature": 0.2, "max_tokens": 2048},
}

# 角色 -> 叙事配置里承载该角色参数的子段。
_ROLE_SUB_KEY: dict[str, str] = {
    "alter": "alter_system",
    "preplan": "schedule_preplan",
}


def role_params(
    narrative_config: dict | None,
    role: str,
    base_config: dict | None = None,
) -> dict[str, Any]:
    """解析某一角色下的 provider/model/temperature/max_tokens。

    优先级：角色专属配置 → 角色子段（alter_system / schedule_preplan）→ 叙事全局默认
    → 插件（AI 幕间剧场）主配置 → 内置默认。
    所有角色都走中央 ``ai_llm``，因此默认复用插件的 provider_id / model_preference。
    """
    narrative = narrative_config or {}
    base = base_config or {}
    role_cfg = narrative.get(role) or {}
    if not isinstance(role_cfg, dict):
        role_cfg = {}
    sub_cfg: dict = {}
    sub_key = _ROLE_SUB_KEY.get(role)
    if sub_key and isinstance(narrative.get(sub_key), dict):
        sub_cfg = narrative[sub_key]
    defaults = _ROLE_DEFAULTS.get(role, _ROLE_DEFAULTS["main"])

    provider_id = str(
        role_cfg.get("provider_id")
        or sub_cfg.get("provider_id")
        or narrative.get("provider_id")
        or base.get("provider_id")
        or ""
    ).strip()
    model = str(
        role_cfg.get("model")
        or role_cfg.get("model_id")
        or sub_cfg.get("model")
        or narrative.get("model_preference")
        or base.get("model_preference")
        or ""
    ).strip()

    def _number(key: str, fallback: float | int) -> float | int:
        value = role_cfg.get(key)
        if value is None:
            value = sub_cfg.get(key)
        if value is None:
            value = narrative.get(key)
        if value is None:
            value = defaults.get(key, fallback)
        return value

    temperature = float(_number("temperature", 0.8))
    max_tokens = int(_number("max_tokens", 8192))
    return {
        "provider_id": provider_id,
        "model": model,
        "temperature": min(2.0, max(0.0, temperature)),
        "max_tokens": min(131072, max(8, max_tokens)),
    }


# ---------------------------------------------------------------------------
# 中央 ai_llm 调用
# ---------------------------------------------------------------------------

async def complete_text(
    system_prompt: str,
    payload: dict,
    *,
    narrative_config: dict | None = None,
    role: str = "main",
    base_config: dict | None = None,
    service=None,
) -> str:
    """通过中央 ``ai_llm.service.complete`` 发起一次纯文本调用并返回文本。"""
    from .. import central

    params = role_params(narrative_config, role, base_config)
    instance = service or central.get_service()
    if instance is None:
        raise NarrativeModelError(central.status()["message"])
    messages = [
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, default=str),
        }
    ]
    result = await instance.complete(
        messages,
        system_prompt=str(system_prompt or ""),
        provider_id=params["provider_id"],
        model=params["model"],
        temperature=params["temperature"],
        max_tokens=params["max_tokens"],
        consumer_plugin="ai_interlude_narrative",
        enable_runtime_tools=False,
        prepare_context=False,
    )
    return str(result.get("text") or "")


async def complete_json(
    system_prompt: str,
    payload: dict,
    *,
    narrative_config: dict | None = None,
    role: str = "main",
    base_config: dict | None = None,
    service=None,
) -> dict:
    """一次结构化 JSON 调用：返回解析后的 dict。"""
    text = await complete_text(
        system_prompt,
        payload,
        narrative_config=narrative_config,
        role=role,
        base_config=base_config,
        service=service,
    )
    return parse_json_object(text)


# ---------------------------------------------------------------------------
# 各模型角色的便捷封装
# ---------------------------------------------------------------------------

async def narrate(
    phase: str,
    payload: dict,
    *,
    narrative_config: dict | None = None,
    base_config: dict | None = None,
    prompts: dict | None = None,
    service=None,
) -> dict:
    """主叙事调用：返回结构化叙事结果。"""
    prompts = prompts or {}
    narrative = narrative_config or {}
    system_prompt = prompt_builder.system_prompt(
        phase,
        prompts.get("main_prompt") or narrative.get("main_prompt"),
        prompts.get("format_prompt") or narrative.get("format_prompt"),
        str(prompts.get("fixed_prompt") or narrative.get("fixed_prompt") or ""),
        str(prompts.get("base_style_prompt") or narrative.get("base_style_prompt") or ""),
        str(prompts.get("story_style_prompt") or narrative.get("story_style_prompt") or ""),
        alter_enabled=bool(narrative.get("alter_system", {}).get("enabled")),
        agency_enabled=bool(narrative.get("agency_window", {}).get("enabled", True)),
        perspective_enabled=bool(narrative.get("perspective_enabled", False)),
        output_recovery=bool(payload.get("outputRecovery", False)),
        chat_capabilities=narrative.get("chat_capabilities"),
        has_quoted_message=bool(payload.get("currentEvent", {}).get("quotedMessage")),
        sticker_catalog=narrative.get("sticker_catalog"),
        schedule_preplan_enabled=bool(narrative.get("schedule_preplan", {}).get("enabled")),
        streaming_reply_first=bool(narrative.get("streaming_reply_first", False)),
        cache_first_payload=bool(narrative.get("cache_first_payload", False)),
    )
    return await complete_json(
        system_prompt,
        payload,
        narrative_config=narrative_config,
        role="main",
        base_config=base_config,
        service=service,
    )


async def analyze_alter(
    payload: dict,
    *,
    narrative_config: dict | None = None,
    base_config: dict | None = None,
    service=None,
) -> dict:
    """Alter 侧端：低频气氛分析，返回 ``description``。"""
    narrative = narrative_config or {}
    alter_cfg = narrative.get("alter_system", {}) or {}
    system_prompt = prompt_builder.alter_analysis_prompt(
        str(alter_cfg.get("prompt", "") or "")
    )
    return await complete_json(
        system_prompt,
        payload,
        narrative_config=narrative_config,
        role="alter",
        base_config=base_config,
        service=service,
    )


async def compact_entries(
    payload: dict,
    *,
    narrative_config: dict | None = None,
    base_config: dict | None = None,
    prompts: dict | None = None,
    service=None,
) -> dict:
    """连续性压缩调用：返回 scene/arc/facts/statePatches/schedulePreplan。"""
    prompts = prompts or {}
    narrative = narrative_config or {}
    system_prompt = prompt_builder.compaction_prompt(
        str(prompts.get("fixed_prompt") or narrative.get("fixed_prompt") or ""),
        str(prompts.get("compaction_main_prompt") or narrative.get("compaction_main_prompt") or ""),
        str(prompts.get("compaction_fixed_prompt") or narrative.get("compaction_fixed_prompt") or ""),
        str(prompts.get("compaction_style_prompt") or narrative.get("compaction_style_prompt") or ""),
    )
    return await complete_json(
        system_prompt,
        payload,
        narrative_config=narrative_config,
        role="compaction",
        base_config=base_config,
        service=service,
    )


async def plan_timeline(
    payload: dict,
    *,
    narrative_config: dict | None = None,
    base_config: dict | None = None,
    service=None,
) -> dict:
    """时间导演调用：为自动推进窗口产出 beats。"""
    return await complete_json(
        prompt_builder.timeline_director_prompt(),
        payload,
        narrative_config=narrative_config,
        role="timeline",
        base_config=base_config,
        service=service,
    )


async def review_schedule_preplan(
    payload: dict,
    variation_level: str = "stable",
    *,
    narrative_config: dict | None = None,
    base_config: dict | None = None,
    service=None,
) -> dict:
    """日程预规划调用：返回 outcome/regimes/exceptions。"""
    return await complete_json(
        prompt_builder.schedule_preplan_prompt(variation_level),
        payload,
        narrative_config=narrative_config,
        role="preplan",
        base_config=base_config,
        service=service,
    )


async def compact_overlay(
    payload: dict,
    *,
    narrative_config: dict | None = None,
    base_config: dict | None = None,
    prompts: dict | None = None,
    service=None,
) -> dict:
    """Overlay 设定演化压缩调用。"""
    prompts = prompts or {}
    narrative = narrative_config or {}
    system_prompt = prompt_builder.overlay_compaction_prompt(
        str(prompts.get("fixed_prompt") or narrative.get("fixed_prompt") or ""),
        str(prompts.get("compaction_fixed_prompt") or narrative.get("compaction_fixed_prompt") or ""),
        str(prompts.get("compaction_style_prompt") or narrative.get("compaction_style_prompt") or ""),
    )
    return await complete_json(
        system_prompt,
        payload,
        narrative_config=narrative_config,
        role="overlay",
        base_config=base_config,
        service=service,
    )
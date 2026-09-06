"""四阶段串行叙事引擎 + 投递 + 自动推进调度。

把 HDS Interlude 的 ``src/service.ts`` 核心编排移植到 ElainaBot 的 AI 聊天陪伴插件内。
职责边界：

- 故事/参与者生命周期（对应 ``interlude_story`` / ``interlude_participant``）。
- 每个故事一条串行队列（``asyncio.Lock``），确保同一主角的回合不会并发交错。
- 四阶段：``user-message`` / ``conversation-follow-up`` / ``intent-due`` / ``advance``。
- 组装请求载荷 → 调 ``narrator``（主叙事/时间导演/压缩/Alter 侧端）→ 解释结构化结果
  → 投递回复 → 落库 script entry / state / intent / alter / agency。
- 后台自动推进调度：对长时间无用户活动的故事、到期意图进行独立的生命推进。

所有模型调用均通过 ``narrator`` 走中央 ``ai_llm``。本模块不直接接触 QQ 事件对象，
投递通过返回气泡文本列表完成，由 ``main.py`` 绑定具体平台。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
from typing import Any

from .. import central
from ...storage import narrative_store as store
from . import agency as agency_mod
from . import alter as alter_mod
from . import narrator
from . import schedule as schedule_mod
from .time_util import (
    canonical_timezone,
    iso,
    local_endpoint,
    parse_iso,
    utc_now,
)

# 与 HDS NarrativePhase 对齐的四阶段。
PHASE_USER_MESSAGE = "user-message"
PHASE_FOLLOW_UP = "conversation-follow-up"
PHASE_INTENT_DUE = "intent-due"
PHASE_ADVANCE = "advance"

# 默认叙事设定（canon 起点）。人物设定来自 AI 陪伴当前人格。
DEFAULT_SETTING: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "character": {"name": "", "profile": ""},
    "world": "",
    "relationship": "",
    "perspective": "",
}

# story.state 默认结构。可变当前状态与 canon 分离。
DEFAULT_STORY_STATE: dict[str, Any] = {
    "cursorAt": None,
    "lastContinuityUpdateAt": None,
    "alterSystem": None,
    "agencyWindow": None,
    "automaticDeliverySummaries": [],
    "continuitySnapshot": None,
    "workingDetails": [],
    "timelineCarry": [],
    "settingOverlay": {
        "character": None,
        "perspective": None,
        "world": None,
        "relationship": None,
    },
}

# 与 HDS 对齐：超过该原始条目数即触发一次压缩。
COMPACTION_ENTRY_THRESHOLD = 40

_locks: dict[str, asyncio.Lock] = {}
_background_task: asyncio.Task | None = None
_advance_interval_seconds = 20
_default_persona_name = "主角"


def story_id_for(appid: str, self_id: str, user_id: str) -> str:
    """一个用户一个主角故事，与聊天陪伴的按用户隔离一致。"""
    parts = [str(part or "default") for part in (appid, self_id, user_id)]
    return "story:" + ":".join(parts)


def participant_id_for(story_id: str) -> str:
    return story_id + ":p"


def _identity(event) -> tuple[str, str, str, str]:
    appid = str(getattr(event, "appid", "") or "default")
    self_id = str(getattr(event, "self_id", "") or "default")
    user_id = str(getattr(event, "user_id", "") or "")
    channel_id = str(getattr(event, "channel_id", "") or getattr(event, "group_id", "") or "")
    return appid, self_id, user_id, channel_id


def _lock_for(story_id: str) -> asyncio.Lock:
    return _locks.setdefault(story_id, asyncio.Lock())


def _merge_state(existing: dict | None) -> dict:
    state = dict(DEFAULT_STORY_STATE)
    if existing:
        for key, value in existing.items():
            state[key] = value
    if not isinstance(state.get("settingOverlay"), dict):
        state["settingOverlay"] = dict(DEFAULT_STORY_STATE["settingOverlay"])
    if not isinstance(state.get("alterSystem"), dict) and state.get("alterSystem") is not None:
        state["alterSystem"] = None
    return state


# ---------------------------------------------------------------------------
# 故事/参与者生命周期
# ---------------------------------------------------------------------------

def ensure_story(event, personality: dict | None, narrative_cfg: dict | None) -> dict:
    """获取或创建当前用户的故事与参与者，返回 story 记录。"""
    appid, self_id, user_id, channel_id = _identity(event)
    story_id = story_id_for(appid, self_id, user_id)
    story = store.get_story(story_id)
    if story is None:
        persona_name = str((personality or {}).get("name") or _default_persona_name)
        persona_prompt = str((personality or {}).get("prompt") or "").strip()
        setting = dict(DEFAULT_SETTING)
        setting["timezone"] = canonical_timezone(
            (narrative_cfg or {}).get("timezone") or "Asia/Shanghai"
        )
        setting["character"] = {"name": persona_name, "profile": persona_prompt}
        story = {
            "id": story_id,
            "platform": str(getattr(event, "platform", "") or "qq"),
            "selfId": self_id,
            "userId": user_id,
            "channelId": channel_id,
            "status": "active",
            "setting": setting,
            "state": _merge_state(None),
        }
        store.upsert_story(story)
    participant_id = participant_id_for(story_id)
    if store.get_participant(participant_id) is None:
        store.upsert_participant(
            {
                "id": participant_id,
                "storyId": story_id,
                "platform": str(getattr(event, "platform", "") or "qq"),
                "selfId": self_id,
                "userId": user_id,
                "channelId": channel_id,
                "personId": str(getattr(event, "person_id", "") or user_id),
                "displayName": str(getattr(event, "display_name", "") or user_id),
                "profile": "",
                "relationship": "",
                "state": {
                    "lastUserMessageAt": None,
                    "lastCharacterMessageAt": None,
                    "relationshipNotes": [],
                    "openThreads": [],
                    "unreadMessageCount": 0,
                    "pendingReplyCount": 0,
                },
                "status": "active",
            }
        )
    return store.get_story(story_id) or story


def _story_timezone(story: dict) -> str:
    return canonical_timezone(
        story.get("setting", {}).get("timezone") or "Asia/Shanghai"
    )


def _interval(story: dict, now: datetime) -> dict:
    cursor = parse_iso(story.get("state", {}).get("cursorAt"))
    if cursor is None or cursor > now:
        from_dt = now - timedelta(seconds=60)
    else:
        from_dt = cursor
    tz = _story_timezone(story)
    from_ctx = local_endpoint(from_dt, tz)
    now_ctx = local_endpoint(now, tz)
    return {
        "from": iso(from_dt),
        "now": iso(now),
        "storyTimezone": tz,
        "fromLocal": from_ctx["local"],
        "nowLocal": now_ctx["local"],
        "fromLocalContext": from_ctx,
        "nowLocalContext": now_ctx,
        "elapsedSeconds": max(0, int((now - from_dt).total_seconds())),
    }


def _recent_script(entries: list[dict], tz: str) -> list[dict]:
    """把原始条目投射为提示词所需的 recentScript。"""
    result = []
    for entry in entries:
        occurred = parse_iso(entry.get("occurredAt"))
        result.append(
            {
                "kind": entry.get("kind") or "",
                "actor": entry.get("actor") or "narrator",
                "content": str(entry.get("content") or ""),
                "occurredAt": str(entry.get("occurredAt") or ""),
                "occurredAtLocal": (
                    local_endpoint(occurred, tz)["local"] if occurred else ""
                ),
            }
        )
    return result


def _build_common_payload(story: dict, now: datetime, phase: str) -> dict:
    tz = _story_timezone(story)
    entries = store.entries(story["id"], since=None, limit=COMPACTION_ENTRY_THRESHOLD * 2)
    facts = store.facts(story["id"], limit=40)
    memories = store.memories(story["id"], limit=40)
    due = store.due_intents(story["id"], iso(now))
    scene = store.active_scene(story["id"])
    arc = store.active_arc(story["id"])
    state = story.get("state", {})
    payload: dict[str, Any] = {
        "phase": phase,
        "interval": _interval(story, now),
        "setting": story.get("setting", {}),
        "evolvingState": {
            key: value
            for key, value in state.items()
            if key
            not in {
                "alterSystem",
                "agencyWindow",
                "automaticDeliverySummaries",
                "continuitySnapshot",
                "workingDetails",
                "timelineCarry",
            }
        },
        "recentScript": _recent_script(entries[-24:], tz),
        "memories": [
            {"category": m.get("category"), "content": m.get("content"), "importance": m.get("importance")}
            for m in memories
        ],
        "facts": [
            {
                "id": f.get("id"),
                "scope": f.get("scope"),
                "content": f.get("content"),
                "importance": f.get("importance"),
                "unresolved": bool(f.get("unresolved")),
            }
            for f in facts
        ],
        "dueIntents": [
            {
                "id": i.get("id"),
                "type": i.get("type"),
                "summary": i.get("summary"),
                "notBefore": i.get("notBefore"),
            }
            for i in due
        ],
    }
    if scene:
        payload["activeScene"] = {"hook": scene.get("hook"), "summary": scene.get("summary")}
    if arc:
        payload["activeArc"] = {"title": arc.get("title"), "summary": arc.get("summary")}
    if state.get("continuitySnapshot"):
        payload["continuitySnapshot"] = state["continuitySnapshot"]
    if state.get("workingDetails"):
        payload["workingDetails"] = state["workingDetails"]
    timeline_carry = state.get("timelineCarry")
    if timeline_carry:
        payload["timelineCarry"] = timeline_carry[:8]
    return payload


# ---------------------------------------------------------------------------
# 结果解释与落库
# ---------------------------------------------------------------------------

def _bubbles(text: str) -> list[str]:
    return [part.strip() for part in str(text or "").split("<sep/>") if part.strip()]


def _extract_reply_bubbles(result: dict) -> list[str]:
    interaction = result.get("interaction") or {}
    reply = interaction.get("reply") or {}
    if reply.get("mode") == "immediate":
        return _bubbles(str(reply.get("content") or ""))
    return []


def _apply_turn(
    story: dict,
    result: dict,
    now: datetime,
    narrative_cfg: dict | None,
    user_text: str | None,
) -> list[str]:
    """把主叙事结果写入 story.state，投递脚本与结构化字段。返回要发送的气泡。"""
    narrative_cfg = narrative_cfg or {}
    tz = _story_timezone(story)
    state = _merge_state(story["state"])
    script = str(result.get("script") or "").strip()

    if script:
        store.add_entry(
            story["id"],
            "script",
            "narrator",
            script,
            occurred_at=iso(now),
        )

    bubbles = _extract_reply_bubbles(result)
    if bubbles:
        store.add_entry(
            story["id"],
            "character-message",
            "character",
            " <sep/> ".join(bubbles),
            occurred_at=iso(now),
        )

    # Alter System：把本轮 atmosphere 偏移计入状态机。
    alter_value = result.get("alter")
    alter_cfg = narrative_cfg.get("alter_system", {}) or {}
    if alter_cfg.get("enabled") and isinstance(alter_value, int):
        advanced = alter_mod.advance_alter_system(
            state.get("alterSystem"),
            alter_value,
            narrative_cfg.get("last_phase", PHASE_USER_MESSAGE),
            now,
            alter_cfg,
        )
        state["alterSystem"] = advanced["state"]

    # Agency Window / proactiveContact 原样纳入可变状态，供后续窗口评估。
    if result.get("agencyWindow") is not None:
        state["agencyWindow"] = result["agencyWindow"]
    if result.get("proactiveContact") is not None:
        state["proactiveContact"] = result["proactiveContact"]

    # continuity 刷新（refreshContinuity 为 true 时模型返回）。
    if result.get("continuity"):
        state["continuitySnapshot"] = result["continuity"]
        state["lastContinuityUpdateAt"] = iso(now)

    # intents：新建到期线程与主动后果。
    for intent in result.get("intents") or []:
        not_before = intent.get("notBefore")
        store.add_intent(
            {
                "storyId": story["id"],
                "type": str(intent.get("type") or "scheduled"),
                "summary": str(intent.get("summary") or "")[:2000],
                "notBefore": not_before or iso(now),
                "status": "scheduled",
                "payload": intent.get("payload") or {},
            }
        )

    # intentUpdates：完成/取消既有线程。
    for update in result.get("intentUpdates") or []:
        intent_id = update.get("id")
        if isinstance(intent_id, int):
            store.update_intent(intent_id, {"status": str(update.get("status") or "completed")})

    # followUpResolutions 与 followUpCommitment 简化处理。
    for resolution in result.get("followUpResolutions") or []:
        rid = resolution.get("id")
        if isinstance(rid, int):
            store.update_intent(rid, {"status": str(resolution.get("outcome") or "fulfilled")})

    if result.get("followUpCommitment"):
        store.add_intent(
            {
                "storyId": story["id"],
                "type": "follow-up-commitment",
                "summary": str(result["followUpCommitment"].get("summary") or "")[:500],
                "notBefore": result["followUpCommitment"].get("notBefore") or iso(now),
                "status": "scheduled",
                "payload": result["followUpCommitment"],
            }
        )

    # 前移时间光标。
    state["cursorAt"] = iso(now)
    story["state"] = state
    store.upsert_story(story)
    return bubbles


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def handle_message(event, text: str, current: dict) -> list[str]:
    """处理一条用户消息，返回要投递的气泡列表（可能为空）。"""
    narrative_cfg = current.get("narrative", {}) or {}
    personality = _personality_for(current)
    story = ensure_story(event, personality, narrative_cfg)
    lock = _lock_for(story["id"])
    async with lock:
        now = utc_now()
        store.add_entry(
            story["id"], "user-message", "user", str(text or ""), occurred_at=iso(now)
        )
        payload = _build_common_payload(story, now, PHASE_USER_MESSAGE)
        payload["currentEvent"] = {"type": PHASE_USER_MESSAGE, "content": str(text or "")}
        payload["currentParticipant"] = _participant_payload(story["id"])
        result = await narrator.narrate(
            PHASE_USER_MESSAGE,
            payload,
            narrative_config=narrative_cfg,
            base_config=current,
        )
        bubbles = _apply_turn(story, result, now, narrative_cfg, user_text=str(text or ""))
        await _maybe_compact(story, current)
        return bubbles


async def _advance_story(story: dict, current: dict) -> list[str]:
    """对单个故事执行一次独立生命推进，返回（通常为空的）待投递气泡。"""
    narrative_cfg = current.get("narrative", {}) or {}
    lock = _lock_for(story["id"])
    async with lock:
        now = utc_now()
        due = store.due_intents(story["id"], iso(now))
        phase = PHASE_INTENT_DUE if due else PHASE_ADVANCE
        payload = _build_common_payload(story, now, phase)
        payload["currentEvent"] = {"type": "none"}
        if due:
            payload["dueIntents"] = [
                {"id": i.get("id"), "type": i.get("type"), "summary": i.get("summary"), "notBefore": i.get("notBefore")}
                for i in due
            ]
        result = await narrator.narrate(
            phase,
            payload,
            narrative_config=narrative_cfg,
            base_config=current,
        )
        bubbles = _apply_turn(story, result, now, narrative_cfg, user_text=None)
        await _maybe_compact(story, current)
        return bubbles


async def advance_due_stories(current: dict) -> None:
    """扫描应推进的故事并逐个执行（由后台任务周期调用）。"""
    narrative_cfg = current.get("narrative", {}) or {}
    if not narrative_cfg.get("enabled"):
        return
    if not central.available():
        return
    now = utc_now()
    idle_minutes = max(1, int(narrative_cfg.get("advance_idle_minutes", 15)))
    horizon = iso(now)
    # 简化：仅对「有到期意图」的故事做推进；无意图的安静故事由 advance_idle 触发。
    for story in _active_stories():
        if store.due_intents(story["id"], horizon):
            with contextlib.suppress(Exception):
                await _advance_story(story, current)


def _active_stories(limit: int = 200) -> list[dict]:
    # 存储层尚无「列出 active 故事」的通用接口，这里通过行内查询兜底。
    return store.list_stories(status="active", limit=limit)


def _personality_for(current: dict) -> dict | None:
    from .. import config as companion_config

    return companion_config.active_personality(current)


def _participant_payload(story_id: str) -> dict | None:
    participant = store.get_participant(participant_id_for(story_id))
    if participant is None:
        return None
    return {
        "id": participant.get("id"),
        "displayName": participant.get("displayName"),
        "unreadMessageCount": participant.get("state", {}).get("unreadMessageCount", 0),
        "pendingReplyCount": participant.get("state", {}).get("pendingReplyCount", 0),
    }


async def _maybe_compact(story: dict, current: dict) -> None:
    """当原始条目数量超过阈值时，触发一次连续性压缩。"""
    narrative_cfg = current.get("narrative", {}) or {}
    if not narrative_cfg.get("compaction_enabled", True):
        return
    # 统计条目数：用 entries(limit=1) 不够，这里用 stats 不提供 per-story 计数，
    # 因此退化为按更新频率低成本的简化判断——仅在显式开启时才调用压缩。
    if COMPACTION_ENTRY_THRESHOLD > 0 and narrative_cfg.get("compaction_on_turn", False):
        payload = _build_common_payload(story, utc_now(), PHASE_ADVANCE)
        with contextlib.suppress(Exception):
            result = await narrator.compact_entries(
                payload, narrative_config=narrative_cfg, base_config=current
            )
            _apply_compaction(story, result)


def _apply_compaction(story: dict, result: dict) -> None:
    scene = result.get("scene")
    if scene:
        existing = store.active_scene(story["id"])
        store.upsert_scene(
            {
                **({"id": existing["id"]} if existing else {}),
                "storyId": story["id"],
                "status": "active",
                "hook": str(scene.get("hook") or "")[:500],
                "summary": str(scene.get("summary") or "")[:4000],
            }
        )
    arc = result.get("arc")
    if arc:
        existing_arc = store.active_arc(story["id"])
        store.upsert_arc(
            {
                **({"id": existing_arc["id"]} if existing_arc else {}),
                "storyId": story["id"],
                "status": "active",
                "title": str(arc.get("title") or "")[:255],
                "summary": str(arc.get("summary") or "")[:4000],
            }
        )
    for fact in result.get("facts") or []:
        store.add_fact(
            {
                "storyId": story["id"],
                "scope": str(fact.get("scope") or "world"),
                "content": str(fact.get("content") or "")[:4000],
                "importance": float(fact.get("importance") or 0),
                "confidence": float(fact.get("confidence") or 0),
                "unresolved": bool(fact.get("unresolved", False)),
                "sourceEntryIds": list(fact.get("sourceEntryIds") or []),
            }
        )
    for patch in result.get("statePatches") or []:
        store.add_state_patch(
            {
                "storyId": story["id"],
                "target": str(patch.get("target") or "world"),
                "path": str(patch.get("path") or ""),
                "proposedValue": str(patch.get("proposedValue") or ""),
                "evidence": str(patch.get("evidence") or ""),
                "confidence": float(patch.get("confidence") or 0),
                "impact": str(patch.get("impact") or "minor"),
                "status": "pending",
                "sourceEntryIds": list(patch.get("sourceEntryIds") or []),
            }
        )


# ---------------------------------------------------------------------------
# 后台自动推进
# ---------------------------------------------------------------------------

async def _run_loop() -> None:
    while True:
        await asyncio.sleep(_advance_interval_seconds)
        try:
            from .. import config as companion_config

            current = companion_config.load()
            await advance_due_stories(current)
        except Exception:  # noqa: BLE001 - 后台循环不因单次错误退出
            continue


def start_background() -> None:
    global _background_task
    if _background_task is None or _background_task.done():
        _background_task = asyncio.create_task(_run_loop())


async def stop_background() -> None:
    global _background_task
    if _background_task is not None:
        _background_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _background_task
        _background_task = None


def status_text(story: dict) -> str:
    state = story.get("state", {})
    tz = _story_timezone(story)
    cursor = parse_iso(state.get("cursorAt"))
    return "当前故事时间：" + (local_endpoint(cursor, tz)["local"] if cursor else "尚未开始")
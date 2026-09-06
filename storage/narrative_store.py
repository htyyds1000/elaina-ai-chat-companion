"""持续叙事的 SQLite 持久化层。

对应 HDS Interlude 的 ``src/database.ts`` 里 13 张 ``interlude_*`` 表。
与现有聊天上下文（``context.db``）隔离，单独使用 ``narrative.db``。所有 JSON
字段以 TEXT 存储，时间戳统一存 ISO-8601 字符串（UTC），保证按时间排序可用。

本模块只做「序列化/反序列化 + SQL 存取」，不含叙事业务逻辑；业务逻辑在
``engine.py``。线程安全：与 repository.py 相同，使用全局锁。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.RLock()
_connection: sqlite3.Connection | None = None

# 各表的 JSON 列：键为 SQL 列名（snake_case），值为其 JSON 类型（dict/list）。
_JSON_COLUMNS: dict[str, dict[str, str]] = {
    "narrative_story": {"setting": "dict", "state": "dict"},
    "narrative_participant": {"state": "dict"},
    "narrative_script_entry": {"metadata": "dict"},
    "narrative_memory": {},
    "narrative_intent": {"payload": "dict"},
    "narrative_scene": {},
    "narrative_arc": {},
    "narrative_fact": {"embedding": "list", "source_entry_ids": "list"},
    "narrative_state_patch": {"source_entry_ids": "list"},
    "narrative_overlay_snapshot": {"major_events": "list", "source_patch_ids": "list"},
    "narrative_sticker": {"aliases": "list", "embedding": "list"},
    "narrative_web_observation": {},
    "narrative_schedule_preplan": {"regimes": "list", "exceptions": "list", "materialized_days": "list"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS narrative_story (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT '',
    self_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    setting TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT '{}',
    cursor_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ns_identity ON narrative_story(platform, self_id, user_id);

CREATE TABLE IF NOT EXISTS narrative_participant (
    id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    self_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    person_id TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    relationship TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_np_story ON narrative_participant(story_id, status);
CREATE INDEX IF NOT EXISTS idx_np_user ON narrative_participant(user_id);

CREATE TABLE IF NOT EXISTS narrative_script_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    participant_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    occurred_at TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ne_story ON narrative_script_entry(story_id, occurred_at);

CREATE TABLE IF NOT EXISTS narrative_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    participant_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    source_entry_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nm_story ON narrative_memory(story_id, importance);

CREATE TABLE IF NOT EXISTS narrative_intent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    participant_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    not_before TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ni_story ON narrative_intent(story_id, status, not_before);

CREATE TABLE IF NOT EXISTS narrative_scene (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT,
    ended_at TEXT,
    hook TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    entry_count INTEGER NOT NULL DEFAULT 0,
    last_entry_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nscene ON narrative_scene(story_id, status, started_at);

CREATE TABLE IF NOT EXISTS narrative_arc (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    scene_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narc ON narrative_arc(story_id, status, updated_at);

CREATE TABLE IF NOT EXISTS narrative_fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    participant_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'world',
    content TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    unresolved INTEGER NOT NULL DEFAULT 0,
    embedding TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    source_entry_ids TEXT NOT NULL DEFAULT '[]',
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nfact ON narrative_fact(story_id, status, importance);

CREATE TABLE IF NOT EXISTS narrative_state_patch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    participant_id TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    proposed_value TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    impact TEXT NOT NULL DEFAULT 'minor',
    status TEXT NOT NULL DEFAULT 'pending',
    source_entry_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_npatch ON narrative_state_patch(story_id, status, confidence);

CREATE TABLE IF NOT EXISTS narrative_overlay_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    participant_id TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'short',
    period_start TEXT,
    period_end TEXT,
    summary TEXT NOT NULL DEFAULT '',
    major_events TEXT NOT NULL DEFAULT '[]',
    source_patch_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_noverlay ON narrative_overlay_snapshot(story_id, status, target, period_end);

CREATE TABLE IF NOT EXISTS narrative_sticker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL DEFAULT '',
    grp TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    animated INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    hash TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    embedding TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nsticker ON narrative_sticker(status, grp, updated_at);

CREATE TABLE IF NOT EXISTS narrative_web_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    participant_id TEXT NOT NULL DEFAULT '',
    intent_id INTEGER,
    mode TEXT NOT NULL DEFAULT '',
    query TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'done',
    accessed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nweb ON narrative_web_observation(story_id, status, accessed_at);

CREATE TABLE IF NOT EXISTS narrative_schedule_preplan (
    story_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    timezone TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL DEFAULT '',
    valid_through TEXT NOT NULL DEFAULT '',
    last_reviewed_local_date TEXT NOT NULL DEFAULT '',
    last_evidence_entry_id INTEGER,
    review_reason TEXT NOT NULL DEFAULT '',
    regimes TEXT NOT NULL DEFAULT '[]',
    exceptions TEXT NOT NULL DEFAULT '[]',
    materialized_days TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_npreplan ON narrative_schedule_preplan(valid_through, last_reviewed_local_date);
"""

# 每张表「列名（SQL/存储）→ 业务字段名」的映射：SQL 用 snake_case，业务用 camelCase。
_FIELDS: dict[str, dict[str, str]] = {
    "narrative_story": {"id": "id", "platform": "platform", "self_id": "selfId", "user_id": "userId", "channel_id": "channelId", "status": "status", "setting": "setting", "state": "state", "cursor_at": "cursorAt", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_participant": {"id": "id", "story_id": "storyId", "platform": "platform", "self_id": "selfId", "user_id": "userId", "channel_id": "channelId", "person_id": "personId", "display_name": "displayName", "profile": "profile", "relationship": "relationship", "state": "state", "status": "status", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_script_entry": {"id": "id", "story_id": "storyId", "participant_id": "participantId", "kind": "kind", "actor": "actor", "content": "content", "occurred_at": "occurredAt", "metadata": "metadata", "created_at": "createdAt"},
    "narrative_memory": {"id": "id", "story_id": "storyId", "participant_id": "participantId", "category": "category", "content": "content", "importance": "importance", "status": "status", "source_entry_id": "sourceEntryId", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_intent": {"id": "id", "story_id": "storyId", "participant_id": "participantId", "type": "type", "summary": "summary", "not_before": "notBefore", "status": "status", "payload": "payload", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_scene": {"id": "id", "story_id": "storyId", "status": "status", "started_at": "startedAt", "ended_at": "endedAt", "hook": "hook", "summary": "summary", "entry_count": "entryCount", "last_entry_id": "lastEntryId", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_arc": {"id": "id", "story_id": "storyId", "status": "status", "title": "title", "summary": "summary", "scene_count": "sceneCount", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_fact": {"id": "id", "story_id": "storyId", "participant_id": "participantId", "scope": "scope", "content": "content", "importance": "importance", "confidence": "confidence", "unresolved": "unresolved", "embedding": "embedding", "status": "status", "source_entry_ids": "sourceEntryIds", "last_seen_at": "lastSeenAt", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_state_patch": {"id": "id", "story_id": "storyId", "participant_id": "participantId", "target": "target", "path": "path", "proposed_value": "proposedValue", "evidence": "evidence", "confidence": "confidence", "impact": "impact", "status": "status", "source_entry_ids": "sourceEntryIds", "created_at": "createdAt", "applied_at": "appliedAt"},
    "narrative_overlay_snapshot": {"id": "id", "story_id": "storyId", "participant_id": "participantId", "target": "target", "tier": "tier", "period_start": "periodStart", "period_end": "periodEnd", "summary": "summary", "major_events": "majorEvents", "source_patch_ids": "sourcePatchIds", "status": "status", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_sticker": {"id": "id", "asset_id": "assetId", "file_path": "filePath", "grp": "group", "mime_type": "mimeType", "animated": "animated", "size": "size", "hash": "hash", "description": "description", "aliases": "aliases", "status": "status", "embedding": "embedding", "created_at": "createdAt", "updated_at": "updatedAt"},
    "narrative_web_observation": {"id": "id", "story_id": "storyId", "participant_id": "participantId", "intent_id": "intentId", "mode": "mode", "query": "query", "url": "url", "title": "title", "excerpt": "excerpt", "summary": "summary", "status": "status", "accessed_at": "accessedAt", "created_at": "createdAt"},
    "narrative_schedule_preplan": {"story_id": "storyId", "revision": "revision", "timezone": "timezone", "valid_from": "validFrom", "valid_through": "validThrough", "last_reviewed_local_date": "lastReviewedLocalDate", "last_evidence_entry_id": "lastEvidenceEntryId", "review_reason": "reviewReason", "regimes": "regimes", "exceptions": "exceptions", "materialized_days": "materializedDays", "created_at": "createdAt", "updated_at": "updatedAt"},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(data_dir: str) -> None:
    global _connection
    os.makedirs(data_dir, exist_ok=True)
    with _lock:
        if _connection is not None:
            return
        conn = sqlite3.connect(
            os.path.join(data_dir, "narrative.db"), check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _connection = conn


def close() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


def _conn() -> sqlite3.Connection:
    if _connection is None:
        raise RuntimeError("持续叙事存储尚未初始化")
    return _connection


def _encode_row(table: str, record: dict) -> dict:
    """把带 camelCase 的业务字段翻译为 snake_case SQL 列，JSON 列编码。"""
    field_map = _FIELDS[table]
    json_cols = _JSON_COLUMNS[table]
    out: dict[str, Any] = {}
    for column, field in field_map.items():
        if field not in record:
            continue
        value = record[field]
        if column in json_cols:
            value = json.dumps(value, ensure_ascii=False, default=str)
        out[column] = value
    return out


def _decode_row(table: str, row: sqlite3.Row) -> dict:
    field_map = _FIELDS[table]
    json_cols = _JSON_COLUMNS[table]
    record: dict[str, Any] = {}
    for column, field in field_map.items():
        if column not in row.keys():
            continue
        value = row[column]
        if column in json_cols and value is not None:
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                value = {} if json_cols[column] == "dict" else []
        record[field] = value
    return record


def _insert(table: str, record: dict, primary: str) -> str | int:
    encoded = _encode_row(table, record)
    columns = list(encoded.keys())
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})"
    with _lock:
        cursor = _conn().execute(sql, [encoded[c] for c in columns])
        _conn().commit()
        return primary if primary in encoded else int(cursor.lastrowid)


def _update(table: str, record: dict, where: str, params: list) -> None:
    encoded = _encode_row(table, record)
    if not encoded:
        return
    assignments = ",".join(f"{column}=?" for column in encoded)
    sql = f"UPDATE {table} SET {assignments} WHERE {where}"
    with _lock:
        _conn().execute(sql, [encoded[c] for c in encoded] + params)
        _conn().commit()


def _upsert(table: str, record: dict, primary_column: str) -> None:
    """有主键则 UPDATE，否则 INSERT。``primary_column`` 为 SQL 列名。"""
    primary_field = next(
        (field for column, field in _FIELDS[table].items() if column == primary_column),
        None,
    )
    key = record.get(primary_field) if primary_field else None
    if key:
        if _get(table, f"{primary_column}=?", [key]):
            _update(table, record, f"{primary_column}=?", [key])
            return
    _insert(table, record, primary_column)


def _get(table: str, where: str, params: list) -> dict | None:
    with _lock:
        row = _conn().execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
    return _decode_row(table, row) if row else None


def _list(table: str, where: str, params: list, order: str = "id ASC", limit: int = 0) -> list[dict]:
    sql = f"SELECT * FROM {table} WHERE {where}"
    if order:
        sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _lock:
        rows = _conn().execute(sql, params).fetchall()
    return [_decode_row(table, row) for row in rows]


def _delete(table: str, where: str, params: list) -> int:
    with _lock:
        cursor = _conn().execute(f"DELETE FROM {table} WHERE {where}", params)
        _conn().commit()
        return cursor.rowcount


# ---------------------------------------------------------------------------
# 面向叙事引擎的领域函数
# ---------------------------------------------------------------------------

def upsert_story(record: dict) -> None:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    _upsert("narrative_story", record, "id")


def get_story(story_id: str) -> dict | None:
    return _get("narrative_story", "id=?", [story_id])


def list_stories(status: str = "active", limit: int = 200) -> list[dict]:
    return _list(
        "narrative_story", "status=?", [status], order="updated_at DESC", limit=limit
    )


def find_story(platform: str, self_id: str, user_id: str) -> dict | None:
    return _get(
        "narrative_story", "platform=? AND self_id=? AND user_id=?", [platform, self_id, user_id]
    )


def delete_story(story_id: str) -> None:
    for table in _FIELDS:
        if table == "narrative_story":
            _delete(table, "id=?", [story_id])
        elif table == "narrative_sticker":
            continue  # 贴纸是全局资源，不属于单个故事
        else:
            _delete(table, "story_id=?", [story_id])


def upsert_participant(record: dict) -> None:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    _upsert("narrative_participant", record, "id")


def get_participant(participant_id: str) -> dict | None:
    return _get("narrative_participant", "id=?", [participant_id])


def list_participants(story_id: str, status: str = "active") -> list[dict]:
    return _list("narrative_participant", "story_id=?", [story_id], limit=0)


def add_entry(
    story_id: str,
    kind: str,
    actor: str,
    content: str,
    occurred_at: str | None = None,
    participant_id: str = "",
    metadata: dict | None = None,
) -> int:
    record = {
        "storyId": story_id,
        "participantId": participant_id,
        "kind": kind,
        "actor": actor,
        "content": content,
        "occurredAt": occurred_at,
        "metadata": metadata or {},
        "createdAt": now_iso(),
    }
    return int(_insert("narrative_script_entry", record, "id"))


def entries(story_id: str, since: str | None = None, limit: int = 100) -> list[dict]:
    if since:
        return _list(
            "narrative_script_entry",
            "story_id=? AND occurred_at>?",
            [story_id, since],
            order="occurred_at ASC, id ASC",
            limit=limit,
        )
    return _list(
        "narrative_script_entry",
        "story_id=?",
        [story_id],
        order="occurred_at ASC, id ASC",
        limit=limit,
    )


def upsert_scene(record: dict) -> None:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    _upsert("narrative_scene", record, "id")


def active_scene(story_id: str) -> dict | None:
    return _get("narrative_scene", "story_id=? AND status='active'", [story_id])


def upsert_arc(record: dict) -> None:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    _upsert("narrative_arc", record, "id")


def active_arc(story_id: str) -> dict | None:
    return _get("narrative_arc", "story_id=? AND status='active'", [story_id])


def add_intent(record: dict) -> int:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    return int(_insert("narrative_intent", record, "id"))


def due_intents(story_id: str, now: str, statuses: tuple[str, ...] = ("scheduled",)) -> list[dict]:
    placeholders = ",".join("?" for _ in statuses)
    return _list(
        "narrative_intent",
        f"story_id=? AND status IN ({placeholders}) AND not_before<=?",
        [story_id, *statuses, now],
        order="not_before ASC, id ASC",
        limit=50,
    )


def update_intent(intent_id: int, patch: dict) -> None:
    patch["updatedAt"] = now_iso()
    _update("narrative_intent", patch, "id=?", [intent_id])


def add_memory(record: dict) -> int:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    return int(_insert("narrative_memory", record, "id"))


def memories(story_id: str, status: str = "active", limit: int = 40) -> list[dict]:
    return _list(
        "narrative_memory",
        "story_id=? AND status=?",
        [story_id, status],
        order="importance DESC, id DESC",
        limit=limit,
    )


def add_fact(record: dict) -> int:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    return int(_insert("narrative_fact", record, "id"))


def facts(story_id: str, status: str = "active", limit: int = 100) -> list[dict]:
    return _list(
        "narrative_fact",
        "story_id=? AND status=?",
        [story_id, status],
        order="importance DESC, id ASC",
        limit=limit,
    )


def update_fact(fact_id: int, patch: dict) -> None:
    patch["updatedAt"] = now_iso()
    _update("narrative_fact", patch, "id=?", [fact_id])


def add_state_patch(record: dict) -> int:
    record.setdefault("createdAt", now_iso())
    return int(_insert("narrative_state_patch", record, "id"))


def pending_patches(story_id: str, target: str = "") -> list[dict]:
    where = "story_id=? AND status='pending'"
    params: list = [story_id]
    if target:
        where += " AND target=?"
        params.append(target)
    return _list("narrative_state_patch", where, params, order="id ASC", limit=100)


def update_state_patch(patch_id: int, patch: dict) -> None:
    _update("narrative_state_patch", patch, "id=?", [int(patch_id)])


def add_overlay_snapshot(record: dict) -> int:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    return int(_insert("narrative_overlay_snapshot", record, "id"))


def overlay_snapshots(story_id: str, target: str = "", status: str = "active") -> list[dict]:
    where = "story_id=? AND status=?"
    params: list = [story_id, status]
    if target:
        where += " AND target=?"
        params.append(target)
    return _list("narrative_overlay_snapshot", where, params, order="period_end ASC", limit=100)


def add_web_observation(record: dict) -> int:
    record.setdefault("createdAt", now_iso())
    return int(_insert("narrative_web_observation", record, "id"))


def add_sticker(record: dict) -> int:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    return int(_insert("narrative_sticker", record, "id"))


def get_schedule_preplan(story_id: str) -> dict | None:
    return _get("narrative_schedule_preplan", "story_id=?", [story_id])


def upsert_schedule_preplan(record: dict) -> None:
    record.setdefault("createdAt", now_iso())
    record["updatedAt"] = now_iso()
    _upsert("narrative_schedule_preplan", record, "story_id")


def stats() -> dict:
    with _lock:
        counts = {}
        for table in _FIELDS:
            counts[table] = _conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts
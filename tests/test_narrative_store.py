"""narrative_store SQLite 持久化单测。"""

from __future__ import annotations

import pytest

from AI聊天陪伴.storage import narrative_store as store


@pytest.fixture()
def connected(tmp_path):
    store.connect(str(tmp_path))
    yield tmp_path
    store.close()


def test_story_upsert_and_get(connected):
    story = {
        "id": "story:a:b:c",
        "platform": "qq",
        "selfId": "b",
        "userId": "c",
        "channelId": "d",
        "status": "active",
        "setting": {"timezone": "Asia/Shanghai"},
        "state": {"cursorAt": None},
    }
    store.upsert_story(story)
    fetch = store.get_story("story:a:b:c")
    assert fetch is not None
    assert fetch["setting"]["timezone"] == "Asia/Shanghai"


def test_list_stories(connected):
    for i in range(2):
        store.upsert_story({"id": f"story:{i}", "status": "active", "setting": {}, "state": {}})
    result = store.list_stories("active", 10)
    assert len(result) == 2


def test_entry_and_due_intents(connected):
    store.upsert_story({"id": "story:1", "status": "active", "setting": {}, "state": {}})
    entry_id = store.add_entry("story:1", "script", "narrator", "文本", occurred_at="2026-01-01T00:00:00Z")
    assert entry_id > 0
    entries = store.entries("story:1")
    assert any(item["kind"] == "script" for item in entries)

    store.add_intent({"storyId": "story:1", "type": "scheduled", "summary": "提醒", "notBefore": "2026-01-01T00:00:00Z", "status": "scheduled"})
    due = store.due_intents("story:1", "2026-01-02T00:00:00Z")
    assert len(due) == 1
    assert due[0]["summary"] == "提醒"


def test_delete_story(connected):
    store.upsert_story({"id": "story:9", "status": "active", "setting": {}, "state": {}})
    store.add_entry("story:9", "script", "narrator", "x", occurred_at="2026-01-01T00:00:00Z")
    store.delete_story("story:9")
    assert store.get_story("story:9") is None
    assert store.entries("story:9") == []


def test_schedule_preplan_upsert(connected):
    store.upsert_schedule_preplan(
        {
            "storyId": "story:2",
            "timezone": "Asia/Shanghai",
            "validFrom": "2026-01-01",
            "validThrough": "2026-01-14",
            "regimes": [],
            "exceptions": [],
            "materializedDays": [],
        }
    )
    record = store.get_schedule_preplan("story:2")
    assert record is not None
    assert record["validFrom"] == "2026-01-01"
"""narrator 宽容 JSON 解析与角色参数解析单测。"""

from __future__ import annotations

import pytest

from AI聊天陪伴.services.narrative import narrator as n


def test_parse_clean_json():
    assert n.parse_json_object('{"seen": true, "script": "hi"}') == {"seen": True, "script": "hi"}


def test_parse_fenced_json():
    text = "解释文字\n```json\n{\"seen\": false}\n```\n结尾"
    assert n.parse_json_object(text) == {"seen": False}


def test_parse_with_leading_trailing_text():
    text = "OK 返回结果如下：{\"script\": \"abc\"} 结束。"
    assert n.parse_json_object(text)["script"] == "abc"


def test_parse_balanced_truncation():
    text = '{"script": "长文本", "ints": [1,2,3]'
    result = n.parse_json_object(text)
    assert "script" in result


def test_parse_invalid_raises():
    with pytest.raises(n.NarrativeModelError):
        n.parse_json_object("没有 JSON")


def test_extract_text_field_fallback():
    assert n.extract_text_field('{"script": "正文"}') == "正文"
    assert n.extract_text_field("纯文本") == "纯文本"


def test_role_params_defaults():
    params = n.role_params({}, "main", {})
    assert params["provider_id"] == ""
    assert params["model"] == ""
    assert params["temperature"] == 0.8
    assert params["max_tokens"] == 8192


def test_role_params_priority_role_over_sub():
    narrative = {
        "provider_id": "nprov",
        "model_preference": "nmodel",
        "alter_system": {"provider_id": "aprov", "model": "amodel", "temperature": 0.5},
    }
    params = n.role_params(narrative, "alter", {"provider_id": "bprov"})
    # alter 角色专属段没有配置 → 落到 alter_system 子段。
    assert params["provider_id"] == "aprov"
    assert params["model"] == "amodel"
    assert params["temperature"] == 0.5


def test_role_params_global_fallback():
    narrative = {"provider_id": "nprov", "model_preference": "nmodel"}
    params = n.role_params(narrative, "compaction", {})
    assert params["provider_id"] == "nprov"
    assert params["model"] == "nmodel"


def test_role_params_role_cfg_non_dict_ignored():
    narrative = {"alter": "oops", "provider_id": "nprov"}
    params = n.role_params(narrative, "alter", {})
    assert params["provider_id"] == "nprov"
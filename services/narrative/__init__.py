"""持续叙事基础模块包。

把 HDS Interlude（TypeScript/Koishi）的持续叙事引擎移植到 ElainaBot v2 的
AI 幕间剧场插件内，作为「幕间模式」高级玩法。本包内模块尽量保持纯逻辑，
便于单独测试；与框架/中央 AI LLM 的交互集中在 narrator.py 与 engine.py。
"""

from __future__ import annotations
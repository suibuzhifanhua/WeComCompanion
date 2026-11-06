# -*- coding: utf-8 -*-
"""tests/test_deai.py — 去AI味功能（v1.0.330）
① 内置技能定义合法（结构/工具实名/规则要点在 prompt 里）
② 快捷入口默认清单含 deai（横线占位/双路输入说明）
③ build_agent_tools 注入链正常（deai 启用时 prompt 带规则）
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools.skills import SKILLS  # noqa: E402
from agent_tools import build_agent_tools  # noqa: E402


def test_skill_definition():
    deai = next((s for s in SKILLS if s["id"] == "deai"), None)
    assert deai, "内置技能缺 deai"
    assert deai["name"] == "去AI味" and deai.get("icon") == "✨"
    assert deai.get("default") is True
    # 工具实名校验
    from agent_tools.registry import TOOLS
    real = {t["function"]["name"] for t in TOOLS}
    for t in deai["tools"]:
        assert t in real, f"工具不存在：{t}"
    # 规则要点齐（对应主流去AI味方法论）
    p = deai["prompt"]
    for kw in ("首先", "综上所述", "值得注意的是", "赋能", "总分总", "只改表达不改事实",
               "read_wecom_page", "不升华"):
        assert kw in p, f"规则缺要点：{kw}"
    print("PASS 技能定义：结构/工具实名/五步规则齐")


def test_quick_link_default():
    js = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "js", "02-nav-quicklinks.js"), encoding="utf-8").read()
    m = re.search(r'\{id:"deai".*?\}(?=\s*\];)', js, re.S)
    assert m, "快捷入口默认清单缺 deai"
    seg = m.group(0)
    for kw in ("去AI味", "read_wecom_page", "______", "只改表达不改事实", "改写后全文"):
        assert kw in seg, f"入口缺要素：{kw}"
    print("PASS 快捷入口：deai 在默认清单（双路输入+占位符）")


def test_injection_chain():
    cfg = {"enabled_skills": None, "skill_overrides": {}, "custom_skills": [],
           "connectors": {}, "models": [], "feature_flags": {"skills": True}}
    # v1.0.373：模拟企微已绑定（否则企微工具被屏蔽——纯 agent 模式过滤）
    import agent_tools.dispatch as _dp
    _dp._wecom_ok_cache["v"] = True
    _dp._wecom_ok_cache["ts"] = float("inf")
    tools, prompts = build_agent_tools(cfg)
    names = {t.get("function", {}).get("name") for t in tools}
    assert {"read_wecom_page", "list_wecom_windows"} <= names, "deai 工具应注入"
    assert "去AI味改写规范" in prompts, "deai 规范应注入 system"
    print("PASS 注入链：deai 工具+规范进 build_agent_tools")


if __name__ == "__main__":
    test_skill_definition()
    test_quick_link_default()
    test_injection_chain()
    print("\n== 全部通过：去AI味 ==")

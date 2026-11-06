# -*- coding: utf-8 -*-
"""test_feature_flags — 功能管理总闸（v1.0.312，对齐 WorkBuddy 插件管理）。

用例：
1. DEFAULT_CONFIG 的 feature_flags 段与 FEATURE_FLAGS 键表一致（防抹段），默认值取各自消费方兜底
2. skills=False：build_agent_tools 只剩 finish + 停用提示（技能工具全部消失）
3. connectors=False：已配置连接器的工具不注入 + dispatch_tool 直调被拦截
4. POST /api/config feature_flags 逐键合并（部分键更新不影响其他键）
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.result import is_err, err_msg  # noqa: E402

from core import config_store  # noqa: E402
from agent_tools import build_agent_tools, dispatch_tool, CONNECTORS  # noqa: E402


def test_default_declaration():
    """默认值段必须等于 FEATURE_FLAGS（v1.0.617 起键名只有一处定义）。

    旧断言把键集合写死成 5 个，于是 self_repair_daily / self_auto_fix 长期"路由能收、
    UI 能勾、默认段没有"，而测试一直绿；现在锁的是"名单本身"，加键改一处即可。
    """
    ff = config_store.DEFAULT_CONFIG.get("feature_flags")
    assert isinstance(ff, dict) and set(ff.keys()) == set(config_store.FEATURE_FLAG_KEYS), ff
    assert ff == config_store.FEATURE_FLAG_DEFAULTS, ff
    # 默认值必须等于消费方原本的兜底，不然"补默认值"这件事本身会改变行为
    assert ff["self_auto_fix"] is False and ff["auto_save_dialogs"] is False, ff
    assert all(ff[k] is True for k in ("skills", "connectors", "team", "memory", "evolve",
                                       "self_repair_daily")), ff
    print("PASS DEFAULT_CONFIG.feature_flags == FEATURE_FLAG_DEFAULTS（8 键，危险键默认关）")


def test_skills_gate():
    cfg = {"enabled_skills": None, "skill_overrides": {}, "custom_skills": [],
           "connectors": {}, "models": [], "feature_flags": {"skills": False}}
    tools, prompts = build_agent_tools(cfg)
    names = {t.get("function", {}).get("name") for t in tools}
    assert names == {"finish"}, f"skills 关闸后应只剩 finish，实际: {names}"
    # 注意：build_agent_tools 返回的 prompts 是 join 后的字符串（非列表）
    assert isinstance(prompts, str) and "技能系统已停用" in prompts, prompts[-200:]
    print("PASS skills=False：全部技能工具停用（仅剩 finish）+ 停用提示注入")


def test_connectors_gate():
    # 找一个带 tool 的连接器定义构造已启用配置（generic-http 要求 base_url）
    conn = next((c for c in CONNECTORS if c.get("tool")), None)
    if not conn:
        print("SKIP 无带 tool 的连接器定义")
        return
    base = {"enabled_skills": None, "skill_overrides": {}, "custom_skills": [], "models": [],
            "connectors": {conn["id"]: {"enabled": True, "base_url": "https://api.example.com"}}}
    # 闸开：连接器工具注入
    tools_on, _ = build_agent_tools({**base, "feature_flags": {"connectors": True}})
    names_on = {t.get("function", {}).get("name") for t in tools_on}
    assert conn["tool"]["function"]["name"] in names_on, names_on
    # 闸关：不注入；dispatch 直调被拦截
    tools_off, _ = build_agent_tools({**base, "feature_flags": {"connectors": False}})
    names_off = {t.get("function", {}).get("name") for t in tools_off}
    assert conn["tool"]["function"]["name"] not in names_off, names_off
    out = dispatch_tool(conn["tool"]["function"]["name"], {}, {**base, "feature_flags": {"connectors": False}})
    assert is_err(out) and "停用" in err_msg(out), out
    print(f"PASS connectors=False：连接器工具不注入 + dispatch 直调被拦截（{conn['id']}）")


def test_config_post_merge():
    tmpdir = tempfile.mkdtemp(prefix="wec_ff_")
    cfg_file = os.path.join(tmpdir, "config.json")
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "models": [], "feature_flags": {"skills": True}}, f, ensure_ascii=False)
    orig = config_store.config_path
    config_store.config_path = lambda: cfg_file
    try:
        from flask import Flask
        from routes.management import bp
        app = Flask(__name__)
        app.register_blueprint(bp)
        c = app.test_client()
        # 只关 skills：其他键不动
        r = c.post("/api/config", json={"feature_flags": {"skills": False}})
        assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
        raw = json.load(open(cfg_file, encoding="utf-8"))
        ff = raw["feature_flags"]
        assert ff["skills"] is False, ff
        assert ff.get("connectors") is True and ff.get("team") is True and ff.get("memory") is True, \
            "部分键更新不得影响其他键（DEFAULT 合并兜底）"
        # 脏类型拒绝
        r = c.post("/api/config", json={"feature_flags": "garbage"})
        assert r.status_code == 200, "非 dict 的 feature_flags 应被忽略而非 500"
        print("PASS POST /config feature_flags 逐键合并 + 脏数据安全")
    finally:
        config_store.config_path = orig


if __name__ == "__main__":
    test_default_declaration()
    test_skills_gate()
    test_connectors_gate()
    test_config_post_merge()
    print("\n== 全部通过：功能管理总闸 ==")

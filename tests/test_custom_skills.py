# -*- coding: utf-8 -*-
"""test_custom_skills — 技能中心自定义技能（v1.0.303）+ 任务模板 null 修复回归。

背景：
1. 模板 P0（2026-09-11 用户截图）：磁盘 config "templates": null → /api/templates
   返回 JSON null → 前端 templates.length 抛 "Cannot read properties of null"。
   根因 DEFAULT_CONFIG["templates"]=None，合并逻辑对 None 默认值无修复能力。
2. 技能中心只能启用/优化内置技能，不能自建。v1.0.303 新增 custom_skills 顶层段
   （必须声明进 DEFAULT_CONFIG，防 v1.0.300 dsh 同款抹段坑）+ 3 个 CRUD 端点
   + build_agent_tools 同口径注入。

用例：
1. DEFAULT_CONFIG 含 custom_skills（list）且 templates 默认 []（非 None）
2. load_config 修复脏数据：磁盘 "templates": null → 内存 []
3. _custom_skills 形状校验：坏项（非 dict/缺 id/缺 name）丢弃
4. build_agent_tools：自定义技能 enabled=None 全开注入 / 显式列表排除不注入 / 包含注入
5. Flask test client CRUD：创建（id 自动生成+显式模式自动启用）→ 列表 custom 标记
   → 详情 → PUT 更新 → POST /skills 启用设置保留自定义 id → DELETE 删除并清 enabled
6. GET /api/templates 在磁盘 null 时返回 []（不返回 null）
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_store  # noqa: E402
from agent_tools import _custom_skills, build_agent_tools  # noqa: E402

CUSTOM = {
    "id": "custom_test_1",
    "name": "测试自定义技能",
    "desc": "回归测试用",
    "icon": "🧪",
    "prompt": "测试提示词：先 save_file 落盘再 finish。",
    "tools": ["save_file", "read_wecom_page"],
    "judge": "none",
    "default": True,
}


from conftest import _patch_config_path  # noqa: E402


def test_default_declaration():
    d = config_store.DEFAULT_CONFIG
    assert isinstance(d.get("custom_skills"), list), "custom_skills 必须声明进 DEFAULT_CONFIG（否则被 load/save 抹掉）"
    assert d.get("templates") == [], "templates 默认必须是 []（原 None 是模板页 P0 根因）"
    print("PASS DEFAULT_CONFIG 含 custom_skills:[] 且 templates:[]")


def test_load_heals_null_templates():
    tmpdir = tempfile.mkdtemp(prefix="wec_tpl_null_")
    cfg_file = os.path.join(tmpdir, "config.json")
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "templates": None, "custom_skills": None}, f, ensure_ascii=False)
    orig = _patch_config_path(cfg_file)
    try:
        cfg = config_store.load_config()
        assert cfg["templates"] == [], f"磁盘 null 应被默认 [] 修复，实际：{cfg['templates']!r}"
        assert cfg["custom_skills"] == [], f"custom_skills 磁盘 null 应修复为 []，实际：{cfg['custom_skills']!r}"
        print("PASS load_config 修复磁盘 templates:null / custom_skills:null → []")
    finally:
        config_store.config_path = orig


def test_custom_skills_filter():
    cfg = {"custom_skills": [
        dict(CUSTOM),
        {"id": "x"},                      # 缺 name → 丢
        {"name": "无 id"},                # 缺 id → 丢
        "garbage",                        # 非 dict → 丢
        None,                             # 非 dict → 丢
    ]}
    out = _custom_skills(cfg)
    assert len(out) == 1 and out[0]["id"] == "custom_test_1", out
    assert _custom_skills({}) == []
    assert _custom_skills({"custom_skills": None}) == []
    print("PASS _custom_skills 形状校验（坏项全丢，空/None 安全）")


def test_build_agent_tools_injection():
    base = {"enabled_skills": None, "skill_overrides": {}, "connectors": {},
            "wecom": {}, "models": [], "judge_model_id": ""}
    # v1.0.373：模拟企微已绑定（否则企微工具被屏蔽——纯 agent 模式过滤）
    import agent_tools.dispatch as _dp
    _dp._wecom_ok_cache["v"] = True
    _dp._wecom_ok_cache["ts"] = float("inf")
    # enabled=None（默认全开）→ 自定义技能工具+prompt 注入
    tools, prompts = build_agent_tools({**base, "custom_skills": [dict(CUSTOM)]})
    names = {t.get("function", {}).get("name") for t in tools}
    assert "save_file" in names and "read_wecom_page" in names, names
    assert "测试提示词" in prompts, prompts[:200]
    # 显式列表排除 → 不注入
    tools2, prompts2 = build_agent_tools({**base, "enabled_skills": ["window-sense"], "custom_skills": [dict(CUSTOM)]})
    names2 = {t.get("function", {}).get("name") for t in tools2}
    assert "save_file" not in names2, names2
    assert "测试提示词" not in prompts2
    # 显式列表包含 → 注入
    tools3, prompts3 = build_agent_tools({**base, "enabled_skills": ["custom_test_1"], "custom_skills": [dict(CUSTOM)]})
    names3 = {t.get("function", {}).get("name") for t in tools3}
    assert "save_file" in names3
    assert "测试提示词" in prompts3
    print("PASS build_agent_tools：自定义技能 全开注入 / 列表排除不注入 / 列表包含注入")


def _make_client(cfg_file):
    from flask import Flask
    from routes.skills import bp as skills_bp
    from routes.templates import bp as templates_bp
    app = Flask(__name__)
    app.register_blueprint(skills_bp)
    app.register_blueprint(templates_bp)
    return app.test_client()


def test_crud_endpoints():
    tmpdir = tempfile.mkdtemp(prefix="wec_cuskill_")
    cfg_file = os.path.join(tmpdir, "config.json")
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "models": [], "enabled_skills": ["window-sense"],
                   "templates": None}, f, ensure_ascii=False)
    orig = _patch_config_path(cfg_file)
    try:
        c = _make_client(cfg_file)
        # 1) 创建（显式启用模式 → 自动加入 enabled_skills）
        r = c.post("/api/skills/custom", json={"name": CUSTOM["name"], "desc": CUSTOM["desc"],
                                               "icon": CUSTOM["icon"], "prompt": CUSTOM["prompt"],
                                               "tools": ["save_file", "read_wecom_page", "not_a_real_tool"]})
        d = r.get_json()
        assert r.status_code == 200 and d.get("ok"), d
        sid = d["id"]
        assert sid.startswith("custom_"), sid
        raw = json.load(open(cfg_file, encoding="utf-8"))
        assert len(raw["custom_skills"]) == 1, raw["custom_skills"]
        assert raw["custom_skills"][0]["tools"] == ["save_file", "read_wecom_page"], "非法工具名应被过滤"
        assert sid in raw["enabled_skills"], "显式启用模式下新建技能应自动启用"
        # 2) 列表带 custom 标记
        d = c.get("/api/skills").get_json()
        cs = [s for s in d["skills"] if s.get("custom")]
        assert len(cs) == 1 and cs[0]["id"] == sid and cs[0]["enabled"] is True, cs
        assert all("custom" in s for s in d["skills"]), "所有技能项都应带 custom 字段"
        # 3) 详情
        d = c.get(f"/api/skills/{sid}").get_json()
        assert d.get("ok") and d.get("custom") is True and d["current"]["name"] == CUSTOM["name"], d
        # 4) PUT 更新
        r = c.put(f"/api/skills/custom/{sid}", json={"name": "改名技能", "tools": ["save_file"]})
        assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
        d = c.get(f"/api/skills/{sid}").get_json()
        assert d["current"]["name"] == "改名技能" and d["current"]["tools"] == ["save_file"], d["current"]
        # 5) 启用设置保存：自定义 id 不再被静默过滤
        ids = [s["id"] for s in c.get("/api/skills").get_json()["skills"]]
        c.post("/api/skills", json={"enabled_skills": ids})
        raw = json.load(open(cfg_file, encoding="utf-8"))
        assert sid in raw["enabled_skills"], "自定义 id 应通过 enabled_skills 校验"
        # 6) 名称空 → 400
        r = c.put(f"/api/skills/custom/{sid}", json={"name": "   "})
        assert r.status_code == 400, r.status_code
        # 7) DELETE 删除 + enabled 清理
        r = c.delete(f"/api/skills/custom/{sid}")
        assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
        raw = json.load(open(cfg_file, encoding="utf-8"))
        assert raw["custom_skills"] == [] and sid not in raw["enabled_skills"], raw
        # 8) 删不存在的 → 404
        assert c.delete(f"/api/skills/custom/{sid}").status_code == 404
        print("PASS Flask CRUD：创建/列表/详情/更新/启用校验/删除/404 边界全过")
    finally:
        config_store.config_path = orig


def test_templates_api_null_safe():
    tmpdir = tempfile.mkdtemp(prefix="wec_tpl_api_")
    cfg_file = os.path.join(tmpdir, "config.json")
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "models": [], "templates": None}, f, ensure_ascii=False)
    orig = _patch_config_path(cfg_file)
    try:
        c = _make_client(cfg_file)
        body = c.get("/api/templates")
        assert body.status_code == 200, body.status_code
        data = body.get_json()
        # v1.0.366：磁盘 templates 为 null 时不再返回 []，而是返回内置场景模板
        # （原 null 防御目的不变——不再让前端 .length 崩溃）
        assert isinstance(data, list) and data, f"应返回内置模板列表，实际：{data!r}"
        assert all(t.get("builtin") for t in data), data
        print("PASS GET /api/templates 磁盘 null → 返回内置模板列表（前端安全）")
    finally:
        config_store.config_path = orig


if __name__ == "__main__":
    test_default_declaration()
    test_load_heals_null_templates()
    test_custom_skills_filter()
    test_build_agent_tools_injection()
    test_crud_endpoints()
    test_templates_api_null_safe()
    print("\n== 全部通过：自定义技能 + 模板 null 修复回归 ==")

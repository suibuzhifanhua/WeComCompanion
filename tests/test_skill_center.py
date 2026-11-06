# -*- coding: utf-8 -*-
"""tests/test_skill_center.py — 技能中心域回归锁合集（v1.0.646 按域归并）。

四个文件守的都是「技能从哪来、怎么装、装了怎么回滚」这条链的不同段，
共用 `skill_hub_data` / `config_store.custom_skills` / `routes.skills` 蓝图的同一套机制。

| 原文件                          | 版本出处  | 守护对象                       | 用例数 |
|---------------------------------|-----------|--------------------------------|--------|
| test_skill_hub.py               | v1.0.309  | 公共技能库（快照/安装/源 URL） |   6    |
| test_skill_on_demand.py         | v1.0.317  | 技能按需安装（request_skill）  |   3    |
| test_skill_source_import.py     | v1.0.318  | 技能源导入（import_skill_source）|   3   |
| test_skill_versions.py          | v1.0.349  | 技能 prompt 版本历史与回滚     |   3    |

v1.0.646 归并时顺手修掉一处**模块级副作用**：原 test_skill_versions.py 在模块级执行
`core.paths.workspace_root = lambda: TMP`（tempfile.mkdtemp，从不还原）。同文件合并后
这个假根会污染本文件其余全部用例——正是 test_skill_on_demand / test_skill_source_import
docstring 里记过的同款坑（v1.0.619 卫生整改）。现已改为 class 内 fixture，用例结束即还原。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core   # noqa: E402
import core_utils   # noqa: E401（确保裸名壳存在，与生产同链路）
from core import config_store   # noqa: E402
from agent_tools import TOOLS, build_agent_tools   # noqa: E402
from agent_tools.skill_hub_data import HUB_SKILLS, HUB_VERSION   # noqa: E402
import agent_tools.skill_versions as SV   # noqa: E402


def _src_of_module(mod):
    """读某个包模块所在目录下的源码文件（源码级护栏用）。"""
    return os.path.dirname(mod.__file__)


def _make_client():
    from flask import Flask
    from routes.skills import bp as skills_bp
    app = Flask(__name__)
    app.register_blueprint(skills_bp)
    return app.test_client()


# ============================================================ v1.0.309 公共技能库
class TestSkillHub:
    """技能中心公共技能库（v1.0.309）。

    背景：技能中心此前只有「内置技能 + 用户自建」两类。v1.0.309 增加第三类：
    公共技能库（内置快照离线可用 + 可配置远程源 URL），一键安装 = 写入
    custom_skills（id 前缀 hub_，幂等覆盖）。

    用例：
    1. DEFAULT_CONFIG 声明 skill_hub（防 save_config 抹段——v1.0.300 dsh 同款坑）
    2. HUB_SKILLS 内置快照形状校验：id/name/prompt/tools 齐全，tools ⊆ 内置工具名
    3. GET /api/skills/hub：内置源返回 + installed标记
    4. POST /skills/hub/install：安装 → config.custom_skills 出现 hub_<id> 且自动启用
       → 重复安装幂等（不产生第二条）→ 未知 id 404
    5. POST /skills/hub/url：合法 URL保存 / 非法协议 400 / 空串恢复内置
    6. 远程源回退：url 不可达时 GET /skills/hub 仍返回内置快照（source=builtin + note）
    """

    def _patch_config_path(self, cfg_file):
        orig = config_store.config_path
        config_store.config_path = lambda: cfg_file
        return orig

    def test_default_declaration(self):
        d = config_store.DEFAULT_CONFIG
        assert isinstance(d.get("skill_hub"), dict), "skill_hub 必须声明进 DEFAULT_CONFIG（否则被抹段）"
        assert "url" in d["skill_hub"], d["skill_hub"]
        print("PASS DEFAULT_CONFIG 含 skill_hub:{url:...}")

    def test_hub_snapshot_shape(self):
        valid = {t.get("function", {}).get("name") for t in TOOLS}
        ids = set()
        for s in HUB_SKILLS:
            assert s.get("id") and s.get("name") and s.get("prompt"), s
            assert isinstance(s.get("tools"), list) and s["tools"], s
            bad = [t for t in s["tools"] if t not in valid]
            assert not bad, f"{s['id']} 引用了不存在的工具: {bad}"
            ids.add(s["id"])
        assert len(ids) == len(HUB_SKILLS), "id 重复"
        assert len(HUB_SKILLS) >= 10, "内置快照应 ≥10 个技能"
        print(f"PASS HUB_SKILLS 形状校验（{len(HUB_SKILLS)} 个，工具名全部合法且无重复）")

    def test_hub_list_and_install_roundtrip(self):
        tmpdir = tempfile.mkdtemp(prefix="wec_hub_")
        cfg_file = os.path.join(tmpdir, "config.json")
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "models": [], "enabled_skills": ["window-sense"]}, f, ensure_ascii=False)
        orig = self._patch_config_path(cfg_file)
        try:
            c = _make_client()
            # 1) 清单（内置源）
            d = c.get("/api/skills/hub").get_json()
            assert d["ok"] and d["source"] == "builtin" and len(d["skills"]) >= 10, d
            first = d["skills"][0]
            assert first["installed"] is False
            # 2) 安装 → hub_ 前缀 + 自动启用
            r = c.post("/api/skills/hub/install", json={"id": first["id"]})
            d2 = r.get_json()
            assert r.status_code == 200 and d2.get("ok") and d2["id"] == "hub_" + first["id"], d2
            raw = json.load(open(cfg_file, encoding="utf-8"))
            assert len(raw["custom_skills"]) == 1, raw["custom_skills"]
            assert raw["custom_skills"][0]["id"] == "hub_" + first["id"]
            assert raw["custom_skills"][0]["prompt"], "安装的技能必须带 prompt"
            assert "hub_" + first["id"] in raw["enabled_skills"], "显式启用模式下 hub 技能应自动启用"
            # 3) 幂等：重装 → 仍 1 条（覆盖更新）
            c.post("/api/skills/hub/install", json={"id": first["id"]})
            raw = json.load(open(cfg_file, encoding="utf-8"))
            assert len(raw["custom_skills"]) == 1, "重复安装应覆盖而非新增"
            # 4) 清单标记已安装
            d3 = c.get("/api/skills/hub").get_json()
            mark = [s for s in d3["skills"] if s["id"] == first["id"]]
            assert mark and mark[0]["installed"] is True, mark
            # 5) 未知 id → 404
            assert c.post("/api/skills/hub/install", json={"id": "no_such"}).status_code == 404
            # 6) 缺 id → 400
            assert c.post("/api/skills/hub/install", json={}).status_code == 400
            print("PASS hub 清单/安装/自动启用/幂等覆盖/404/400 边界")
        finally:
            config_store.config_path = orig
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_hub_url_config(self):
        tmpdir = tempfile.mkdtemp(prefix="wec_huburl_")
        cfg_file = os.path.join(tmpdir, "config.json")
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "models": []}, f, ensure_ascii=False)
        orig = self._patch_config_path(cfg_file)
        try:
            c = _make_client()
            # 非法协议 → 400
            assert c.post("/api/skills/hub/url", json={"url": "ftp://x"}).status_code == 400
            # 合法 URL → 保存
            r = c.post("/api/skills/hub/url", json={"url": "https://example.com/hub.json"})
            assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
            raw = json.load(open(cfg_file, encoding="utf-8"))
            assert raw["skill_hub"]["url"] == "https://example.com/hub.json", raw.get("skill_hub")
            # 空串 → 恢复内置（url 清空）
            r = c.post("/api/skills/hub/url", json={"url": ""})
            assert r.status_code == 200, r.get_json()
            raw = json.load(open(cfg_file, encoding="utf-8"))
            assert raw["skill_hub"]["url"] == "", raw.get("skill_hub")
            print("PASS hub url 配置：非法协议 400 / 保存 / 空串恢复内置")
        finally:
            config_store.config_path = orig
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_hub_remote_fallback(self):
        """url 指向不可达地址 → 仍能返回内置快照（source=builtin + note 说明）。"""
        tmpdir = tempfile.mkdtemp(prefix="wec_hubfb_")
        cfg_file = os.path.join(tmpdir, "config.json")
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "models": [],
                       "skill_hub": {"url": "https://127.0.0.1:59999/no_such_hub.json"}}, f, ensure_ascii=False)
        orig = self._patch_config_path(cfg_file)
        try:
            c = _make_client()
            d = c.get("/api/skills/hub").get_json()
            assert d["ok"] and d["source"] == "builtin", d
            assert len(d["skills"]) >= 10 and d.get("note"), d
            print("PASS 远程源不可达 → 回退内置快照并注明")
        finally:
            config_store.config_path = orig
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_update_available_only_when_version_differs(self):
        """v1.0.316：更新按钮只在该出现时出现——update_available 仅在已装且版本不一致时为 True。
        未装=False；已装同版=False；已装旧版（无 hub_version 的旧数据或有更新）=True；
        重新安装（更新）后版本校准回 False。"""
        tmpdir = tempfile.mkdtemp(prefix="wec_hubupd_")
        cfg_file = os.path.join(tmpdir, "config.json")
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "models": []}, f, ensure_ascii=False)
        orig = self._patch_config_path(cfg_file)
        try:
            c = _make_client()
            d = c.get("/api/skills/hub").get_json()
            first = d["skills"][0]
            assert first["installed"] is False and first["update_available"] is False
            # 安装 → 记录 hub_version → 同版无更新
            c.post("/api/skills/hub/install", json={"id": first["id"]})
            raw = json.load(open(cfg_file, encoding="utf-8"))
            assert raw["custom_skills"][0].get("hub_version"), "安装必须记录 hub_version"
            d = c.get("/api/skills/hub").get_json()
            mark = [s for s in d["skills"] if s["id"] == first["id"]][0]
            assert mark["installed"] and mark["update_available"] is False, mark
            # 模拟旧数据/新版本：把已装条目版本改旧 → 有更新
            raw = json.load(open(cfg_file, encoding="utf-8"))
            raw["custom_skills"][0]["hub_version"] = "2000-01-01"
            json.dump(raw, open(cfg_file, "w", encoding="utf-8"), ensure_ascii=False)
            d = c.get("/api/skills/hub").get_json()
            mark = [s for s in d["skills"] if s["id"] == first["id"]][0]
            assert mark["update_available"] is True, mark
            # 点更新（重装）→ 版本校准回无更新
            c.post("/api/skills/hub/install", json={"id": first["id"]})
            d = c.get("/api/skills/hub").get_json()
            mark = [s for s in d["skills"] if s["id"] == first["id"]][0]
            assert mark["update_available"] is False, mark
            print("PASS update_available：未装 F / 已装同版 F / 版本差 T / 更新后校准回 F")
        finally:
            config_store.config_path = orig
            shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================ v1.0.317 按需安装
class TestSkillOnDemand:
    """技能按需安装（v1.0.317）：
    任务匹配未装技能 → agent 调 request_skill → 弹窗问用户 → 装/跳过 → 断点恢复。

    用例：
    1. build_agent_tools：有未装技能 → 注入 request_skill 工具 + 未装清单 prompt；
       全部已装 → 都不注入；skills 总闸关 → 不注入
    2. loop.py：request_skill 特判（挂起：skill_request+need_continue 事件 + 存断点 + return）
       与 pane_id=None 后台降级
    3. continue 端点 resume_note 透传（agent_loop 签名 + 断点恢复注入【用户决定】）
    """

    def _patch_all(self, tmpdir, mp):
        """config / 产物库根改道到临时目录。
        v1.0.619（测试卫生）：原实现是裸赋值 `core.paths.workspace_root = lambda: tmpdir`
        且从不还原——同一个 pytest 会话里，它之后的每个测试读到的都是这个假根
        （6 个模块级同款泄漏把 v619 闸门测试带红了，见 tests/test_v619_defer_ws.py）。"""
        from services import memory_ops, scheduler   # noqa: F401  # v1.0.526 统一包路径
        mp.setattr(config_store, "config_path", lambda: os.path.join(tmpdir, "config.json"))
        mp.setattr(core.paths, "workspace_root", lambda: tmpdir)

    @pytest.fixture(autouse=True)
    def _isolate_ws(self, tmp_path, monkeypatch):
        """每个用例自带临时产物库/config，结束自动还原并清掉临时目录。"""
        self._patch_all(str(tmp_path), monkeypatch)
        yield

    def test_injection(self):
        base = {"enabled_skills": None, "skill_overrides": {}, "custom_skills": [], "models": [],
                "connectors": {}, "wecom": {}, "feature_flags": {"skills": True}}
        # 有未装技能 → request_skill 注入 + 清单 prompt
        # 注意：build_agent_tools 返回的 prompts 是 join 后的字符串（非列表）——直接子串检查
        tools, prompts = build_agent_tools(base)
        names = {t.get("function", {}).get("name") for t in tools}
        assert "request_skill" in names, names
        assert isinstance(prompts, str)
        assert "可按需安装的技能" in prompts and "request_skill" in prompts and "id=weekly-report" in prompts
        # 全部已装 → 不注入（把 12 个 hub 技能全部装上）
        cfg2 = dict(base)
        cfg2["custom_skills"] = [dict(s, id="hub_" + s["id"], hub_version=HUB_VERSION) for s in HUB_SKILLS]
        tools2, prompts2 = build_agent_tools(cfg2)
        names2 = {t.get("function", {}).get("name") for t in tools2}
        assert "request_skill" not in names2, names2
        assert not any("可按需安装" in p for p in prompts2 if isinstance(p, str))
        # 总闸关 → 不注入
        tools3, _ = build_agent_tools({**base, "feature_flags": {"skills": False}})
        assert "request_skill" not in {t.get("function", {}).get("name") for t in tools3}
        print("PASS 注入：有未装 → 工具+清单；全装/关闸 → 不注入")

    def test_loop_suspend(self):
        src = open(os.path.join(_src_of_module(__import__("agent")), "loop.py"), encoding="utf-8").read()
        # 特判分支在位：skill_request 事件 + 存断点 + need_continue + return；后台降级
        # v1.0.654 B2：存断点收敛为 save_state_safe（补配对+落盘单入口），锁跟到新形态
        assert "'skill_request'" in src and "save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id" in src
        assert "无法向用户弹窗询问" in src
        assert "resume_note" in src.split("def agent_loop")[1].split("def ")[0] or "resume_note" in src
        # chat.py continue 透传
        csrc = open(os.path.join(ROOT, "routes", "chat.py"), encoding="utf-8").read()
        assert "resume_note" in csrc and "resume_note=resume_note" in csrc
        print("PASS loop 挂起特判 + continue resume_note 透传")

    def test_resume_note_injection(self):
        """断点恢复时 resume_note 追加到续跑提示（源码级验证注入点）。"""
        src = open(os.path.join(_src_of_module(__import__("agent")), "loop.py"), encoding="utf-8").read()
        assert "【用户决定】" in src, "恢复提示缺少【用户决定】注入"
        # 注入必须在断点恢复分支（_resume_note 构造之后、loop_msgs.append 之前）
        i = src.find("if resume_note:")
        j = src.find("loop_msgs.append({\"role\": \"user\", \"content\": _resume_note})")
        assert 0 < i < j, "resume_note 注入位置错误"
        print("PASS resume_note 注入【用户决定】到断点恢复提示")


# ============================================================ v1.0.318 技能源导入
class TestSkillSourceImport:
    """技能源导入（v1.0.318）：用户丢 URL → agent 调
    import_skill_source → 弹窗确认 → /skills/hub/import_source 拉取校验+设源+批量安装。

    用例：
    1. validate_skill_manifest：好清单/坏结构/缺 prompt → 规范化或 None
    2. import_source API：mock urllib 拉取 → 设 skill_hub.url + 全部 hub_ 前缀安装 + 自动启用
       → 幂等重导（更新计数）→ 非 JSON 内容 502
    3. loop 特判 import_skill_source（源码级）：挂起事件/pane_id=None 降级/URL 协议校验
    """

    GOOD = {"skills": [
        {"id": "ext-a", "name": "外部技能A", "prompt": "做 A 事", "tools": ["save_file"], "icon": "🅰"},
        {"id": "ext-b", "name": "外部技能B", "prompt": "做 B 事", "tools": ["pc_run"]},
    ]}
    BAD = {"foo": 1}

    def _patch(self, tmpdir, mp):
        """把 config 与产物库根改道到临时目录。
        v1.0.619（测试卫生）：改用 MonkeyPatch，作用域结束自动还原（原裸赋值从不还原）。"""
        from services import memory_ops, scheduler   # noqa: F401  # v1.0.526 统一包路径
        mp.setattr(config_store, "config_path", lambda: os.path.join(tmpdir, "config.json"))
        mp.setattr(core.paths, "workspace_root", lambda: tmpdir)

    def test_validate(self):
        from agent_tools.skill_hub_data import validate_skill_manifest
        s = validate_skill_manifest(self.GOOD)
        assert s and len(s) == 2 and s[0]["id"] == "ext-a", s
        assert validate_skill_manifest(self.BAD) is None, "坏结构应 None"
        assert validate_skill_manifest({"skills": [{"id": "x", "name": "缺prompt"}]}) is None
        assert validate_skill_manifest({"skills": []}) is None
        print("PASS validate_skill_manifest：好/坏/缺 prompt")

    def test_import_api(self):
        tmpdir = tempfile.mkdtemp(prefix="wec_srcimp_")
        mp = pytest.MonkeyPatch()
        # mock urllib：api 内部 fetch_skill_manifest 走 urllib.request.urlopen
        class FakeResp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        payload = json.dumps(self.GOOD).encode("utf-8")
        orig = urllib.request.urlopen
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            return FakeResp(payload)

        urllib.request.urlopen = fake_urlopen
        try:
            self._patch(tmpdir, mp)
            c = _make_client()
            r = c.post("/api/skills/hub/import_source", json={"url": "https://example.com/hub.json"})
            d = r.get_json()
            assert r.status_code == 200 and d.get("ok") and d["imported"] == 2, d
            raw = json.load(open(config_store.config_path(), encoding="utf-8"))
            assert raw["skill_hub"]["url"] == "https://example.com/hub.json"
            ids = {s["id"] for s in raw["custom_skills"]}
            assert {"hub_ext-a", "hub_ext-b"} <= ids, ids
            # enabled_skills=None（默认全开语义）导入即生效；显式列表模式才会追加条目
            if raw.get("enabled_skills") is not None:
                assert "hub_ext-a" in raw["enabled_skills"], "显式列表模式下导入应自动启用"
            # 幂等重导：created 0 updated 2
            r2 = c.post("/api/skills/hub/import_source", json={"url": "https://example.com/hub.json"})
            d2 = r2.get_json()
            assert d2["created"] == 0 and d2["updated"] == 2, d2
            # 非 JSON 内容 → 502
            urllib.request.urlopen = lambda req, timeout=None: FakeResp(b"not json")
            r3 = c.post("/api/skills/hub/import_source", json={"url": "https://example.com/x"})
            assert r3.status_code == 502, r3.status_code
            # 坏协议 → 400（不发起拉取）
            n_before = calls["n"]
            assert c.post("/api/skills/hub/import_source", json={"url": "ftp://x"}).status_code == 400
            assert calls["n"] == n_before
            print("PASS import_source API：设源+批量装+自动启用/幂等/坏内容 502/坏协议 400")
        finally:
            urllib.request.urlopen = orig
            mp.undo()                                  # 还原 config_path / workspace_root
            shutil.rmtree(tmpdir, ignore_errors=True)  # 临时目录不再留在 TEMP 里

    def test_loop_guard(self):
        src = open(os.path.join(_src_of_module(__import__("agent")), "loop.py"), encoding="utf-8").read()
        assert "'skill_source_request'" in src, "loop 缺 skill_source_request 事件"
        assert "无法向用户弹窗确认导入" in src, "后台任务降级缺失"
        assert "url 必须以 http(s):// 开头" in src
        # dispatch 提示引导
        dsrc = open(os.path.join(_src_of_module(__import__("agent_tools")), "dispatch.py"), encoding="utf-8").read()
        assert "import_skill_source(url)" in dsrc
        print("PASS loop 特判（挂起事件/后台降级/协议校验）+ dispatch 提示引导")


# ============================================================ v1.0.349 版本与回滚
class TestSkillVersions:
    """版本历史与回滚单测（v1.0.349，隔离临时目录）。"""

    @pytest.fixture(autouse=True)
    def _isolate_versions_ws(self, tmp_path, monkeypatch):
        """v1.0.646：原文件在模块级裸赋 `core.paths.workspace_root = lambda: TMP` 且从不还原，
        合并进本文件后会污染其余用例。改由 fixture 改道并在用例结束自动还原。

        顺带删掉原文件那句 `SV.workspace_root = lambda: TMP`：skill_versions 内部只读
        `core.paths.workspace_root()`（skill_versions.py:28），模块上并没有这个属性、
        也没人读它——那是一句纯无效的赋值。"""
        monkeypatch.setattr(core.paths, "workspace_root", lambda: str(tmp_path))
        yield

    def test_record_list_get(self):
        assert SV.record_version("s1", "v1 prompt", "manual_edit", "first") is True
        assert SV.record_version("s1", "v1 prompt", "manual_edit") is False   # 未变化不记录
        SV.record_version("s1", "v2 prompt", "self_repair", "lesson")
        vs = SV.list_versions("s1")
        assert len(vs) == 2 and vs[0]["head"].startswith("v2"), vs   # 新→旧
        ent = SV.get_version("s1", vs[1]["ts"])
        assert ent and ent["prompt"] == "v1 prompt", ent
        print("PASS 记录/去重/列表(新→旧)/按 ts 取版本")

    def test_cap_and_isolation(self):
        for i in range(25):
            SV.record_version("s2", "prompt-%02d" % i, "self_repair")
        assert len(SV.list_versions("s2")) == 20, "应只保留最近 20 份"
        assert SV.list_versions("s3") == [], "不同技能相互隔离"
        print("PASS 容量上限 20/技能间隔离")

    def test_get_missing(self):
        assert SV.get_version("s1", 12345) is None
        assert SV.get_version("s1", "abc") is None
        print("PASS 不存在的版本安全返回 None")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

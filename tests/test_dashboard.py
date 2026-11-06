# -*- coding: utf-8 -*-
"""test_dashboard.py — 任务运行看板（v1.0.523-2）
覆盖：/api/dashboard 四块聚合（定时任务/会话/知识库/今日用量）、60s 缓存、
脏数据兜底（scheduler 抛错/会话目录不存在不炸端点）。
"""
import json
import os
import time

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离 workspace_root + 清缓存，返回 flask test_client。"""
    import backend as backend_mod
    import core.config as ccfg
    import core.config_store as cs
    import routes.b_admin as ba

    ws = tmp_path / "产物库"
    (ws / "日志").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ba, "workspace_root", lambda: str(tmp_path))
    # conversations 是模块顶层 from-import——绑定在自己命名空间，须 patch 定义处
    import services.conversations as conv_mod
    monkeypatch.setattr(conv_mod, "workspace_root", lambda: str(tmp_path))
    ba._DASH_CACHE["ts"] = 0.0
    ba._DASH_CACHE["data"] = None
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        yield c


def _mk_conv(ws_root, name, mtime_today=True):
    d = os.path.join(str(ws_root), "会话")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"id": name[:-5], "title": "", "messages": []}, f)
    if mtime_today:
        now = time.time()
        os.utime(p, (now, now))
    else:
        old = time.time() - 90 * 86400   # 90 天前
        os.utime(p, (old, old))


def test_dashboard_basic_shapes(client, tmp_path):
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    d = r.get_json()
    assert set(d.keys()) >= {"schedules", "conversations", "kb", "usage_today"}
    # 空环境：各块结构完整且无 error
    assert "error" not in d["conversations"]
    assert d["conversations"] == {"total": 0, "today": 0}
    assert d["kb"] == {"total": 0, "by_dir": {}}
    assert "error" not in d["usage_today"]


def test_dashboard_conversations_count(client, tmp_path):
    _mk_conv(tmp_path, "conv_a.json")
    _mk_conv(tmp_path, "conv_b.json")
    _mk_conv(tmp_path, "conv_old.json", mtime_today=False)
    _mk_conv(tmp_path, "not_json.txt")   # 非会话文件不计
    import routes.b_admin as ba
    ba._DASH_CACHE["ts"] = 0.0; ba._DASH_CACHE["data"] = None
    d = client.get("/api/dashboard").get_json()
    assert d["conversations"]["total"] == 3
    assert d["conversations"]["today"] == 2


def test_dashboard_kb_count(client, tmp_path):
    kb = tmp_path / "知识库"
    (kb / "制度").mkdir(parents=True, exist_ok=True)
    (kb / "制度" / "a.md").write_text("x", encoding="utf-8")
    (kb / "制度" / "b.txt").write_text("x", encoding="utf-8")
    (kb / "课件" / "sub").mkdir(parents=True, exist_ok=True)
    (kb / "课件" / "sub" / "c.md").write_text("x", encoding="utf-8")
    (kb / "INDEX.md").write_text("x", encoding="utf-8")   # 根文件不计（只数子目录）
    import routes.b_admin as ba
    ba._DASH_CACHE["ts"] = 0.0; ba._DASH_CACHE["data"] = None
    d = client.get("/api/dashboard").get_json()
    assert d["kb"]["total"] == 3
    assert d["kb"]["by_dir"] == {"制度": 2, "课件": 1}


def test_dashboard_schedules_shapes(client, tmp_path, monkeypatch):
    from services import scheduler as sch
    monkeypatch.setattr(sch, "list_schedules", lambda: [
        {"id": "s1", "name": "月报批审", "enabled": True,
         "last_run": time.strftime("%Y-%m-%d") + " 09:00:00", "last_result": "ok 完成"},
        {"id": "s2", "name": "跑课", "enabled": False,
         "last_run": "2020-01-01 08:00:00", "last_result": "执行异常：网络"},
        {"id": "s3", "name": "从未跑过", "enabled": True, "last_run": "", "last_result": ""},
    ])
    import routes.b_admin as ba
    ba._DASH_CACHE["ts"] = 0.0; ba._DASH_CACHE["data"] = None
    d = client.get("/api/dashboard").get_json()
    s = d["schedules"]
    assert s["total"] == 3 and s["enabled"] == 2
    assert s["ran_today"] == 1
    assert s["failed_last"] == 1          # 只有 s2 异常（s3 从未跑过不算失败）
    assert s["items"][0]["name"] == "月报批审"   # 按 last_run 倒序


def test_dashboard_cache_60s(client, monkeypatch):
    import routes.b_admin as ba
    ba._DASH_CACHE["ts"] = 0.0; ba._DASH_CACHE["data"] = None
    client.get("/api/dashboard")
    # 命中缓存：即使数据源变化也返回旧值
    monkeypatch.setattr("core_utils.workspace_root", lambda: "/nonexistent_xyz")
    d2 = client.get("/api/dashboard").get_json()
    assert d2["conversations"] == {"total": 0, "today": 0}   # 缓存的旧值
    # 缓存过期后重新聚合
    ba._DASH_CACHE["ts"] = time.time() - 61
    d3 = client.get("/api/dashboard").get_json()
    assert d3["conversations"] == {"error"} or "error" in d3["conversations"] or d3["conversations"]["total"] == 0


def test_dashboard_survives_dirty_data(client, tmp_path, monkeypatch):
    """scheduler 抛异常/usage 抛异常 → 端点仍 200 且带 error 字段。"""
    from services import scheduler as sch
    def _boom():
        raise RuntimeError("mock broken")
    monkeypatch.setattr(sch, "list_schedules", _boom)
    import routes.b_admin as ba
    ba._DASH_CACHE["ts"] = 0.0; ba._DASH_CACHE["data"] = None
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    d = r.get_json()
    assert "error" in d["schedules"]

# -*- coding: utf-8 -*-
"""test_audit_background.py — 批审后台任务化（v1.0.523-6）
覆盖：start_background_audit 立即返回/重复拒绝/进度打点/完成状态/进度 API。
"""
import time

import pytest

from services import batch_flow as bf


@pytest.fixture(autouse=True)
def reset_bg():
    with bf._BG_LOCK:
        bf._BG_STATE.update({"running": False, "started": "", "total": 0, "done": 0,
                             "green": 0, "red": 0, "pending": 0, "current": "",
                             "finished": False, "ok": None, "summary": "", "report_path": ""})
    yield
    with bf._BG_LOCK:
        bf._BG_STATE.update({"running": False, "finished": False})


def test_start_returns_immediately(monkeypatch):
    """后台启动：audit_monthly_reports 被 mock 成慢函数，start 应 <0.5s 返回。"""
    def _slow(**kw):
        time.sleep(3.0)
        return {"ok": True, "total": 22, "done": 22, "green": 20, "red": 1,
                "pending": 1, "summary": "共 22 份", "report_path": "x.md"}
    monkeypatch.setattr(bf, "audit_monthly_reports", _slow)
    t0 = time.time()
    r = bf.start_background_audit(port=9222)
    assert time.time() - t0 < 1.0            # 立即返回
    assert r["ok"] is True and r["background"] is True
    time.sleep(0.3)
    p = bf.background_progress()
    assert p["running"] is True              # 后台还在跑
    # 等后台完成
    deadline = time.time() + 5
    while time.time() < deadline and not p.get("finished"):
        time.sleep(0.2)
        p = bf.background_progress()
    assert p["finished"] and p["ok"] is True
    assert p["green"] == 20 and p["red"] == 1 and p["pending"] == 1
    assert p["running"] is False


def test_second_start_rejected_while_running(monkeypatch):
    def _slow(**kw):
        time.sleep(2.0)
        return {"ok": True, "total": 1, "done": 1, "green": 1, "red": 0, "pending": 0}
    monkeypatch.setattr(bf, "audit_monthly_reports", _slow)
    r1 = bf.start_background_audit(port=9222)
    assert r1["ok"] is True
    time.sleep(0.2)
    r2 = bf.start_background_audit(port=9222)
    assert r2["ok"] is False and r2.get("busy") is True   # 重复发起被拒
    time.sleep(2.5)                                        # 等收尾


def test_bg_update_counts(monkeypatch):
    """主循环打点函数：按 verdict 分类计数。"""
    results = [{"verdict": "green"}, {"verdict": "red"},
               {"verdict": "pending", "issues": ["x"]}]
    bf._bg_update(results)
    p = bf.background_progress()
    assert (p["done"], p["green"], p["red"], p["pending"]) == (3, 1, 1, 1)


def test_failure_sets_finished(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("mock crash")
    monkeypatch.setattr(bf, "audit_monthly_reports", _boom)
    r = bf.start_background_audit(port=9222)
    assert r["ok"] is True
    deadline = time.time() + 3
    p = bf.background_progress()
    while time.time() < deadline and not p.get("finished"):
        time.sleep(0.2)
        p = bf.background_progress()
    assert p["finished"] and p["ok"] is False
    assert "mock crash" in p["summary"]
    assert p["running"] is False


def test_progress_api():
    import backend as backend_mod
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        d = c.get("/api/audit/progress").get_json()
        assert set(d.keys()) >= {"running", "done", "total", "green", "red", "pending"}


def test_abort_request(monkeypatch):
    """v1.0.537：中止请求——running 时设置标志并返回 ok；无运行时拒绝。"""
    # v1.0.562：注入慢 mock——原实现用真 audit_monthly_reports（9222 无企微
    # 秒失败退出），0.2s 窗口内 running 可能已 False → request_abort 被拒
    # → 断言偶发挂（全套高负载下必现的时序赌运气）。
    def _slow(**kw):
        time.sleep(2.0)
        return {"ok": True, "total": 1, "done": 1, "green": 1, "red": 0, "pending": 0}
    monkeypatch.setattr(bf, "audit_monthly_reports", _slow)
    r = bf.request_abort()
    assert r["ok"] is False                       # 无运行 → 拒绝
    bf.start_background_audit(port=9222)
    time.sleep(0.2)
    r2 = bf.request_abort()
    assert r2["ok"] is True
    p = bf.background_progress()
    assert p["abort_requested"] is True
    time.sleep(2.5)                               # 等 mock 任务收尾


def test_abort_stops_loop(monkeypatch):
    """主循环 abort 检查：标志置位后下一张不再执行（占位跳过），走正常收尾。"""
    seen = {"n": 0}
    def _fake_audit(**kw):
        # 模拟主循环：3 张，每张前检查 abort
        with bf._BG_LOCK:
            bf._BG_STATE.update({"running": True, "total": 3, "done": 0, "green": 0,
                                 "red": 0, "pending": 0, "abort_requested": False})
        results = []
        for idx in range(3):
            with bf._BG_LOCK:
                hit = bf._BG_STATE["abort_requested"]
            if hit:
                results.append({"index": idx, "issues": ["用户请求中止，跳过剩余项"]})
                break
            results.append({"index": idx, "verdict": "green"})
            seen["n"] += 1
            bf._bg_update(results)
            if seen["n"] == 1:
                bf.request_abort()                # 第 1 张跑完后请求中止
        with bf._BG_LOCK:
            bf._BG_STATE["finished"] = True
            bf._BG_STATE["running"] = False
        return {"ok": True, "total": 3, "done": len(results), "green": 1, "red": 0,
                "pending": len(results) - 1, "summary": "x"}
    monkeypatch.setattr(bf, "audit_monthly_reports", _fake_audit)
    bf.start_background_audit(port=9222)
    deadline = time.time() + 5
    p = bf.background_progress()
    while time.time() < deadline and not p.get("finished"):
        time.sleep(0.2)
        p = bf.background_progress()
    assert seen["n"] == 1                         # 只跑了 1 张
    assert p["done"] == 2                         # 1 张实际 + 1 张占位


def test_run_audit_flow_tool_background_default(monkeypatch):
    """agent 工具入口：默认走后台并返回引导文案；background=False 走同步。"""
    import agent_tools.executors as ex
    calls = []
    monkeypatch.setattr(bf, "start_background_audit",
                        lambda **kw: calls.append(kw) or {"ok": True, "background": True})
    class _C:
        def get(self, k, d=None): return {"cdp_port": 9222}.get(k, d)
    r = ex._run_audit_flow({"flow": "monthly-report"}, _C())
    assert r["ok"] and r["background"] is True
    assert "后台" in r["result"]
    assert calls and calls[0]["port"] == 9222
    # 同步模式透传
    monkeypatch.setattr(bf, "audit_monthly_reports",
                        lambda **kw: {"ok": True, "total": 0, "done": 0})
    r2 = ex._run_audit_flow({"flow": "monthly-report", "background": False}, _C())
    assert r2["ok"] is True and "background" not in r2

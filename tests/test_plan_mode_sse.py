# -*- coding: utf-8 -*-
"""v1.0.483：三模式 SSE 契约回归——plan 模式事件必须是可直接编码的 SSE 字符串。
背景：v1.0.472 plan 流式化时 yield dict → Flask 响应迭代 TypeError（expected
bytes-like, dict found）→ 前端拿空流，「先出方案」永远没回答（用户实测 21:09）。
本测试用 mock 流端到端跑 /api/chat mode=plan，锁死字节格式契约。"""
import json
import sys
import os
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("WECOM_TEST_MODE", "1")


def test_plan_mode_sse_bytes():
    """plan 模式 /api/chat 响应体必须是可拼接的 SSE 字节流（data: {...}）。"""
    import backend

    def fake_stream(model, msgs, **kw):
        yield ("reasoning", "思考中...")
        yield ("token", "## 📋 执行方案\n1. 测试步骤")
        yield ("done", None)

    import llm_client
    client = backend.app.test_client()
    client.get("/")   # 种鉴权 cookie
    with mock.patch.object(llm_client, "stream_llm_events", fake_stream), \
         mock.patch.object(llm_client, "resolve_model", return_value={"name": "T", "model": "t", "base_url": "http://x"}):
        rv = client.post("/api/chat", json={"messages": [{"role": "user", "content": "做个ppt"}],
                                            "pane_id": "testPanePlan", "mode": "plan"})
        assert rv.status_code == 200, rv.status_code
        body = rv.get_data()   # ← 此处曾抛 TypeError: expected bytes-like, dict found
    assert b'"type": "token"' in body or b'"type":"token"' in body, body[:200]
    assert "执行方案" in body.decode("utf-8"), body[:300]
    # meta 首事件 + done 终事件齐备
    assert (b'"type": "meta"' in body or b'"type":"meta"' in body), body[:200]
    assert (b'"type": "done"' in body or b'"type":"done"' in body), body[-200:]
    # 每个事件都是 data: 行（SSE 格式）
    lines = [l for l in body.decode("utf-8").splitlines() if l.strip()]
    assert all(l.startswith("data: ") for l in lines), lines[:3]
    print("PASS plan 模式 SSE 字节契约（meta/token/done 全为 data: 行）")


def test_plan_mode_reasoning_forwarded():
    """plan 模式必须转发 reasoning 事件（推理模型思考期前端可见，不再失明）。"""
    import backend

    def fake_stream(model, msgs, **kw):
        yield ("reasoning", "我需要先分析任务...")
        yield ("token", "方案正文")

    import llm_client
    client = backend.app.test_client()
    client.get("/")
    with mock.patch.object(llm_client, "stream_llm_events", fake_stream), \
         mock.patch.object(llm_client, "resolve_model", return_value={"name": "T", "model": "t", "base_url": "http://x"}):
        rv = client.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}],
                                            "pane_id": "testPanePlan2", "mode": "plan"})
        body = rv.get_data()
    assert "推理" in body.decode("utf-8") or b"reasoning" in body, body[:200]
    print("PASS plan 模式 reasoning 事件转发")


if __name__ == "__main__":
    test_plan_mode_sse_bytes()
    test_plan_mode_reasoning_forwarded()
    print("\n== 全部通过：plan 模式 SSE 契约 ==")

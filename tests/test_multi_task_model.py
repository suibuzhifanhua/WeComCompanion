# -*- coding: utf-8 -*-
"""v1.0.486：多任务并行 + 每任务独立模型 回归。
锁死两条链路：①/api/chat 的 model_id 透传到 agent_loop；②/chat/continue 从断点
恢复会话自己的 model_id（与 readonly 同机制）。"""
import sys
import os
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_gen(*a, **kw):
    """假 agent_loop：捕获 kwargs 并产出一条合法 SSE。"""
    yield "data: " + '{"type":"meta","model":"T","ctx_chars":0,"rounds":0,"max_rounds":1}' + "\n\n"
    yield "data: " + '{"type":"token","text":"ok"}' + "\n\n"
    yield "data: " + '{"type":"done","summary":{"tool_calls":0,"elapsed_s":0.1}}' + "\n\n"


def test_chat_model_id_passthrough():
    import routes.chat as C
    import backend
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return _fake_gen(**kw)

    client = backend.app.test_client()
    client.get("/")   # 种鉴权 cookie
    with mock.patch.object(C, "agent_loop", side_effect=_spy):
        rv = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
            "pane_id": "conv_testA", "model_id": "my-model-2"})
        assert rv.status_code == 200, rv.status_code
        rv.get_data()
    assert captured.get("model_id") == "my-model-2", captured
    assert captured.get("pane_id") == "conv_testA", captured
    print("PASS /api/chat model_id 透传到 agent_loop")


def test_continue_restores_model_id():
    """断点持久化 model_id → /chat/continue 恢复（请求未带时用持久值）。"""
    import routes.chat as C
    import backend
    from agent.session import _save_chat_state, chat_states, _chat_state_lock

    pane = "conv_testB"
    # 注意：_save_chat_state 内部自持锁（Lock 不可重入）——测试不能在外层再包 with
    _save_chat_state(pane, [{"role": "system", "content": "s"},
                             {"role": "user", "content": "hi"}], 1,
                     readonly=False, model_id="persisted-model")
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return _fake_gen(**kw)

    client = backend.app.test_client()
    client.get("/")
    with mock.patch.object(C, "agent_loop", side_effect=_spy):
        rv = client.post("/api/chat/continue", json={"pane_id": pane})
        assert rv.status_code == 200, rv.status_code
        rv.get_data()
    assert captured.get("model_id") == "persisted-model", captured
    # 请求显式带的 model_id 优先于持久值
    with mock.patch.object(C, "agent_loop", side_effect=_spy):
        rv = client.post("/api/chat/continue", json={"pane_id": pane, "model_id": "override"})
        rv.get_data()
    assert captured.get("model_id") == "override", captured
    # 清理
    with _chat_state_lock:
        chat_states.pop(pane, None)
    print("PASS /chat/continue 恢复断点 model_id（显式指定优先）")


if __name__ == "__main__":
    test_chat_model_id_passthrough()
    test_continue_restores_model_id()
    print("\n== 全部通过：多任务模型链路 ==")

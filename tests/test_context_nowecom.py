# -*- coding: utf-8 -*-
"""v1.0.305：未附加企微上下文时的行为（未启动企微也能对话/操控电脑）。
① use_wecom_context=False 不再调 wb.window_tree_text（此前每条消息都白打一次必败 CDP 请求+错误日志）
② 注入「运行环境」system 提示：告知 pc_*/文件/联网可用，勿以缺企微窗口为由拒绝任务
③ use_wecom_context=True 但 CDP 不可用 → 仍走既有「获取失败」兜底，不炸。
运行：pytest tests/test_context_nowecom.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.context as ctx
import wecom_bridge as wb


def test_nowecom_skips_window_tree_and_notes(monkeypatch):
    """未附加企微上下文：不调 window_tree_text，且注入运行环境提示（含 pc_* 能力清单）。"""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise RuntimeError("CDP down")

    monkeypatch.setattr(wb, "window_tree_text", _boom)
    msgs = ctx.build_chat_messages([{"role": "user", "content": "你好"}], {}, 9222, None,
                                   use_wecom_context=False)
    assert called["n"] == 0, f"use_wecom_context=False 不应调 window_tree_text，实际 {called['n']} 次"
    joined = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    assert "未附加企微窗口上下文" in joined, "应注入运行环境提示"
    assert "pc_screenshot" in joined and "pc_run" in joined, "提示应列明本机操控工具"
    assert "list_wecom_windows" in joined, "提示应说明企微任务的探测路径"
    # 用户消息仍在末尾、未被吞
    assert msgs[-1] == {"role": "user", "content": "你好"}


def test_wecom_context_true_cdp_down_falls_back(monkeypatch):
    """附加企微上下文但 CDP 不可用：走既有「获取失败」兜底，不抛异常。"""
    def _raise(*a, **k):
        raise RuntimeError("CDP down")

    monkeypatch.setattr(wb, "window_tree_text", _raise)
    monkeypatch.setattr(wb, "capture", _raise)
    msgs = ctx.build_chat_messages([{"role": "user", "content": "hi"}], {}, 9222, None,
                                   use_wecom_context=True)
    joined = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    assert "获取失败" in joined, "CDP 不可用时应出现既有兜底文案"
    assert "未附加企微窗口上下文" not in joined, "已请求企微上下文时不应出现「未附加」提示"


def test_wecom_context_true_cdp_ok_no_note(monkeypatch):
    """CDP 正常：注入窗口关系图，且不出现「未附加」提示。"""
    monkeypatch.setattr(wb, "window_tree_text", lambda *a, **k: "窗口1《列表页》\n窗口2《详情页》(父:窗口1)")
    monkeypatch.setattr(wb, "capture", lambda *a, **k: {"__error__": "跳过 capture"})
    msgs = ctx.build_chat_messages([{"role": "user", "content": "hi"}], {}, 9222, None,
                                   use_wecom_context=True)
    joined = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    assert "企微窗口关系" in joined
    assert "未附加企微窗口上下文" not in joined

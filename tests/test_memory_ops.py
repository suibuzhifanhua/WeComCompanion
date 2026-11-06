# -*- coding: utf-8 -*-
"""test_memory_ops — 本地记忆沉淀（v1.0.312，对齐 WorkBuddy 记忆沉淀）。

用例：
1. remember_async 总闸关闭时不动笔；_extract 无记忆时零写入
2. 提炼落盘：带时间戳追加 + 二次去重（同内容不重复）
3. 上限裁最旧（MAX_LINES）
4. memory_text 尾部读取 + clear 清空
5. build_chat_messages 注入 [长期记忆] 段（flags 开）且 flags 关时不注入
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_store  # noqa: E402
from services import memory_ops  # noqa: E402


def _patch(tmpdir):
    orig_cp = config_store.config_path
    config_store.config_path = lambda: os.path.join(tmpdir, "config.json")
    orig_ws = memory_ops.workspace_root
    memory_ops.workspace_root = lambda: tmpdir
    return orig_cp, orig_ws


def _restore(cp, ws):
    config_store.config_path = cp
    memory_ops.workspace_root = ws


def test_gate_and_extract_none(monkeypatch=None):
    tmpdir = tempfile.mkdtemp(prefix="wec_mem_")
    cp, ws = _patch(tmpdir)
    try:
        # 总闸关：remember_async 直接返回（无线程写入）
        cfg = {"feature_flags": {"memory": False}}
        memory_ops.remember_async("用户喜欢表格", "好的", cfg)
        assert not os.path.isfile(memory_ops.memory_path()), "闸关时不应写记忆"
        # 提炼结果「无」→ 不写
        orig = memory_ops._extract_one_memory
        memory_ops._extract_one_memory = lambda t, c: None
        try:
            memory_ops.remember_async("今天天气", "好的", {"feature_flags": {"memory": True}})
        finally:
            memory_ops._extract_one_memory = orig
        assert not os.path.isfile(memory_ops.memory_path()), "无记忆时不应创建文件"
        print("PASS 总闸关闭/无记忆 零写入")
    finally:
        _restore(cp, ws)


def test_append_dedup_and_cap(monkeypatch=None):
    tmpdir = tempfile.mkdtemp(prefix="wec_mem2_")
    cp, ws = _patch(tmpdir)
    try:
        ok1 = memory_ops._append_memory("用户偏好用表格汇报数据")
        ok2 = memory_ops._append_memory("用户偏好用表格汇报数据")     # 完全重复
        ok3 = memory_ops._append_memory("项目 CHD-Adventure 的构建脚本是 build.py")
        assert ok1 is True and ok2 is False and ok3 is True, (ok1, ok2, ok3)
        txt = open(memory_ops.memory_path(), encoding="utf-8").read()
        assert txt.count("用户偏好用表格汇报数据") == 1, "去重失败"
        # v1.0.328：cap 拆独立函数（Jaccard 去重会把纯前缀填充条目判重——语义正确，
        # cap 逻辑改由本函数直接单测）
        fake = [f"行{i}" for i in range(210)]
        capped = memory_ops.cap_lines(fake)
        assert len(capped) == memory_ops.MAX_LINES, len(capped)
        assert capped[-1] == "行209" and capped[0] == "行10", "应裁最旧留最新"
        # memory_text 尾读
        mt = memory_ops.memory_text(200)
        assert mt and "构建脚本" in mt, "尾读应包含已有条目"
        # clear
        n = memory_ops.clear_memory()
        assert n == 2 and not os.path.isfile(memory_ops.memory_path())
        print("PASS 追加/去重/上限裁最旧/尾读/清空")
    finally:
        _restore(cp, ws)


def test_inject_in_chat_messages():
    tmpdir = tempfile.mkdtemp(prefix="wec_mem3_")
    cp, ws = _patch(tmpdir)
    try:
        memory_ops._append_memory("用户的项目叫企微伴侣，产物库统一放产出")
        cfg = {"feature_flags": {"memory": True}, "models": [], "skill_overrides": {},
               "custom_skills": [], "connectors": {}, "enabled_skills": None,
               "wecom": {}, "skill_hub": {}}
        from agent.context import build_chat_messages
        msgs = build_chat_messages([{"role": "user", "content": "在吗"}], cfg, 9222, None, False)
        sysc = " ".join(m["content"] for m in msgs if m["role"] == "system")
        assert "长期记忆" in sysc and "企微伴侣" in sysc, sysc[:300]
        # 闸关 → 不注入
        cfg["feature_flags"]["memory"] = False
        msgs2 = build_chat_messages([{"role": "user", "content": "在吗"}], cfg, 9222, None, False)
        sysc2 = " ".join(m["content"] for m in msgs2 if m["role"] == "system")
        assert "长期记忆" not in sysc2
        print("PASS build_chat_messages 注入 [长期记忆]（闸开注入/闸关不注入）")
    finally:
        _restore(cp, ws)


if __name__ == "__main__":
    test_gate_and_extract_none()
    test_append_dedup_and_cap()
    test_inject_in_chat_messages()
    print("\n== 全部通过：记忆沉淀 ==")

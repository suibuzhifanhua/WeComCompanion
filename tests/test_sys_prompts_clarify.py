# -*- coding: utf-8 -*-
"""test_sys_prompts_clarify — 主 agent 提示词必须含「创作类先澄清」纪律（v1.0.311）。

背景：用户反馈「叫 agent 做 PPT 它不问风格/适用人群，其他 agent 都会」。
根因：SYSTEM_PROMPT 全是执行纪律（持续调用工具/不要中途停止），无创作类澄清指引。
修复：新增【创作类任务先澄清】段——一轮问齐 5 项（用途受众/风格/篇幅/素材/交付格式），
执行类任务（答题/填表/审核）不受影响；用户说「直接做」时免问带假设先产出。
本测试防未来逐字迁移/重写时丢失该段。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.sys_prompts import SYSTEM_PROMPT  # noqa: E402


def test_clarify_section_present():
    assert "创作类任务先澄清" in SYSTEM_PROMPT, "SYSTEM_PROMPT 缺少创作类澄清段"
    print("PASS SYSTEM_PROMPT 含【创作类任务先澄清】段")


def test_clarify_key_points():
    for kw in ("用途与受众", "风格", "篇幅", "素材", "交付格式", "一轮", "直接做", "假设"):
        assert kw in SYSTEM_PROMPT, f"澄清段缺关键要素: {kw}"
    print("PASS 澄清段五要素齐全（用途受众/风格/篇幅/素材/交付格式）+ 一轮问齐 + 直接做免问")


def test_execute_tasks_unaffected():
    """执行纪律必须仍在，且明确两者分流不冲突。"""
    assert "必须持续调用工具一步步完成" in SYSTEM_PROMPT, "执行纪律段丢失"
    assert "不需要澄清直接执行" in SYSTEM_PROMPT, "缺少执行类/创作类分流说明"
    print("PASS 执行类任务纪律保留 + 分流说明在位")


def test_quick_tpl_agents_keep_confirm_flow():
    """快捷入口/模板设计助手的需求确认机制不受影响。"""
    from agent.sys_prompts import _QUICK_AGENT_SYSTEM, _TPL_AGENT_SYSTEM
    assert "必须与用户确认的硬指标" in _QUICK_AGENT_SYSTEM
    assert "必须与用户确认的硬指标" in _TPL_AGENT_SYSTEM
    print("PASS 快捷入口/模板设计助手的多轮确认机制未被波及")


if __name__ == "__main__":
    test_clarify_section_present()
    test_clarify_key_points()
    test_execute_tasks_unaffected()
    test_quick_tpl_agents_keep_confirm_flow()
    print("\n== 全部通过：创作类澄清纪律 ==")

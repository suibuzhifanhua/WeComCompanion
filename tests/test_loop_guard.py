# -*- coding: utf-8 -*-
"""tests/test_loop_guard.py — v1.0.345 LoopGuard 三路熔断状态机单测。
覆盖：warn/kill 边界、复位语义（写打断读/读打断写/成功复位错循环）、
导航白名单、偶发 error 不误杀。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.loop_guard import LoopGuard, is_read_tool, make_sig, is_nav_click  # noqa: E402


def test_write_warn_then_kill():
    g = LoopGuard()
    seq = []
    for i in range(10):
        act, n = g.on_write("click_wecom_element", {"target": "按钮A", "pick": 0}, False)
        seq.append((act, n))
    # 连续第 4 次 warn（首次），第 8 次 kill（warned 已置位）
    assert seq[3] == ("warn", 4), seq[3]
    assert seq[7] == ("kill", 8), seq[7]
    assert all(a == "kill" for a, _ in seq[7:]), seq[7:]
    print("PASS 写循环 warn=4/kill=8 边界")


def test_write_interrupted_by_read_resets():
    g = LoopGuard()
    for _ in range(3):
        g.on_write("click_wecom_element", {"target": "A"}, False)
    g.on_read("read_wecom_page", {"pick": 0}, False)   # 穿插读 → 写计数复位
    act, n = g.on_write("click_wecom_element", {"target": "A"}, False)
    assert (act, n) == ("ok", 1), (act, n)   # 重新从 1 计数
    print("PASS 穿插读操作复位写循环计数（逐题推进不误伤）")


def test_write_err_then_success_resets_err():
    g = LoopGuard()
    for _ in range(5):   # 同一写操作连续报错 5 次（< err_warn=6）
        act, n = g.on_write("click_wecom_element", {"target": "A"}, True)
    assert act == "ok" and n == 5, (act, n)
    act, n = g.on_write("click_wecom_element", {"target": "A"}, False)   # 成功 → 错计数复位
    assert act == "ok", (act, n)
    for _ in range(5):   # 再报错 5 次 → 从 1 重新累计，不触发 warn
        act, n = g.on_write("click_wecom_element", {"target": "A"}, True)
    assert act == "ok" and n == 5, (act, n)
    print("PASS 成功写操作复位错循环计数（偶发 error 重试不误杀）")


def test_err_cycle_warn_then_kill():
    g = LoopGuard()
    acts = []
    for _ in range(12):
        acts.append(g.on_write("click_wecom_element", {"target": "B"}, True)[0])
    assert acts[5] == "warn", acts   # 连续 6 次 error → warn
    assert acts[9] == "kill", acts   # 干预后到 10 次 → kill
    print("PASS 错循环 warn=6/kill=10")


def test_read_cycle_and_reset():
    g = LoopGuard()
    for _ in range(4):
        g.on_read("read_wecom_page", {"pick": 0}, False)
    act, n = g.on_read("read_wecom_page", {"pick": 0}, True)   # 报错 → 读计数复位
    assert act == "ok" and n == 0, (act, n)
    act, n = g.on_read("read_wecom_page", {"pick": 0}, False)
    assert n == 1, n    # 重新计数
    print("PASS 读循环报错复位")


def test_nav_whitelist_higher_threshold():
    g = LoopGuard()
    nav_target = "\u4e0b\u4e00\u9898"   # "next question"
    acts = [g.on_write("click_wecom_element", {"target": nav_target, "pick": 0}, False)[0]
            for _ in range(12)]
    assert "warn" not in acts[:9], f"nav clicks within 9 should not warn: {acts}"
    assert acts[9] == "warn", acts   # nav_warn=10 触发（第 10 次点击）
    assert "kill" not in acts[:12], "12 次内不应 kill（nav_kill=20）"
    assert is_nav_click("click_wecom_element", {"target": "\u4e0b\u4e00\u9898"})
    assert not is_nav_click("click_wecom_element", {"target": "submit"})
    print("PASS 导航白名单独立阈值 warn=10/kill=20")


def test_finish_resets_all():
    g = LoopGuard()
    for _ in range(6):
        g.on_write("click_wecom_element", {"target": "A"}, False)
    g.on_finish()
    act, n = g.on_write("click_wecom_element", {"target": "A"}, False)
    assert (act, n) == ("ok", 1), (act, n)
    print("PASS finish 复位全部计数")


def test_helpers():
    assert is_read_tool("read_wecom_page") and is_read_tool("finish") and not is_read_tool("click_wecom_element")
    s1 = make_sig("t", {"b": 1, "a": 2})
    s2 = make_sig("t", {"a": 2, "b": 1})
    assert s1 == s2, "签名应参数序无关（sort_keys）"
    print("PASS 辅助函数（is_read_tool/make_sig 序无关）")


if __name__ == "__main__":
    test_write_warn_then_kill()
    test_write_interrupted_by_read_resets()
    test_write_err_then_success_resets_err()
    test_err_cycle_warn_then_kill()
    test_read_cycle_and_reset()
    test_nav_whitelist_higher_threshold()
    test_finish_resets_all()
    test_helpers()
    print("== loop_guard ALL PASS ==")

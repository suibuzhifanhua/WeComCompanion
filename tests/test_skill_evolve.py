# -*- coding: utf-8 -*-
"""tests/test_skill_evolve.py — 技能自进化（v1.0.329）
覆盖：条件闸 / 沉淀全链（mock LLM）/ 工具白名单 / 名称去重 / 每日冷却 / 总闸 /
_memory_watch 集成（tool 事件收集 + ask 模式跳过 + distill 调用参数）。
"""
import io
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

import core_utils  # noqa: E402
from core import config_store  # noqa: E402

WS = tempfile.mkdtemp(prefix="wec_sevol_")
core.paths.workspace_root = lambda: WS
config_store.config_path = lambda: os.path.join(WS, "config.json")

from services import skill_evolve  # noqa: E402
import llm_client  # noqa: E402

# v1.0.556：skill_evolve 在模块级绑定了 core_utils.workspace_root（每日额度
# state.json 的落点）。此前只补丁 core.paths——state 一直写进【真实产物库】并跨
# 进程累积，当日跑满 DAILY_LIMIT 后整个测试文件因额度闸静默空转（dedup 断言
# 0==1，随当天累计跑数波动）。把 workspace_root 与 state 落点一并指到测试 WS。
core_utils.workspace_root = lambda: WS
skill_evolve.workspace_root = lambda: WS

_CALLS = {"n": 0}


def _fake_llm(payload):
    def f(model, messages):
        _CALLS["n"] += 1
        return json.dumps(payload, ensure_ascii=False)
    return f


def _reset_config():
    io.open(config_store.config_path(), "w", encoding="utf-8").write(json.dumps(
        {"schema_version": 2, "models": [], "custom_skills": [],
         "feature_flags": {"evolve": True}}, ensure_ascii=False))


def _reset_state():
    """v1.0.559：清当日额度 state——其它测试（agent 场景等）留下的 daemon 线程
    可能在本文件测试执行期间醒来调用 _bump_quota（agent_loop 收尾沉淀钩子），
    把当日计数顶到 DAILY_LIMIT → distill 被额度闸静默拦截 → autos 断言 0==1
    （间歇性、随线程时序波动）。每个 distill 类测试开头调用，时序无关。"""
    try:
        os.remove(os.path.join(WS, "自进化", "state.json"))
    except OSError:
        pass


def _autos():
    return [s for s in config_store.load_config().get("custom_skills", [])
            if str(s.get("id", "")).startswith("auto_")]


def _good_payload(name="下载整理"):
    return {"worth": True, "name": name, "icon": "📁", "desc": "按类型批量整理下载文件夹",
            "prompt": "1. 列出目标目录文件清单 2. 按扩展名归类为文档/图片/压缩包/其他 "
                      "3. 用 pc_files move 逐类移动 4. 输出整理前后对比表格说明",
            "tools": ["pc_files", "pc_run"]}


def test_gates_and_full_chain(monkeypatch):
    """条件闸 + 沉淀全链 + 工具白名单。"""
    _reset_config()
    _reset_state()
    _CALLS["n"] = 0
    # 条件闸：无工具不进 LLM
    skill_evolve._distill("整理下载文件夹", "已完成整理" + "x" * 100, [], {"feature_flags": {}})
    assert _CALLS["n"] == 0
    # 答复太短
    skill_evolve._distill("整理下载文件夹", "短", ["pc_files"], {"feature_flags": {}})
    assert _CALLS["n"] == 0
    # 正常沉淀
    monkeypatch.setattr(llm_client, "_llm_complete", _fake_llm(_good_payload()))
    monkeypatch.setattr(llm_client, "resolve_model", lambda cfg, mid=None: {"name": "fake"})
    skill_evolve._distill("帮我把下载文件夹按类型整理一下",
                          "已完成：共整理 37 个文件，文档 12、图片 20、压缩包 5。" + "x" * 120,
                          ["pc_files", "pc_run"], {"feature_flags": {"evolve": True}})
    assert _CALLS["n"] == 1
    autos = _autos()
    assert len(autos) == 1 and autos[0]["name"] == "下载整理" and autos[0]["auto"] is True
    assert autos[0]["tools"] == ["pc_files", "pc_run"]
    # 工具白名单：全不合法 → 放弃
    monkeypatch.setattr(llm_client, "_llm_complete", _fake_llm({"worth": True, "name": "伪造工具技能", "icon": "🔧",
                                          "desc": "x", "prompt": "步骤" * 60,
                                          "tools": ["not_a_real_tool"]}))
    skill_evolve._distill("批量重命名照片文件按日期归档", "已完成重命名" + "y" * 100,
                          ["pc_files"], {"feature_flags": {"evolve": True}})
    assert len(_autos()) == 1, "工具全不合法应放弃"
    print("PASS 条件闸 + 沉淀全链 + 工具白名单")


def test_dedup_and_worth_false(monkeypatch):
    """名称去重（Jaccard + 归一化子串）与 worth=false。"""
    _reset_config()
    _reset_state()
    monkeypatch.setattr(llm_client, "resolve_model", lambda cfg, mid=None: {"name": "fake"})
    monkeypatch.setattr(llm_client, "_llm_complete", _fake_llm(_good_payload("下载整理")))
    skill_evolve._distill("整理下载文件夹任务一", "完成" + "a" * 100, ["pc_files"], {})
    assert len(_autos()) == 1
    # 同义变体名（归一化后「下载整理」⊂「下载文件整理」）应拦截
    monkeypatch.setattr(llm_client, "_llm_complete", _fake_llm(_good_payload("下载 文件整理")))
    skill_evolve._distill("把下载目录文件分类整理", "完成" + "b" * 100, ["pc_files"], {})
    assert len(_autos()) == 1, "相似名应被拦"
    # 不同名但 worth=false
    monkeypatch.setattr(llm_client, "_llm_complete", _fake_llm({"worth": False, "reason": "一次性事务"}))
    skill_evolve._distill("查今天上海天气并告知", "今天上海晴 25 度。" + "w" * 90,
                          ["fetch_webpage"], {})
    assert len(_autos()) == 1, "worth=false 不入库"
    print("PASS 名称去重 + worth=false")


def test_daily_cooldown_and_switch(monkeypatch):
    """每日上限与总闸。"""
    _reset_config()
    monkeypatch.setattr(llm_client, "resolve_model", lambda cfg, mid=None: {"name": "fake"})
    monkeypatch.setattr(llm_client, "_llm_complete", _fake_llm(_good_payload()))
    st = skill_evolve._load_state()
    st.update({"date": __import__("datetime").date.today().isoformat(),
               "count": skill_evolve.DAILY_LIMIT})
    skill_evolve._save_state(st)
    _CALLS["n"] = 0
    skill_evolve._distill("再做一次周报汇总并生成文档", "周报已生成" + "q" * 100,
                          ["create_docx"], {})
    assert _CALLS["n"] == 0, "达每日上限不应进 LLM"
    # maybe_distill_async 总闸：evolve=False 不启线程
    _CALLS["n"] = 0
    import time
    skill_evolve.maybe_distill_async("任务描述足够长的一句话任务", "完" * 120, ["pc_files"],
                                     {"feature_flags": {"evolve": False}})
    time.sleep(0.4)
    assert _CALLS["n"] == 0
    print("PASS 每日冷却 + 总闸")


def test_memory_watch_integration(monkeypatch):
    """_memory_watch：tool 事件收集、ask 跳过、distill 参数正确。"""
    import routes.chat as rc

    captured = {}

    def fake_distill(task, final, tools, cfg):
        captured.update({"task": task, "final": final, "tools": list(tools)})

    monkeypatch.setattr("services.skill_evolve.maybe_distill_async", fake_distill)  # v1.0.526 统一包路径
    monkeypatch.setattr("services.memory_ops.remember_async", lambda *a, **k: None)  # v1.0.526 统一包路径

    def gen():
        yield {"type": "tool", "name": "pc_files", "args": {}}
        yield {"type": "tool", "name": "pc_files", "args": {}}          # 重复名去重
        yield {"type": "tool", "name": "👥 团队·规划", "args": {}}       # 团队卡排除
        yield {"type": "token", "text": "【完成】已整理 37 个文件。" + "d" * 120}

    list(rc._memory_watch(gen(), "整理下载文件夹", {"feature_flags": {}},
                          mode="agent"))
    assert captured["tools"] == ["pc_files"], captured
    assert captured["task"] == "整理下载文件夹"
    assert "【完成】" not in captured["final"] and "已整理" in captured["final"]

    # ask 模式：不调 distill
    captured.clear()
    list(rc._memory_watch(gen(), "问个问题", {"feature_flags": {}}, mode="ask"))
    assert not captured, "ask 模式应跳过 distill"
    print("PASS _memory_watch 集成（tool 收集/ask 跳过/参数）")


def test_concurrent_distill_no_overwrite(monkeypatch):
    """v1.0.331 并发落盘：两个 distill 线程同时写，两条技能都必须入库（锁内重读最新配置）。"""
    import threading
    _reset_config()
    # 重置每日冷却（前面测试可能把 count 打满）
    skill_evolve._save_state({"date": __import__("datetime").date.today().isoformat(), "count": 0})
    monkeypatch.setattr(llm_client, "resolve_model", lambda cfg, mid=None: {"name": "fake"})
    monkeypatch.setattr(llm_client, "_llm_complete", _fake_llm(None))  # 占位，下面按线程覆盖

    def make_llm(name):
        p = _good_payload(name)
        return lambda model, messages: json.dumps(p, ensure_ascii=False)

    # 共享 mock 按消息内容返回对应 payload（全局 mock 会被两线程互相覆盖造成假竞争）
    def routed_llm(model, messages):
        text = str((messages or [{}])[0].get("content", ""))
        return make_llm("服务器巡检" if "服务器巡检" in text else "报销单整理")(model, messages)

    results = {"服务器巡检": False, "报销单整理": False}

    def work(name):
        llm_client._llm_complete = routed_llm
        skill_evolve._distill(f"{name}的任务描述" + "很" * 20,
                              "已完成该任务，流程如下" + "x" * 100, ["pc_files"], {})
        results[name] = any(s["name"] == name for s in _autos())

    t1 = threading.Thread(target=work, args=("服务器巡检",))
    t2 = threading.Thread(target=work, args=("报销单整理",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert all(results.values()), f"并发落盘丢技能：{results}，autos={[s['name'] for s in _autos()]}"
    # 计数也应为 2（同锁原子更新）
    st = skill_evolve._load_state()
    assert st.get("count") == 2, st
    print("PASS 并发落盘无覆盖（两技能都入库 + 计数=2）")


if __name__ == "__main__":
    test_gates_and_full_chain()
    test_dedup_and_worth_false()
    test_daily_cooldown_and_switch()
    test_concurrent_distill_no_overwrite()
    test_memory_watch_integration(None)
    print("\n== 全部通过：技能自进化 ==")

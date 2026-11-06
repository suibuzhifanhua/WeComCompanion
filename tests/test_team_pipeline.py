# -*- coding: utf-8 -*-
"""test_team_pipeline — 智能体团队 规划→执行→审查（v1.0.312）。

mock 全部 LLM 调用（agent.team._llm_complete/_plan_model），gen_factory 用假生成器。
用例：
1. 总闸关：team_pipeline 直通 gen_factory（无规划/审查事件）
2. 正常路径：规划 tool+tool_result 事件 → 执行事件透传 → 执行计划以 system 注入（在用户消息前）
3. 审查 REVISE：追加审查 tool 事件 + 带意见的补跑（第二次 gen_factory 收到 assistant+user 修补消息）
4. 审查 PASS：不补跑
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.team import team_pipeline  # noqa: E402


def _fake_gen(events):
    def g(msgs):
        yield from events
        g.last_msgs = msgs
    return g


def _parse(e):
    """v1.0.361（S3 配套）：team_pipeline 现在产出 SSE 字符串（"data: {...}\\n\\n"），
    非 SSE 项（直通的内部事件）原样返回——解析成 dict 便于断言。"""
    if isinstance(e, str) and e.startswith("data: "):
        try:
            return json.loads(e[6:].strip())
        except Exception:
            return e
    return e


import json  # noqa: E402


def _patch_llm(plan="1. 目标：做PPT\n2. 步骤省略", review="REVISE\n- 补充第3页"):
    """monkeypatch agent.team 的 LLM 三入口。"""
    import agent.team as T
    orig = (T._llm_complete, T._plan_model)
    calls = {"plan": 0, "review": 0}
    T._plan_model = lambda cfg: "judge-x"
    def fake_llm(model, msgs, **kw):
        sysc = msgs[0].get("content", "") if msgs else ""
        if "规划者" in sysc:
            calls["plan"] += 1
            return plan
        if "审查者" in sysc:
            calls["review"] += 1
            return review
        return "PASS"
    T._llm_complete = fake_llm
    return T, orig, calls


def test_gate_off_passthrough():
    T, orig, _ = _patch_llm()
    try:
        cfg = {"feature_flags": {"team": False}}
        base = [{"role": "user", "content": "任务"}]
        events = [{"type": "token", "text": "【完成】好了"}]
        g = _fake_gen(events)
        out = list(team_pipeline(base, "任务", cfg, g))
        assert out == events, "闸关应直通"
        assert g.last_msgs == base, "闸关不应改写消息"
        print("PASS 总闸关：直通不包装")
    finally:
        T._llm_complete, T._plan_model = orig


def test_plan_inject_and_execute():
    T, orig, calls = _patch_llm()
    try:
        cfg = {"feature_flags": {"team": True}, "models": [{"id": "judge-x", "enabled": True}]}
        base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "帮我做个PPT"}]
        events = [{"type": "token", "text": "【完成】PPT已生成"}]
        g = _fake_gen(events)
        out = list(team_pipeline(base, "帮我做个PPT", cfg, g))
        out_parsed = [_parse(e) for e in out]
        types_seen = [e["type"] for e in out_parsed if isinstance(e, dict)]
        assert "tool" in types_seen and "tool_result" in types_seen, out_parsed
        assert any(isinstance(e, dict) and e.get("name") == "👥 团队·规划" for e in out_parsed), out_parsed
        # 执行计划以 system 注入且在用户消息之前
        last = g.last_msgs
        assert last[-1]["role"] == "user", "用户消息应保持在末尾"
        inj = [m for m in last if m["role"] == "system" and "执行计划" in m.get("content", "")]
        assert inj, "计划应注入 system"
        assert calls["plan"] == 1 and calls["review"] == 1, calls
        print("PASS 规划事件 + 计划注入（用户消息仍在末尾）+ 规划/审查各调一次")
    finally:
        T._llm_complete, T._plan_model = orig


def test_review_revise_repair():
    T, orig, calls = _patch_llm(review="REVISE\n- 第2页数据错了，改成 42\n- 补交付路径")
    try:
        cfg = {"feature_flags": {"team": True}}
        base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "做报告"}]
        events = [{"type": "token", "text": "【完成】报告写完了"}]
        calls_n = {"n": 0}

        def g(msgs):
            calls_n["n"] += 1
            if not hasattr(g, "history"):
                g.history = []
            g.history.append(msgs)
            yield from events
        out = list(team_pipeline(base, "做报告", cfg, g))
        out_parsed = [_parse(e) for e in out]
        assert any(isinstance(e, dict) and e.get("name") == "👥 团队·审查" for e in out_parsed), out_parsed
        assert calls_n["n"] == 2, f"审查 REVISE 应补跑一轮，实际执行 {calls_n['n']} 次"
        repair_msgs = g.history[1]
        assert repair_msgs[-2]["role"] == "assistant", "补跑消息应含上一轮答复"
        assert repair_msgs[-1]["role"] == "user" and "审查意见" in repair_msgs[-1]["content"], repair_msgs[-1]
        print("PASS 审查 REVISE → 审查事件 + 带意见补跑一轮")
    finally:
        T._llm_complete, T._plan_model = orig


def test_review_pass_no_repair():
    T, orig, _ = _patch_llm(review="PASS")
    try:
        cfg = {"feature_flags": {"team": True}}
        base = [{"role": "user", "content": "任务"}]
        events = [{"type": "token", "text": "【完成】完成"}]
        calls_n = {"n": 0}

        def g(msgs):
            calls_n["n"] += 1
            yield from events
        out = list(team_pipeline(base, "任务", cfg, g))
        out_parsed = [_parse(e) for e in out]
        assert not any(isinstance(e, dict) and e.get("name") == "👥 团队·审查" for e in out_parsed), out_parsed
        assert calls_n["n"] == 1, "PASS 不应补跑"
        print("PASS 审查 PASS：不补跑")
    finally:
        T._llm_complete, T._plan_model = orig


if __name__ == "__main__":
    test_gate_off_passthrough()
    test_plan_inject_and_execute()
    test_review_revise_repair()
    test_review_pass_no_repair()
    print("\n== 全部通过：智能体团队流水线 ==")

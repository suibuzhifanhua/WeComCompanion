# -*- coding: utf-8 -*-
"""tests/test_agent_scenarios.py — v1.0.345 agent_loop scenario regression.
Scripted LLM + scripted dispatch drive agent_loop; assertions run on SSE events.
NOTE: source kept ASCII-only (this machine's editor may write GBK; Chinese via \\u).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import agent.loop as AL  # noqa: E402
import core_utils  # noqa: E402

CP = "\u4ea7\u7269\u5e93"      # workspace dir name
CC = "\u4ea7\u51fa"            # outputs subdir


def make_cfg():
    return {"enabled_skills": [], "skill_overrides": {}, "custom_skills": [],
            "models": [{"id": "m1", "name": "T", "base_url": "http://127.0.0.1:59999/v1",
                        "api_key": "", "model": "t", "enabled": True}],
            "connectors": {}, "wecom": {}, "feature_flags": {"skills": False}}


class ScriptedStream:
    def __init__(self, rounds):
        self.rounds = rounds
        self.i = 0

    def __call__(self, model, messages, tools=None, response_format=None, poll_abort=None):
        r = self.rounds[min(self.i, len(self.rounds) - 1)]
        self.i += 1
        yield from r


from conftest import _done  # noqa: E402


from conftest import _call  # noqa: E402


def _collect(gen):
    evs = []
    for item in gen:
        s = item.strip() if isinstance(item, str) else ""
        if s.startswith("data:"):
            try:
                evs.append(json.loads(s[5:].strip()))
            except Exception:  # noqa: BLE001
                pass
    return evs


def _patch_dispatch(handler):
    AL.dispatch_tool = handler


def test_finish_qa_empty_then_ok():
    """finish with empty final_answer -> rejected once; with real summary -> passes."""
    orig_stream = AL.stream_llm_events
    orig_dispatch = AL.dispatch_tool
    rounds = [
        [_done("", [_call("finish", {})])],
        [_done("", [_call("finish", {"final_answer": "OK: processed 3 items, all passed, results saved."})])],
    ]
    AL.stream_llm_events = ScriptedStream(rounds)
    seen = {"rej": 0, "ok": 0, "called": []}

    def handler(name, args, cfg):
        seen["called"].append(name)
        return {"__finish__": True, "final_answer": args.get("final_answer", "")}

    _patch_dispatch(handler)
    try:
        evs = _collect(AL.agent_loop([{"role": "user", "content": "task"}], make_cfg(), pane_id="s1"))
    finally:
        AL.stream_llm_events = orig_stream
        AL.dispatch_tool = orig_dispatch
    for e in evs:
        if e.get("type") == "tool_result" and "finish" in str(e.get("summary")) and "final_answer" in str(e.get("summary")):
            seen["rej"] += 1
        if e.get("type") == "done":
            seen["ok"] += 1
            assert e.get("summary", {}).get("rounds", 0) >= 1, e
    assert seen["rej"] == 1, f"expect exactly 1 reject, got {seen['rej']}"
    assert seen["ok"] == 1, f"expect 1 normal finish, got {seen['ok']}"
    # 被拒的 finish 不应到达 dispatch（质量校验在 dispatch 前拦截），只有放行的那次到达
    assert seen["called"] == ["finish"], seen["called"]
    print("PASS finish empty-summary rejected once, then passes; done.summary present")


def test_finish_claimed_missing_file():
    """final_answer claims a workspace file that does not exist -> rejected; existing file -> passes."""
    tmp = tempfile.mkdtemp()
    core.paths.workspace_root = lambda: tmp
    orig_stream = AL.stream_llm_events
    orig_dispatch = AL.dispatch_tool
    ghost = CP + "/" + CC + "/ghost_report_v345.md"
    real = CP + "/" + CC + "/real_report_v345.md"

    def handler(name, args, cfg):
        return {"__finish__": True, "final_answer": args.get("final_answer", "")}

    _patch_dispatch(handler)
    try:
        AL.stream_llm_events = ScriptedStream([
            [_done("", [_call("finish", {"final_answer": "saved " + ghost + " please check"})])],
            [_done("", [])],
            [_done("", [])],
        ])
        evs = _collect(AL.agent_loop([{"role": "user", "content": "task"}], make_cfg(), pane_id="s2a"))
        rej = sum(1 for e in evs
                  if e.get("type") == "tool_result" and "final_answer" in str(e.get("summary"))
                  and "save_file" in str(e.get("summary")))
        assert rej == 1, f"ghost claim should be rejected once, got {rej}"
        assert any(e.get("type") == "done" for e in evs), "should finish normally after reject"
        os.makedirs(os.path.join(tmp, CC), exist_ok=True)
        open(os.path.join(tmp, CC, "real_report_v345.md"), "w", encoding="utf-8").write("x")
        AL.stream_llm_events = ScriptedStream([
            [_done("", [_call("finish", {"final_answer": "report saved at " + real + " please check"})])],
        ])
        evs2 = _collect(AL.agent_loop([{"role": "user", "content": "task"}], make_cfg(), pane_id="s2b"))
        assert any(e.get("type") == "done" for e in evs2), "real file should pass"
        assert not any(e.get("type") == "tool_result" and "save_file" in str(e.get("summary"))
                       for e in evs2), "real file should NOT be rejected"
    finally:
        AL.stream_llm_events = orig_stream
        AL.dispatch_tool = orig_dispatch
    print("PASS finish claimed-file existence check (ghost rejected / real passed)")


def test_write_loop_warn_then_kill():
    """Same write op repeated: warn at 4th, kill at 8th after intervention."""
    orig_stream = AL.stream_llm_events
    orig_dispatch = AL.dispatch_tool
    rounds = [[_done("", [_call("click_wecom_element", {"target": "same-button", "pick": 0})])]
              for _ in range(12)]
    AL.stream_llm_events = ScriptedStream(rounds)
    _patch_dispatch(lambda name, args, cfg: {"ok": True})
    try:
        evs = _collect(AL.agent_loop([{"role": "user", "content": "task"}], make_cfg(), pane_id="s3"))
    finally:
        AL.stream_llm_events = orig_stream
        AL.dispatch_tool = orig_dispatch
    warn = any(e.get("type") == "token" and "detected repeated" in str(e.get("text")).lower() for e in evs) \
        or any(e.get("type") == "token" and "[检测到连续重复操作" in str(e.get("text")) for e in evs)
    err = any(e.get("type") == "error" and "死循环" in str(e.get("text")) for e in evs)
    assert warn, "4th repeat should trigger warn intervention"
    assert err, "repeats after warn should hit kill at 8"
    print("PASS write-loop warn -> kill breaker chain")


def test_read_dedup_second_read_trimmed():
    """Second read of identical page body -> trimmed unchanged version."""
    body = "page-body-A" * 1000   # 11000 chars
    orig_stream = AL.stream_llm_events
    orig_dispatch = AL.dispatch_tool
    rounds = [
        [_done("", [_call("read_wecom_page", {"pick": 0})])],
        [_done("", [_call("read_wecom_page", {"pick": 0})])],
        [_done("", [])],
    ]
    AL.stream_llm_events = ScriptedStream(rounds)

    def handler(name, args, cfg):
        return {"ok": True, "body": body, "title": "T", "url": "http://x"}

    _patch_dispatch(handler)
    trimmed = {"n": 0}
    try:
        evs = _collect(AL.agent_loop([{"role": "user", "content": "task"}], make_cfg(), pane_id="s4"))
    finally:
        AL.stream_llm_events = orig_stream
        AL.dispatch_tool = orig_dispatch
    for e in evs:
        if e.get("type") == "tool_result" and "unchanged" in json.dumps(e, ensure_ascii=False):
            trimmed["n"] += 1
    assert trimmed["n"] == 1, f"second read should hit dedup once, got {trimmed['n']}"
    print("PASS read dedup: second identical read returns trimmed version")


def test_batch_gauge_in_meta():
    """After append_progress success, next meta carries batch gauge."""
    orig_stream = AL.stream_llm_events
    orig_dispatch = AL.dispatch_tool
    rounds = [
        [_done("", [_call("append_progress", {"key": "k1", "content": "item1 ok"})])],
        [_done("", [])],
    ]
    AL.stream_llm_events = ScriptedStream(rounds)
    _patch_dispatch(lambda name, args, cfg: {"ok": True, "saved": True})
    gauges = []
    try:
        evs = _collect(AL.agent_loop([{"role": "user", "content": "batch task"}], make_cfg(), pane_id="s5"))
    finally:
        AL.stream_llm_events = orig_stream
        AL.dispatch_tool = orig_dispatch
    for e in evs:
        if e.get("type") == "meta" and e.get("batch"):
            gauges.append(e["batch"])
    assert gauges and gauges[-1].get("done") == 1, f"meta should carry batch done=1, got {gauges}"
    print("PASS batch progress gauge injected into meta.batch")


def test_nav_click_whitelist_not_killed():
    """Nav clicks (prev/next question): no kill within the first 14 tool results.
    (normal write would kill at 8 -> <=8 tool results before the kill error;
    nav uses kill=20, so all 14 scripted tool results appear before any error)"""
    orig_stream = AL.stream_llm_events
    orig_dispatch = AL.dispatch_tool
    nav_target = "\u4e0b\u4e00\u9898"   # "next question"
    rounds = [[_done("", [_call("click_wecom_element", {"target": nav_target, "pick": 0})])]
              for _ in range(14)]
    AL.stream_llm_events = ScriptedStream(rounds)
    _patch_dispatch(lambda name, args, cfg: {"ok": True})
    tool_results_before_error = 0
    seen_error = False
    try:
        for item in AL.agent_loop([{"role": "user", "content": "exam"}], make_cfg(), pane_id="s6"):
            s = item.strip() if isinstance(item, str) else ""
            if not s.startswith("data:"):
                continue
            try:
                e = json.loads(s[5:].strip())
            except Exception:  # noqa: BLE001
                continue
            if e.get("type") == "tool_result":
                if seen_error:
                    break
                tool_results_before_error += 1
            elif e.get("type") == "error" and "死循环" in str(e.get("text")):
                seen_error = True
    finally:
        AL.stream_llm_events = orig_stream
        AL.dispatch_tool = orig_dispatch
    assert not seen_error or tool_results_before_error >= 14, \
        f"nav kill should not fire before 14 tool results (got {tool_results_before_error})"
    print("PASS nav whitelist: no kill within 14 nav clicks"
          f" (tool_results_before_error={tool_results_before_error})")


def test_stuck_rescue_hint():
    """v1.0.347 卡住自救：hub_missing_skills 读取 + 干预提示文本组装。"""
    from agent_tools import hub_missing_skills
    from agent.loop import _stuck_rescue_hint
    cfg = make_cfg()
    cfg["skill_hub"] = {"url": ""}   # 内置快照兜底（离线可用）
    missing = hub_missing_skills(cfg)
    assert isinstance(missing, list), type(missing)
    hint = _stuck_rescue_hint("demo-skill(demo)")
    assert "request_skill" in hint and "demo-skill(demo)" in hint
    assert _stuck_rescue_hint("") == ""
    assert _stuck_rescue_hint(None) == ""
    print("PASS stuck-rescue hint helper + hub listing read")


if __name__ == "__main__":
    test_finish_qa_empty_then_ok()
    test_finish_claimed_missing_file()
    test_write_loop_warn_then_kill()
    test_read_dedup_second_read_trimmed()
    test_batch_gauge_in_meta()
    test_nav_click_whitelist_not_killed()
    test_stuck_rescue_hint()
    print("== agent scenarios ALL PASS ==")

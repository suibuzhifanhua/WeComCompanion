# -*- coding: utf-8 -*-
"""tests/test_llm_stream.py — LLM 流式链路回归锁合集（v1.0.646 按域归并）。

三个文件守的是同一条链的三个失效点，且失败形态高度一致：**熔断/重试逻辑写对了，
但链路被提前掐断，于是"日志齐全、任务静默死锁"**。

| 原文件                     | 版本出处          | 守护对象                     | 用例数 |
|----------------------------|-------------------|------------------------------|--------|
| test_no_progress.py        | v1.0.287 / v1.0.293 | 零产出空转 & 无限推理熔断   |   4    |
| test_stream_retry.py       | v1.0.294          | 流式中断自动重试链路         |   2    |
| test_v561_llm_pool.py      | v1.0.561          | abort 后连接池污染清理       |   4    |
"""
import inspect
import json
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client as L   # noqa: E402


# ====================================================== v1.0.287 / 293 零产出熔断
class TestNoProgressFuse:
    """零产出空转专项熔断（v1.0.287）+ 无限推理循环熔断（v1.0.293）。

    模拟「data 行活跃但 content/reasoning/tool 参数均 0 字符」的空转流
    （vLLM 空 delta chunk），断言 LLM_NO_PROGRESS_TIMEOUT 内熔断报错；
    再模拟正常思考流（reasoning token 持续产出），断言不被误杀。
    """

    MODEL = {"name": "T", "base_url": "http://fake/v1", "enable_thinking": True}

    class _FakeResp:
        """可编程假 SSE 响应。"""

        def __init__(self, line_factory):
            self._factory = line_factory
            self.closed = False

        def iter_lines(self, decode_unicode=True):
            for line in self._factory():
                if self.closed:
                    return
                yield line

        def close(self):
            self.closed = True

    class _FakeSession:
        def __init__(self, line_factory):
            self._f = line_factory

        def post(self, *a, **k):
            r = TestNoProgressFuse._FakeResp(self._f)
            r.status_code = 200
            return r

    def _run(self, line_factory, monkeypatch):
        """跑流收集事件，返回 (elapsed, error_text)。

        v1.0.646：原来直接赋值 `L._HTTP_SESSION = ...` 且从不还原，假 Session 会留在
        模块上污染后续用例。改走 monkeypatch（用例结束自动还原）。
        """
        monkeypatch.setattr(L, "_HTTP_SESSION", self._FakeSession(line_factory))
        t0 = time.time()
        err = ""
        # v1.0.344：error 事件升级为 ("error", text, code) 三元组——索引访问兼容两种长度
        for ev in L._stream_llm_events_once(self.MODEL, [{"role": "user", "content": "hi"}]):
            if ev[0] == "error":
                err = str(ev[1])
                break
        return time.time() - t0, err

    def test_idle_spin_breaks(self, monkeypatch):
        """空转流（空 delta 的 data 行不停吐）→ 应在 NO_PROGRESS_TIMEOUT 附近熔断。"""
        L.LLM_NO_PROGRESS_TIMEOUT = 2  # 测试加速
        empty = "data: " + json.dumps({"choices": [{"delta": {}}]})

        def lines():
            while True:
                yield empty

        dt, err = self._run(lines, monkeypatch)
        assert err, f"应熔断报错，实际事件流正常结束（{dt:.1f}s）"
        assert "无任何实质产出" in err, f"报错文案不符：{err[:120]}"
        assert 1.5 <= dt <= 8, f"熔断耗时异常：{dt:.1f}s"
        print(f"PASS 空转熔断：{dt:.1f}s → {err[:70]}")

    def test_reasoning_flow_not_killed(self, monkeypatch):
        """正常思考流（reasoning token 持续产出）→ 不应被零产出熔断误杀。"""
        L.LLM_NO_PROGRESS_TIMEOUT = 2  # 测试加速

        def lines():
            n = 0
            while True:
                n += 1
                if n > 200:  # 持续思考 200 个 token 后正常收尾
                    yield "data: " + json.dumps({"choices": [{"delta": {"content": "完成"}}]})
                    yield "data: [DONE]"
                    return
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"reasoning": "思考" * 5}}]})

        dt, err = self._run(lines, monkeypatch)
        assert not err, f"正常思考流被误杀：{err[:120]}"
        print(f"PASS 思考流不误杀：{dt:.1f}s 正常收尾")

    def test_reasoning_loop_breaks(self, monkeypatch):
        """v1.0.293 无限推理循环：reasoning token 持续流（零产出熔断不触发）但
        始终无 content/工具调用 → 应在 NO_CONTENT_TIMEOUT 附近熔断报错。
        （现场 2026-09-07 博易云测试：DaoPro 大上下文连吐 15 分钟纯 reasoning，
        旧逻辑只能等 900s 总时长熔断。）"""
        L.LLM_NO_PROGRESS_TIMEOUT = 9999   # 思考 token 算产出，零产出熔断本就不该触发
        L.LLM_NO_CONTENT_TIMEOUT = 2       # 测试加速

        def lines():
            while True:
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"reasoning": "思考" * 5}}]})
                time.sleep(0.001)

        dt, err = self._run(lines, monkeypatch)
        assert err, f"无限推理流应熔断，实际未报错（{dt:.1f}s）"
        assert "无可执行产出" in err, f"报错文案不符：{err[:120]}"
        assert 1.5 <= dt <= 10, f"熔断耗时异常：{dt:.1f}s"
        print(f"PASS 无限推理熔断：{dt:.1f}s → {err[:70]}")

    def test_reasoning_then_content_not_killed(self, monkeypatch):
        """v1.0.293 不误杀：reasoning 思考一段后正常落 content 收尾（思考时长在
        NO_CONTENT_TIMEOUT 内）→ 不应熔断。"""
        L.LLM_NO_PROGRESS_TIMEOUT = 9999
        L.LLM_NO_CONTENT_TIMEOUT = 2

        def lines():
            n = 0
            while True:
                n += 1
                if n > 100:  # 思考 100 个 token（<1s，远在 2s 窗口内）后正常收尾
                    yield "data: " + json.dumps({"choices": [{"delta": {"content": "完成"}}]})
                    yield "data: [DONE]"
                    return
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"reasoning": "思考" * 5}}]})

        dt, err = self._run(lines, monkeypatch)
        assert not err, f"正常思考→产出流被误杀：{err[:120]}"
        print(f"PASS 思考后产出流不误杀：{dt:.1f}s 正常收尾")


# ====================================================== v1.0.294 流式中断自动重试
class TestStreamInterruptRetry:
    """流式中断自动重试链路（v1.0.294，P0 回归）。

    回归缺陷（2026-09-07 16:46 财务任务现场）：agent/loop.py 旧逻辑在 error 事件分支
    直接 return，会关闭 stream_llm_events 生成器（for 循环被废弃 → GeneratorExit 打进
    yield 点），使其外层自动重试（LLM_STREAM_EMPTY_RETRIES 次重发）永不执行——熔断正确
    触发（「流式中断…走自动重试」日志齐全）后任务却静默死锁（无「自动重试 1/2」、无新
    「请求开始」，3 分钟无动作）。

    本测直接对 stream_llm_events 外层生成器断言两种消费方式的差异：
      1) 消费「到底」（新 loop.py：流式中断 error 不 return，继续 next()）→ 触发 1 次
         自动重发（共 2 次请求）并最终拿到 done —— 证明熔断后链路真能自愈；
      2) 对照旧 bug：消费端一遇 error 就 break（等价旧 return 提前关闭生成器）→ 只有
         1 次请求、拿不到 done —— 证明旧逻辑确实掐死了自动重试。
    """

    MODEL = {"name": "T"}

    def _make_fake_once(self, state):
        """返回假 _stream_llm_events_once：第 1 次吐 reasoning 后报「流式中断」error
        （模拟无内容熔断），第 2 次（重试）正常 token+done 收尾。"""
        def fake_once(model, messages, tools=None, response_format=None, poll_abort=None):
            n = state["calls"]
            state["calls"] += 1
            if n == 0:  # 第 1 次请求（0-indexed）
                yield ("reasoning", "思考中…")
                yield ("error", "LLM 流式中断：已 300s 无可执行产出（content/工具调用）")
                return
            yield ("token", "回答内容")
            yield ("done", {"text": "回答内容", "tool_calls": [], "usage": None})
        return fake_once

    def test_retry_recovers_when_consumed_to_completion(self):
        """新 loop.py 行为：消费 stream_llm_events 到底（error 不 return）→ 应自动重试并拿到 done。"""
        state = {"calls": 0}
        _orig_once_345 = L._stream_llm_events_once   # v1.0.345：防 monkeypatch 泄漏到其它测试
        L._stream_llm_events_once = self._make_fake_once(state)
        L.LLM_STREAM_EMPTY_RETRIES = 2
        try:
            events = []
            # 关键：不提前 return/break——一直消费到生成器自然结束（StopIteration）
            for ev in L.stream_llm_events(self.MODEL, [{"role": "user", "content": "hi"}]):
                events.append(ev[0])
                if ev[0] == "error":
                    continue  # 新 loop.py：流式中断只推提示、不 return，继续 next() 触发重试
            assert events[-1] == "done", f"重试后应拿到 done，实际事件序列：{events}"
            assert "error" in events, "应包含触发重试的流式中断 error 事件"
            assert state["calls"] == 2, f"应触发 1 次自动重试（共 2 次请求），实际 {state['calls']} 次"
            print(f"PASS 消费到底自动重试自愈：{state['calls']} 次请求，事件序列={events}")
        finally:
            L._stream_llm_events_once = _orig_once_345

    def test_old_bug_dies_without_retry(self):
        """对照旧 bug：消费端一遇 error 就 break（等价旧 return 关闭生成器）→ 无重试、拿不到 done。"""
        state = {"calls": 0}
        _orig_once_345 = L._stream_llm_events_once   # v1.0.345：防 monkeypatch 泄漏到其它测试
        L._stream_llm_events_once = self._make_fake_once(state)
        L.LLM_STREAM_EMPTY_RETRIES = 2
        try:
            got_done = False
            for ev in L.stream_llm_events(self.MODEL, [{"role": "user", "content": "hi"}]):
                if ev[0] == "error":
                    break  # 旧 loop.py：return（此处 break 等价模拟提前关闭生成器）
                if ev[0] == "done":
                    got_done = True
            assert not got_done, "旧逻辑（error 即 break）不应拿到 done"
            assert state["calls"] == 1, f"旧逻辑不应触发重试（仅 1 次请求），实际 {state['calls']} 次"
            print(f"PASS 旧 bug 复现：error 即退出 → 无重试（{state['calls']} 次请求），任务静默死锁")
        finally:
            L._stream_llm_events_once = _orig_once_345


# ====================================================== v1.0.561 连接池污染清理
class TestLlmPoolCleanup:
    """LLM 连接池污染修复回归（v1.0.561）。

    根因：硬停止 closesocket 绕过 urllib3 强关 socket → 连接池残留 fd=-1 死连接 →
    后续每轮首请求 select(-1) 报「file descriptor cannot be a negative integer (-1)」
    → 重试第 2 次才成功（用户实测：流程能跑但每轮多等数秒、日志刷失败）。
    修复：abort 后清池 + 请求失败重试前清池。
    """

    def _fake_session(self, calls):
        """构造带 poolmanager 的假 Session，记录 clear 调用。"""
        s = types.SimpleNamespace()
        s.adapters = {
            "http://": types.SimpleNamespace(
                poolmanager=types.SimpleNamespace(clear=lambda: calls.append("clear-http"))),
            "https://": types.SimpleNamespace(
                poolmanager=types.SimpleNamespace(clear=lambda: calls.append("clear-https"))),
        }
        return s

    # ===================== _clear_http_pool =====================
    def test_clear_pool_drops_all_adapters(self):
        calls = []
        L._HTTP_SESSION = self._fake_session(calls)
        try:
            L._clear_http_pool()
        finally:
            L._HTTP_SESSION = None
        assert sorted(calls) == ["clear-http", "clear-https"], calls

    def test_clear_pool_safe_when_no_session(self):
        L._HTTP_SESSION = None
        L._clear_http_pool()          # 不应抛异常
        # 异常 Session 也不抛
        bad = types.SimpleNamespace(adapters={"http://": None})
        L._HTTP_SESSION = bad
        L._clear_http_pool()
        L._HTTP_SESSION = None

    # ===================== abort 后清池 =====================
    def test_abort_clears_pool(self, monkeypatch):
        """硬停止 closesocket 后必须清连接池——否则死连接残留，每轮首请求失败重试。

        v1.0.646：本用例手工往 _ACTIVE_RESPONSES 里塞了一份 ref，而 abort_active_responses
        只关 socket、**不注销**（注销是注册方 finally 的责任，llm_client.py:139-164）。
        原文件（test_v561_llm_pool.py）按文件名排在 v555 之后，残留污染不到人；并入
        test_llm_stream.py 后排到了 tests/test_v555_batch2.py 之前，那条
        `assert len(_ACTIVE_RESPONSES) == 0` 就被这份残留带红。故补 finally 自行注销。
        """
        calls = []
        monkeypatch.setattr(L, "_HTTP_SESSION", self._fake_session(calls))
        ref = L._ActiveLLMReq(None, types.SimpleNamespace(
            close=lambda: calls.append("closesock")))
        with L._ACTIVE_LOCK:
            L._ACTIVE_RESPONSES.add(ref)
        try:
            n = L.abort_active_responses()
            assert n == 1
            assert "closesock" in calls
            assert "clear-http" in calls and "clear-https" in calls, \
                "硬停止后必须清空 keep-alive 池（fd=-1 死连接断根）"
        finally:
            with L._ACTIVE_LOCK:
                L._ACTIVE_RESPONSES.discard(ref)

    # ===================== 重试路径清池（源码护栏） =====================
    def test_retry_clears_pool_before_retry(self):
        src = inspect.getsource(L._stream_body)
        # 失败分支内必须先 _clear_http_pool() 再决定是否重试
        assert "_clear_http_pool()" in src
        i_fail = src.index("请求失败(第")
        i_clear = src.index("_clear_http_pool()", i_fail)
        i_retry = src.index("if attempt < LLM_MAX_RETRIES", i_fail)
        assert i_fail < i_clear < i_retry, "清池必须发生在失败日志之后、重试判断之前"


if __name__ == "__main__":
    t0 = time.time()
    TestNoProgressFuse().test_idle_spin_breaks()
    TestNoProgressFuse().test_reasoning_flow_not_killed()
    TestNoProgressFuse().test_reasoning_loop_breaks()
    TestNoProgressFuse().test_reasoning_then_content_not_killed()
    TestStreamInterruptRetry().test_retry_recovers_when_consumed_to_completion()
    TestStreamInterruptRetry().test_old_bug_dies_without_retry()
    TestLlmPoolCleanup().test_clear_pool_drops_all_adapters()
    TestLlmPoolCleanup().test_clear_pool_safe_when_no_session()
    TestLlmPoolCleanup().test_retry_clears_pool_before_retry()
    print(f"ALL PASS（{time.time() - t0:.1f}s）")

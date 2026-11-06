# -*- coding: utf-8 -*-
"""tests/test_agent_runtime.py — 按域归并的回归锁合集

v1.0.648 由若干"按版本号起的文件"按域合并而来。每条 class/函数的 docstring 保留
原始出处与事故现场——版本编号是这些锁的**理由**，抹掉等于把锁降级成断言。
版本出处见各段上方的 `# ---- 来自 xxx ----` 标记。
"""
import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.result import is_err, err_msg  # noqa: E402
from core.logging import last_agent_activity_epoch
import ast
import datetime
import inspect


# ============================================================ 来自 test_v592_activity_guard.py（v1.0.592）


_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


def _ts(seconds_ago):
    return (datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago)) \
        .strftime("%Y-%m-%d %H:%M:%S")


def test_activity_line_survives_chatty_tail(tmp_path):
    """活动行被几百行退出噪音挤出旧 400 行窗口时，仍要探得到（漏判=误杀运行中任务）。"""
    p = tmp_path / "companion.log"
    lines = [f"{_ts(30)} [agent] 调用工具 read_wecom_page：{{\"pick\": 0}}"
             + "  " + "x" * 200]                      # 活动行
    lines += [f"{_ts(1)} [system] 进程正常退出（窗口关闭/托盘退出）"] * 600
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    got = last_agent_activity_epoch([str(p)])
    assert got is not None and got != 0.0, "有活动行却探测不到 → 守卫会误判空闲/读不到"
    assert abs(got - float(datetime.datetime.strptime(_ts(30), "%Y-%m-%d %H:%M:%S").timestamp())) < 2


def test_readable_but_idle_returns_zero_not_none(tmp_path):
    """读到了、确实没有活动行 → 0.0（空闲），不能是 None（=读不到，会让守卫 fail-closed）。"""
    p = tmp_path / "companion.log"
    p.write_text("\n".join(f"{_ts(5)} [system] 噪音行 {i}" for i in range(50)) + "\n",
                 encoding="utf-8")
    assert last_agent_activity_epoch([str(p)]) == 0.0


def test_unreadable_still_returns_none(tmp_path):
    """真的读不到（文件不存在/路径变化）仍返回 None，守卫的 fail-closed 保护不能被削弱。"""
    assert last_agent_activity_epoch([os.path.join(str(tmp_path), "没有这个文件.log")]) is None


def test_activity_found_in_megabyte_tail_window(tmp_path):
    """>1MB 的字节窗口分支：活动行在窗口内、但被 500+ 行噪音压在后头，仍须探到。"""
    p = tmp_path / "big.log"
    head = [f"{_ts(90000)} [system] 远古噪音 " + "y" * 400 for _ in range(3000)]
    act = f"{_ts(60)} [agent] 工具 list_wecom_windows 返回（0.0s）：ok"
    tail = [f"{_ts(59)} [system] 收尾噪音 " + "z" * 400 for _ in range(600)]
    p.write_text("\n".join(head + [act] + tail) + "\n", encoding="utf-8")
    assert p.stat().st_size > (1 << 20), "构造文件未超过 1MB，没走到字节窗口分支"
    got = last_agent_activity_epoch([str(p)])
    assert got not in (None, 0.0), "尾部窗口内的活动行漏判 → 守卫会误杀运行中的任务"


def test_build_guard_would_not_block_on_idle_log(tmp_path):
    """把守卫的判断式原样搬过来验语义：空闲日志既不算「读不到」，也不算「近期有活动」。"""
    import time
    p = tmp_path / "companion.log"
    p.write_text("\n".join(f"{_ts(400)} [system] 退出噪音") + "\n", encoding="utf-8")
    ts = last_agent_activity_epoch([str(p)])
    assert ts is not None, "空闲日志被判成「读不到」→ 守卫 fail-closed，打包永远打不动"
    assert not (time.time() - ts < 180)

# ============================================================ 来自 test_v622_exam_budget.py（v1.0.622）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.loop import soft_budget_state   # noqa: E402
from agent_tools.skills import SKILLS      # noqa: E402

_T = 1000000.0          # 累计 100 万 prompt_tokens（今天被截断那次约在这个量级）


def test_zero_turns_the_cap_off():
    """0 必须是"关闭"，不能被 `or` 吞回默认值。"""
    cfg = {"agent": {"soft_budget_tokens": 0, "soft_budget_minutes": 0}}
    hit, tok, mins = soft_budget_state(cfg, 99_999_999, 9999 * 60, 0)
    assert hit is False, f"填 0 关不掉：hit={hit} tok={tok} min={mins}"


def test_default_still_catches_idle_burning():
    """基础额度仍有效：一项都没完成却烧到默认 40 万 → 该收尾（保险丝没被拆掉）。"""
    hit, tok, mins = soft_budget_state({}, _T, 60, 0)
    assert hit is True
    assert tok == 400000 and mins == 120


def test_budget_grows_with_progress():
    """今天实测：一场 9 题的考试到第 5 题时累计约 100 万 prompt_tokens（messages=100 条）。
    同样这个消耗：一项没完成 → 触顶（空转该拦）；已答完 5 题 → 不该拦（以执行完为准）。"""
    hit_0, tok_0, _m = soft_budget_state({}, _T, 60, 0)
    hit_5, tok_5, min_5 = soft_budget_state({}, _T, 60, 5)
    assert hit_0 is True and hit_5 is False, (hit_0, hit_5)
    assert tok_0 == 400000, tok_0
    assert tok_5 == 400000 + 5 * 200000, tok_5          # 每完成一项追加一份额度
    assert min_5 == 120 + 5 * 15                        # 墙钟也按项给额度
    # 答到第 8 题更不该被掐
    assert soft_budget_state({}, _T, 60, 8)[0] is False


def test_wall_clock_is_independent():
    """token 没超但墙钟超 → 仍触顶；关掉墙钟（0）后由 token 独判。"""
    hit, _t, mins = soft_budget_state({}, 1000, 121 * 60, 0)
    assert hit is True and mins == 120
    hit2 = soft_budget_state({"agent": {"soft_budget_minutes": 0}}, 1000, 9999 * 60, 0)[0]
    assert hit2 is False


def test_garbage_config_falls_back_not_raises():
    """配置写错不许把整轮任务带崩：非数字回落默认，负数夹到 0（0 本身就是"关闭该项"）。"""
    hit, tok, mins = soft_budget_state({"agent": {"soft_budget_tokens": "abc",
                                                 "soft_budget_minutes": -5}}, _T, 60, 0)
    assert tok == 400000, tok            # 坏值 → 回落默认
    assert mins == 0, mins               # 负数 → 0 = 墙钟不参与判定
    assert hit is True                   # 仍由 token 兜住（不是"报错即放弃判定"）


# ---------- 接线：循环里用的就是这个纯函数 ----------

def test_loop_uses_the_pure_function():
    """grep 源码不算证据：AST 判 agent_loop 里确实调了 soft_budget_state，
    并且把 progress_done 传进去了（否则"按进度加额"是句空话）。"""
    tree = ast.parse(io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "agent", "loop.py"), encoding="utf-8").read())
    loop_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "agent_loop")
    calls = [n for n in ast.walk(loop_fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "soft_budget_state"]
    assert calls, "agent_loop 没调用 soft_budget_state（判定逻辑又回到内联，测不到）"
    args = ast.dump(calls[0])
    assert "_progress_done" in args, "调用时没传已完成项数"
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "agent", "loop.py"), encoding="utf-8").read()
    assert "or 400000" not in src and "or 120" not in src, "0 被 `or 默认` 吞掉的写法又回来了"


# ---------- ③ 长流程技能必须有 append_progress（上下文精简 + 额度来源） ----------

def test_long_run_skills_declare_append_progress():
    by_id = {s["id"]: s for s in SKILLS}
    for sid in ("exam-solver", "study-solver", "oa-approval"):
        s = by_id[sid]
        assert "append_progress" in (s.get("tools") or []), \
            f"{sid} 没声明 append_progress：长任务既拿不到进度额度，也不触发上下文自动精简"
        assert "append_progress(" in s.get("prompt", ""), f"{sid} 提示里没要求逐项记录"


def test_tail_message_says_progress_and_resume():
    """被收尾时那句话要交代"已完成几项 + 怎么接着做"，只说「预算上限」用户只能重跑整场。"""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "agent", "loop.py"), encoding="utf-8").read()
    i = src.find("已达到任务资源预算")
    assert i > 0
    seg = src[i - 60:i + 260]
    assert "_progress_done" in seg and "继续" in seg, seg

# ============================================================ 来自 test_v628_stream_diag.py（v1.0.628）


# ---------------------------------------------------------------- ① SSE 事件形态
def _agent_like_events(answer="已生成 12 页 PPT，文件在 产出/PPT。"):
    """按 agent_loop 的**真实产出形态**造事件流：现成 SSE 字符串，不是 dict。
    （旧单测喂 dict 假桩，把 _memory_watch 的 isinstance(ev, dict) 恒假一路掩绿。）"""
    return [
        'data: {"type":"meta","model":"DaoPro","rounds":1}\n\n',
        'data: {"type":"reasoning","text":"先规划章节结构……"}\n\n',
        'data: {"type":"tool","name":"pc_write_file","args":{}}\n\n',
        'data: {"type":"tool","name":"👥 团队·规划","args":{}}\n\n',
        'data: {"type":"token","text":"正文前半\\n\\n"}\n\n',
        'data: {"type":"token","text":"---\\n\\n【完成】' + answer + '"}\n\n',
        ': keepalive\n\n',
        'data: {"type":"done","summary":{"rounds":1}}\n\n',
    ]


def test_memory_watch_parses_real_sse_strings(monkeypatch, tmp_path):
    """真实 SSE 字符串流：agent 轮必须落库成【完成】之后的正文，而不是空串。"""
    import routes.chat as rc
    captured = {}
    fake_distill = lambda *a, **k: captured.update({"args": a})  # noqa: E731
    monkeypatch.setattr("services.skill_evolve.maybe_distill_async", fake_distill)
    monkeypatch.setattr("services.memory_ops.remember_async", lambda *a, **k: None)
    stored = []
    monkeypatch.setattr("services.conversations.append_message",
                        lambda cid, role, content, **kw: stored.append((role, content)))

    def gen():
        for e in _agent_like_events():
            yield e

    list(rc._memory_watch(gen(), "生成一个ai演变史ppt", {"feature_flags": {}},
                          conv_id="conv_x", mode="agent"))
    as_agent = [c for r, c in stored if r == "agent"]
    assert as_agent, "会话落库没写 agent 轮"
    assert "已生成 12 页 PPT" in as_agent[-1], f"agent 轮内容不符（旧 bug 是空串）：{as_agent[-1]!r}"
    assert "【完成】" not in as_agent[-1]
    # 记忆沉淀/技能自进化依赖 final 非空 → 现在必须被触发，且工具名去重、团队卡排除
    assert captured.get("args"), "final 仍为空 → 记忆沉淀/技能自进化被整轮跳过"
    assert captured["args"][2] == ["pc_write_file"], captured["args"]


def test_memory_watch_still_accepts_dict_events(monkeypatch):
    """向后兼容：dict 形态事件（内部消费者/测试桩）解析结果与字符串一致。"""
    import routes.chat as rc
    stored = []
    monkeypatch.setattr("services.conversations.append_message",
                        lambda cid, role, content, **kw: stored.append((role, content)))
    monkeypatch.setattr("services.memory_ops.remember_async", lambda *a, **k: None)
    monkeypatch.setattr("services.skill_evolve.maybe_distill_async", lambda *a, **k: None)

    def gen():
        yield {"type": "token", "text": "【完成】字典形态也要能落库"}

    list(rc._memory_watch(gen(), "任务", {"feature_flags": {}}, conv_id="c", mode="ask"))
    assert stored[-1][1] == "字典形态也要能落库", stored


def test_no_dict_only_event_check_left_in_chat_routes():
    """口径单点：routes/chat.py 里不得再有「按 dict 判定 SSE 事件」的写法
    （plan 落库那条链和 memory 链同款 bug，改一处漏一处的历史已经重演过一次）。"""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "routes", "chat.py"), encoding="utf-8").read()
    assert "isinstance(ev, dict) and ev.get(\"type\")" not in src, src[:200]
    assert "isinstance(_ev, dict)" not in src
    assert "_parse_sse_event" in src, "会话/记忆链没复用 SSE 解析单一事实来源"


# ---------------------------------------- ①b 系统播报不进会话（v1.0.632 B3）
def test_sys_notices_are_not_stored_as_agent_reply(monkeypatch):
    """用户点停止 / 超轮次时没有 `【完成】`，落库走 `buf[-4000:]` 兜底支：
    带 sys 标记的播报必须被跳过，否则"[LLM 流式中断，自动重试中…]"这类
    软件自己写的状态说明会被当成 AI 回复存进会话、下一轮回灌给模型。"""
    import routes.chat as rc
    stored = []
    monkeypatch.setattr("services.conversations.append_message",
                        lambda cid, role, content, **kw: stored.append((role, content)))
    monkeypatch.setattr("services.memory_ops.remember_async", lambda *a, **k: None)
    monkeypatch.setattr("services.skill_evolve.maybe_distill_async", lambda *a, **k: None)

    def gen():
        yield 'data: {"type":"token","text":"\\n\\n---\\n\\n[已达到主思考轮次上限，自动进入收尾总结…]","sys":1}\n\n'
        yield 'data: {"type":"token","text":"\\n\\n[LLM 流式中断，自动重试中…]","sys":1}\n\n'
        yield 'data: {"type":"token","text":"已按 12 页生成完毕"}\n\n'

    list(rc._memory_watch(gen(), "生成ppt", {"feature_flags": {}}, conv_id="c", mode="agent"))
    agent = [c for r, c in stored if r == "agent"][-1]
    assert agent == "已按 12 页生成完毕", f"播报被当成 AI 回复存进会话：{agent!r}"
    assert "[LLM 流式中断" not in agent and "已达到主思考轮次" not in agent


def test_only_notices_stores_no_fake_reply(monkeypatch):
    """整轮只有播报（例如用户一开始就点停止）→ agent 轮存空串，
    由 history_messages 的空轮跳过兜住，绝不把播报喂进上下文。"""
    import routes.chat as rc
    stored = []
    monkeypatch.setattr("services.conversations.append_message",
                        lambda cid, role, content, **kw: stored.append((role, content)))
    monkeypatch.setattr("services.memory_ops.remember_async", lambda *a, **k: None)

    def gen():
        yield 'data: {"type":"token","text":"\\n\\n---\\n\\n[检测到连续重复操作，已暂停并引导模型重新决策]","sys":1}\n\n'

    list(rc._memory_watch(gen(), "任务", {"feature_flags": {}}, conv_id="c", mode="ask"))
    assert [c for r, c in stored if r == "agent"][-1] == ""


def test_loop_notices_all_marked_sys():
    """agent_loop 的每一条**字面量**播报都必须经 `_sys_notice_sse` 发出（带 sys 标记）——
    按文案匹配来过滤会随字面量漂移失效，所以用结构化标记 + 这条源码锁。
    判据只盯"text" 后面跟字符串字面量的行（模型真正文是 `'text':ev[1]`，不算播报）。"""
    import inspect
    from agent import loop as AL
    assert "def _sys_notice_sse" in inspect.getsource(AL)
    src = inspect.getsource(AL.agent_loop)
    leftover = [l.strip()[:110] for l in src.splitlines()
                if "token" in l and "json.dumps" in l
                and ("'text': '" in l or '"text": "' in l)
                and "【完成】" not in l          # 完成事件带真答复，必须照旧进落库
                and "sys" not in l]
    assert not leftover, f"仍有未打 sys 标记的播报：{leftover}"
    assert src.count("_sys_notice_sse(") >= 3, "播报点应收敛到 _sys_notice_sse"


def test_sys_notice_sse_shape():
    from agent.loop import _sys_notice_sse
    import json as _j
    ev = _sys_notice_sse("[测试播报]")
    assert ev.startswith("data: ") and ev.endswith("\n\n")
    d = _j.loads(ev[5:].strip())
    assert d == {"type": "token", "text": "[测试播报]", "sys": 1}


# ------------------------------------------------------------------ ② 空轮不入上下文
def test_history_messages_skips_empty_turns(tmp_path, monkeypatch):
    from services import conversations as C
    monkeypatch.setattr(C, "conv_dir", lambda: str(tmp_path))
    cid = C.create(title="生成ppt")
    C.append_message(cid, "user", "生成一个ai演变史ppt")
    C.append_message(cid, "agent", "")          # 旧 bug 留下的空 assistant 轮
    C.append_message(cid, "agent", "已生成 12 页")  # 修复后的正常轮
    C.append_message(cid, "user", "再加一页")
    msgs = C.history_messages(cid)
    assert all((m.get("content") or "").strip() for m in msgs), \
        f"空轮仍被拼进上下文（思考模型据此可能只思考不输出）：{msgs}"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"], msgs


def test_history_messages_capped_per_message(tmp_path, monkeypatch):
    """补上 agent 全文落库后，历史注入必须有单则上限：20 条 × 2 万字会顶爆本地模型。
    存档留全文（前端重开会话看全文），喂模型只留开头。"""
    from services import conversations as C
    monkeypatch.setattr(C, "conv_dir", lambda: str(tmp_path))
    cid = C.create(title="长答复")
    long_answer = "开头条目\n" + ("内容" * 5000) + "\n末尾产物路径：产出/x.pptx"
    C.append_message(cid, "user", "列全部清单")
    C.append_message(cid, "agent", long_answer)
    stored = C.load(cid)["messages"][1]["content"]
    assert stored == long_answer, "存档被截了——重开会话要看全文，只能截注入侧"
    hist = C.history_messages(cid)
    assert len(hist[1]["content"]) < 2000, f"注入侧没截断：{len(hist[1]['content'])} 字"
    assert "开头条目" in hist[1]["content"] and "历史过长" in hist[1]["content"]# ---------------------------------------------------------------- ③ 重试不再原样重发
def _patch_once(state, err_text):
    """假 _stream_llm_events_once：每次调用记录收到的模型参数；第 1 轮报中断，之后成功。"""
    def fake_once(model, messages, tools=None, response_format=None, poll_abort=None):
        state.setdefault("models", []).append(dict(model))
        state["calls"] = state.get("calls", 0) + 1
        if state["calls"] == 1:
            yield ("reasoning", "还在想" * 30)
            yield ("error", err_text, "stream_interrupt")
            return
        yield ("token", "答案")
        yield ("done", {"text": "答案", "tool_calls": [], "usage": None})
    return fake_once


def _run_retry(monkeypatch, model, err_text):
    import llm_client as L
    state = {}
    orig = L._stream_llm_events_once
    monkeypatch.setattr(L, "_stream_llm_events_once", _patch_once(state, err_text))
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)   # 跳过硬重试退避
    monkeypatch.setattr(L, "LLM_STREAM_EMPTY_RETRIES", 2)
    try:
        for ev in L.stream_llm_events(model, [{"role": "user", "content": "hi"}]):
            pass
    finally:
        L._stream_llm_events_once = orig
    return state


def test_think_loop_retry_disables_thinking(monkeypatch):
    """no-content 熔断（思考循环）→ 重试请求必须关思考，不再同参数白烧 300s。"""
    import llm_client as L
    model = {"name": "DaoPro", "enable_thinking": True,
             "caps": {"can_disable_thinking": True}}
    st = _run_retry(monkeypatch, model,
                    f"LLM 流式中断：已 300s {L._THINK_LOOP_MARK}（content/工具调用）")
    assert st["calls"] == 2, st
    assert st["models"][0]["enable_thinking"] is True
    assert st["models"][1].get("enable_thinking") is False, \
        "思考循环重试仍带思考参数（等价于原样重发，必然再卡 300s）"


def test_transient_break_retries_unchanged(monkeypatch):
    """瞬态断流（网络/空闲）→ 保持原样重发，思考档位不得被顺手改掉。"""
    model = {"name": "DaoPro", "enable_thinking": True,
             "caps": {"can_disable_thinking": True}}
    st = _run_retry(monkeypatch, model, "LLM 流式中断：连接被对端关闭")
    assert st["calls"] == 2, st
    assert st["models"][1]["enable_thinking"] is True, st["models"][1]


def test_think_loop_not_downgraded_when_model_cannot_disable(monkeypatch):
    """模型声明关不掉思考 → 绝不塞 chat_template_kwargs 去砸服务端。"""
    import llm_client as L
    model = {"name": "Locked", "enable_thinking": True,
             "caps": {"can_disable_thinking": False}}
    st = _run_retry(monkeypatch, model,
                    f"LLM 流式中断：已 300s {L._THINK_LOOP_MARK}（content/工具调用）")
    assert st["calls"] == 2, st
    assert st["models"][1]["enable_thinking"] is True, st["models"][1]


# ------------------------------------------------------- ④ 文案/日志的可分诊性
def test_no_content_message_reports_reasoning_chars():
    """熔断文案必须自带「思考多少字 / 正文 0 字」——旧文案只报「已收 0 字符」，
    把「只思考不落地」和「全静默」写成同一句话，事后无法分诊。"""
    import llm_client as L
    src = inspect.getsource(L._stream_body)
    assert "_reasoning_chars" in src, "思考字数没单独统计"
    assert "思考已收" in src and "_THINK_LOOP_MARK" in src
    # 中断路径日志同时带正文与思考两个计数（旧日志只有正文，思考多少字看不见）
    assert "思考 {_reasoning_chars} 字" in src
    Lsrc = inspect.getsource(L.stream_llm_events)
    assert "_THINK_LOOP_MARK" in Lsrc, "外层重试没按思考循环标记分派"


def test_think_loop_mark_is_single_source():
    """标记常量必须同时被 raise 侧和重试侧引用（引用同一标识符，不是各写一份字面量），
    否则改文案只会改一处，重试策略静默失效。"""
    import llm_client as L
    assert L._THINK_LOOP_MARK not in inspect.getsource(L._stream_body), \
        "raise 侧应引用常量，不该再手写一份字面量"
    assert "_THINK_LOOP_MARK" in inspect.getsource(L._stream_body)
    assert "_THINK_LOOP_MARK" in inspect.getsource(L.stream_llm_events)


def test_agent_loop_interrupt_texts(monkeypatch):
    """agent 播报/兜底文案按故障分派（真分支，不做源码字面量断言）：
    思考循环给处置动作，瞬态断流保持原文案（前端与其它测试按原字符串锁着）。"""
    from agent.loop import _interrupt_notice, _interrupt_tip
    think_err = "LLM 流式中断：已 300s 无可执行产出（content/工具调用）（思考已收 1.2 万字）"
    net_err = "LLM 流式中断：连接被对端关闭"
    assert "只想不做" in _interrupt_notice(think_err)
    assert "重试" in _interrupt_notice(think_err), "播报要说明系统在做什么"
    assert _interrupt_notice(net_err) == "[LLM 流式中断，自动重试中…]"
    assert _interrupt_notice("") == "[LLM 流式中断，自动重试中…]"
    assert "调成「关」" in _interrupt_tip(think_err), "思考循环兜底没给处置动作"
    assert _interrupt_tip(net_err) == (
        "LLM 流异常中断，未收到完成信号。（已保存断点，可点击「继续」重试）")
    # 两个分派点必须真的挂在 agent_loop 上（否则文案写对了也永远不显示）
    src = inspect.getsource(__import__("agent.loop", fromlist=["loop"]).agent_loop)
    assert "_interrupt_notice(_err_text)" in src and "_interrupt_tip(_interrupt_reason)" in src


def test_no_content_break_reports_both_counts(monkeypatch):
    """行为级：纯思考流真跑到 no-content 熔断，报错必须同时给出思考字数与正文 0 字。
    （旧文案只报「已收 0 字符」——把「只思考不落地」和「全静默」写成一件事，无法分诊。）"""
    import llm_client as L

    thought = json.dumps({"choices": [{"delta": {"reasoning": "想"}}]}, ensure_ascii=False)
    holder = {}

    class _FakeResp:
        def __init__(self):
            self.closed = False
            self.encoding = "utf-8"
            self.status_code = 200

        def iter_lines(self, decode_unicode=True):
            while not self.closed:
                yield "data: " + thought
                time.sleep(0.05)

        def close(self):
            self.closed = True

    class _FakeSession:
        def post(self, *a, **k):
            r = _FakeResp()
            holder["r"] = r
            return r

    monkeypatch.setattr(L, "_HTTP_SESSION", _FakeSession())
    monkeypatch.setattr(L, "LLM_NO_CONTENT_TIMEOUT", 1)
    monkeypatch.setattr(L, "LLM_NO_PROGRESS_TIMEOUT", 30)   # 只让 no-content 这一路触发
    monkeypatch.setattr(L, "LLM_GLOBAL_SEMAPHORE", None)
    err = ""
    for ev in L._stream_llm_events_once(
            {"name": "T", "base_url": "http://fake/v1", "enable_thinking": True},
            [{"role": "user", "content": "hi"}]):
        if ev[0] == "error":
            err = str(ev[1])
            break
    holder.get("r") and holder["r"].close()
    assert "无可执行产出" in err, f"没走 no-content 熔断：{err[:120]}"
    assert "思考已收" in err and "正文 0 字" in err, f"报错缺分诊计数：{err[:160]}"
    # 计数必须是**真数出来的**（写死 0 也算"有这句话"，那等于没分诊）
    m = re.search(r"思考已收 (\d+) 字", err)
    assert m and int(m.group(1)) > 0, f"思考字数没被统计出来：{err[:160]}"


# ============================================================ 来自 test_v616_parallel.py（v1.0.616）

from conftest import _read  # noqa: E402


CHAT = _read("js", "10-chat.js")
MIC = _read("js", "14-mic.js")
WS = _read("js", "11-workspace-kb-study.js")


def test_no_modal_drops_requests_anymore():
    for mid in ("apprModal", "askUserModal", "skillReqModal", "skillSrcModal"):
        assert 'document.getElementById("%s")) return' % mid not in CHAT, \
            "%s 又回到占用即丢弃" % mid
    assert CHAT.count("modalBusy(") >= 4, CHAT.count("modalBusy(")


def test_modal_queue_registers_all_four_openers():
    for kind in ("skillReq", "skillSrc", "appr", "askUser"):
        assert "__MODAL_OPEN.%s" % kind in CHAT, kind
        assert kind in re.search(r"__MODAL_HOST = \{([^}]*)\}", CHAT).group(1), kind
    assert "function _drainModals" in CHAT and "MutationObserver" in CHAT


def test_drain_skips_empty_queues_before_touching_dom():
    """流式输出期间每一帧 DOM 变更都会进这里，队列空时必须先跳过再查 DOM。"""
    body = CHAT[CHAT.index("function _drainModals"):]
    body = body[:body.index("\n}") + 2]
    assert body.index("length) continue") < body.index("getElementById")


def test_approval_modal_honors_callbacks():
    i = CHAT.index("async function showApprovalModal")
    seg = CHAT[i:i + 4600]
    assert "opts.onGrant" in seg and "opts.onDeny" in seg
    assert "opts.noRelax" in seg, "宏重放仍会显示降档按钮（重放序列没有本任务权限档可调）"


def test_mic_state_is_per_button():
    i = MIC.index("async function toggleMic")
    seg = MIC[i:i + 900]
    assert "__micBtnId !== btnId" in seg, "又是无差别停止当前录音"
    assert re.search(r"if\(!other\) return;", seg), "同按钮再点应当只是停止"


def test_macro_frontend_wires_gate_events():
    i = WS.index("async function _macroRun")
    seg = WS[i:i + 2200]
    for ev in ("approval_request", "macro_paused", "macro_done"):
        assert '"%s"' % ev in seg, ev
    assert "showApprovalModal(" in seg and "noRelax" in seg
    assert "AbortController" in WS and ".abort()" in WS, "重放仍无法中途停止"
    assert "start:start" in seg or "start: start" in seg, "续跑没把 start 传给后端"


def test_macro_resume_does_not_reconfirm():
    """批准后是"接着跑"，不该再弹一次"请恢复到起始状态"的确认（那会误导用户重头来）。"""
    i = WS.index("async function runMacro")
    seg = WS[i:i + 700]
    assert "if(start)" in seg and "confirmAsk" in seg

# ============================================================ 来自 test_v632_bg_pane.py（v1.0.632）


from agent.session import is_background_pane, BACKGROUND_PANE_PREFIXES   # noqa: E402


@pytest.mark.parametrize("pid", ["sched-daily1", "study-engine-1730000000", None, ""])
def test_background_sessions(pid):
    assert is_background_pane(pid) is True, pid


@pytest.mark.parametrize("pid", ["opPane", "cv_op", "s5", "chat-1"])
def test_ui_sessions_are_not_background(pid):
    assert is_background_pane(pid) is False, pid


# ---------------------------------------------------------------- 单一事实源
from conftest import _src  # noqa: E402


def test_prefix_tuple_is_not_hand_copied_anymore():
    """全仓只允许 session.py 出现这个前缀元组字面量——再手抄一条就红。"""
    import glob
    skip = ("dist", "build", "tests", "docs", "__pycache__", "node_modules", ".git", "产物库")
    files = [p for p in glob.glob(os.path.join(ROOT, "*.py"))]
    for d in ("agent", "agent_tools", "core", "routes", "services", "wecom_bridge", "tools"):
        files += glob.glob(os.path.join(ROOT, d, "*.py"))
    allowed = os.path.normpath(os.path.join(ROOT, "agent", "session.py"))
    offenders = []
    for p in files:
        if os.path.normpath(p) == allowed:
            continue
        if any(seg in skip for seg in os.path.normpath(p).split(os.sep)):
            continue
        s = io.open(p, encoding="utf-8").read()
        for n, ln in enumerate(s.splitlines(), 1):
            if '"sched-"' in ln and '"study-"' in ln and "BACKGROUND_PANE_PREFIXES" not in ln:
                offenders.append(f"{os.path.relpath(p, ROOT)}:{n}: {ln.strip()[:70]}")
    assert not offenders, "后台前缀又被手抄了一份：\n" + "\n".join(offenders)


def test_both_gate_sites_call_the_helper():
    """两个闸门点都必须走同一个口径（此前一处算无 pane、一处不算）。"""
    src = _src("agent", "loop.py")
    assert src.count("is_background_pane(pane_id)") == 2, src.count("is_background_pane(pane_id)")
    assert "startswith((\"sched-\"" not in src


def test_state_eviction_uses_the_helper():
    src = _src("agent", "session.py")
    i = src.find("def _save_chat_state")
    body = src[i:src.find("def _bp_write", i)]
    assert body.count("is_background_pane(") == 3, body[:200]
    assert 'startswith((' not in body, "断点配额里还留着手抄前缀"


# ---------------------------------------------------------------- 无 pane 不再挂起
def test_headless_call_cannot_hang_on_an_unanswerable_popup(monkeypatch):
    """无 pane 直调（宏/脚本）没有弹窗可点：命中不可撤销写操作要明确拒绝，不是挂起。"""
    import security_ops as SO
    r = SO.gate_write_tool("run_audit_flow", {"auto_approve": True, "flow": "报销"},
                           {"security": {"permission_level": "ask_every"}},
                           level="auto", is_read=False,
                           background=is_background_pane(None))
    assert is_err(r), r
    assert "后台" in err_msg(r)

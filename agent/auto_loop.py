# -*- coding: utf-8 -*-
"""agent.auto_loop — 考试无人值守续跑链（v1.0.248 引入，v1.0.302 入 agent 层，v1.0.345 拆出独立模块）。

_auto_loop_chain 包住 agent_loop 的 SSE 生成器：finish 且 final_answer 含
「RESULT: FAIL」→ 自动开新会话续跑（新会话不带旧历史，靠答题台账落盘传状态），
另含流式中断的自动断点续跑（v1.0.271）。拆出动机：loop.py 主循环与本链是两个
独立关注点（单会话循环 vs 跨会话编排），拆开后各自可测、可改。

导入说明：本模块【懒导入】agent_loop（函数内 import）——loop.py 末尾 re-export
本模块符号，模块级互导会成环；懒导入保证任意导入顺序都安全。
"""
import json

from core_utils import log_event
from llm_client import is_stream_interrupt   # v1.0.654 A3：流式中断判定单源化
from .session import _is_aborted, chat_states
from .context import build_chat_messages
from .team import _parse_sse_event   # v1.0.470：SSE 解析单一事实来源（team 不依赖本包内模块，无环）

AUTO_LOOP_MAX_RESTARTS = 6   # 每个任务最多自动开 6 个续跑会话（会话内 agent 自身还会循环多轮）
AUTO_LOOP_STATES = {}        # pane_id -> {"restarts": int}（会话结束在 finally 清理）

_AUTO_LOOP_NEXT_PROMPT = (
    "（自动续跑·第 {n} 次会话）上一会话考试未达标已自动结束。请继续执行考试：\n"
    "①read_workspace_file 读一次答题记录台账，只看【最新一轮得分】与【锁定/排除结论】两节；\n"
    "②按单变量规则定本轮 12 题完整答案（只改 1-2 个最可疑项，其余照抄最优组合），"
    "计划一行落盘到台账；\n"
    "③若当前页面是成绩页 → 点「再考一次」再进答题页；已在答题页则直接开始作答；\n"
    "④逐题机械作答（禁重读台账/禁回看已答题/禁无工具调用的总结轮），"
    "unanswered=0 → 一次性落盘 → 交卷（每点一次 read 确认）→ 读成绩；\n"
    "⑤finish 汇报，final_answer 最后一行必须写 RESULT: PASS（通过）或 RESULT: FAIL（未通过）。"
)


def _auto_loop_chain(first_gen, *, pane_id, cfg, port, messages, pick,
                     use_wecom_context, max_restarts=AUTO_LOOP_MAX_RESTARTS,
                     allow_fail_restart=True):
    """包住 agent_loop 的 SSE 生成器：finish 且 RESULT: FAIL → 自动开新会话续跑。
    新会话用「原始任务消息 + 续跑指令」重建上下文（不带旧会话历史，靠台账传状态）。

    v1.0.650（A4）新增 `allow_fail_restart=False`：跑课后台线程（study_engine）要的只是
    「流式中断→从断点续跑」这一半自愈——FAIL 那一半会在同一个浏览器现场里再答一遍卷子，
    而外层本来就有 MAX_ATTEMPTS 次全新尝试 + `_prune_zn_windows` 收窗兜底，行为不能被我
    顺手换掉。关掉它之后，排空生成器对"已通过/平台侧失效"两类收尾是零成本的（内层 gen 此时已耗尽）。
    """
    from .loop import agent_loop   # 懒导入防循环（见模块 docstring）
    gen = first_gen
    restarts = 0
    # v1.0.271：流式中断自动续跑——LLM 流式中断（服务端空转/超时）是瞬态故障且 agent 层
    # 已保存断点。不再让任务停住等用户手动点「继续」，而是自动走与「继续」按钮完全相同
    # 的会话恢复路径（agent_loop 从断点恢复），至多 MAX_ERR_CONTINUES 次。
    # 现场实测 2026-09-02→03：11 次中断，每次浪费一整个尝试周期（15-25min）。
    err_continues = 0
    MAX_ERR_CONTINUES = 2
    try:
        while True:
            from .sse_parse import FinalAnswerTracker   # v1.0.654 A4：滚动缓冲单源化
            _fa = FinalAnswerTracker()
            saw_need_continue = saw_error = False
            last_err = ""
            last_err_code = None
            for item in gen:
                yield item
                ev = _parse_sse_event(item)
                if not isinstance(ev, dict):
                    continue
                t = ev.get("type")
                if t == "token":
                    _fa.feed(ev.get("text") or "")
                elif t == "need_continue":
                    saw_need_continue = True
                elif t == "error":
                    saw_error = True
                    last_err = ev.get("text") or ""
                    last_err_code = ev.get("code")
            final_answer = _fa.result()
            # 不续跑的情形：流异常/待人工继续/通过/无 RESULT 标记/模型没写标记
            if saw_need_continue:
                log_event(f"[auto_loop] 待人工继续，不自动续跑 pane={pane_id}")
                break
            if saw_error:
                # v1.0.271：流式中断（含「LLM 流式中断」/「流异常中断」）= 瞬态故障且断点
                # 已保存 → 自动「继续」（与用户点按钮完全同路径），免人工介入。
                # v1.0.344：优先按结构化 code 判断，字符串匹配兜底（v1.0.654 A3 单源化）。
                _is_interrupt = is_stream_interrupt(last_err_code, last_err)
                if (_is_interrupt
                        and not _is_aborted(pane_id)
                        and err_continues < MAX_ERR_CONTINUES
                        and pane_id in chat_states):
                    err_continues += 1
                    log_event(f"[auto_loop] 流式中断 → 自动从断点续跑（{err_continues}/{MAX_ERR_CONTINUES}）pane={pane_id}")
                    gen = agent_loop(cfg=cfg, pane_id=pane_id)
                    continue
                log_event(f"[auto_loop] 会话异常且不可自动续跑，停止 pane={pane_id}")
                break
            if "RESULT: FAIL" not in final_answer or not allow_fail_restart:
                if "RESULT: PASS" in final_answer:
                    log_event(f"[auto_loop] 考试通过，自动续跑结束 pane={pane_id}")
                elif not allow_fail_restart:
                    log_event(f"[auto_loop] FAIL 续跑通道按调用方要求关闭（交外层重开会话）pane={pane_id}")
                break
            if restarts >= max_restarts:
                # v1.0.344 修复：此处原写成 "\\n\\n"（f-string 字面反斜杠 n），导致 SSE 事件
                # 没有真实换行结尾——前端按 "\n\n" 分帧，这条提示永远无法被分发，且文本无换行。
                yield f"data: {json.dumps({'type':'token','text':'\n\n---\n\n[auto_loop] 已达自动续跑上限（' + str(max_restarts) + ' 次）仍未通过，停止续跑。可手动重新发起任务。'}, ensure_ascii=False)}\n\n"
                log_event(f"[auto_loop] 达到续跑上限 {max_restarts}，停止 pane={pane_id}")
                break
            if _is_aborted(pane_id):
                log_event(f"[auto_loop] 检测到 abort，停止续跑 pane={pane_id}")
                break
            restarts += 1
            log_event(f"[auto_loop] RESULT: FAIL → 自动开第 {restarts} 个续跑会话 pane={pane_id}")
            nxt = {"role": "user", "content": _AUTO_LOOP_NEXT_PROMPT.format(n=restarts)}
            msgs = build_chat_messages(messages + [nxt], cfg, port, pick,
                                       use_wecom_context=use_wecom_context)
            gen = agent_loop(msgs, cfg, pane_id=pane_id)
    finally:
        AUTO_LOOP_STATES.pop(pane_id, None)

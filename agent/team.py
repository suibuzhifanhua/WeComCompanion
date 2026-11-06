"""agent.team — 智能体团队：规划 → 执行 → 审查（v1.0.312，对齐 WorkBuddy「智能体团队管理」）
=================================================================
复杂任务自动组建多智能体分工（对齐截图语义）。当前实现为「角色流水线」形态：
  ①规划者（judge 模型优先，否则主模型）：分析任务 → 产出执行计划注入上下文
  ②执行者（主 agent_loop，即现有单智能体内核）
  ③审查者（judge 模型优先）：对照任务与最终答复挑遗漏/错误 → 不通过则带意见补跑一轮（最多 1 次）
所有阶段事件以 tool 消息形态流向前端（用户能看到流水线推进）；总开关 feature_flags.team。
数据全部本地；审查/规划各多花一次 LLM 调用，简单任务建议不开（前端开关默认关）。
"""
import json

from core_utils import log_event
from llm_client import judge_model, resolve_model, _llm_complete
from .session import _is_aborted   # v1.0.472：规划/审查阶段可被「停止」中断

_PLAN_PROMPT = (
    "你是「智能体团队」的规划者。分析用户任务，产出一份精炼执行计划供执行者直接照做：\n"
    "1. 任务目标一句话；2. 拆解步骤（编号，每步一句话，标注用什么能力：企微页面操作/本机命令/文件读写/联网检索/直接撰写）；"
    "3. 关键风险与注意点（≤3 条）；4. 若任务含创作类交付物，列出需要向用户澄清的决策点（如有）。\n"
    "只输出计划本身（Markdown），不要寒暄。任务过简（一句话能做完）时只输出：任务简单，直接执行即可。"
)

_REVIEW_PROMPT = (
    "你是「智能体团队」的审查者。对照用户任务检查执行者的执行过程与最终答复：\n"
    "- 是否完成了任务的全部要求（无遗漏步骤/未回答的子问题）\n"
    "- 工具调用轨迹与答复声称是否一致：答复声称保存/处理了 N 项，轨迹里应有对应调用；\n"
    "  轨迹明显缺失关键步骤（如未读页面就说已审核、未保存文件就说已交付）→ 判 REVISE\n"
    "- 是否有明显错误、编造或自相矛盾\n"
    "- 交付物路径/结论是否交代清楚\n"
    "输出格式（严格）：第一行只写 PASS 或 REVISE；REVISE 时换行后给 ≤3 条具体修补指令"
    "（每条一句话，可直接执行）。不要寒暄。"
)


def _plan_model(cfg):
    return judge_model(cfg) or resolve_model(cfg)


def _parse_sse_event(ev):
    """v1.0.470 修复（P0-3）：把 agent_loop 产出的 SSE 事件统一解析为 dict。
    agent_loop yield 的是 "data: {...}\\n\\n" 字符串（见 team v1.0.361 注释），
    原执行阶段误用 isinstance(ev, dict) 判断——恒为 False，累积文本/final_answer/
    tool_trace 永远为空，审查阶段因 not final_answer 直接 return，整个审查
    流水线静默 no-op。本函数与 auto_loop 的解析逻辑保持一致（单一事实来源）。"""
    if isinstance(ev, dict):
        return ev
    if not isinstance(ev, str):
        return None
    s = ev.strip()
    if not s.startswith("data:"):
        return None
    try:
        obj = json.loads(s[5:].strip())
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


def team_pipeline(base_msgs, user_text, cfg, gen_factory, pane_id=None):
    """包住 agent_loop 的团队流水线生成器。

    base_msgs: build_chat_messages 产物（含 system/上下文/用户消息）
    user_text: 用户本轮原始消息（规划/审查的输入）
    gen_factory: callable(msgs) -> agent_loop 生成器（执行阶段可多次进入：审查补跑）
    """
    flags = cfg.get("feature_flags") or {}
    if not flags.get("team", True):
        yield from gen_factory(base_msgs)
        return
    model = _plan_model(cfg)
    if not model:
        # 没模型可用（理论少见）：直接单智能体执行
        yield from gen_factory(base_msgs)
        return

    # —— ①规划 ——
    # v1.0.472：规划是同步 _llm_complete（10-90s），调用前检查 abort——用户点停止时
    # 不再先跑完规划再进执行（agent_loop 虽会立即退出，但规划本身白耗一次 LLM 调用）
    if pane_id and _is_aborted(pane_id):
        return
    plan = ""
    try:
        # v1.0.652（审计 P2-4）：补 deadline/poll_abort——旧调用无墙钟上限（最坏 3 次
        # 流尝试 × 900s ≈ 45 分钟）且调用中点「停止」无反应，与 _judge_escalate/ask_judge
        # 的可取消口径不一致。
        plan = (_llm_complete(model, [
            {"role": "system", "content": _PLAN_PROMPT},
            {"role": "user", "content": (user_text or "")[:2000]},
        ], deadline=300,
            poll_abort=(lambda: bool(pane_id and _is_aborted(pane_id))) if pane_id else None
        ) or "").strip()
    except Exception as e:  # noqa: BLE001
        log_event(f"[team] 规划失败（降级单智能体）：{e}")
    if plan and "直接执行即可" not in plan:
        # v1.0.361 审查修复（S3）：产出 SSE 字符串（原 yield dict 到 WSGI 层直接断流）
        # v1.0.592 修复（凭据外泄）：这里原本把整个 `model` 字典塞进 args——那是
        # load_config() 解密后的模型定义，**含明文 api_key**；前端 addToolMsg 会渲染它，
        # 且 `pushHistory({role:"tool", text: ...JSON.stringify(j.args)})`（js/10-chat.js:159）
        # 把它写进会话历史 → 落盘到 产物库/对话记录/*.md，密钥从此以明文常驻、并可被
        # "读产出/对话记录"类工具再次带进上下文发往远端模型。只展示名称即可。
        # （非 dict 时按名称字符串处理——测试与历史调用点都出现过传 id 的形态。）
        _mname = (model.get("name") or model.get("id") or "") if isinstance(model, dict) \
            else (str(model) if model else "")
        yield "data: " + json.dumps({"type": "tool", "name": "👥 团队·规划",
                                     "args": {"模型": _mname,
                                              "计划长度": f"{len(plan)} 字"}},
                                    ensure_ascii=False) + "\n\n"
        yield "data: " + json.dumps({"type": "tool_result", "summary": plan[:400]}, ensure_ascii=False) + "\n\n"
        exec_msgs = list(base_msgs)
        # 计划插在用户消息之前（system 区），避免打断用户消息在末尾的契约
        exec_msgs.insert(-1 if len(exec_msgs) > 1 else len(exec_msgs),
                         {"role": "system", "content": f"[智能体团队·执行计划（按此推进，用户未看到）]\n{plan}"})
    else:
        exec_msgs = base_msgs

    # —— ②执行 ——
    # v1.0.315 修复：原 events 列表全量累积（长任务内存冗余）且 final 抽取只看最后 80 个
    # 事件——长答复的【完成】起点会被窗口截掉，审查拿到空答复静默跳过。改为流式累积 token 文本。
    # v1.0.345：同步收集工具调用轨迹，供审查者核对"声称做了"与"实际做了"是否一致。
    # v1.0.470（P0-3）：改用 _parse_sse_event 解析 SSE 字符串（原 isinstance(ev, dict) 恒假）。
    # v1.0.654 A4：token 累积/滚动缓冲收敛到 sse_parse.FinalAnswerTracker（原与
    # auto_loop 各持一份 95% 相同实现）。
    from .sse_parse import FinalAnswerTracker
    _fa = FinalAnswerTracker()
    tool_trace = []
    for ev in gen_factory(exec_msgs):
        obj = _parse_sse_event(ev)
        if isinstance(obj, dict):
            t = obj.get("type")
            if t == "token":
                _fa.feed(obj.get("text") or "")
            elif t == "tool":
                try:
                    nm = obj.get("name") or "?"
                    a = obj.get("args") or {}
                    tool_trace.append(nm + " " + json.dumps(a, ensure_ascii=False)[:60])
                except Exception:  # noqa: BLE001
                    tool_trace.append(str(obj.get("name") or "?"))
        yield ev
    final_answer = _fa.result()

    # —— ③审查 ——
    # v1.0.345：审查输入加入工具调用轨迹——审查者可核对"最终答复声称完成的步骤"
    # 是否真有对应工具调用（防只说不做/编造产物），以及明显遗漏的关键步骤。
    # v1.0.472：用户已点停止（agent 中断）→ 跳过审查，直接结束——原实现审查阶段
    # 同步 _llm_complete（10-90s），停止后仍照跑，用户视角「点了停止没反应」。
    _trace_text = "、".join(tool_trace[-30:]) if tool_trace else "（无工具调用记录）"
    if pane_id and _is_aborted(pane_id):
        log_event(f"[team] 用户已中断，跳过审查 pane={pane_id}")
        return
    try:
        # v1.0.652（审计 P2-4）：审查阶段同补 deadline/poll_abort（与规划一致）
        verdict = (_llm_complete(model, [
            {"role": "system", "content": _REVIEW_PROMPT},
            {"role": "user", "content": (
                f"【用户任务】\n{(user_text or '')[:1500]}\n\n"
                f"【执行者工具调用轨迹（按序，末 30 条）】\n{_trace_text}\n\n"
                f"【执行者最终答复】\n{final_answer[:2500]}")},
        ], deadline=300,
            poll_abort=(lambda: bool(pane_id and _is_aborted(pane_id))) if pane_id else None
        ) or "").strip()
    except Exception as e:  # noqa: BLE001
        log_event(f"[team] 审查失败（跳过）：{e}")
        verdict = "PASS"
    # v1.0.515 修复（审计 P2-19）：verdict 解析收紧——原实现"非 PASS 前缀即触发
    # 修补"，审查模型输出空串/寒暄/乱码都会白烧一次完整修补轮。现仅显式
    # FAIL/REVISE 前缀触发修补，其余（含 PASS/空/无法解析）一律视为通过
    # （与 judge 异常的 fail-open 口径一致：宁可交付也不无限修补）。
    _v_up = verdict.upper()
    if not final_answer:
        return
    if not (_v_up.startswith("FAIL") or _v_up.startswith("REVISE")):
        return
    fix = "\n".join(verdict.splitlines()[1:]).strip()
    if not fix:
        return
    yield "data: " + json.dumps({"type": "tool", "name": "👥 团队·审查", "args": {"结论": "REVISE"}}, ensure_ascii=False) + "\n\n"
    yield "data: " + json.dumps({"type": "tool_result", "summary": fix[:400]}, ensure_ascii=False) + "\n\n"
    log_event(f"[team] 审查要求修补：{fix[:80]}")

    # —— ③b 带审查意见补跑一轮（最多 1 次，不再二次审查） ——
    repair_msgs = list(exec_msgs)
    repair_msgs.append({"role": "assistant", "content": final_answer or "（上一轮未产出有效答复）"})
    repair_msgs.append({"role": "user",
                        "content": f"【团队审查意见——请按此修补后重新交付】\n{fix}"})
    for ev in gen_factory(repair_msgs):
        yield ev

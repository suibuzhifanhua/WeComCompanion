"""agent.loop_ops — agent_loop 的模块级辅助函数（v1.0.523-7 自 loop.py 拆出）
=================================================================
范围：消息配对修复/压缩阈值/工具结果截断/收尾守卫/认领文件校验/judge 升级等
纯模块级函数与常量（原 L27-222 逐字搬移，零逻辑改动）。
主循环（agent_loop）仍在 loop.py——其工具分发段与生成器语义深度耦合，
完整拆分待共享状态收敛后的专项（见 v1.0.509 审计报告 P1-6 暂缓说明）。
"""
import hashlib
import json
import os
import re

from core_utils import log_event, _mask_args
from core import paths  # noqa: F401（属性查找：workspace_root 可被测试 patch——test_agent_scenarios 依赖）
from llm_client import _llm_complete, resolve_model
from .session import _is_aborted, _save_chat_state, CONTEXT_COMPRESS_THRESHOLD, KEEP_LEADING_MSGS
from .messages import _msg_chars


def _align_tool_pairing(msgs):
    """v1.0.361 审查修复（S4）：从尾部向前对齐——若首条是 tool 结果而其所属
    assistant(tool_calls) 不在列表中，向前丢弃孤儿 tool 消息直到边界干净。
    用于 _reset_after_item 等砍中段的场景，防 tool_calls/tool 配对断裂。"""
    i = 0
    while i < len(msgs) and msgs[i].get("role") == "tool":
        i += 1
    return msgs[i:] if i else msgs


def _reset_after_item(loop_msgs, keep_tail=5):
    """append_progress 成功后的上下文精简（v1.0.198）：批量任务每处理完一项，
    砍掉该项的全部工具往返历史，只保留：
      - 开头指令（system + 原始 user 指令，KEEP_LEADING_MSGS 条）
      - 最近 keep_tail 条（含 append_progress 返回的累计进度 + 该项最后状态）
    下一个单据从干净上下文开始，不被上一个单据的 fields/read 详情污染
    （Qwen 等小模型上下文窗口有限，单上下文累积到上限被压缩后会丢失任务指令，
    表现为"只处理了一个就停"——这正是本次优化的直接动机）。
    v1.0.361 审查修复（S4）：tail 头部对齐——一轮多工具调用时 tail 可能以 tool
    结果开头而其 assistant(tool_calls) 被砍，产生孤儿 tool 消息（严格后端 400）。"""
    if not loop_msgs:
        return loop_msgs
    try:
        lead = list(loop_msgs[:KEEP_LEADING_MSGS])
        tail = _align_tool_pairing(list(loop_msgs[-keep_tail:]))
        merged = lead + tail
        if len(merged) < len(loop_msgs):
            return merged
        return loop_msgs
    except Exception:  # noqa: BLE001
        return loop_msgs


def _model_compress_threshold(cfg, base_chars=0):
    """v1.0.206 模型特性自适应：压缩阈值按模型上下文窗口缩放。
    max_input_tokens 有值 → min(80000, max_input×2) 且 ≥20000（1 token≈2 字符留余量）；
    无值 → 全局默认 80000。reasoning 模型额外 ×1.3（思考内容占上下文）。
    效果：大窗口模型（ornith 256K）上下文可更宽松不误压缩；小窗口模型更早压缩防爆窗。
    v1.0.345 基础载荷感知：base_chars 为 loop_msgs[0]（system 消息，含技能提示词/
    记忆/用户规则）的字符数。旧实现阈值是绝对值——全技能开启时基础载荷 27K+ 就已经
    超过 40K 阈值，对话刚开始压缩器就永久满负荷（实测见 docs/agent-evaluation-2.md）。
    新实现：阈值 = 基础载荷 + 对话预算（对话预算沿用原缩放逻辑），基础载荷不参与
    "是否压缩"的判定，只占窗口。"""
    try:
        m = resolve_model(cfg) if cfg else None
        if not m:
            return int(base_chars + CONTEXT_COMPRESS_THRESHOLD)
        dialog = CONTEXT_COMPRESS_THRESHOLD
        mi = m.get("max_input_tokens")
        if mi and int(mi) > 0:
            dialog = max(20000, min(80000, int(mi) * 2))
        if bool(m.get("enable_thinking")) or bool(m.get("thinking_supported_levels")):
            dialog = min(120000, int(dialog * 1.3))
        return int(max(0, base_chars) + dialog)
    except Exception:  # noqa: BLE001
        return int(max(0, base_chars) + CONTEXT_COMPRESS_THRESHOLD)



# v1.0.345 工具结果分级上限：旧版 20K 一刀切——列表/保存类工具的结果远用不满，
# 却和页面快照同配额；分级后常规工具 10K、文件读取 16K、页面快照 20K（保持原值）。
_TOOL_RESULT_CAPS = {
    "read_wecom_page": 20000, "capture_wecom_window": 20000,
    "read_workspace_file": 16000, "analyze_monthly_attachment": 16000,
}


def _tool_result_cap(name):
    return _TOOL_RESULT_CAPS.get(name, 10000)


def _first_user_title(msgs):
    """v1.0.355：取首条 user 消息前 20 字做会话标题（aborted/正常两条保存路径原各写一份）。"""
    for m in msgs:
        if m.get("role") == "user" and (m.get("content") or "").strip():
            return (m["content"]).strip()[:20]
    return None


def save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls=None):
    """补 tool 配对 + 存断点的**单一入口**（v1.0.654 B2）。
    原 loop.py 里「_fill_pending_tool_results + _save_chat_state」两行连排 ×12，
    另有若干裸 _save_chat_state——漏补配对会让断点带悬空 tool_calls，「继续」在
    严格后端上永久 400（v1.0.555 P0-4 / v1.0.652 P1-8 同族问题，靠人工对齐反复漏）。
    calls=None 表示本轮尚无工具调用（LLM 错误路径），跳过配对直接存。"""
    if calls is not None:
        _fill_pending_tool_results(loop_msgs, calls)
    _save_chat_state(pane_id, loop_msgs, current_round, readonly, model_id)


def _fill_pending_tool_results(loop_msgs, calls):
    """v1.0.361 审查修复（S4）：挂起（request_skill/源导入/安全审批）return 前为本轮
    尚无 tool 结果的调用补占位 tool 消息——维持 assistant.tool_calls 与 tool 结果
    一一配对（悬空结构会让严格后端 400，「继续」永远失败）。"""
    responded = {m.get("tool_call_id") for m in loop_msgs if m.get("role") == "tool"}
    for c in calls or []:
        cid = c.get("id")
        if cid and cid not in responded:
            loop_msgs.append({"role": "tool", "tool_call_id": cid,
                              "content": "（已挂起等待用户操作：用户确认后按提示继续，或换路径推进）"})


def _stuck_rescue_hint(hub_hint):
    """v1.0.347 卡住自救：熔断干预消息尾部随附未安装公共技能清单，
    引导 agent 在反复失败时「先找技能再硬试」——request_skill 请求安装后
    借助其专属工具与流程，通常比继续盲目尝试可靠。无清单（全已装/读取失败）返回空。"""
    if not hub_hint:
        return ""
    return ("\n\n【卡住自救——先找技能再硬试】检测到以下公共技能尚未安装，其中可能有与本任务"
            "匹配的专属工具与流程：" + hub_hint + "。若存在匹配项，优先调用 request_skill"
            "（skill_id 填对应 id）向用户请求安装——同意后会自动加载，通常比继续盲试可靠得多；"
            "确认都不匹配，再按上面的方式换路径推进。")


def _finish_guard_reason(fp, exam):
    """finish 预提交校验（v1.0.190）：返回拒绝原因字符串；None=放行。

    硬性规则（防"编造已完成直接交卷"的幻觉，把约束从 prompt 层升级到代码层）：
      - 最近一次页面读取有 form_progress 且 required > filled → 表单必填项未填完
      - 最近一次页面读取有 answered/unanswered 且 unanswered > 0 → 题目未答完
    页面没读过（两个缓存都空）或读到的进度已满足 → 放行。
    """
    if fp and isinstance(fp, dict):
        req, filled = fp.get("required"), fp.get("filled")
        if isinstance(req, int) and req > 0 and (not isinstance(filled, int) or filled < req):
            return f"表单还有必填项未填写（必填 {filled or 0}/{req}），请先补填再交卷/提交"
    if exam and isinstance(exam, dict):
        un = exam.get("unanswered")
        if isinstance(un, int) and un > 0:
            return f"还有 {un} 道题目未作答，请完成后再交卷"
    return None


# v1.0.344 finish 二段校验：识别 final_answer 中「声称已保存的产物库文件路径」。
# 只匹配带文件扩展名的路径（产物库/产出/报告.md），目录性提及（产物库/产出）不误伤。
_CLAIM_PATH_RE = re.compile(r"产物库[/\\][^\s，。；、）()\"'`]*?\.[A-Za-z0-9]{1,5}")


def _missing_claimed_files(final_answer):
    """final_answer 里声称已保存的产物库文件，逐个核对是否真实存在。
    返回缺失路径列表（最多 5 个）；无声称/全部存在 → 空列表。"""
    try:
        fa = str(final_answer or "")
        if "产物库" not in fa:
            return []
        
        root = paths.workspace_root()
        missing = []
        for raw in set(_CLAIM_PATH_RE.findall(fa)):
            p = raw.rstrip("。，,;；)）.")
            norm = p.replace("\\", "/")
            # v1.0.344：去重放在归一化之后——「产物库/a.md」与「产物库\a.md」在 findall
            # 阶段是两个不同字符串，归一化后才相同（实测混合分隔符会报两条）。
            if norm in missing:
                continue
            cand = os.path.join(root, *norm.replace("产物库/", "", 1).split("/")) \
                if norm.lower().startswith("产物库") else os.path.join(root, *norm.split("/"))
            if not os.path.exists(cand):
                missing.append(norm)
            if len(missing) >= 5:
                break
        return missing
    except Exception:  # noqa: BLE001
        return []  # 校验自身异常不阻塞 finish（保守放行）


def _judge_escalate(tag, loop_msgs, judge, escalated, pane_id):
    """双模型分工·自动难度升级：小模型卡住/需判断时，用 judge 大模型对最近上下文生成判断建议注入。
    v1.0.345：per-tag 允许升级 2 次（旧版只 1 次）——任务中期同类二次卡住是更强的求救信号，
    却被旧版全局去重挡掉。第 2 次升级的注入内容自带「再次卡住」语境，judge 建议会更直接。
    未配置 judge 返回 False（纯单模型，零行为）。
    v1.0.555（P1-8）：返回值由 bool 改为 (ok, detail)——judge 不可用/失败时调用方
    据此向前端推送可见提示。原实现失败只写一行日志，finish 被拦→升级静默失败→
    小模型原样再撞→streak 到 3 硬终止，终止文案"连续 3 次交卷被拦截"完全不提
    judge 不可用，排障方向被引向"小模型不达标"（真实原因是升级层瘫了）。"""
    if not judge:
        return False, "judge 未配置"
    n = escalated.get(tag, 0)
    if n >= 2:
        return False, f"{tag} 升级额度已用尽（2 次）"
    # v1.0.515 修复（审计 P1-8）：升级前检查 abort——judge 调用是同步 _llm_complete
    #（10-90s），用户已点停止/任务已被看门狗置 abort 时不再发起新的大模型请求
    if pane_id and _is_aborted(pane_id):
        return False, "任务已中止"
    # v1.0.361 审查修复（O4）：升级额度改为成功后递增——原在调用前预扣，judge 网络抖动
    # 失败也会烧掉 2 次额度，真正的卡死反而得不到大模型介入。
    try:
        recent = loop_msgs[-10:]
        _again = "（注意：这是本任务同类卡住的再次发生——上次建议未能脱困，请给出更直接、更可执行的替代路径。）" if n == 1 else ""
        msgs = [{"role": "system", "content": "你是判断大模型。下面是 agent（小模型）当前卡住/需要判断的上下文节选，请给出明确、可执行的下一步判断或建议（简洁，直接给结论，不要复述）。"},
                {"role": "user", "content": _again + json.dumps(recent, ensure_ascii=False)[:6000]}]
        advice = _llm_complete(judge, msgs, disable_thinking=True,
                               poll_abort=(None if pane_id is None else (lambda: _is_aborted(pane_id))),
                               deadline=300)
        if advice:
            escalated[tag] = n + 1
            loop_msgs.append({"role": "user", "content": f"（大模型判断建议·{tag}）{advice}"})
            log_event(f"[agent] 难度自动升级：judge 模型介入（{tag} 第{n+1}次）pane={pane_id}")
            return True, "ok"
        return False, "judge 无响应（服务未启动/超时/不可达）"
    except Exception as e:  # noqa: BLE001
        log_event(f"[agent] judge 升级失败（{tag}）：{e}")
        return False, f"judge 调用异常：{e}"


# v1.0.470（P2-9）：Ask 纪律文案抽成模块常量（测试断言 + 单一来源）
_ASK_DISCIPLINE = ("\n\n[Ask 只问答模式纪律] 本轮为只问答模式：不要生成任何文件"
                   "（Excel/Word/PPT/文本文件一律不要），也不要调用写类工具；"
                   "表格用 Markdown 表格、长文本直接在回复正文中展示。"
                   "若确需澄清，用 ask_user **一次性问全所有问题**（最多一轮），"
                   "之后直接基于合理假设给出完整回答——禁止反复多轮追问。")



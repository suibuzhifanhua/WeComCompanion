"""agent.loop — agent_loop 主循环（逐字自 backend.py 迁出，T04；函数内熔断状态保持局部变量原样）。"""

import hashlib
import json
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import wecom_bridge as wb

from core_utils import log_event, save_chat_log, _mask_args
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）
from llm_client import resolve_model, stream_llm_events, judge_model, _llm_complete, \
    is_stream_interrupt, with_thinking_override, _THINK_LOOP_MARK
from agent_tools import build_agent_tools, dispatch_tool, set_current_pane, is_file_producer
from security_ops import permission_level as resolve_permission_level, gate_write_tool, begin_approval
from .session import (
    _set_abort, _is_aborted, _save_chat_state, chat_states, _chat_state_lock,
    MAX_ROUNDS, TAIL_FINISH_ROUNDS,
    abort_generation, globally_aborted_since,
    get_pane_permission, set_pane_permission, is_background_pane,
)
from .loop_guard import LoopGuard, is_read_tool
from .messages import _msg_chars, _compress_loop_msgs, CONTEXT_COMPRESS_THRESHOLD, KEEP_LEADING_MSGS
from agent_tools.registry import _PARALLEL_SAFE_TOOLS
from .context import build_chat_messages  # v1.0.302：_auto_loop_chain 搬入本模块需要


# v1.0.523-7：模块级辅助函数拆至 loop_ops.py（原 L27-222 逐字搬移）——
# 显式 re-import 保持 from agent.loop import X 的既有引用兼容。
from core.result import is_err, err_msg, err, to_model   # v1.0.655 B5 / v1.0.656 统一
from .loop_ops import (
    _align_tool_pairing, _reset_after_item, _model_compress_threshold,
    _TOOL_RESULT_CAPS, _tool_result_cap, _first_user_title,
    _fill_pending_tool_results, _stuck_rescue_hint, _finish_guard_reason,
    _CLAIM_PATH_RE, _missing_claimed_files, _judge_escalate,
    _ASK_DISCIPLINE, save_state_safe,
)


# v1.0.650（F4）：默认值搬到 core/config_store.py（设置页要和 agent 读同一份数字，
# 两处各写一遍必漂）。这里保留同名引用——本文件其余代码与测试都按这个名字取。
from core.config_store import AGENT_SOFT_BUDGET_DEFAULTS as SOFT_BUDGET_DEFAULTS


def soft_budget_state(cfg, prompt_tokens, elapsed_s, progress_done):
    """无限续跑模式的资源软预算判定（纯函数，便于行为测试）。
    返回 (是否触顶, 生效的 token 额度, 生效的分钟额度)。

    两条口径是本轮（v1.0.622，用户实测「答题跑到第 5 题被判预算上限」）改的：
      · **0 = 关闭该项**（原实现 `int(cfg.get(k) or 默认)` 把 0 吞成默认，
        于是"跑到做完为止"在配置里根本没法表达）；
      · **额度跟着进度走**：累计 prompt_tokens 天然随轮数涨（上下文每轮重传一次，
        今天实测一场 9 题的考试跑到 messages=100 条），用固定额度去卡批量/答题任务
        等于"不许跑长任务"。真正该拦的是**零推进空转**——一项都没完成却把额度烧光。
        所以每完成一项（append_progress 成功）追加一份额度；配合自动上下文精简，
        推进中的长任务不会触顶，空转的仍在基础额度处被收尾。
    任何一项基础额度为 0 表示该项不参与判定；两项都为 0 = 完全关闭软预算。
    """
    try:
        ag = (cfg or {}).get("agent") or {}
    except Exception:  # noqa: BLE001
        ag = {}

    def _pick(key):
        v = ag.get(key)
        if v is None:
            return SOFT_BUDGET_DEFAULTS[key]
        try:
            return max(0, int(v))          # 0 保留（=关闭），负数按 0 处理
        except Exception:  # noqa: BLE001
            return SOFT_BUDGET_DEFAULTS[key]

    base_t = _pick("soft_budget_tokens")
    base_m = _pick("soft_budget_minutes")
    tok_eff = base_t + _pick("soft_budget_tokens_per_item") * max(0, int(progress_done or 0))
    min_eff = base_m + _pick("soft_budget_minutes_per_item") * max(0, int(progress_done or 0))
    hit = ((base_t > 0 and int(prompt_tokens or 0) >= tok_eff)
           or (base_m > 0 and float(elapsed_s or 0) >= min_eff * 60))
    return hit, tok_eff, min_eff


def _interrupt_notice(err_text):
    """流式中断的实时播报文案（v1.0.628）：思考循环不再和瞬态断流共用一句话——
    用户看到「流式中断，自动重试中…」连弹三条，无从判断该干什么。"""
    if _THINK_LOOP_MARK in (err_text or ""):
        return "[模型只想不做（思考 5 分钟零正文），自动关思考重试…]"
    return "[LLM 流式中断，自动重试中…]"


def _interrupt_tip(err_text):
    """重试耗尽后的兜底文案（v1.0.628）：思考循环给处置动作，
    原「未收到完成信号」把需要换档位/换模型的故障说成了网络抖动。"""
    if _THINK_LOOP_MARK in (err_text or ""):
        return ("模型一直在思考、迟迟不输出正文或动作（自动重试已试过关思考）。"
                "建议：点「继续」前先在本轮把「🧠 思考」调成「关」，或换一个模型重试。")
    return "LLM 流异常中断，未收到完成信号。（已保存断点，可点击「继续」重试）"


def _sys_notice_sse(text):
    """**系统播报**的 SSE 包装（v1.0.632，B3）：带 `sys:1` 标记。

    这类方括号提示（进入收尾、软预算、流式中断重试、judge 介入…）是软件替用户
    写的状态说明，**不是模型说的话**。前端照旧渲染（它只读 type/text），但
    `routes/chat.py::_memory_watch` 会跳过带 sys 的 token——否则用户点停止或
    任务超轮次时没有 `【完成】`，落库就走 `buf[-4000:]` 兜底支，把一堆播报当成
    AI 回复存进会话，再被下一轮当"模型自己说过的话"回灌上下文（模型会开始模仿
    这些方括号文本）。用结构化标记而不是按文案匹配：改播报字面量不会悄悄失效。"""
    return "data: " + json.dumps({'type': 'token', 'text': text, 'sys': 1},
                                 ensure_ascii=False) + "\n\n"


# ==================== 回答末尾的「本次生成的文件」清单（v1.0.634，用户要求） ====================
# 之前只有两处：① 步骤卡下面那条文件行（v1.0.627/629）——要滚上去找；
# ② 正文里模型自己写的路径被 linkify 成按钮——模型不写就没有、写错字就不亮。
# 用户要的是**确定性**：任务产出过文件，回答末尾就一定有一份能点的清单。
# 所以这段由后端在收尾时拼进最终回答文本（而不是前端另画一块）：
# 走的是同一条 token 通道 → 实时气泡、会话落库、重开历史三处天然一致，
# 不会出现"当场看得到、翻历史没了"这种最难查的双渲染分叉。

_MAX_MANIFEST_ROWS = 12


def _ws_disp_path(p):
    """产物路径 → 前端认得的展示形态：产物库内给 `产物库/产出/x.pptx`（正斜杠），
    产物库外保留原绝对路径。

    必须是正斜杠：`js/09-chat-render.js::_WS_PATH_RE` 只认 `产物库/<目录>/文件名.扩展名`
    这一种写法（反斜杠不在它的前缀里），清单每行都指望它变成可点按钮。"""
    s = str(p or "").strip()
    if not s:
        return ""
    try:
        rel = os.path.relpath(s, paths.workspace_root())
        if rel and not rel.startswith(".."):
            # workspace_root() 本身就是 产物库/ 那一层，所以相对路径只剩 `产出/x.pptx`；
            # 补回段名给用户看才是他们在产物库页里认识的那个路径，也是 linkify 认得的写法。
            return ("%s/%s" % (paths.WORKSPACE_NAME, rel.replace(os.sep, "/")))
    except Exception as e:  # noqa: BLE001
        rel = ""
        log_event(f"[agent] 产物路径相对化失败（按绝对路径列出）：{type(e).__name__}: {e}")
    return s.replace("\\", "/")


# 交付物扩展名白名单——与 js/09-chat-render.js::_WS_PATH_RE 的那份**必须同步改**，
# 否则清单里列出来的行在正文里点不动（两边各自匹配一份，历史上就漂过一次）。
_PRODUCED_EXT = ("md|txt|csv|tsv|xlsx?|json|html?|pdf|docx?|pptx?|png|jpe?g|webp|zip|py")
# 绝对路径：Windows 盘符开头（反斜杠或正斜杠都收）。前面的负向回顾是必需的——
# 少了它，`https://example.com/a.md` 会被咬成"盘符 s:"开头的路径（与 JS 侧同一颗雷，
# 两边都补了；台子 T4 就是专门盯这个）。只认盘符，不认 POSIX `/a/b.pdf`：那会把
# URL 里的半截路径当成产出，而本机产出不会有 POSIX 路径。
_ANS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9/])[A-Za-z]:[\\/](?:[^\s\"'“”‘’<>|?*\[\]，。；、）】《》]{1,200}?)"
    r"\.(?:%s)" % _PRODUCED_EXT, re.I)


def _answer_file_paths(text):
    """v1.0.641（用户实报）：模型把交付物写到**产物库外**（桌面/下载是常见落点）时，
    既没有任何工具结果带它的路径、磁盘差集又只扫产物库，于是「本次生成的文件」
    把它漏掉——而路径明明就写在回答正文里。

    所以这里把正文里出现的绝对路径抽出来，**逐个 stat 确认真有这个文件**才算产出：
    模型吹过的路径、示例代码里的假路径、被提示注入塞进来的一句 `C:\\Windows\\x.pdf`
    （不存在）都会因为 isfile 不成立而被丢掉。存在性之外不再做别的判断，
    能不能跳转由 /api/workspace/locate 那道白名单管（两层互不信任）。"""
    out = []
    for m in _ANS_PATH_RE.finditer(str(text or "")):
        p = m.group(0)
        try:
            if p not in out and os.path.isfile(p):
                out.append(p)
        except OSError:
            continue
    return out


def _produced_manifest(files, final_answer=""):
    """拼清单文本；没有产物返回空串（**没产生文件就不显示这一块**）。

    两层去重：① 同一路径只列一次（批量任务里同一个进度/报告文件被反复写是常态）；
    ② 模型正文里已经原样写过这个路径的不再重复——那一处本来就会被 linkify 成按钮。"""
    seen, rows = set(), []
    ans = str(final_answer or "")
    # 斜杠归一：正文里模型常写 `C:\Users\…\x.pdf`（反斜杠），清单出的是正斜杠。
    ans_fwd = ans.replace("\\", "/")
    ws = paths.WORKSPACE_NAME + "/"
    for p in (files or []):
        d = _ws_disp_path(p)
        if not d or d in seen:
            continue
        seen.add(d)
        # v1.0.634 的去重只管**产物库内**：那种路径正文里一定会被 linkify 成按钮，
        # 清单再列一次是重复。产物库外的绝对路径（桌面/下载）**必须照列**——
        # 用户实报"生成在桌面的 PDF 没被列出来"，正是被这条去重吞掉的第二次。
        if d.startswith(ws) and (d in ans or d in ans_fwd):
            continue
        rows.append(d)
    if not rows:
        return ""
    shown = rows[:_MAX_MANIFEST_ROWS]
    lines = ["\n\n---\n\n📎 本次生成的文件（%d）" % len(rows)]
    lines += ["- " + r for r in shown]
    if len(rows) > len(shown):
        lines.append("- …另有 %d 个未列出（完整清单见产物库）" % (len(rows) - len(shown)))
    return "\n".join(lines) + "\n"


def agent_loop(messages=None, cfg=None, max_rounds=None, pane_id=None, readonly=False,
               resume_note=None, model_id=None, notify=True, permission_level=None,
               thinking=None):
    """readonly=True（v1.0.313 Ask 模式）：写类工具（非读类前缀且非 finish/ask_judge）
    调用直接返回结构化错误——只允许读取/检索/问答，不碰页面与文件。
    resume_note（v1.0.317）：/api/chat/continue 附带的一行上下文（如「用户已同意安装
    技能 X」/「用户拒绝安装」），断点恢复时追加到续跑提示末尾。
    model_id（v1.0.486）：**每任务独立模型**——多任务会话并行时各任务可指定不同
    模型；None=用全局默认。
    permission_level（v1.0.590）：**每任务独立权限级别**——ask_every 每次询问 /
    auto 自动审批 / full 完全访问。None 时按「本 pane 记忆 → 全局配置」依次回退。
    与 command_mode（危险命中后的严重度）正交；full 只降级「询问」，
    block 档的致命命令与保护路径拦截仍然生效。
    notify（v1.0.570 用户需求）：任务结束（完成/停止/异常中断）时弹 Windows 原生
    Toast。调用方自带完成通知的（scheduler 定时任务）传 False 防双响。"""
    # 无限续跑模式：未显式指定轮数（max_rounds=None，常规对话）→ 不设轮次上限，
    # 上下文超阈值自动压缩后继续跑，不再 100 轮触顶中断；显式传入的轮数（如工作台
    # 快捷任务的 5 轮约束）保留原「主预算 + 收尾续跑」逻辑。
    unlimited = (max_rounds is None)
    if max_rounds is None:
        max_rounds = MAX_ROUNDS
    # v1.0.555（P1-11）：快照全停代次——本任务启动后的"停止所有"才命中本任务，
    # 任务收尾不再清全局键（原 _set_abort(None, False) 让后台任务逃逸全停）。
    _my_abort_gen = abort_generation()

    def _stop_requested():
        """pane 级中断 或 本任务启动后的全局全停。"""
        return _is_aborted(pane_id) or globally_aborted_since(_my_abort_gen)
    # 主预算 + 自动收尾续跑预算（达到主轮次上限后自动续跑收尾，无需手动点「继续」）；
    # 无限续跑模式下无上限，total_rounds 取无穷，永不因轮数退出。
    total_rounds = float("inf") if unlimited else max_rounds + TAIL_FINISH_ROUNDS
    _rounds_display = max_rounds if not unlimited else "∞"  # 日志展示用（无限模式不显示/100）
    _meta_max = max_rounds if not unlimited else 0          # 前端 meta：0 表示不设上限
    model = resolve_model(cfg, model_id)
    if not model:
        yield f"data: {json.dumps({'type':'error','text':'未配置任何模型。请在「模型管理」里添加（如 DeepSeek）。'}, ensure_ascii=False)}\n\n"
        return
    # v1.0.601 会话级思考档位：聊天输入行的「🧠 思考」下拉随请求带上来，
    # 只在这次请求的模型副本上生效（浅拷贝），模型管理里的配置不被对话改脏。
    model = with_thinking_override(model, thinking)

    # 双模型分工：judge（大模型）做主要判断，小模型（model）跑工具主循环；
    # judge=None（未配置 judge_model_id）→ 纯单模型，下面所有升级路径自动跳过。
    judge = judge_model(cfg)
    judge_escalated = {}   # tag -> 已升级次数（v1.0.345：per-tag 允许 2 次）

    # 权限级别（v1.0.590）：显式传入 > 本 pane 记忆（断点恢复）> 全局配置默认。
    # 解析一次并写回侧表，_save_chat_state / 断点落盘随会话持久化——「继续」后
    # 权限档不会掉回默认（与 v1.0.485 readonly 持久化同一个坑的同一个教训）。
    permission_level = (str(permission_level).strip().lower() if permission_level else "") \
        or get_pane_permission(pane_id) or resolve_permission_level(cfg)
    if permission_level not in ("ask_every", "auto", "full"):
        permission_level = "auto"
    # 后台/无人值守任务（定时 sched-、跑课 study-、以及无 pane 的直调）没有人在屏幕前
    # 点弹窗——继承「每次询问」会让任务在第一个写操作上永久挂住。与 request_skill 的
    # 「后台任务无法弹窗」同一处理原则：降级为自动审批并留痕。
    if permission_level == "ask_every" and is_background_pane(pane_id):
        permission_level = "auto"
        log_event(f"[agent] 后台任务（pane={pane_id or '无'}）不支持逐次询问，权限级别降为自动审批")
    set_pane_permission(pane_id, permission_level)
    if permission_level != "auto":
        log_event(f"[agent] 本会话权限级别={permission_level}（pane={pane_id}）")

    # v1.0.592 修复（P1）：新任务开局先清掉本 pane 的「当前跟随窗口」。
    # v1.0.590 把 _AUTO_PICK 从进程级单值改成按 pane 隔离，但只做了隔离、没做回收：
    # 该表只在窗口关闭/序号越界时 pop（windows._set_auto_pick / close 路径），
    # drop_chat_state 也不清它 → 上一段任务点进详情页留下的 index 会一路带进下一段任务。
    # 下一段任务里凡是省略 pick 的 read/click/close_wecom_page，都会作用在"上个任务遗留的那个
    # 窗口序号"上；期间用户关掉若干窗口后序号漂移，close_wecom_page 就可能关掉用户此刻
    # 正在看的别的窗口。断点续跑（resume_note 有值）必须保留跟随窗，故只在非 resume 时清。
    if pane_id and not resume_note:
        try:
            from wecom_bridge.windows import set_auto_pick_scope, _set_auto_pick
            set_auto_pick_scope(pane_id)
            _set_auto_pick(None)
        except Exception as e:  # noqa: BLE001
            log_event(f"[agent] 跟随窗口复位失败（不致命）：{e}")

    # （v1.0.240）v1.0.237 的「点选项前必须先 ask_judge」硬拦截已移除：
    # 无限次考试新策略是首答阶段快速作答不逐题问大模型（用户定调：效率优先），
    # 硬拦截与该策略直接冲突。judge 的使用改由技能策略驱动（试错阶段存疑题按需问），
    # 代码层兜底仍保留 _judge_escalate（卡住时自动拉大模型）。

    # 对话元信息（模型名/上下文估算/轮次）推给前端，显示在聊天栏
    # 注意：向用户展示的 max_rounds 用「主思考预算」MAX_ROUNDS（100），而非 total_rounds(110)。
    # TAIL_FINISH_ROUNDS(10) 是达到主预算后自动收尾的内部安全网，不属于用户的真实思考预算，
    # 若一并计入会让用户误以为有 110 轮自由思考空间，且轮次计数含收尾段会虚高进度（如 105/110 看着像 95% 完成）。
    _model_name = model.get("name") or model.get("model") or "?"
    yield f"data: {json.dumps({'type':'meta','model':_model_name,'ctx_chars':0,'rounds':0,'max_rounds':max_rounds}, ensure_ascii=False)}\n\n"

    # 尝试恢复断点上下文（续跑由用户显式点击「继续」驱动，故重置轮次预算为 0，
    # 否则续跑会因 current_round 已等于 max_rounds 而立即退出，导致「继续」无效）。
    with _chat_state_lock:
        state = chat_states.pop(pane_id, None) if pane_id else None
    if state:
        # 兜底：断点数据损坏/结构异常时降级为新对话，不抛异常导致「继续」500
        try:
            _s_msgs = state.get("loop_msgs")
            loop_msgs = list(_s_msgs) if isinstance(_s_msgs, list) else []
        except Exception:  # noqa: BLE001
            log_event(f"[agent] 断点数据异常，降级为新对话 pane={pane_id}")
            loop_msgs = []
        if not loop_msgs and messages:
            loop_msgs = list(messages)
        resumed_from = state.get("rounds", 0) if isinstance(state.get("rounds", 0), int) else 0
        # v1.0.652（审计 P0-2）：readonly/model_id 从断点**自读**——恢复分支此前只读
        # loop_msgs/rounds，这两个字段靠调用方传参；routes 的 /chat/continue 专门补了
        # 还原（v1.0.485），auto_loop 的两条自动续跑路径没有等价物 → Ask（只问答）
        # 会话断流自动续跑后 readonly 回落 False，pc_run/click 等写工具全部解禁
        #（v1.0.485 安全修复的旁路复发）。合并语义与 routes/chat.py 一致：
        # readonly 只收紧不放松（显式参数 OR 断点），model_id 显式参数优先。
        if state.get("readonly"):
            readonly = True
        if not model_id:
            model_id = state.get("model_id") or None
        log_event(f"[agent] 从断点恢复 pane={pane_id}，已完成 {resumed_from} 轮")
        # 提醒模型继续推进，而不是简单重复
        # P3-6 增强：从已保存上下文提取「已完成步骤摘要 + 最近一步结果」注入，
        # 断点续跑时模型能明确感知前序进展，避免中间历史压缩/截断导致重复执行已完成步骤。
        # v1.0.190 增强：额外提取最后一次【写操作】签名（点击/输入/滚动等）单独提醒，
        # 防断点恢复后模型重复执行已生效的页面操作（如再点一次"已提交"按钮）。
        _done_steps, _last_out = [], ""
        _last_write = ""
        for m in loop_msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = (tc.get("function") or {}).get("name") or "?"
                    _done_steps.append(fn)
                    # 写操作 = 非读类工具（capture_/list_/read_/search_/query_/get_ 开头 与 finish 之外）
                    if not (fn.startswith(("capture_", "list_", "read_", "search_", "query_", "get_"))
                            or fn == "finish"):
                        try:
                            _args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                        except Exception:  # noqa: BLE001
                            _args = {}
                        _last_write = f"{fn} {json.dumps(_args, ensure_ascii=False)[:80]}"
            elif m.get("role") == "tool" and m.get("content"):
                _last_out = str(m["content"]).strip()
        _resume_note = ("请继续完成上述任务，不要重复已经执行过的步骤，直接推进到下一步。"
                        "【已完成步骤摘要】" + ("、".join(_done_steps[-20:]) or "（无）"))
        if _last_write:
            _resume_note += (f"【上次执行的页面操作】{_last_write}——该操作若已生效（已点击/已输入/已选择），"
                             "绝对不要重复执行；请先 read_wecom_page 确认页面当前状态再决定下一步。")
        if _last_out:
            _resume_note += f"【最近一步结果】{_last_out[:120]}"
        if resume_note:
            _resume_note += f"\n【用户决定】{resume_note}"
        loop_msgs.append({"role": "user", "content": _resume_note})
    else:
        loop_msgs = list(messages) if messages else []
        resumed_from = 0

    # P3-8：维护上下文增量字符计数，供压缩判断使用，避免每轮对全量消息求和扫描
    # v1.0.470（P2-9）：Ask 纪律注入从「就地改调用方 messages」移到这里改 loop_msgs
    # 副本（且替换而非原地改 dict）——原实现会污染 build_chat_messages 产物，team
    # 流水线的 repair_msgs 共享同一批 dict 时 Ask 文案被重复追加泄漏到其它分支。
    if readonly and loop_msgs:
        try:
            _first_sys_idx = next((i for i, _m in enumerate(loop_msgs)
                                   if isinstance(_m, dict) and _m.get("role") == "system"), None)
            if _first_sys_idx is not None:
                _sys_copy = dict(loop_msgs[_first_sys_idx])
                _sys_copy["content"] = (_sys_copy.get("content") or "") + _ASK_DISCIPLINE
                loop_msgs[_first_sys_idx] = _sys_copy
        except Exception:  # noqa: BLE001
            pass
    _msgs_chars = sum(_msg_chars(m) for m in loop_msgs)

    # 动态拼装当前应加载的工具（已启用技能 + 已配置连接器）
    # v1.0.345：带任务文本做场景匹配——工具集与 build_chat_messages 的技能提示词保持
    # 同一套匹配结果（同一 task_text、确定性关键词匹配），避免"提示词说了能用、工具没挂"。
    # 位置注意：必须在 loop_msgs 建立之后（断点恢复/新对话两分支汇合处）——v1.0.345 首版
    # 曾放在前文导致 UnboundLocalError（本模块内首个真实回归，教训：引用局部变量的初始化
    # 必须跟其定义的分支汇合点对齐）。
    from agent_tools import _extract_task_text
    tools, _ = build_agent_tools(cfg, task_text=_extract_task_text(loop_msgs))
    log_event(f"[agent] 开始对话 pane={pane_id}，模型={model.get('name')}，工具 {len(tools)} 个")
    # v1.0.347 卡住自救清单：未安装公共技能（与 request_skill 注入同源读取）。
    # 熔断干预（写/读/错三路 warn）时随附此清单，引导 agent「先找技能再硬试」。
    from agent_tools import hub_missing_skills
    _hub_hint = "；".join(f"{s['id']}({s.get('name')})" for s in hub_missing_skills(cfg))

    finished = False
    final_answer = ""
    _aborted = False
    current_round = 0
    _tail_rounds_used = 0   # v1.0.470（P1-7）：无限模式软预算收尾段已用轮数
    no_call_rounds = 0  # 连续无工具调用轮数（完成确认机制用）
    # —— 死循环熔断状态机（v1.0.345 抽出为 LoopGuard 类，判定逻辑/复位语义见 agent/loop_guard.py；
    #     干预消息构造与上下文回退手术仍在本循环内执行）——
    _guard = LoopGuard()
    tail_started = False    # 是否已进入自动收尾续跑段（达到主轮次上限后插入收尾引导）
    # —— finish 预提交校验状态（v1.0.190）：从最近一次页面读取结果缓存表单/答题进度，
    #    finish（交卷/提交/结束）前做硬性检查——必填未填完/题目未答完直接拒绝交卷，
    #    防"模型编造已完成直接 finish"的幻觉。连续被拒 3 次仍不补齐则终止并保存断点。 ——
    _finish_guard_fp = None      # 最近一次 read 的 form_progress {required, filled}
    _finish_guard_exam = None    # 最近一次 read 的 {answered, unanswered}
    _finish_reject_streak = 0    # finish 连续被拒次数（成功放行/其它写操作重置）
    _judge_fail_n = 0            # v1.0.555（P1-8）：judge 升级失败计数（终止文案附注，指向真实根因）
    _last_links = None           # 最近一次 read 的 links（可点击链接入口清单，批量任务用）
    # v1.0.344 批量进度仪表 + 任务成本汇总：
    _progress_done = 0           # 本次会话 append_progress 成功次数（批量任务已完成项数）
    _t_start = time.monotonic()  # 任务起始时刻（v1.0.515 审计 P2-15：改单调时钟——time.time 受 NTP 校正/手动改时影响，回拨会延长 2h 软预算、前跳会误触发）
    _tool_n = 0                  # 工具调用总次数
    _tool_err_n = 0              # 工具返回失败次数
    _tool_names = []             # v1.0.582：工具名序列（相似任务指纹 → 技能自动沉淀）
    _produced_files = []         # v1.0.634：本次任务产出的文件路径（回答末尾清单，按调用顺序）
    # v1.0.635（用户实报"清单漏了产出的 ppt"）：工具看不见的产出（agent 写脚本再 pc_run
    # 跑出来的 .pptx）靠磁盘差集兜。快照失败只意味着清单退回"只列工具上报的"，不影响任务。
    try:
        from core.workspace_io import snapshot_outputs as _snap_outputs, new_outputs as _diff_outputs
        _out_snapshot = _snap_outputs()
    except Exception as _se:  # noqa: BLE001
        _diff_outputs = None
        _out_snapshot = None
        log_event(f"[agent] 产物快照失败（清单只列工具上报的产出）：{type(_se).__name__}: {_se}")
    _usage_sum = {"prompt_tokens": 0, "completion_tokens": 0}  # LLM token 累计（模型上报时）
    _prompt_chars_total = 0  # v1.0.515（审计 P1-6）：各轮上下文字符累计——服务端不回 usage 时按此估算 token 预算
    _last_read_hash = None       # v1.0.345 read 去重：上次页面正文内容 hash
    _compress_fail_streak = 0    # v1.0.555（P0-3 配套）：上下文压缩连续失败计数——
                                 # 原 except 无限静默（loop_ops 拆模块漏 import 时每轮
                                 # NameError 全被吞，上下文治理实际下线无人知晓）。
                                 # 连续 3 次失败即 fail-loud 推送错误事件。

    # v1.0.345 基础载荷字符数（system 消息 = 主 prompt + 技能提示词 + 记忆 + 用户规则），
    # 压缩阈值 = 基础载荷 + 对话预算——基础载荷不再挤占对话余量（详见 docs/agent-evaluation-2.md）。
    # 断点恢复时 loop_msgs[0] 同样是 system 消息（结构不变契约），取值安全。
    try:
        _base_chars = _msg_chars(loop_msgs[0]) if loop_msgs else 0
    except Exception:  # noqa: BLE001
        _base_chars = 0

    _WARN_WRITE_ERR = ("（系统警告）检测到你连续 {streak} 次执行同一操作但每次都报错（元素不存在/页面未就绪/点击无响应等），疑似陷入死循环。"
                       "立即停止重复该操作。请先调用 read_wecom_page 查看当前页面最新状态："
                       "若页面内容与上次读取基本相同，说明该元素已失效或页面状态已变化（可能已答题/已跳转/控件位置变化），"
                       "不要再尝试点击它，直接标记此步异常、记录原因后推进到下一步，或调用 finish 结束；"
                       "若页面已变化，基于新页面改用 list_interactive_elements 重新定位目标元素后执行不同的、能推进任务的操作。"
                       "不要盲目反复重试同一失败操作。")
    _WARN_WRITE = ("（系统警告）检测到你连续 {streak} 次执行完全相同的操作，疑似陷入死循环。"
                   "立即停止重复同一操作。请先调用 read_wecom_page 查看当前页面最新状态："
                   "若页面内容与上次读取基本相同，说明刚才的操作无效（如点击无响应、元素已失效），"
                   "不要再尝试该操作，直接标记此步异常、记录原因后推进到下一步，或调用 finish 结束；"
                   "若页面内容已有明显变化，基于新页面执行不同的、能推进任务的操作。"
                   "若你确认这是正常推进（例如逐题翻页），可先读取一次页面再继续下一步。"
                   "注意：如果刚才的工具调用报错了，这属于正常重试，请先读取页面确认状态后再尝试其它方式，不要盲目反复执行同一操作。")
    _WARN_READ = ("（系统警告）检测到你连续 {streak} 次读取完全相同的页面/元素，但页面内容没有变化，疑似陷入读取死循环。"
                  "立即停止重复读取同一内容。请尝试换一种方式推进："
                  "1. 用 scroll_wecom_window 滚动页面，看是否有新内容加载出来；"
                  "2. 用 list_interactive_elements 换个方式定位目标元素；"
                  "3. 若是页面异步加载慢，最多再等待并读取 1 次；"
                  "4. 若仍无进展，标记此步异常、记录原因后跳过当前题目/步骤，继续下一步或调用 finish 结束。"
                  "不要连续多次读取相同内容。")

    def _apply_warn(kind, msg_text, warn_text, call_idx):
        """v1.0.355：写/读熔断 warn 三段同构收敛（原 ~60 行逐字重复）——
        回退 asst_idx → 插干预消息（附卡住自救提示）→ judge 升级 → 返回待 yield 的 SSE 文案列表。
        v1.0.361 审查修复（S4）：assistant 只保留「已有 tool 结果」的调用项（截到 call_idx），
        防止出现带 tool_calls 却缺 tool 响应的悬空结构（严格后端直接 400）。"""
        nonlocal _msgs_chars
        _kept_calls = [tc for tc in (asst.get("tool_calls") or [])[:call_idx]]
        _new_asst = {"role": "assistant", "content": round_text}
        if _kept_calls:
            _new_asst["tool_calls"] = _kept_calls
        loop_msgs[asst_idx] = _new_asst
        _msgs_chars += _msg_chars(loop_msgs[asst_idx]) - _msg_chars(asst)
        # v1.0.555（P0-4 修复）：原 `del loop_msgs[asst_idx + 1:]` 把 asst_idx 之后
        # 的所有消息一并删除——包括 _kept_calls（calls[:call_idx]）按序追加的
        # call_idx 条 tool 结果，导致"带 tool_calls、零 tool 响应"的悬空结构，
        # 严格后端（vLLM/sglang/OpenAI 兼容）直接 400，且损坏态会被
        # _save_chat_state 持久化，「继续」后持续 400。
        # 正确边界：只删 call_idx 之后的消息（该次调用的结果+后续全部）。
        del loop_msgs[asst_idx + 1 + call_idx:]
        loop_msgs.append({"role": "user", "content": msg_text + _stuck_rescue_hint(_hub_hint)})
        _msgs_chars += _msg_chars(loop_msgs[-1])
        evs = []
        _esc_ok, _esc_detail = _judge_escalate(kind, loop_msgs, judge, judge_escalated, pane_id)
        if _esc_ok:
            _msgs_chars += _msg_chars(loop_msgs[-1])
            evs.append('\n\n---\n\n[大模型判断介入中…]')
        elif _esc_detail != "judge 未配置":
            # v1.0.555（P1-8）：judge 配了但升级失败（不可达/超时/额度用尽）——原静默
            # 无感知，用户只看到小模型反复卡住，排障被引向"小模型不达标"。推可见提示
            # 指向真实根因（配合已知不可达中继，升级功能实际下线而无人知晓）。
            evs.append(f"\n\n---\n\n[大模型暂不可用（{_esc_detail}），已回退小模型自行判断]")
        evs.append(warn_text)
        return evs

    while current_round < total_rounds:
        round_no = current_round + 1
        # 上下文压缩：每轮开始前检查 loop_msgs 总字符数，超阈值则压缩中间历史，
        # 避免 LM Studio 上下文被截断导致模型丢失原始任务指令（答题一半就停的根因之一）。
        # P3-8：基于增量字符计数判断，未超阈值时零扫描直接通过。
        # 字符增量追踪维护不精确时，直接用实测值校准：
        try:
            loop_msgs, _msgs_chars = _compress_loop_msgs(loop_msgs, prev_total=_msgs_chars,
                                                         threshold=_model_compress_threshold(cfg, _base_chars))
            _compress_fail_streak = 0
        except Exception as e:  # noqa: BLE001
            # 增量误差自第二轮起收敛到零，避免旧 total 随压缩截断误差累积导致阈值失真。
            # 兜底：压缩是优化项，失败不中断对话，保留原上下文继续推进
            _compress_fail_streak += 1
            log_event(f"[agent] 上下文压缩失败，跳过本轮压缩：{e}\n{traceback.format_exc().rstrip()}")
            # v1.0.555（P0-3 配套）：连续 3 次失败不再静默——上下文无界增长会直接被
            # 服务端截断指令（长任务"只处理一个就停"的根因），必须让用户看见。
            if _compress_fail_streak >= 3:
                # v1.0.652（审计 P2-1）：去掉 .encode("utf-8")——全文件其余 yield 均为 str，
                # 唯独此处 bytes 会破坏 SSE 事件流的 str 契约（team._parse_sse_event 与
                # _memory_watch 对 bytes 判 None，该告警在 auto_loop/记忆链路里静默丢失）。
                yield 'data: ' + json.dumps({"type": "error",
                      "text": f"上下文压缩连续 {_compress_fail_streak} 次失败（{e}），"
                              "长对话可能被模型服务截断指令，建议结束任务重新开始",
                      "code": "compress_failed"}, ensure_ascii=False) + "\n\n"
                _compress_fail_streak = 0   # 报过一次后重置，避免每轮刷屏
        # —— 达到最大轮数自动优化：主预算耗尽后，自动插入收尾引导进入续跑段，
        #    让模型自行总结/收尾，不再干等用户手动点「继续」 ——
        if not unlimited and current_round >= max_rounds and not tail_started:
            tail_started = True
            loop_msgs.append({"role": "user", "content":
                "（系统提示）你已达到主思考轮次上限。请立即基于已有进展收尾："
                "若任务已完成，调用 finish 工具输出最终总结；若仅差一两个必要步骤，执行完立刻调用 finish；"
                "否则调用 finish 汇报当前进度与受阻原因。不要开启新的无关操作，尽快收束。"})
            _msgs_chars += _msg_chars(loop_msgs[-1])
            log_event(f"[agent] 达到主轮次上限({max_rounds})，自动进入收尾续跑段（最多 {TAIL_FINISH_ROUNDS} 轮）")
            yield _sys_notice_sse('\n\n---\n\n[已达到主思考轮次上限，自动进入收尾总结…]')
        # v1.0.470（P1-7）：无限模式 token 软预算——max_rounds=None 时唯一退出条件是
        # finish/无调用/abort，模型每轮换着花样调用工具即可无限烧 token（_usage_sum
        # 原本只用于展示）。累计 prompt_tokens 超阈值（小模型上下文 32K 级 × 50 轮成本）
        # 或任务超时，插入与收尾段相同的"立即收尾"引导；仍不收尾则强制终止存断点。
        # v1.0.562：阈值可配置（config.agent.soft_budget_tokens / soft_budget_minutes，
        # 默认 400000 / 120 分钟）——批量审核类任务单笔工具结果大（页面正文/字段采集），
        # 40 万默认会提前触顶强制收尾（用户实测：审核跑到一半被判完成）。
        if unlimited and not tail_started:
            # v1.0.515 修复（审计 P1-6）：token 预算兜底估算——部分服务端（LM Studio
            # 部分配置/Ollama 原生通道）不回 usage，_usage_sum 恒 0，token 预算永不
            # 触发。无 usage 数据时按累计 prompt 字符数估算（CJK 混合 ~2.5 字符/token，
            # 保守低估——宁可少估不误杀，墙钟仍是最终兜底）。
            _pt = _usage_sum.get("prompt_tokens", 0)
            if not _pt:
                _pt = int(_prompt_chars_total / 2.5)
            _soft_budget_hit, _budget_tokens, _budget_minutes = soft_budget_state(
                cfg, _pt, time.monotonic() - _t_start, _progress_done)
            if _soft_budget_hit and current_round > 0:
                tail_started = True
                loop_msgs.append({"role": "user", "content":
                    "（系统提示）本次任务已消耗大量资源（累计 token/耗时达到预算上限）。请立即收尾："
                    "若任务已完成，调用 finish 输出最终总结；否则调用 finish 汇报当前进度与受阻原因。"
                    "不要开启新的无关操作。"})
                _msgs_chars += _msg_chars(loop_msgs[-1])
                log_event(f"[agent] 无限模式触达软预算（tokens={_pt}，"
                          f"elapsed={time.monotonic()-_t_start:.0f}s），进入强制收尾 pane={pane_id}")
                yield _sys_notice_sse(
                    '\n\n---\n\n[已达到任务资源预算'
                    + (f'（已完成 {_progress_done} 项）' if _progress_done else '')
                    + '，自动进入收尾总结…进度已存断点，点「继续」可接着做]')
        if _stop_requested():
            _aborted = True
            break
        # 每轮开始向聊天栏推送上下文占用与轮次进度（模型不变时前端只更新数字）
        # 分母用主思考预算 max_rounds，收尾段 (round_no>max_rounds) 进度条自然顶满，符合用户认知
        # 无限续跑模式下 rounds 显示真实累计轮数、max_rounds=0 表示不设上限（前端据此隐藏分母）
        # v1.0.344：批量任务附 batch 进度仪表（links 总数/已记录项数），无批量语义时为 null
        _batch_meta = ({"links": len(_last_links or []), "done": _progress_done}
                       if (_last_links or _progress_done) else None)
        yield f"data: {json.dumps({'type':'meta','model':_model_name,'ctx_chars':_msgs_chars,'rounds':round_no if unlimited else min(round_no, max_rounds),'max_rounds':_meta_max,'batch':_batch_meta}, ensure_ascii=False)}\n\n"
        _prompt_chars_total += _msgs_chars   # v1.0.515（P1-6）：本轮 prompt ≈ 当前上下文
        got_done = False
        # v1.0.628：记住本轮最后一次「流式中断」的原始原因——重试耗尽后兜底文案
        # 原本一律写成"未收到完成信号"，把「模型只思考 5 分钟不输出」这种需要用户
        # 换档位/换模型的故障，说成了和网络抖动同一句话（用户只能盲试）。
        _interrupt_reason = ""
        calls = []
        round_text = ""
        round_start = time.time()
        first_token_sent = False
        try:
            for ev in stream_llm_events(model, loop_msgs, tools,
                                        poll_abort=(None if pane_id is None else _stop_requested)):
                if _stop_requested():
                    _aborted = True
                    break
                if ev[0] == "token":
                    if not first_token_sent and current_round > 0:
                        # 非首轮的 LLM 输出前插一条分隔线，避免多轮文本粘连成一坨
                        first_token_sent = True
                        _sep_event = json.dumps({'type': 'token', 'text': '\n\n---\n\n', 'sys': 1}, ensure_ascii=False)
                        yield f"data: {_sep_event}\n\n"
                    round_text += ev[1]
                    yield f"data: {json.dumps({'type':'token','text':ev[1]}, ensure_ascii=False)}\n\n"
                if ev[0] == "reasoning":
                    # 思考流转发前端（显示"推理中…"状态），不进 round_text
                    yield f"data: {json.dumps({'type':'reasoning','text':ev[1]}, ensure_ascii=False)}\n\n"
                elif ev[0] == "error":
                    # 保存断点：LLM 流式中断/请求失败等显式 error 也要允许「继续」恢复。
                    # 修复（2026-08-27）：此前 error 事件路径直接 return 不保存断点，
                    # 「继续」会 400 无断点可恢复；仅 except 异常路径有保存。
                    try:
                        save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)
                    except Exception:  # noqa: BLE001
                        pass
                    _err_text = ev[1] if isinstance(ev[1], str) else str(ev[1])
                    # v1.0.344：结构化错误码（llm_client error 元组第三元素），
                    # 字符串匹配保留作旧格式兜底（测试桩/第三方兼容）。
                    _err_code = ev[2] if len(ev) > 2 else None
                    # v1.0.294：流式中断（空闲/零产出/无内容熔断、断流）= 瞬态故障，
                    # stream_llm_events 外层会自动重发（LLM_STREAM_EMPTY_RETRIES 次）。
                    # 此处若 return，会关闭 stream_llm_events 生成器（for 循环被废弃 →
                    # GeneratorExit 打进 yield 点），其外层自动重试因此【永不执行】——
                    # 现场 2026-09-07 财务任务无内容熔断触发后静默死锁（无"自动重试"日志）
                    # 即此因。故对"流式中断"只推一条瞬态提示并 continue（不 return），
                    # 让本 for 循环继续 next() 触发 stream_llm_events 的重试；重试耗尽后
                    # 生成器自然 StopIteration，落到下方 not got_done 兜底（存断点+提示继续）。
                    # 非"流式中断"的 error（HTTP 500 / 请求失败等，各有连接层重试）才 return 显式报错。
                    if is_stream_interrupt(_err_code, _err_text):   # v1.0.654 A3：判定单源化
                        _interrupt_reason = _err_text   # v1.0.628：留原始故障原因给兜底文案
                        yield _sys_notice_sse('\n\n' + _interrupt_notice(_err_text))
                        continue  # 不 return —— 交给 stream_llm_events 自动重试
                    yield f"data: {json.dumps({'type':'error','text':_err_text,'code':_err_code or 'unknown'}, ensure_ascii=False)}\n\n"
                    return
                elif ev[0] == "done":
                    got_done = True
                    calls = ev[1].get("tool_calls") or []
                    round_text = ev[1].get("text") or round_text
                    _usage = ev[1].get("usage")
                    if _usage:
                        # v1.0.344：token 用量累计（模型/服务端上报时才有值）
                        try:
                            _usage_sum["prompt_tokens"] += int(_usage.get("prompt_tokens") or 0)
                            _usage_sum["completion_tokens"] += int(_usage.get("completion_tokens") or 0)
                        except Exception as e:  # noqa: BLE001
                            # v1.0.648：用量累计失败原为静默——结果是「今日用量」凭空少一段，
                            # 用户对着偏小的数字调预算，而没有任何一行说它少算了。
                            log_event(f"[usage] 用量累计跳过（服务端 usage 字段异常）：{e}")
                        yield f"data: {json.dumps({'type':'usage','usage':_usage}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            # 兜底：stream_llm_events 已拦一层，这里再兜一层，任何未预期异常
            # 都不让 SSE 裸断流——转显式 error 并保存断点，用户可「继续」重试。
            log_event(f"[agent] LLM 轮次异常（第 {round_no} 轮）：{e}\n{traceback.format_exc().rstrip()}")
            # v1.0.646：此前保存失败是静默 pass，而下面这句文案照旧告诉用户
            #「已保存断点，可点击继续」——用户点了才失败，且日志里查不到原因。
            _bp_ok = True
            try:
                save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)
            except Exception as _bpe:  # noqa: BLE001
                _bp_ok = False
                log_event(f"[agent] 断点保存失败（pane={pane_id}）：{_bpe}\n"
                          f"{traceback.format_exc().rstrip()}")
            _tail = ("已保存断点，可点击「继续」重试" if _bp_ok
                     else "断点保存失败（详见 companion.log），本次无法从中断处继续")
            yield f"data: {json.dumps({'type':'error','text':f'对话过程异常：{e}（{_tail}）'}, ensure_ascii=False)}\n\n"
            return

        if not got_done:
            # v1.0.470（P2-15）：用户主动 abort（点停止）也会走到这里——先判 abort，
            # 为真直接跳 aborted 收尾：不发 error 文案（用户主动停止却收到报错属矛盾 UI）、
            # 不落断点（断点恢复会把停止前的半轮当任务续跑）。
            if _aborted:
                break
            # 流异常中断未收到 done：同样保存断点，保证「继续」可恢复
            try:
                save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)
            except Exception:  # noqa: BLE001
                pass
            yield f"data: {json.dumps({'type':'error','text':_interrupt_tip(_interrupt_reason)}, ensure_ascii=False)}\n\n"
            break

        if not calls:
            no_call_rounds += 1
            if no_call_rounds == 1 and current_round > 0 and not tail_started:
                # 完成确认机制：仅对已进入多轮工具循环的轮次追加确认消息再让 LLM 输出一轮，
                # 避免长任务中本地模型丢失指令、误输出无工具调用的"结束语"导致提前收工；
                # 首轮直接回答（简单问答）或收尾段（tail_started）不再追加确认，省一次 LLM 调用（P2-2/P3-1）。
                # v1.0.195：若最近读取的页面有链接入口（批量列表任务），确认消息明确要求继续处理，
                # 防"模型分析完列表页就停"——把"逐个处理列表项"从抽象指令变成运行时强制引导。
                _batch_hint = ""
                if _last_links:
                    _batch_hint = (f"注意：最近读取的页面有 {len(_last_links)} 个链接入口（列表项，如单据/月报/详情），"
                                   "若当前任务是逐个处理这些列表项，请继续从 links 逐一点击进入处理，处理完回到列表再点下一个，"
                                   "全部处理完才算完成，不要提前结束。")
                loop_msgs.append({"role": "assistant", "content": round_text})
                loop_msgs.append({"role": "user",
                                  "content": "（系统确认）你刚才未调用任何工具就直接回复了。若任务尚未完成，请继续调用工具推进；若确实已完成，请明确回复『已完成』。"
                                              + _batch_hint})
                _msgs_chars += _msg_chars(loop_msgs[-2]) + _msg_chars(loop_msgs[-1])
                log_event(f"[agent] 第 {round_no}/{_rounds_display} 轮无工具调用（首次），追加完成确认消息，再跑一轮确认")
                current_round += 1
                continue
            # 首轮直接回答 / 连续两轮无调用 / 收尾段无调用：判定完成
            log_event(f"[agent] 第 {round_no}/{_rounds_display} 轮无工具调用（耗时 {time.time()-round_start:.1f}s），判定任务完成")
            # v1.0.573（用户截图实锤）：模型只输出思考（reasoning）零正文零工具调用
            # 就结束——界面气泡停在「推理中」没有回复，通知却弹「✅完成」。
            # 此处显式向前端交代异常态，通知文案也会据此分态（见下方通知块）。
            if not (round_text or "").strip():
                yield f"data: {json.dumps({'type': 'error', 'text': '⚠️ 模型只输出了思考内容，没有生成回复（常见于思考被截断）。可点「继续」重试，或降低思考强度/更换模型。'}, ensure_ascii=False)}\n\n"
            finished = True
            break

        # 有工具调用，重置连续无工具调用计数
        no_call_rounds = 0

        # 把 assistant 这轮回复（可能含工具调用）加入上下文
        asst = {"role": "assistant", "content": round_text}
        asst["tool_calls"] = [{"id": c["id"], "type": "function",
                               "function": {"name": c["name"],
                                            "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                              for c in calls]
        loop_msgs.append(asst)
        _msgs_chars += _msg_chars(asst)
        asst_idx = len(loop_msgs) - 1  # 死循环熔断时需回退本轮未执行工具，记录 asst 位置

        # 执行工具
        # —— 效率优化：本轮"纯读取"工具（capture/list/read/web_search/fetch 等，见 _PARALLEL_SAFE_TOOLS）
        #    彼此无副作用、无依赖，并行执行把整轮耗时从"逐个等"降为"一次并发"；
        #    写操作（点击/输入/滚动等）有状态依赖，保持串行保证执行顺序。
        #    SSE 事件仍严格按 calls 原始顺序发送，前端感知与串行一致。
        #    v1.0.344 边界修复：只并行化第一个写操作【之前】的读调用——若模型同轮发出
        #    [click, read]（点击后立即回读确认），read 在并行阶段提前执行会拿到点击前的
        #    旧页面状态，"每步操作后 read 确认"的纪律被系统性破坏。写后的读保持串行按序。
        _prefetch = {}
        _first_write_idx = next((i for i, _c in enumerate(calls)
                                 if _c["name"] not in _PARALLEL_SAFE_TOOLS), len(calls))
        _read_calls = [c for c in calls[:_first_write_idx] if c["name"] in _PARALLEL_SAFE_TOOLS]
        if len(_read_calls) > 1 and _stop_requested():
            # v1.0.515（审计 P1-8）：并行预取批次最长可达 4 个读工具并发，先查 abort 再启动
            _aborted = True
        if len(_read_calls) > 1 and not _aborted:

            def _exec_one(c):
                try:
                    # v1.0.555（P1-7）：worker 线程也设置 pane 上下文——线程级隔离，
                    # 并行预取的读工具若触发长阻塞调用（ask_judge 等）同样可被停止。
                    set_current_pane(pane_id)
                    return c["id"], dispatch_tool(c["name"], c["args"], cfg)
                except Exception as e:  # noqa: BLE001
                    # v1.0.614：补日志。原先只把异常拼成一句 __error__ 回传给模型，
                    # 栈整个丢掉——这正是 v1.0.599「流水线异常」那类「界面一句笼统失败」的
                    # 同型缺口（日志里 0 痕迹，只能靠猜）。
                    log_event("[agent] 工具执行异常：%s\n%s" % (c["name"], traceback.format_exc().rstrip()))
                    return c["id"], err(f"工具执行异常：{c['name']} - {e}")   # v1.0.656 统一

            _tp0 = time.time()
            with ThreadPoolExecutor(max_workers=min(len(_read_calls), 4)) as _ex:
                for _cid, _out in _ex.map(_exec_one, _read_calls):
                    _prefetch[_cid] = _out
            log_event(f"[agent] 并行执行 {len(_read_calls)} 个读工具（{time.time()-_tp0:.1f}s）")

        for _ci, c in enumerate(calls):
            # v1.0.555（P1-7）：串行主循环设置 pane 上下文——ask_judge 等长阻塞工具
            # 据此把 abort 钩子透传给 _llm_complete（原 judge 调用无 deadline 无
            # poll_abort，单次可阻塞 ~15 分钟且点停止无效）。
            set_current_pane(pane_id)
            # v1.0.515 修复（审计 P1-8）：工具批次内插入 abort 检查——原实现一轮
            # [click, read, pc_run…] 串行执行期间完全不响应停止，点停止/看门狗 abort
            # 要等整批跑完+回到轮首才生效；单长工具（pc_run 300s/dsh_run 1800s）
            # 执行期间仍不可停（属 OS 级阻塞），但批次内每个工具之间即停。
            if _stop_requested():
                _aborted = True
                break
            t0 = time.time()
            _tool_n += 1
            try:
                _tool_names.append(str(c.get("name") or ""))   # v1.0.582：工具名序列（相似任务指纹用）
            except Exception:  # noqa: BLE001
                pass  # silent-ok: 工具名记录失败不影响工具执行（仅指纹/统计用）
            # v1.0.652（审计 P2-9）：SSE 推前端的 args 过 _mask_args——本事件会被
            # pushHistory 落进 产物库/对话记录/*.md 明文常驻，type_wecom_text（密码）/
            # pc_run（token/连接串）/save_file（文件全文）都是口子；工具实际执行
            # 与模型上下文仍用原始 args，只脱敏"给人看/落盘"的这一路。
            yield f"data: {json.dumps({'type':'tool','name':c['name'],'args':_mask_args(c['args'])}, ensure_ascii=False)}\n\n"
            log_event(f"[agent] 调用工具 {c['name']}：{_mask_args(c['args'])}")
            # v1.0.313 Ask 模式（readonly）：写类工具一律拦截——只允许读类/收尾/问询
            # v1.0.470（P1-4）：判定改用 is_read_tool 单一口径（原前缀判定把 web_search/
            # fetch_webpage 等只读工具误判为写类）；_intercepted 标记防止下方 prefetch
            # 分支把拦截结果无条件覆盖（原 bug：拦截形同虚设）。
            # v1.0.485：ask_user 放行——只问答模式下 agent 追问澄清（受众/风格/篇幅）
            # 是合法交互，拦截它只会逼模型瞎猜；但 create_*/save_*/pc_run 等写类照拦。
            _intercepted = False
            _is_read = is_read_tool(c["name"])
            _ask_block = readonly and not (_is_read or c["name"] in ("finish", "ask_user"))
            # v1.0.590 权限级别「每次询问」：写类工具统一在这一点挂起（读类不询问）。
            # v1.0.592 修复（P2）：readonly 已经要拦下的调用【不再】先算闸门——
            # gate_write_tool 内部会 consume(一次性批准令牌) 并 audit(..., False) 落盘，
            # 先算后丢等于凭空吃掉一次批准、并写进一条拦截原因与真实原因不符的审计记录
            #（安全审计是合规口径，必须与真实拦截原因一致）。
            # v1.0.614：闸门不再只在 ask_every 档计算——`_ALWAYS_ASK` 里的"参数级不可撤销写"
            # （如 run_audit_flow(auto_approve=true) 真提交审批单）在 auto 档也要问。
            # 闸门对不命中的调用仍恒返回 None，所以其余行为与改动前逐字一致。
            _bg_pane = is_background_pane(pane_id)
            _gate = None if _ask_block else gate_write_tool(
                c["name"], c["args"], cfg, permission_level,
                is_read=_is_read, background=_bg_pane)
            if _ask_block:
                out = err("当前为 Ask（只问答）模式：不允许执行写类操作（页面点击/输入/文件写入/命令执行等）。"
                          "如需动手执行，请切换到 Agent 模式。")   # v1.0.656 统一
                _intercepted = True
                log_event(f"[agent] Ask 模式拦截写工具 {c['name']}")
            elif _gate and is_err(_gate):   # v1.0.656 单谓词
                # 后台会话（定时/刷课）没有弹窗可点：闸门直接给拒绝理由，不挂起。
                out = _gate
                _intercepted = True
                log_event(f"[agent] 后台会话拒绝不可撤销写工具 {c['name']}")
                # v1.0.616（审查 P1）：拒绝必须**发事件**。此前只回灌模型、流上什么都没有，
                # 调度侧 _blocked 只统计 ask_user/approval_request → 整晚一份没提交，
                # 通知与产出却都写着「✅ 定时任务完成」。
                yield f"data: {json.dumps({'type': 'approval_denied', 'tool': c['name'], 'reason': str(err_msg(_gate))[:200]}, ensure_ascii=False)}\n\n"
            elif _gate:
                out = _gate
                _intercepted = True
                log_event(f"[agent] 权限闸门挂起写工具 {c['name']}：{_gate.get('command', '')[:60]}")
            elif c["name"] == "request_skill":
                # v1.0.317 按需安装：向用户弹窗请求安装技能。挂起-恢复（复用断点）：
                # 存断点 → skill_request 事件 → need_continue 结束本轮；前端用户选择后
                # /api/chat/continue 带 resume_note 恢复。pane_id 缺失（后台任务）无法询问，降级提示继续。
                _sid = str((c.get("args") or {}).get("skill_id") or "").strip()
                _reason = str((c.get("args") or {}).get("reason") or "").strip()
                log_event(f"[agent] request_skill skill_id={_sid} reason={_reason[:60]}")
                if not pane_id:
                    out = err("当前为后台任务，无法向用户弹窗询问。请不依赖该技能，改用基础工具完成本任务。")   # v1.0.656 统一
                elif not _sid:
                    out = err("缺少 skill_id。请从「可按需安装的技能」清单中选择正确的 id 重试。")   # v1.0.656 统一
                else:
                    save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                    yield f"data: {json.dumps({'type': 'skill_request', 'skill_id': _sid, 'reason': _reason}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'need_continue', 'text': '正在等待你确认是否安装技能——请用弹窗选择「安装并继续」或「跳过」。'}, ensure_ascii=False)}\n\n"
                    return
            elif c["name"] == "import_skill_source":
                # v1.0.318 技能源导入：用户丢来清单 URL → 弹窗确认 → 同意后设为远程源并批量安装。
                # 拉取/校验在弹窗确认时由 /skills/hub/import_source 执行（loop 不联网）。
                _url = str((c.get("args") or {}).get("url") or "").strip()
                log_event(f"[agent] import_skill_source url={_url[:100]}")
                if not pane_id:
                    out = err("当前为后台任务，无法向用户弹窗确认导入。请告知用户手动到「技能中心 → 远程源设置」配置。")   # v1.0.656 统一
                elif not _url.startswith(("http://", "https://")):
                    out = err("url 必须以 http(s):// 开头，请向用户确认正确的清单地址后重试。")   # v1.0.656 统一
                else:
                    save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                    yield f"data: {json.dumps({'type': 'skill_source_request', 'url': _url}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'need_continue', 'text': '正在等待你确认是否导入该技能源——请用弹窗选择「确认导入」或「跳过」。'}, ensure_ascii=False)}\n\n"
                    return
            elif c["name"] == "ask_judge":
                # 界面即时反馈：判断大模型推理耗时较长（10~90s），不提示会被误认为卡死
                yield _sys_notice_sse('\n[⏳ 判断大模型思考中…（think 已关闭，通常 5~20s）]')
            if _intercepted:
                # v1.0.590 修复（P0）：被 Ask（只问答）或权限闸门拦下的调用，绝不能再落到
                # 下面的 prefetch / finish / dispatch 分支。原实现前两个分支各写了
                # `not _intercepted and ...`，但收尾的 `else: out = dispatch_tool(...)`
                # 不受该标记约束 → 拦截结果被真实执行结果整体覆盖：只问答模式下
                # click/type/send_message/pc_run 等写类工具**实际照常执行**，用户和模型
                # 都看不到（那句 Ask 错误文案被丢弃）。v1.0.470（P1-4）的「_intercepted
                # 防覆盖」只堵了前两个分支，第三个漏了；而回归测试是 grep 源码里有没有
                # "当前为 Ask（只问答）模式"这句字符串、不验行为，所以这个洞活了很久。
                pass
            elif c["id"] in _prefetch:
                # 读类工具：已在并行阶段执行，直接取结果
                out = _prefetch[c["id"]]
            elif c["name"] == "finish":
                # —— finish 预提交校验（v1.0.190）：防"编造已完成直接交卷"的幻觉 ——
                # 硬性检查最近一次页面读取的进度；不通过则拒绝执行 finish，把错误回灌给
                # 模型让它继续补齐（模型下一轮看到失败原因后 re-read 页面再处理）。
                _rej = _finish_guard_reason(_finish_guard_fp, _finish_guard_exam)
                if _rej:
                    _finish_reject_streak += 1
                    log_event(f"[agent] finish 被预提交校验拦截（第 {_finish_reject_streak} 次）：{_rej}")
                    # 双模型分工·难度自动升级：交卷判断被拒（小模型进度/判断有误）→ 拉大模型给建议
                    _esc_ok, _esc_detail = _judge_escalate("finish_reject", loop_msgs, judge, judge_escalated, pane_id)
                    if _esc_ok:
                        yield _sys_notice_sse('\n\n---\n\n[大模型判断介入中…]')
                    elif _esc_detail != "judge 未配置":
                        # v1.0.555（P1-8）：judge 升级失败可见化——终止文案同时附失败计数，
                        # 排障不再被引向"小模型不达标"（真实原因：升级层瘫了）。
                        _judge_fail_n += 1
                        yield _sys_notice_sse(
                            f'\n\n---\n\n[大模型暂不可用（{_esc_detail}），已回退小模型自行判断]')
                    if _finish_reject_streak >= 3:
                        # 连续 3 次被拒仍不补齐 → 硬性终止，不再放行（避免无限纠缠）
                        save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                        _tail = f"（期间 judge 升级失败 {_judge_fail_n} 次，双模型兜底实际未生效）" if _judge_fail_n else ""
                        yield f"data: {json.dumps({'type':'error','text':'检测到任务未完成但 agent 连续 3 次尝试交卷均被拦截（' + _rej + '）。已自动终止并保存断点，请检查页面后点击「继续」或重新发起。' + _tail}, ensure_ascii=False)}\n\n"
                        return
                    out = err("finish 被预提交校验拦截：" + _rej + "。请继续执行未完成的步骤，全部完成后再次调用 finish。")   # v1.0.656 统一
                else:
                    # v1.0.344 二段校验：final_answer 非空 + 声称文件存在性。
                    # 防两类假完成：①无总结直接 finish；②声称"已保存到产物库/xxx"但文件
                    # 从未真正落盘（幻觉产物）。拒绝信息回灌给模型自行修正，连续 3 次仍不过则终止存断点。
                    # 注意：不设最小长度阈值——微型任务（如"帮我点提交"）的合法短总结会被误伤；
                    # 只拦确定性缺陷。streak 只在两道校验都通过时清零（提前清零会让连续拒绝永远到不了 3）。
                    _fa = str((c.get("args") or {}).get("final_answer") or "").strip()
                    _qrej = None
                    if not _fa:
                        _qrej = "finish 缺少 final_answer 参数：请输出完整的最终总结（做了什么/结果数据/注意事项）后重新调用 finish。"
                    else:
                        _miss = _missing_claimed_files(_fa)
                        if _miss:
                            _qrej = ("final_answer 声称已保存以下文件，但文件系统中不存在（可能未真正落盘或路径写错）："
                                     + "、".join(_miss)
                                     + "。请先用 save_file/download_file 真正生成文件，或修正总结中的路径后再 finish。")
                    if _qrej:
                        _finish_reject_streak += 1
                        log_event(f"[agent] finish 被质量校验拦截（第 {_finish_reject_streak} 次）：{_qrej[:100]}")
                        if _finish_reject_streak >= 3:
                            save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                            yield f"data: {json.dumps({'type':'error','text':'finish 连续 3 次未通过质量校验（' + _qrej + '）。已终止并保存断点，可点击「继续」或重新发起。'}, ensure_ascii=False)}\n\n"
                            return
                        out = err("finish 被质量校验拦截：" + _qrej)   # v1.0.656 统一
                    else:
                        _finish_reject_streak = 0
                        out = dispatch_tool(c["name"], c["args"], cfg)
            else:
                out = dispatch_tool(c["name"], c["args"], cfg)
            # 防御：工具返回值必须为 dict，否则统一包装（防 list/None 等导致 .get() 崩溃断流）
            if not isinstance(out, dict):
                out = {"ok": True, "result": out}
            # v1.0.345 read 去重：系统鼓励"每步操作后 read 确认"，同一页面内容反复读取
            # 以前是全价入上下文（单条最多 20K 字符）。现在对正文做内容 hash——与上一次
            # 读取完全一致时返回瘦身版（前 200 字 + 未变化说明），form_progress/links/
            # answered 等结构化字段原样保留（finish 校验不受影响）。跨页/内容变化不命中。
            if (c["name"] in ("read_wecom_page", "capture_wecom_window")
                    and not is_err(out) and isinstance(out.get("body"), str) and out["body"]):   # v1.0.656
                _bh = hashlib.md5(out["body"].encode("utf-8", "replace")).hexdigest()
                if _bh == _last_read_hash:
                    _saved = len(out["body"])
                    out = dict(out)
                    out["body"] = (out["body"][:200]
                                   + f"…【系统注】页面内容与上一次读取完全一致（{_saved} 字符已省略）——"
                                     "这就是当前最新状态，无需再次确认；如需看到新内容请先滚动/切换页面/执行操作。")
                    out["unchanged"] = True
                    log_event(f"[agent] read 去重命中：{c['name']} 内容未变，省 {_saved} 字符")
                else:
                    _last_read_hash = _bh
            if is_err(out):   # v1.0.655 B5 单谓词
                _tool_err_n += 1
            log_event(f"[agent] 工具 {c['name']} 返回（{time.time()-t0:.1f}s）：{'error' if is_err(out) else 'ok'}   # v1.0.655 B5")
            # 缓存最近一次页面读取的进度（供 finish 预提交校验使用）
            if isinstance(out, dict) and out.get("ok") is not False and not out.get("__error__"):
                _fp = out.get("form_progress")
                if isinstance(_fp, dict) and isinstance(_fp.get("required"), int):
                    _finish_guard_fp = _fp
                if out.get("answered") is not None or out.get("unanswered") is not None:
                    _finish_guard_exam = {"answered": out.get("answered"), "unanswered": out.get("unanswered")}
                _lk = out.get("links")
                if isinstance(_lk, list) and _lk:
                    _last_links = _lk
            # 把执行结果摘要回传前端，让用户看到每步是成功还是失败
            if out.get("__approval_needed__"):
                # v1.0.320 安全审批挂起：存断点 → 弹窗（批准/拒绝）→ continue(resume_note) 恢复；
                # 批准时前端先 POST /api/security/grant 发放一次性令牌，agent 原样重试即放行。
                _ap = out
                save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                # v1.0.652（审计 P0-1）：nonce 只进 SSE（前端弹窗回传给 /api/security/grant），
                # 不进任何模型可见内容——hash 可被模型自算，nonce 才是"用户点了批准"的凭证。
                _ap_nonce = begin_approval(_ap.get('hash', ''))
                yield f"data: {json.dumps({'type': 'approval_request', 'kind': _ap.get('kind'), 'tool': _ap.get('tool', ''), 'hash': _ap.get('hash', ''), 'nonce': _ap_nonce, 'reason': _ap.get('reason', ''), 'command': _ap.get('command', ''), 'path': _ap.get('path', '')}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'need_continue', 'text': '安全中心需要你确认本次操作——请在弹窗中选择「批准执行」或「拒绝」。'}, ensure_ascii=False)}\n\n"
                return
            if out.get("__ask_user__"):
                # v1.0.373 用户抉择弹窗（用户需求 1）：任务中需要用户选择风格/主题/方案等时，
                # agent 调 ask_user 弹窗询问而不是在回复里提问等用户打字。存断点 → 推
                # ask_user 事件 → 前端弹选择卡（点选项/自由输入）→ /chat/continue 以
                # 【用户决定】resume_note 恢复（不需要令牌，也不重试本工具——回答即结果）。
                save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                yield f"data: {json.dumps({'type': 'ask_user', 'question': out.get('question', ''), 'options': out.get('options', []), 'allow_text': out.get('allow_text', True)}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'need_continue', 'text': '任务需要你做出选择——请在弹窗中点选或填写。'}, ensure_ascii=False)}\n\n"
                return
            # v1.0.361 审查修复（M1）：全量序列化只做一次——原实现摘要与上下文消息各
            # dumps 一遍（read_page 返回体 50K+ 时第二遍纯浪费），各自切片复用。
            out = to_model(out)   # v1.0.656 边界：内部 {ok,error} → 模型可见 __error__（形状零变化）
            _full = json.dumps(out, ensure_ascii=False)
            if is_err(out):   # v1.0.655 B5 单谓词
                summary = "失败：" + (err_msg(out) or "未知错误")
            elif out.get("__finish__"):
                summary = "任务完成"
            else:
                summary = _full[:420]
            _ev = {'type': 'tool_result', 'name': c['name'], 'summary': summary[:400]}
            # v1.0.629 修复（用户实报"生成的文件还是点不动"）：把产物路径原样带给前端，
            # 步骤卡下方那条「📄 文件名 · 📂 所在文件夹」才有数据可渲染——v1.0.627 的
            # 前端钩子读的是 o.path，而本事件从来只发 name+summary，钩子恒拿 undefined。
            # 只在 ok=True 时发：__approval_needed__/失败结果里的 path 是"待操作对象"，
            # 不是已产出的文件，挂上去等于给用户一个点开不存在东西的按钮。
            _p = out.get("path")
            if out.get("ok") is True and isinstance(_p, str) and _p.strip():
                _ev['path'] = _p.strip()
                # v1.0.634：再单独标一份"这是本次**产出**的文件"——只读工具（read/
                # pc_files(op=read)/dsh_status）的返回体同样带 path，那是"被读的文件"，
                # 不是产物。判定口径集中在 agent_tools.registry.is_file_producer。
                if is_file_producer(c["name"], c["args"], out):
                    _ev['produced'] = _p.strip()
                    _produced_files.append(_p.strip())
            yield f"data: {json.dumps(_ev, ensure_ascii=False)}\n\n"
            if out.get("__finish__"):
                finished = True
                final_answer = out.get("final_answer", "")
                # v1.0.470（P1-5）：finish 后立即终止本轮剩余工具——原实现继续跑完
                # [finish, click_wecom_element] 这类同轮调用序列，任务已判定完成仍产生
                # 页面副作用，结果写进上下文却永不再被消费。未执行的调用补占位 tool
                # 消息维持 assistant.tool_calls 配对完整（严格后端 400 防护，同 S4）。
                _fill_pending_tool_results(loop_msgs, calls)
                log_event(f"[agent] finish 后终止本轮剩余 {max(0, len(calls) - _ci - 1)} 个未执行工具调用 pane={pane_id}")
                break
            loop_msgs.append({"role": "tool", "tool_call_id": c["id"],
                              "content": _full[:_tool_result_cap(c["name"])]})
            _msgs_chars += _msg_chars(loop_msgs[-1])

            # —— 上下文精简（v1.0.198）：append_progress 成功 = 当前项已记录完毕，
            # 砍掉该项的全部工具往返历史（上一个单据的 fields/read 详情对下一项无用），
            # 只留指令头 + 最近状态 + 累计进度。下一个单据从干净上下文开始，
            # 解决单上下文累积到小模型窗口上限后指令丢失（"只处理一个就停"）。
            # v1.0.203：审完必关——close_wecom_page 关详情页 → 回列表 → 点下一项。
            if c["name"] == "append_progress" and isinstance(out, dict) and out.get("ok"):
                _progress_done += 1
                # v1.0.373（体验 A1）：批量任务进度实时推给前端——「已完成 N 项」
                try:
                    yield f"data: {json.dumps({'type': 'task_progress', 'done': _progress_done}, ensure_ascii=False)}\n\n"
                except Exception:  # noqa: BLE001
                    pass
                try:
                    _before_n = len(loop_msgs)
                    loop_msgs = _reset_after_item(loop_msgs)
                    _cur_pick = wb._get_auto_pick()
                    _cur_txt = (f"当前跟随窗口 index={_cur_pick}（就是你刚审核完的详情页）。"
                                if _cur_pick is not None
                                else "当前跟随窗口未指定。")
                    loop_msgs.append({"role": "user",
                                      "content": f"（系统）本项已记录。【审核完必须关页再继续，按序执行】"
                                      f"1) close_wecom_page 关闭当前详情页（刚审核完的那份）；"
                                      f"2) list_wecom_windows 查看剩余窗口（列表页应还在，标题含'待办'）；"
                                      f"3) read_wecom_page pick=<列表页序号> 取新 links（已审核的项会消失）；"
                                      f"4) 从新 links 点下一项（按列表顺序）；"
                                      f"5) 审核 → append_progress → 回到步骤 1。\n"
                                      f"⚠️ 不关详情页 = 标签堆积 + 反复审核同一项（已实测发生）。"
                                      f"{_cur_txt}"})
                    # 精简后 loop_msgs 已很小（lead+tail），全量求和为 O(小常数)，无需增量
                    _msgs_chars = sum(_msg_chars(m) for m in loop_msgs)
                    log_event(f"[agent] append_progress 成功，上下文已精简 {_before_n}→{len(loop_msgs)} 条")
                except Exception as _e:  # noqa: BLE001
                    log_event(f"[agent] 上下文精简失败：{_e}")

            # —— 死循环熔断（v1.0.345 判定逻辑抽出为 LoopGuard，复位/阈值语义与原内联实现
            #     逐位一致；干预消息构造与上下文回退手术仍在此执行）——
            _is_read_tool = is_read_tool(c["name"])
            if not _is_read_tool:
                _is_err = is_err(out)   # v1.0.655 B5 单谓词
                _act, _streak = _guard.on_write(c["name"], c["args"], _is_err)
                if _act == "kill":
                    if _is_err:
                        log_event(f"[agent] 干预后仍连续执行同一写操作持续报错（{c['name']} {_mask_args(c['args'])}，连续 {_streak} 次 error），判定死循环，终止 pane={pane_id}")
                        save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                        yield f"data: {json.dumps({'type':'error','text':'检测到死循环：同一操作连续执行多次均报错仍盲目重试，已自动终止。可点击「继续」从断点重新开始，或重新发起对话。'}, ensure_ascii=False)}\n\n"
                        return
                    log_event(f"[agent] 干预后仍连续重复执行写操作（连续 {_streak} 次），判定死循环，终止 pane={pane_id}")
                    save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                    yield f"data: {json.dumps({'type':'error','text':'检测到死循环：模型连续反复执行相同操作，已自动终止。可点击「继续」从断点重新开始，或重新发起对话。'}, ensure_ascii=False)}\n\n"
                    return
                if _act == "warn":
                    log_event(f"[agent] 检测到{'同一写操作持续报错' if _is_err else '连续重复写操作'}（{c['name']} {_mask_args(c['args'])}，连续 {_streak} 次），插入干预消息")
                    for _t in _apply_warn("write_err_loop" if _is_err else "write_loop",
                                          (_WARN_WRITE_ERR if _is_err else _WARN_WRITE).format(streak=_streak),
                                          '\n\n---\n\n[检测到同一操作持续报错，已暂停并引导模型换方式推进]' if _is_err else '\n\n---\n\n[检测到连续重复操作，已暂停并引导模型重新决策]',
                                          _ci):
                        yield _sys_notice_sse(_t)
                    break
            else:
                if c["name"] == "finish":
                    # 收尾工具不参与任何循环检测，复位全部计数
                    _guard.on_finish()
                    continue
                _is_err = is_err(out)   # v1.0.655 B5 单谓词
                _act, _streak = _guard.on_read(c["name"], c["args"], _is_err)
                if _act == "kill":
                    log_event(f"[agent] 干预后仍连续重复读取同一页面/元素（连续 {_streak} 次），判定读死循环，终止 pane={pane_id}")
                    save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                    yield f"data: {json.dumps({'type':'error','text':'检测到死循环：模型连续反复读取同一页面/元素，已自动终止。可点击「继续」从断点重新开始，或重新发起对话。'}, ensure_ascii=False)}\n\n"
                    return
                if _act == "warn":
                    log_event(f"[agent] 检测到连续重复读操作（{c['name']} {_mask_args(c['args'])}，连续 {_streak} 次），插入干预消息")
                    for _t in _apply_warn("read_loop", _WARN_READ.format(streak=_streak),
                                          '\n\n---\n\n[检测到连续重复读取，已暂停并引导模型换方式推进]',
                                          _ci):
                        yield _sys_notice_sse(_t)
                    break

        if finished:
            break
        # v1.0.470（P1-7）：无限模式软预算收尾段超时强制终止——tail_started 后给
        # TAIL_FINISH_ROUNDS 轮机会，模型仍不 finish 则存断点等用户「继续」，
        # 防止软预算收尾本身被模型无视而继续无限循环。
        if unlimited and tail_started:
            _tail_rounds_used += 1
            if _tail_rounds_used > TAIL_FINISH_ROUNDS:
                save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
                log_event(f"[agent] 无限模式强制收尾超时（{TAIL_FINISH_ROUNDS} 轮未 finish），存断点 pane={pane_id}")
                yield f"data: {json.dumps({'type':'need_continue','text':'任务资源预算已达上限且未能收尾，已保存断点——可点击「继续」从断点接着执行。'}, ensure_ascii=False)}\n\n"
                break
        current_round += 1

    if _aborted:
        # 结束前清理自身 abort 标志（含全局残留），避免影响下一次对话被误判为已停止
        _set_abort(pane_id, False)
        # v1.0.555（P1-11）：不再清全局键——"停止所有"后先收尾的任务曾把
        # _abort_flags[None] 清掉，仍在跑的后台任务只依赖全局键判定而逃逸。
        # 全停现由代次（globally_aborted_since）判定，收尾无权清除。
        yield f"data: {json.dumps({'type':'aborted'}, ensure_ascii=False)}\n\n"
        try:
            save_chat_log(loop_msgs, _first_user_title(loop_msgs))
        except Exception as e:  # noqa: BLE001
            log_event(f"[agent] 保存聊天记录失败：{e}")
        return

    if not finished and current_round >= total_rounds:
        # 主预算 + 自动收尾续跑预算均已耗尽才存断点并提示「继续」；
        # LLM 报错等提前 break 的情况已单独 yield error，不再误发 need_continue。
        save_state_safe(pane_id, loop_msgs, current_round, readonly, model_id, calls)   # v1.0.654 B2
        log_event(f"[agent] 达到总轮次上限({total_rounds}，含收尾段)，等待用户点击「继续」 pane={pane_id}")
        yield f"data: {json.dumps({'type':'need_continue','text':'已达到最大思考轮次（' + str(total_rounds) + '轮，含自动收尾），任务仍未收束，可点击「继续」从断点接着执行。'}, ensure_ascii=False)}\n\n"
    elif final_answer:
        # 把 finish 工具里的总结输出给用户
        # v1.0.634（用户要求）：回答末尾追加「本次生成的文件」清单——任务没产出文件时
        # `_produced_manifest` 返回空串，这一块整体不出现（不留一个空标题）。
        # v1.0.635：先并入磁盘差集——agent 用 pc_run 跑脚本产出的文件没有任何工具结果
        # 带它的路径，只靠工具上报就会"列了三个 .py、正主 .pptx 反而没有"。
        _all_files = list(_produced_files)
        if _diff_outputs:
            try:
                for _p in _diff_outputs(_out_snapshot):
                    if _p not in _all_files:
                        _all_files.append(_p)
            except Exception as e:  # noqa: BLE001
                log_event(f"[agent] 产物差集比对失败（清单只列工具上报的产出）：{type(e).__name__}: {e}")
        # v1.0.641：第三个来源——正文里点名、且磁盘上真有的绝对路径。
        # 覆盖"脚本把 PDF 写到桌面"这类既无工具上报、又在产物库之外的产出。
        try:
            for _p in _answer_file_paths(final_answer):
                if _p not in _all_files:
                    _all_files.append(_p)
        except Exception as e:  # noqa: BLE001
            log_event(f"[agent] 正文产出路径提取失败（忽略，清单仍列前两路）：{type(e).__name__}: {e}")
        _mf = _produced_manifest(_all_files, final_answer)
        if _mf:
            log_event(f"[agent] 本次任务产出 {len(_all_files)} 个文件（工具上报 "
                      f"{len(_produced_files)} + 磁盘差集补 "
                      f"{len(_all_files) - len(_produced_files)}），已在回答末尾列清单")
        _done_event = json.dumps({'type': 'token', 'text': '\n\n---\n\n【完成】' + final_answer + _mf}, ensure_ascii=False)
        yield f"data: {_done_event}\n\n"

    # 自动把本次对话保存到产物库/对话记录
    try:
        if finished:
            save_chat_log(loop_msgs, _first_user_title(loop_msgs))
    except Exception as e:  # noqa: BLE001
        log_event(f"[agent] 保存聊天记录失败：{e}")
    # v1.0.344 任务级成本/轨迹汇总（P0 度量衡）：随 done 事件下发 + 落日志。
    _task_summary = {
        "rounds": current_round,
        "tool_calls": _tool_n,
        "tool_errors": _tool_err_n,
        "batch_done": _progress_done,
        "elapsed_s": round(time.monotonic() - _t_start, 1),   # v1.0.573：_t_start 是 monotonic，原 time.time() 相减=epoch 值（界面显示 17.9 亿秒）
        "prompt_tokens": _usage_sum["prompt_tokens"],
        "completion_tokens": _usage_sum["completion_tokens"],
    }
    log_event(f"[agent] 任务汇总 pane={pane_id}：{json.dumps(_task_summary, ensure_ascii=False)}")
    # v1.0.582（用户需求）：相似任务检测——同指纹（文本归一化+工具序列）执行满
    # 3 次且未询问过 → 向前端发 skill_suggest 事件（界面内建议卡片，一键固化为
    # 自定义技能）。纯对话（无工具）不记录，详见 services/task_fingerprint.py。
    if _tool_names and _tool_err_n == 0 and finished and not _stop_requested():
        try:
            from agent_tools import _extract_task_text
            from services.task_fingerprint import record_and_check
            _sug = record_and_check(_extract_task_text(loop_msgs), _tool_names)
            if _sug:
                yield f"data: {json.dumps({'type': 'skill_suggest', **_sug}, ensure_ascii=False)}\n\n"
                log_event(f"[agent] 相似任务检测命中（{_sug['count']} 次），已建议固化为技能：{_sug['title']}")
        except Exception as e:  # noqa: BLE001
            log_event(f"[warn] 相似任务检测异常（忽略）：{e}")
    # v1.0.570（用户需求）：任务结束 → Windows 原生 Toast（横幅来源「企业微信伴侣」）。
    # 完成/用户停止/异常中断三态都通知；摘要取最后一轮模型回复。调用方自带
    # 完成通知的（scheduler 定时任务按任务名通知）传 notify=False 防双响。
    if notify:
        try:
            from core.notify import native_toast as _toast
            # v1.0.582（用户实测"你好"误报）：完成判定 break 前**不 append** loop_msgs
            # ——首轮直接回答（纯对话）场景 loop_msgs 无 assistant → _any_reply 恒 False
            # → 有回复却弹「未产出」。_tail/_any_reply 必须把**本轮 round_text** 纳入。
            _tail = (round_text or "").strip()
            if not _tail:
                for _m in reversed(loop_msgs):
                    if isinstance(_m, dict) and _m.get("role") == "assistant" and _m.get("content"):
                        _tail = str(_m["content"]).strip()
                        break
            _any_reply = bool(_tail) or any(
                isinstance(_m, dict) and _m.get("role") == "assistant"
                and str(_m.get("content") or "").strip()
                for _m in loop_msgs)
            if finished and not _stop_requested():
                if _any_reply:
                    # v1.0.585（用户澄清）：**所有任务完成都要通知**——包括纯对话
                    # （发"你好"也是一次任务，横幅摘要显示模型的回复内容）
                    _toast("✅ 任务完成", _tail[:120] or f"耗时 {round(time.monotonic() - _t_start)}s")
                else:
                    # 零正文零工具的"完成"按事实报异常态（原 fallback
                    # 「耗时 Ns，工具调用 0 次」会被误读为正常完成——用户截图实锤）
                    _toast("⚠️ 任务未产出回复",
                           "模型只输出了思考内容——可点「继续」重试或更换模型")
            elif _stop_requested():
                _toast("⏹ 任务已停止", "按你的操作中断，已完成部分保留")
            else:
                _toast("⚠️ 任务异常中断", _tail[:120] or "LLM 流中断，已保存断点可点「继续」")
        except Exception:  # noqa: BLE001
            pass
    # v1.0.373（体验C9 任务宏）：任务成功完成时，把全部工具调用序列推给前端——
    # 用户可「存为宏」，同构页面（下周同款审核单）一键重放，不必再过一遍 LLM。
    if finished and not _stop_requested():
        # v1.0.592：采集逻辑抽成 _collect_task_calls 并去掉外层的 `except Exception: pass`
        # ——那句把"宏序列一个都没采到"吞得干干净净，正是键名写错能长期不被发现的原因。
        _seq = _collect_task_calls(loop_msgs)
        if _seq:
            yield f"data: {json.dumps({'type': 'task_calls', 'calls': _seq[:200]}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type':'done','summary':_task_summary}, ensure_ascii=False)}\n\n"


def _collect_task_calls(loop_msgs):
    """v1.0.373（体验C9 任务宏）：从 loop 消息里摘出可重放的写类工具序列。

    v1.0.592 修复（P1）：读参数时用的是 `_fn.get("args")`，而上面写入 loop_msgs 时的键是
    OpenAI 线上格式的 `"arguments"`（见本文件 asst["tool_calls"] 构造处）→ 恒取不到 →
    每条宏步骤都记成空参数，重放时全步因缺 target 校验失败；外层又包着 except: pass，
    所以"存为宏"从上线起就是静默废的。现在优先读 arguments，保留 args 作为旧断点兜底。
    ask_user/finish 不入宏（前者是与人对话、后者是收尾，重放无意义）。
    """
    seq = []
    for m in loop_msgs or []:
        if not isinstance(m, dict):
            continue
        for c in m.get("tool_calls") or []:
            if not isinstance(c, dict):
                continue
            fn = c.get("function") or {}
            nm = fn.get("name") or ""
            if not nm or nm in ("ask_user", "finish"):
                continue
            raw = fn.get("arguments")
            if raw is None:
                raw = fn.get("args")
            try:
                a = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:  # noqa: BLE001
                a = {}
            if not isinstance(a, dict):
                a = {}
            seq.append({"name": nm, "args": a})
    return seq


# ---------- auto_loop 考试无人值守续跑（v1.0.248 引入；v1.0.345 拆至 agent/auto_loop.py） ----------
# 此处保留 re-export：外部消费方（routes/chat.py / study_engine.py / agent/__init__.py）
# 仍从 agent.loop / agent 包导入，符号与行为不变。auto_loop 对 agent_loop 为懒导入，无循环依赖。
from .auto_loop import (  # noqa: E402,F401  （re-export，保持导入路径兼容）
    AUTO_LOOP_MAX_RESTARTS, AUTO_LOOP_STATES, _auto_loop_chain,
)

# ---------- 技能 / 连接器管理 ----------

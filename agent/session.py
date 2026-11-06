"""agent.session — 会话状态：abort 标志 / 轮次与熔断常量 / 压缩阈值 / 断点存储。
逐字自 backend.py 迁出（T04）。
v1.0.405（M5）：断点持久化到 产物库/断点/<pane_id>.json——长任务跑几小时后重启
软件，「继续」不再直接 400 无断点；内存 chat_states 与盘上文件统一经
_save_chat_state / drop_chat_state / restore_persisted_chat_state 三个入口管理。"""

import json
import os
import re
import threading
import time

from core_utils import log_event
from core.config import atomic_write_json  # v1.0.510（P2-13）
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）

_abort_lock = threading.Lock()
_abort_flags = {}
# v1.0.555（P1-11）：全局全停代次。原设计用 _abort_flags[None] 作全局键，
# 但 agent_loop 收尾无条件 _set_abort(None, False)——用户按"停止所有"后，
# 先收尾的 UI 任务会把全局键清掉，仍在跑的后班任务（pane 键未被
# abort_all 置位——只置位了当时已在中止中的 pane）只依赖全局键判定，
# 于是逃逸"全停"继续烧 token/操作页面。
# 代次语义：abort_all 时代次 +1；任务启动时快照代次，当前代次 > 快照 =
# 该任务被全停命中。任务收尾不清代次（天然免疫逃逸）；之后启动的新任务
# 快照新代次，不受旧全停影响（原设计要靠"新对话清全局键"兜底，语义更脆）。
_abort_gen = {"n": 0}


# v1.0.632（B7）：后台/无人值守会话的判定口径收敛到这一处。
# 此前 `("sched-", "study-")` 这个元组在 5 个地方各抄一遍（agent/loop.py 权限降级
# 与写工具闸门、agent/session.py 断点配额三条），而且抄成了两种顺序——
# 加一种后台会话（如批审引擎、宏重放）要改 5 处，漏一处的表现是：
#   · 漏在权限降级 → 后台任务在第一个写操作上永久挂住等一个没人点的弹窗；
#   · 漏在断点配额 → 后台断点把用户「继续」的断点挤掉。
# 两类都要跑很久才暴露，正是最不该靠"记得改全"的口径。
# 现有两种后台会话：services/scheduler.py 的 `sched-<id>`、services/study_engine.py 的
# `study-engine-<ts>`（全仓唯一两处 pane_id 生成点）。再加第三种时改这一个元组即可。
BACKGROUND_PANE_PREFIXES = ("sched-", "study-")


def is_background_pane(pane_id):
    """没有人在屏幕前点弹窗的会话 ⇒ True（pane_id 为空也算：直调同样没有弹窗）。"""
    if not pane_id:
        return True
    return str(pane_id).startswith(BACKGROUND_PANE_PREFIXES)


def abort_generation():
    """读取当前全停代次（任务启动时快照）。"""
    with _abort_lock:
        return _abort_gen["n"]


def request_abort_all():
    """v1.0.555（P1-11）：请求全停——代次 +1，对所有已启动任务生效。"""
    with _abort_lock:
        _abort_gen["n"] += 1
        _abort_flags.pop(None, None)   # 旧全局键废止，统一走代次判定


def globally_aborted_since(start_gen):
    """start_gen 之后是否发生过全停请求。"""
    with _abort_lock:
        return _abort_gen["n"] > start_gen


def _set_abort(pane_id, val):
    """设置/清除某个 pane 的中断标志。pane_id 为 None 时作为全局兜底键。"""
    with _abort_lock:
        if val:
            _abort_flags[pane_id] = True
        else:
            _abort_flags.pop(pane_id, None)


def _is_aborted(pane_id):
    """检查某个 pane 是否被请求中断。pane_id 为 None 时也兜底检查全局键。"""
    with _abort_lock:
        if pane_id is not None and _abort_flags.get(pane_id):
            return True
        return _abort_flags.get(None, False)

# 主思考轮次参考值：常规对话已改为「无限续跑」——max_rounds=None 时不设轮次上限，
# 上下文超 CONTEXT_COMPRESS_THRESHOLD 自动压缩后继续跑，不再 100 轮触顶中断；
# 该常量仅作为默认展示参考，以及显式传入 max_rounds 的任务级约束（如快捷任务 5 轮）的分母。
MAX_ROUNDS = 100
# 达到显式任务级轮次上限后的自动收尾续跑轮数：不再干等用户手动点「继续」，
# 自动追加收尾引导，让模型总结/收尾；收尾段用尽仍未完成才降级为 need_continue。
# （仅对显式传入 max_rounds 的约束型任务生效，无限续跑模式不涉及。）
TAIL_FINISH_ROUNDS = 10

# 死循环熔断阈值：写操作（点击/输入/滚动等）连续执行相同「工具名+参数」的警告/终止次数。
# P1-3 修复（财务审核误杀）：判定改在工具执行后，工具返回 error（CDP 抖动/页面未就绪等）
# 视为合理重试直接重置计数；仅当连续执行【成功】的同一写操作才累计。
# LOOP_WARN_AT=4：连续 4 次成功重复写操作时插入干预消息给模型一次重新决策机会；
# LOOP_KILL_AT=8：干预后仍连续重复到 8 次（即警告后最多再容忍 4 次）才判定死循环终止，
# 给财务审核等场景的偶发页面无响应留足重试空间。
LOOP_WARN_AT = 4
LOOP_KILL_AT = 8
# 读操作死循环熔断阈值：读类工具（read_wecom_page/read_wecom_fields/list_interactive_elements 等）
# 连续执行相同「工具名+参数」的警告/终止次数。写操作在页面上的实际行动会重置读计数，
# 因此「点下一题→读→点下一题→读」的逐题推进不会被误伤；而模型反复读同一元素/同一页
# （如答题时某选项反复读取无果）会累计触发干预，避免一路空转到轮次上限。
LOOP_READ_WARN_AT = 5
LOOP_READ_KILL_AT = 9
# LOOP_ERR_WARN_AT=6：同一写操作连续报错 6 次时插入干预消息；LOOP_ERR_KILL_AT=10：干预后仍持续报错到 10 次则终止。
# 与成功写操作计数独立：偶发 error 重试（1~3 次后成功/换操作）不会被误杀，持续 error 死循环会被拦截。
LOOP_ERR_WARN_AT = 6
LOOP_ERR_KILL_AT = 10
# P2 导航白名单（v1.0.247）：「上一题/下一题」连续点击是合法翻题（改答案回退时要连点数次「上一题」），
# 2026-09-01 17:24 实测：连点 4 次「上一题」（合法导航）即达 LOOP_WARN_AT=4 触发干预 + judge
# （本地模型 judge 一轮要数分钟，干预损失大于循环本身）。导航型点击目标用独立更高阈值，
# 其它写操作仍按 LOOP_WARN_AT/LOOP_KILL_AT。
LOOP_NAV_WARN_AT = 10
LOOP_NAV_KILL_AT = 20

# 上下文压缩阈值（字符）：loop_msgs 总字符数超过该值时压缩中间工具往返历史，
# 避免本地模型（LM Studio）上下文被截断而丢失原始任务指令。
# v1.0.287：80000 → 40000。实证（2026-09-05/06）：DaoPro/vLLM 共享服务在 ~104K
# 字符（80000×reasoning1.3）上下文反复空转 0 字符至 900s 熔断（一夜 3 次），而引擎
# 跑课会话（<50K 字符）一夜 32 门零熔断——空转与上下文字符量强相关。压到 40000
# （reasoning ×1.3 = 52000）让稳态上下文落在实证安全区；压缩机制本身成熟
# （leading 6 + 摘要 + recent 20 + 大 tool 结果截断），代价只是更频繁的中间历史折叠。
# 有 max_input_tokens 的本地模型仍按 min(80000, mi×2) 放宽，不受影响。
CONTEXT_COMPRESS_THRESHOLD = 40000
# 压缩时保留最前面的原始指令（system/user 消息）条数
KEEP_LEADING_MSGS = 6
# 压缩时保留最近 N 条消息原文（保证 assistant tool_calls 与 tool 结果对应关系完整）
# v1.0.290：20 → 8。实证（2026-09-07 博易云考试假卡）：考试每答一题产生 4-6 条消息
# （LLM 请求 + 工具调用 + read_wecom_page 大快照 + LLM 请求），recent=20 时保留最近
# 3-4 题完整往返（含每题 2-5KB 页面快照），压缩后上下文仍顶在 52K，LLM 在压缩后的
# 大上下文上「只思考不输出工具调用」→ 假卡 3 分钟。recent=8 只保留最近约 2 题，
# 已答题（每题相互独立、对后续无价值）全部折叠为摘要，稳态上下文降到 ~20-30K，
# 落在实证零假卡安全区。答题/长流程受益最大；短对话（<8 条）不受影响。
KEEP_RECENT_MSGS = 8

# 被轮次上限中断的对话上下文，key=前端 paneId
chat_states = {}

# chat_states 在 Flask 多线程下被读写，统一加锁保护
_chat_state_lock = threading.Lock()
# chat_states 容量上限：超出时淘汰最旧断点（近似 LRU），避免多 pane 长期累积内存
CHAT_STATES_MAX = 10


def _bp_path(pane_id):
    """断点文件路径：pane_id 白名单化（防路径穿越），中英文/数字/下划线保留。"""
    safe = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", str(pane_id))[:80]
    return os.path.join(paths.workspace_root(), "断点", (safe or "pane") + ".json")


# ---------------- 按会话（pane）记忆的权限级别（v1.0.590） ----------------
# 与「每任务独立模型」同构：agent_loop 入口写一次，断点保存/恢复时随 pane 取用。
# 之所以做成侧表而不是给 _save_chat_state 加参数——它在 loop.py 里有 15 处调用点，
# 逐个加参会把一次功能改动放大成一次高风险大搬运（本项目已因此漏过 import）。
_PANE_PERM = {}
_perm_lock = threading.Lock()


def set_pane_permission(pane_id, level):
    if not pane_id:
        return
    with _perm_lock:
        if level:
            _PANE_PERM[str(pane_id)] = str(level)


def get_pane_permission(pane_id):
    if not pane_id:
        return None
    with _perm_lock:
        return _PANE_PERM.get(str(pane_id))


def drop_pane_permission(pane_id):
    """断点清理时同步清掉，避免长期运行累积。"""
    if not pane_id:
        return
    with _perm_lock:
        _PANE_PERM.pop(str(pane_id), None)


def _save_chat_state(pane_id, loop_msgs, rounds, readonly=False, model_id=None):
    """保存对话断点（供「继续」恢复）。容量超 CHAT_STATES_MAX 时淘汰最旧断点。
    v1.0.302：配额只算 UI pane 断点——淘汰条件从「总数超上限」改为「非 study- 前缀
    key 数超上限」，跑课引擎会话（study-engine-*，一门课一个、跑完自行 pop）不计入
    配额，防引擎跑课时静默挤掉用户「继续」上下文。
    v1.0.361 审查修复（O1）：已存在的 key 先 pop 再重插（touch）——CPython dict
    对已存在 key 赋值不改变插入序，原实现「近似 LRU」实为 FIFO：反复「继续」的
    活跃 pane 保持最初位置被当最旧淘汰，反而长期不动的 pane 靠后存活。
    v1.0.405（M5）：同步落盘（产物库/断点/）——重启后 restore_persisted_chat_state
    可恢复。保存频率=轮次上限/挂起/正常收尾等终止路径，非每轮，同步写盘可接受。
    v1.0.485：**readonly 一并持久化**——只问答会话经 ask_user/request_skill 挂起再
    「继续」后，原实现 readonly 丢失（agent_loop 默认 False）→ 写类工具全部解禁，
    实测只问答模式下 pc_run（pip install）真实执行。"""
    if not pane_id:
        return
    with _chat_state_lock:
        # v1.0.361 审查修复（O1）：已存在的 key 先 pop 再重插（touch）——CPython dict
        # 对已存在 key 赋值不改变插入序，原实现「近似 LRU」实为 FIFO：反复「继续」的
        # 活跃 pane 保持最初位置被当最旧淘汰，反而长期不动的 pane 靠后存活。
        if pane_id in chat_states:
            chat_states.pop(pane_id, None)
        chat_states[pane_id] = {"loop_msgs": list(loop_msgs), "rounds": rounds,
                                 "readonly": bool(readonly), "model_id": model_id or None,
                                 "permission_level": get_pane_permission(pane_id) or "auto"}
        while sum(1 for k in chat_states if not is_background_pane(k)) > CHAT_STATES_MAX:
            victim = next((k for k in chat_states if not is_background_pane(k)), None)
            if victim is None:
                break  # 无 UI key 可淘汰
            chat_states.pop(victim)
        # v1.0.515 修复（审计 P2-18）：豁免前缀（study-/sched-）自身也设上限——
        # 原实现完全不计配额，每条断点含全量 loop_msgs（内存+盘上双份），长期多任务
        # 运行无界增长。豁免配额 20 条（跑课一门一个/定时任务一任务一个，20 足够），
        # 淘汰最旧的豁免 key。
        _exempt = [k for k in chat_states if is_background_pane(k)]
        while len(_exempt) > 20:
            victim = _exempt.pop(0)
            chat_states.pop(victim, None)
            log_event(f"[session] 豁免断点超上限(20)，淘汰最旧：{victim}")
    _bp_write(pane_id, loop_msgs, rounds, readonly, model_id)


def _bp_write(pane_id, loop_msgs, rounds, readonly=False, model_id=None):
    """断点落盘（原子写）。失败不致命——内存断点仍在，仅重启丢失。"""
    try:
        os.makedirs(os.path.dirname(_bp_path(pane_id)), exist_ok=True)
        # v1.0.510（P2-13）：tmp+os.replace 样板收敛到 core.config.atomic_write_json
        atomic_write_json(_bp_path(pane_id),
                          {"pane_id": pane_id, "rounds": int(rounds or 0),
                           "loop_msgs": list(loop_msgs), "readonly": bool(readonly),
                           "model_id": model_id or None,
                           "permission_level": get_pane_permission(pane_id) or "auto",
                           "ts": time.time()},
                          indent=None)
    except Exception as e:  # noqa: BLE001
        log_event(f"[session] 断点落盘失败（不影响本次会话）：{e}")


def drop_chat_state(pane_id):
    """清理断点（内存 + 盘上文件）。sse_stream 断连清理 / chat.py 中止与新会话覆盖时调用。
    v1.0.405（M5）新增：此前只有内存 pop，盘上文件无清理入口。"""
    if not pane_id:
        return
    with _chat_state_lock:
        chat_states.pop(pane_id, None)
    drop_pane_permission(pane_id)   # v1.0.590：权限级别侧表随断点一起清，避免长驻累积
    try:
        p = _bp_path(pane_id)
        if os.path.isfile(p):
            os.remove(p)
    except OSError as e:
        # v1.0.646：残留断点会让「继续」读到过期状态，删不掉必须留痕
        log_event(f"[session] 断点文件删除失败（残留会让「继续」读到旧状态）：{p}：{e}")
    except Exception as e:  # noqa: BLE001
        log_event(f"[session] 断点文件清理失败（残留可忽略）：{e}")


def restore_persisted_chat_state(pane_id):
    """从盘上恢复断点到内存 chat_states（存在且有效时）。返回是否恢复成功。
    routes/chat.py 的 /chat/continue 在内存无断点时调用——重启后「继续」可用。
    v1.0.485：readonly 一并恢复。"""
    if not pane_id:
        return False
    if pane_id in chat_states:
        return True   # 内存已有，无需恢复
    try:
        with open(_bp_path(pane_id), "r", encoding="utf-8") as f:
            d = json.load(f)
        msgs = d.get("loop_msgs")
        if isinstance(msgs, list) and msgs:
            with _chat_state_lock:
                chat_states[pane_id] = {"loop_msgs": msgs, "rounds": int(d.get("rounds") or 0),
                                         "readonly": bool(d.get("readonly")),
                                         "model_id": d.get("model_id") or None,
                                         "permission_level": d.get("permission_level") or "auto"}
            # 锁外写侧表（避免与 _save_chat_state 的锁内读取形成嵌套顺序）
            set_pane_permission(pane_id, d.get("permission_level"))
            log_event(f"[session] 已从盘上恢复断点：{pane_id}（{len(msgs)} 条消息，readonly={bool(d.get('readonly'))}，model={d.get('model_id') or '默认'}，权限={d.get('permission_level') or 'auto'}）")
            return True
    except FileNotFoundError:
        pass  # silent-ok: 断点文件不存在＝首次运行/已清断点，属正常路径
    except Exception as e:  # noqa: BLE001
        log_event(f"[session] 断点恢复失败：{e}")
    return False

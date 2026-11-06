"""agent_tools.dispatch — 技能覆盖与工具路由（_effective_skill / build_agent_tools / dispatch_tool）。"""

import threading

from core_utils import (log_event)

from llm_client import judge_model

from .connectors import CONNECTORS, is_connector_configured, run_connector

from .executors import _finish_tool, _tool_by_name, run_tool

from .skills import SKILLS

# —— v1.0.555（P1-7）：线程级当前 pane 上下文 ——
# agent_loop 驱动工具执行前设置本线程的 pane_id，长阻塞工具（ask_judge）据此把
# abort 钩子透传给 _llm_complete。原 ask_judge 无 deadline 无 poll_abort：
# judge 挂起时单次可阻塞 ~15 分钟（3 次流尝试 ×240~300s 熔断）且点停止无效，
# 与 _judge_escalate（deadline=300 + poll_abort）形成双标。
_PANE_CTX = threading.local()


def set_current_pane(pane_id):
    """agent_loop 在每轮工具执行前调用（串行主循环 + 各 worker 线程各自设置）。"""
    _PANE_CTX.pane_id = pane_id
    # v1.0.588：把企微「当前跟随窗口」也切到本 pane 作用域。原为进程级单值——
    # 多任务并行时任务 A 点开的详情页会被任务 B 未显式带 pick 的 read/click 继承，
    # 直接把写操作做在别人的窗口上（README 的「多任务并行」此前只做到了 SSE 不断流）。
    try:
        from wecom_bridge.windows import set_auto_pick_scope
        set_auto_pick_scope(pane_id)
    except Exception:  # noqa: BLE001
        pass   # 桥层不可用（未装/未绑定）时不影响 pane 上下文本身


def current_pane():
    return getattr(_PANE_CTX, "pane_id", None)


def pane_abort_hook():
    """返回当前 pane 的 abort 轮询钩子；无 pane 上下文时返回 None（不限）。"""
    pid = current_pane()
    if pid is None:
        return None
    from agent.session import _is_aborted
    return lambda: _is_aborted(pid)

# —— v1.0.373（用户需求2）：企微未绑定时屏蔽全部企微工具（软件退化为纯 agent）——
# 正向缓存 60s（绑定后立即生效由 bind 流程刷新），负向缓存 10s（绑定动作后很快可用）。
_wecom_ok_cache = {"v": False, "ts": 0.0}
_alive_cache = {"v": False, "ts": 0.0}   # v1.0.470（P2）：tasklist 存活检查结果缓存 10s


def _wxwork_alive():
    """v1.0.425：企微主进程存活检查（tasklist）。用于 /json 探活失败时的二次判定
    ——实证企微 CDP HTTP 端点会间歇停摆数秒~数分钟而 WS 不受影响，此时进程活着、
    工具实际可用，笼统返回"未绑定"是误伤。
    v1.0.470（P2）：tasklist 子进程结果缓存 10s——企微未启动场景下模型每调一次
    企微工具都重试一次 tasklist（闪黑框+秒级延迟），进程生死 10s 内不会翻转。"""
    import subprocess
    import time as _t
    now = _t.time()
    if now - _alive_cache.get("ts", 0.0) < 10.0:
        return _alive_cache.get("v", False)
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq WXWork.exe"],
                             capture_output=True, timeout=6,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             ).stdout.decode("gbk", "replace")
        v = "WXWork.exe" in out
    except Exception:  # noqa: BLE001
        v = False
    _alive_cache["v"] = v
    _alive_cache["ts"] = now
    return v


def _wecom_tools_allowed(cfg):
    import time as _t
    now = _t.time()
    v = _wecom_ok_cache.get("v")
    ttl = 60.0 if v else 3.0   # v1.0.425：负向缓存 10s→3s——cdp_up 单发超时的瞬时
                               # 抖动会误报"未绑定"拦住全部企微工具（10s 太长）
    if now - _wecom_ok_cache.get("ts", 0.0) < ttl:
        return v
    ok = False
    try:
        # v1.0.425（2026-10-05 实证）：原 `from . import wecom_bridge` 是 ImportError
        # ——agent_tools 包内没有该子模块（wecom_bridge 是顶层包），import 每次都炸
        # → except → ok=False → **绑定闸自 v1.0.373 起恒为 False**，全部企微工具
        # 被笼统"未绑定"拦截（模型手动点"通过"从未成功过，即用户反馈的根因）。
        import wecom_bridge as wb
        from core.config_store import cdp_port as _cdp_port   # v1.0.588：端口取值口径统一
        port = _cdp_port(cfg)
        ok = bool(wb.cdp_up(port))
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        # 兜底必须在 try 外：/json 端点间歇停摆期间 WS 可用、进程活着 → 放行
        #（工具有自己的结构化错误，真不可用会给具体原因，而非笼统"未绑定"）
        ok = _wxwork_alive()
    _wecom_ok_cache["v"] = ok
    _wecom_ok_cache["ts"] = now
    return ok

def _effective_skill(base, cfg):
    """技能覆盖：cfg['skill_overrides'][id] 存在时，用覆盖字段替换内置定义（未覆盖字段回退内置）。
    供 build_agent_tools / get_skills 等运行时读取生效后的技能定义。"""
    ov = (cfg.get("skill_overrides") or {}).get(base["id"])
    if not ov or not isinstance(ov, dict):
        return base
    merged = dict(base)
    for k in ("name", "desc", "icon", "prompt", "tools", "judge", "default"):
        if k in ov and ov.get(k) is not None:
            merged[k] = ov[k]
    return merged


def _custom_skills(cfg):
    """自定义技能（v1.0.303）：cfg['custom_skills'] 是技能中心用户自建技能列表。
    做基本形状校验后返回可用技能定义 list（脏项：非 dict / 缺 id / 缺 name 直接丢弃，
    避免坏数据中断 build_agent_tools）。"""
    out = []
    for s in (cfg.get("custom_skills") or []):
        if isinstance(s, dict) and s.get("id") and s.get("name"):
            out.append(s)
    return out


# —— v1.0.345 上下文预算：重型技能按任务关键词注入（场景包） ——
# 实测（docs/agent-evaluation-2.md）：全技能开启时每轮基础载荷 52K chars，其中技能提示词
# 22.5K——而压缩阈值 40K，对话余量被挤到 ~6K，小模型工作记忆被无关场景的 SOP 淹没。
# 策略：prompt ≥ _HEAVY_PROMPT_MIN 的内置技能为「重技能」，仅当任务文本命中其场景关键词
# 才注入完整提示词与工具；未命中的折叠为一行提示（工具与 SOP 都不加载，模型可据此提示
# 用户重新描述场景）。轻技能（<900 字符，24 个合计约 5K）永远注入。自定义技能永不折叠
# （用户亲手创建，折叠违背预期）。task_text=None → 全部注入（旧行为，兼容显式调用方）。
_HEAVY_PROMPT_MIN = 900
_HEAVY_SKILL_KEYWORDS = {
    "exam-solver": ["考试", "答题", "交卷", "试题", "再考一次", "单选", "多选",
                    "判断题", "及格", "成绩", "答题卡"],
    "study-solver": ["学习", "课程", "课件", "学时", "必修", "学习中心", "学分", "学完", "播课"],
    "finance-audit": ["单据", "财务", "审核", "月报", "付款", "支出", "报销",
                      "预算执行单", "审批", "待办事宜"],
    "exam-prepare": ["考前", "题库", "采题", "试题采集", "考前采集"],
    "pc-ops": ["屏幕", "截图", "本机", "电脑", "文件夹", "整理下载", "命令行",
               "剪贴板", "进程", "卸载", "安装软件"],
}
# 系统注入类 user 消息前缀：不参与场景匹配（含任务词会误判）
_SYSTEM_HINT_PREFIXES = ("（系统", "（大模型判断建议", "【团队审查", "请继续完成上述任务", "[系统]")


def _match_heavy_skill(skill, task_text):
    kws = _HEAVY_SKILL_KEYWORDS.get(skill.get("id")) or []
    return any(k in task_text for k in kws) if kws else False


def _extract_task_text(messages):
    """从消息列表提取任务场景匹配文本：全部 user 消息（排除系统注入类）拼接，截 2000 字。
    关键词匹配取并集语义——多轮对话/续跑场景下，任务在各条消息里被反复描述，拼接提高召回；
    排除系统注入（确认提示/批量 hints/续跑指令等自带任务词，会造成误判）。"""
    parts = []
    for m in messages or []:
        if m.get("role") != "user":
            continue
        t = str(m.get("content") or "").strip()
        if not t or t.startswith(_SYSTEM_HINT_PREFIXES):
            continue
        parts.append(t)
        if sum(len(p) for p in parts) >= 2000:
            break
    return " ".join(parts)[:2000]


def _split_skills(cfg, task_text):
    """按启用规则展开技能列表 → (active, folded)。
    task_text 为 None 时全部 active（旧行为）；否则重技能未命中关键词的折叠。"""
    flags = cfg.get("feature_flags") or {}
    enabled = cfg.get("enabled_skills")      # None 表示全部启用
    active, folded = [], []
    for _base in SKILLS:
        s = _effective_skill(_base, cfg)
        if not (enabled is None or s["id"] in enabled):
            continue
        if task_text is not None and len(s.get("prompt") or "") >= _HEAVY_PROMPT_MIN \
                and not _match_heavy_skill(s, task_text):
            folded.append(f"{s['id']}（{s.get('name')}）")
        else:
            active.append(s)
    for s in _custom_skills(cfg):
        if not (enabled is None or s.get("id") in enabled):
            continue
        active.append(s)   # 自定义技能永不折叠
    if not (flags.get("skills", True)):
        # 总闸关闭时不区分——调用方走停用分支
        return None, []
    return active, folded


def hub_missing_skills(cfg, limit=12):
    """未安装的公共技能清单（v1.0.347）：build_agent_tools 的 request_skill 注入与
    agent_loop 卡住自救提示共用同一读取逻辑。全部离线可用（内置快照兜底），异常返回空。"""
    try:
        from agent_tools.skill_hub_data import hub_skills_with_fallback
        hub_skills, _, _ = hub_skills_with_fallback(cfg)
        installed_ids = {str(s.get("id", ""))[4:] for s in _custom_skills(cfg)
                         if str(s.get("id", "")).startswith("hub_")}
        return [s for s in hub_skills
                if s.get("id") not in installed_ids and s.get("prompt") and s.get("tools")][:limit]
    except Exception as e:  # noqa: BLE001
        log_event(f"[skills] 公共技能清单读取失败（忽略）：{e}")
        return []


def build_agent_tools(cfg, task_text=None):
    """根据已启用技能 + 已配置连接器，动态拼装 (tools, prompt_extra)。
    已启用技能的判断基于内置 SKILLS + 自定义技能（cfg['custom_skills']，v1.0.303）的 id 集合；
    内置定义优先应用 cfg['skill_overrides'] 覆盖，自定义技能直接用存储定义。
    v1.0.312 功能管理总闸：cfg['feature_flags'].skills=False 时全部技能停用（仅剩 finish，
    对齐 WorkBuddy「插件管理·禁用后全部停用」）；connectors=False 时连接器全部停用。
    v1.0.345 上下文预算：task_text（任务文本）非 None 时，重型技能（prompt≥900 字符）
    按场景关键词匹配注入，未命中的折叠为一行提示——把 20K+ 的无关场景 SOP 从每轮
    上下文里清出去（实测基础载荷 52K → ~30K，对话余量 6K → 20K+）。"""
    flags = cfg.get("feature_flags") or {}
    # v1.0.373（用户需求2）：企微未绑定/未启动 → 全部企微工具不注入（纯 agent 模式）
    wecom_ok = _wecom_tools_allowed(cfg)
    tools = []
    prompts = []
    finish_t = _finish_tool()
    if finish_t:
        tools.append(finish_t)
    has_judge_skill = False
    if flags.get("skills", True):
        active, folded = _split_skills(cfg, task_text)
        for s in (active or []):
            for tn in s.get("tools", []) or []:
                if not wecom_ok and "wecom" in tn:
                    continue
                t = _tool_by_name(tn)
                if t and t not in tools:
                    tools.append(t)
            if s.get("judge") == "model":
                has_judge_skill = True
            if s.get("prompt"):
                prompts.append(s["prompt"])
        if folded:
            listing = "、".join(folded)
            prompts.append(
                "【场景包未注入】以下已启用技能与本任务场景关键词不匹配，其专属提示词与工具本"
                "轮未加载：" + listing + "。若你判断任务实际属于其中某场景，请提示用户在任务描述中"
                "带上对应场景关键词（如「考试答题」「学习课程」「财务单据」）后重新发起，系统会自动"
                "加载；在此之前请用现有工具尽力完成任务，不要假装这些技能可用。")
    else:
        prompts.append("【技能系统已停用】功能管理中技能总开关为关，所有技能与其工具均不可用；"
                       "如任务需要这些能力，请告知用户到「设置 → 功能管理」重新开启。")
    # —— 按需安装（v1.0.317）：未安装的公共技能清单 + request_skill 工具 ——
    # 任务匹配未装技能时 agent 调 request_skill 弹窗询问用户；同意→装好注入下一轮，
    # 拒绝→agent 用基础工具完成。全部已装/总闸关/远程与内置都为空时不注入。
    if flags.get("skills", True):
        missing = hub_missing_skills(cfg)
        if missing:
            rt = _tool_by_name("request_skill")
            if rt and rt not in tools:
                tools.append(rt)
            listing = "\n".join(
                f"- id={s['id']} | {s.get('name')} | {str(s.get('desc') or '')[:60]}"
                for s in missing[:12])
            prompts.append(
                "【可按需安装的技能（未安装）】\n" + listing + "\n"
                "若当前任务与上列某技能高度匹配（用它能显著做得更好），先调用 request_skill"
                "（skill_id 填清单中的 id，reason 一句话）向用户请求安装——用户同意后会自动安装并注入下一轮，"
                "届时直接使用；用户拒绝则改用基础工具完成，不要反复请求。普通小任务不必强求。"
                "另外：用户直接丢来一个技能清单网址（URL）并希望导入时，调用 import_skill_source(url)"
                "（会弹窗请用户确认后批量导入）。")
    # —— 双模型分工（v1.0.234）：启用「判断类」技能且配置了 judge 模型 →
    #    注入 ask_judge 工具 + 统一协作约定（按难度自动：判断密集型任务才挂大模型，
    #    机械类技能 judge=none/script 不挂、纯小模型跑）。——
    if has_judge_skill and judge_model(cfg):
        jt = _tool_by_name("ask_judge")
        if jt and jt not in tools:
            tools.append(jt)
        prompts.append(
            "【双模型分工】本任务已启用「判断大模型」（ask_judge 工具已挂载）："
            "ask_judge 是更强逻辑判断资源，只用于高价值推理场景——有限次数考试逐题问、"
            "审批结论、复杂决策、存疑判定；按其结论执行。机械操作（读取页面、翻页、点击已知目标、滚动）"
            "和简单确定的作答直接处理。"
            "【铁律】无限次数考试全程单模型：快速首答+历史试错排除即可，不调用 ask_judge"
            "（试错零成本，问大模型反而慢）。"
        )
    if flags.get("connectors", True):
        conns = cfg.get("connectors") or {}
        for c in CONNECTORS:
            cc = conns.get(c["id"]) or {}
            if cc.get("enabled") and is_connector_configured(c, cc):
                if c.get("tool") and c["tool"] not in tools:
                    tools.append(c["tool"])
                if c.get("prompt"):
                    prompts.append(c["prompt"])
    # v1.0.649（用户："默认（首次使用）直接进入 ai 助手模式，除非用户手动绑定"）：
    # 工具级门控 v1.0.373 起就有（未绑定 ⇒ 不注入任何 *_wecom_* 工具，本机/联网/生成文件不受影响），
    # 但模型看不到"为什么少了这批工具"——它会照着技能 SOP 硬调，甚至直接声称"已点击通过"。
    # 这里把状态说进提示词，并把出路指给用户，而不是让模型自己猜。
    if not wecom_ok:
        prompts.insert(0, "【当前是 AI 助手形态：企业微信未绑定或未就绪】本轮**没有**注入任何 "
                          "*_wecom_* 页面工具（读页面/点击/输入/发消息/企微截图均不可用）。"
                          "问答、生成文件（md/Excel/PPT/Word）、联网检索、本机文件与命令等能力不受影响，"
                          "请照常完成。涉及企微页面的步骤**不要假装已执行**，"
                          "直接说明需要用户到「设置 → 企业微信」完成绑定后再发起，不要反复试探企微工具。")
    return tools, "\n".join(prompts)

def dispatch_tool(name, args, cfg):
    """把工具调用路由到连接器执行器或内置 run_tool。

    单个工具抛异常时返回结构化错误（{"ok": False, "error": ...}）供 agent 看到并自我纠正，
    而不是向上中断整个 agent_loop。
    v1.0.312：连接器工具调用前复查总闸（防会话中开关后残留直调）。
    v1.0.373（用户需求2）：企微未绑定时企微工具直接返回明确错误（兜底拦截——
    工具清单层已过滤，此处防模型硬调/技能残留引用）。
    """
    try:
        if "wecom" in name and not _wecom_tools_allowed(cfg):
            # v1.0.425：话术区分"未绑定"与"CDP 暂时无响应"（cdp_up 已带 3 次重试，
            # 仍失败多为真未绑定或企微繁忙）；并给出已被实测可行的替代路径。
            return {"ok": False, "error": "企业微信未绑定、未启动或 CDP 暂时无响应：企微相关能力暂不可用。"
                                 "若用户已绑定，多为企微繁忙——稍候重试本工具，或直接改用 "
                                 "run_audit_flow（脚本流水线，不走本闸）完成任务并汇报结果。"}
        conns = cfg.get("connectors") or {}
        for c in CONNECTORS:
            if c.get("tool") and c["tool"].get("function", {}).get("name") == name:
                if not (cfg.get("feature_flags") or {}).get("connectors", True):
                    return {"ok": False, "error": "连接器插件已在「设置 → 功能管理」停用"}
                cc = conns.get(c["id"]) or {}
                if cc.get("enabled") and is_connector_configured(c, cc):
                    return run_connector(c, cc, args)
                return {"ok": False, "error": f"连接器未配置或未启用：{c['name']}"}
        return run_tool(name, args, cfg)
    except Exception as e:  # noqa: BLE001
        log_event(f"[tool-error] {name}: {e}")
        return {"ok": False, "error": f"工具执行异常：{name} - {e}"}

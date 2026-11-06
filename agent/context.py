"""agent.context — 对话上下文拼装（自原 api_chat 内联段行为等价迁移，T04）。
build_chat_messages：窗口正文 / 窗口关系 / 目标窗口元素 三块 system 消息 + 技能提示，
异常兜底文案逐字保留。"""

import traceback

import wecom_bridge as wb

from core_utils import log_event
from core.result import is_err, err_msg
from agent_tools import build_agent_tools
from .sys_prompts import SYSTEM_PROMPT

# 上下文体量上限（与原 api_chat 内联值一致）
CTX_BODY_MAX = 6000
CTX_ELEM_MAX = 100
CTX_ELEM_TEXT_MAX = 40


def build_chat_messages(messages, cfg, port, pick, use_wecom_context=False):
    cap = None
    if use_wecom_context:
        # 若提供了 pick（操作指定窗口），则抓取该窗口，保证「正文上下文」与「元素上下文」来自同一窗口
        try:
            cap = wb.capture(pick, port) if (pick is not None) else wb.capture(port=port)
        except Exception as e:  # noqa: BLE001
            # 兜底：企微 CDP 连接失败/窗口被关时不让请求断流，转成结构化错误注入给 LLM 说明
            log_event(f"[agent] 企微窗口上下文获取失败：{e}\n{traceback.format_exc().rstrip()}")
            cap = {"ok": False, "error": f"企微窗口上下文获取失败：{e}"}
    # 注入当前启用技能 / 已配置连接器的能力提示，让模型知道可用工具范围
    # v1.0.345：带任务文本做场景匹配——重型技能（考试/学习/财务等长 SOP）仅场景命中才
    # 注入，未命中的折叠为一行提示。上下文预算见 docs/agent-evaluation-2.md。
    from agent_tools import _extract_task_text
    task_text = _extract_task_text(messages)
    _, skill_prompt = build_agent_tools(cfg, task_text=task_text)
    system_content = SYSTEM_PROMPT
    if skill_prompt:
        system_content += "\n\n## 已启用能力\n" + skill_prompt
    # v1.0.373（用户需求2）：企微未绑定/未启动 → 明确告知纯 agent 模式（工具清单
    # 已同步剔除企微工具），防止模型照着系统提示里的企微场景指引空跑。
    try:
        from core.config_store import cdp_port as _cdp_port   # v1.0.588：端口取值口径统一
        port = _cdp_port(cfg)
        if not wb.cdp_up(port):
            system_content += (
                "\n\n## ⚠️ 当前为纯 agent 模式（企业微信未绑定）\n"
                "企业微信相关工具本轮全部不可用（read_wecom_page / click_wecom_element / "
                "type_wecom_text / send_wecom_message 等任何 wecom 类工具调用都会失败）——"
                "**不要尝试任何企微窗口操作**，也不要建议用户先去操作企微页面。\n"
                "你现在的可用能力：文件读写与解析、网页搜索与抓取、本机命令、内容创作、"
                "任务宏等通用能力。企微相关任务请提示用户到「设置 → 企微绑定」完成绑定后再来。")
    except Exception as e:  # noqa: BLE001
        log_event(f"[context] 企微绑定状态检查失败（忽略）：{e}")
    # v1.0.313 用户固定规则（三层记忆之用户级规则）：设置页手编，最高优先注入
    user_rules = str(cfg.get("user_rules") or "").strip()
    if user_rules:
        system_content += ("\n\n## 用户固定规则（用户亲手设定，优先级最高，与任何其他指示冲突时以本规则为准）\n"
                           + user_rules[:3000])
    # v1.0.312 本地记忆沉淀：把历史会话提炼出的长期事实注入（尾部 ~3KB），开关在设置→功能管理
    if (cfg.get("feature_flags") or {}).get("memory", True):
        try:
            from services.memory_ops import memory_text
            _mem = memory_text()
            if _mem:
                system_content += ("\n\n## 长期记忆（历史会话沉淀，仅供参考，与当前任务冲突时以用户当前指令为准）\n"
                                   + _mem)
        except Exception as e:  # noqa: BLE001
            log_event(f"[memory] 记忆注入失败（忽略）：{e}")
    msgs = [{"role": "system", "content": system_content}]
    if cap and not is_err(cap):
        ctx = (cap.get("body") or "")[:CTX_BODY_MAX]
        msgs.append({"role": "system", "content":
                     f"[企微当前窗口《{cap.get('title')}》URL={cap.get('url')}] 正文：\n{ctx}"})
    elif cap and is_err(cap):
        # 兜底：上下文获取失败也明确告知模型，避免模型误以为没有窗口
        msgs.append({"role": "system", "content": f"[企微窗口上下文] 获取失败：{err_msg(cap)}（对话仍可继续，只是缺少窗口正文）"})
    # 注入窗口父子关系图（谁由谁触发打开），帮助模型规划跨页流程：进详情/附件等
    # 子窗口后直接切回父窗口序号继续，避免在页面内反复找返回按钮。
    # 2026-09-13：仅在需要企微上下文时调用——未启动企微时这里每条消息都会白打一次
    # 必失败的 CDP 请求并写一条错误日志。
    win_rel = None
    if use_wecom_context:
        try:
            win_rel = wb.window_tree_text(port)
        except Exception as e:  # noqa: BLE001
            log_event(f"[agent] 企微窗口关系获取失败：{e}")
            win_rel = None
    if win_rel:
        msgs.append({"role": "system", "content": f"[企微窗口关系]\n{win_rel}"})
    elif not use_wecom_context:
        # 2026-09-13：前端未附加企微上下文（未选窗口/未启动企微）时明确告知模型能力边界，
        # 避免模型因「没有企微窗口」而拒绝任务——本机操控/文件/联网能力与企微无关。
        msgs.append({"role": "system", "content":
            "[运行环境] 本会话未附加企微窗口上下文（未选择窗口或企微未启动）。"
            "企微相关工具（read_wecom_page/click_wecom_element 等）此时可能不可用或返回连接错误："
            "若任务确需操作企微页面，先调用 list_wecom_windows 探测，探测失败就明确告知用户需先启动企微；"
            "与企微无关的任务直接执行，不要以「没有企微窗口」为由拒绝——本机操控"
            "（pc_screenshot/pc_run/pc_apps/pc_files/pc_clipboard/pc_system_info）、"
            "文件读写（save_file/read_workspace_file/list_workspace_files）、"
            "联网（web_search/fetch_webpage）等工具均不受影响。"})
    # v1.0.623：把本机已就绪的 node/npm/git 连路径一起报给模型。组件是便携版、从来不在
    # 系统 PATH 上，模型不知道它存在，接到"跑个 node 脚本"就 where 落空、满盘 dir 搜
    # （用户原话「找了一圈才找到软件安装的组件」）。路径本身已注入本进程 PATH，
    # 这行提示让它连搜都不用搜——**不需要往系统环境变量里加任何东西**。
    try:
        from core.portable import runtime_hint_text
        _rt = runtime_hint_text()
        if _rt:
            msgs.append({"role": "system", "content": _rt})
    except Exception as e:  # noqa: BLE001
        log_event(f"[agent] 运行时位置提示注入失败（忽略）：{e}")
    if pick is not None and isinstance(pick, int):
        try:
            page = wb.pick_window(wb.list_targets(port), pick)
        except Exception as e:  # noqa: BLE001
            log_event(f"[agent] 企微窗口/元素上下文获取失败：{e}")
            page = None
        if page:
            try:
                elems = wb.get_interactive_elements(pick, port)
            except Exception as e:  # noqa: BLE001
                log_event(f"[agent] 企微可操作元素获取失败：{e}")
                elems = {"ok": False, "error": f"可操作元素获取失败：{e}"}
            if elems and is_err(elems):
                msgs.append({"role": "system", "content":
                    f"[你要操作的窗口] 序号 pick={pick}，标题《{page.get('title')}》URL={page.get('url')}。\n"
                    f"⚠️ 获取可操作元素失败：{err_msg(elems)}"})
            else:
                el_text = ""
                if elems and isinstance(elems.get("elements"), list):
                    el_text = "\n".join(
                        f"  [{e.get('index')}] <{e.get('tag')}> {(e.get('text') or e.get('placeholder') or e.get('name') or '')[:CTX_ELEM_TEXT_MAX]}"
                        for e in elems["elements"][:CTX_ELEM_MAX])
                msgs.append({"role": "system", "content":
                    f"[你要操作的窗口] 序号 pick={pick}，标题《{page.get('title')}》URL={page.get('url')}。\n"
                    f"调用操作类工具(capture_wecom_window/read_wecom_page/click_wecom_element/type_wecom_text/select_wecom_option/scroll_wecom_window)时务必传 pick={pick}。\n"
                    f"该窗口可操作元素（序号/text）：\n{el_text or '（无）'}"})
    msgs += messages
    return msgs

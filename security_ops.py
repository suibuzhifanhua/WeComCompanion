# -*- coding: utf-8 -*-
"""security_ops — 安全中心（v1.0.320，对齐 WorkBuddy 安全中心，按本软件形态落地）
=================================================================
命令安全（黑白名单+三模式）/ 文件保护路径 / 删除审批（挂起-恢复+令牌）/
自动备份（容量上限裁最旧）/ 审计日志（JSONL 独立存档）。
沙箱/传输加密/安全网关对单机本软件形态不适用，不实现。
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from core_utils import log_event
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）

_lock = threading.Lock()
_grants = {}                 # args_hash -> 过期时间戳（审批令牌，一次性）
_pending = {}                # args_hash -> (nonce, 过期时间戳)（待用户批准的审批请求）
_del_window = []             # 最近删除时间戳（批量删除计数窗口）

DEFAULT_SECURITY = {
    "command_mode": "block",      # 【严重度轴】block=拦截危险命令 ask=危险命令弹窗审批 pass=全放行
    "permission_level": "auto",   # 【询问范围轴】v1.0.590 ask_every/auto/full，见「权限级别」小节
    "cmd_blacklist": [],          # 用户自定义危险正则（追加到内置规则）
    "cmd_whitelist": [],          # 放行正则（命中即放行，优先级最高）
    "protected_paths": [],        # 额外保护路径前缀（写入/删除直接拒）
    "delete_confirm_limit": 10,   # 60 秒窗口内删除次数达到该值 → 弹窗审批
    "auto_backup": True,          # 覆盖/删除文件前自动备份到 产物库/备份/
    "backup_limit_mb": 500,       # 备份目录总上限（超限裁最旧日期目录）
}

# 内置危险命令模式（cmd / powershell 通用，误伤率低的典型破坏性结构）
# v1.0.373 审查修复（P3-9）：补高漏报变体——del/erase 无空格斜杠、Remove-Item 参数顺序无关
_BUILTIN_DANGEROUS = [
    r"\bformat\s+[a-z]:", r"\brd\s+/s\b", r"\brmdir\s+/s\b",
    r"\bdel\s*/[sfq]", r"\berase\s*/[sfq]",          # del/s、del/q 等无空格变体
    r"\brm\s+(-[rf]|--recursive)", r"remove-item\s+[^\n]*-recurse",
    r"remove-item\s+[^\n]*-force\b",                  # 无 -recurse 的强删也危险
    r"\bshutdown\b", r"\breg\s+(delete|add|import)\b", r"\bbcdedit\b", r"\bdiskpart\b",
    r"\bcipher\s+/w\b", r"\bvssadmin\s+delete\b", r"\bschtasks\s+/create\b",
    # v1.0.470（P2）：删除死正则 `net user \S+ / (add|delete)`（/ 后要求空格永不匹配，
    # 无空格变体由下一行覆盖）
    r"\bnet\s+user\s+\S+\s+/(add|delete)\b",
    r"\bmkfs\b", r"\bdd\s+if=", r"\btaskkill\s+/f\s+/im\s+(csrss|wininit|lsass)\b",
    # v1.0.649（用户："软件修复的时候不要影响企业微信"）：企微进程族不许命令行杀掉。
    # 覆盖 taskkill / Stop-Process 两种写法与 /f /im、/im /f 两种参数顺序；
    # 绑定流程要重启企微走的是自己的受确认路径，不经过这里（这里只拦"绕过用户直接下手"）。
    r"\btaskkill\b[^|;\n]{0,48}\bwxwork(?:web)?\.exe\b",
    r"\bstop-process\b[^|;\n]{0,48}\bwxwork(?:web)?\b",
    r"\bwevtutil\s+cl\b", r"\bfsutil\s+usn\s+deletejournal\b",
    # v1.0.588 补齐漏报：服务删除/防火墙自禁/证书与文件解码/计划下载执行/无盘删除
    r"\bsc\s+delete\b", r"\bsc\s+config\s+\S+\s+start=\s*disabled\b",
    r"\bnetsh\s+advfirewall\s+set\b", r"\bnetsh\s+firewall\s+set\b",
    r"\bcertutil\s+[-/](decode|decodehex|urlcache|addstore|delstore)\b",
    r"\bbitsadmin\s+/transfer\b", r"\bwmic\s+(?:\S+\s+)?delete\b",
    r"\badd-?mppreference\s+(-exclusion|/exclusion)\b",
    r"\bdisable-?net?adapter\b", r"\b(umount|mount)\s+-o\s+remount\b",
    # PowerShell 下载执行链（IEX cradle）：仅拦"取回+执行"的组合，单用 Invoke-WebRequest
    # 抓网页属正常用法，不拦（避免误伤 agent 的日常联网取数）
    r"\biex\s*\(", r"\binvoke-expression\b",
    r"new-object\s+system\.net\.webclient[^\n]*\.downloadstring",
    r"downloadstring(file)?[^\n]*\|\s*(iex|invoke-expression)\b",
    r"\binvoke-webrequest\b[^\n]*\|\s*invoke-expression\b",
]

# v1.0.588：白名单过泛探针。用户填 `.*` / `.+` 这类规则会让「放行名单」命中一切命令
# （含 format C:），等同于把整套命令管控关掉却毫无提示。用一条典型灾难命令试匹配，
# 命中的白名单规则视为无效并跳过 + 落审计。窄规则（如 ^git\b / pip install）不受影响。
_OVERBROAD_PROBE = ("cmd /c format C: /q & rd /s /q C:\\Windows & "
                    "del /f /s /q C:\\Windows\\System32\\* & reg delete HKLM\\SYSTEM /f & shutdown /s /t 0")
_overbroad_warned = set()   # 同一过泛规则只落一次审计，避免每次工具调用都写盘

_DEFAULT_PROTECTED = [r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
                      r"C:\ProgramData"]

# v1.0.373 审查修复（P1-2）：8.3 短名兜底映射（GetLongPathName 展不开时的已知系统目录短名）
_SHORTNAME_MAP = {
    "progra~1": r"c:\program files",
    "progra~2": r"c:\program files (x86)",
    "all users": r"c:\programdata",
}


def _normalize_for_protect(raw):
    """路径归一用于保护前缀比对：abspath + 展开 8.3 短名（GetLongPathNameW 逐级上溯）。
    短名不展开会让 C:\\Progra~1\\x.dll 绕过 C:\\Program Files 保护（审查 P1-2）。"""
    p = os.path.abspath(str(raw))
    try:
        import ctypes
        cur = p
        while cur and len(cur) > 3 and not os.path.exists(cur):
            nxt = os.path.dirname(cur)
            if nxt == cur:
                break
            cur = nxt
        if cur and os.path.exists(cur):
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetLongPathNameW(cur, buf, 1024) and buf.value:
                tail = p[len(cur):]
                p = buf.value + tail
    except Exception:  # noqa: BLE001
        pass
    low = p.lower()
    for short, full in _SHORTNAME_MAP.items():
        if low.startswith("c:\\" + short + "\\") or low == "c:\\" + short:
            p = full + p[3 + len(short):]
            break
    return p


def sec_cfg(cfg):
    """合并默认配置（用户缺键回退默认，多键保留）。
    v1.0.361 审查修复（O7 配套）：cfg=None 安全（直调 pc_run/pc_files 不带 cfg 的
    场景按默认安全规则执行——原对 None 调 .get 直接抛异常，配合 fail-closed 会把
    所有直调误转人工审批）。"""
    d = dict(DEFAULT_SECURITY)
    d.update((cfg or {}).get("security") or {})
    return d


# ==================== 权限级别（v1.0.590：询问范围轴） ====================
# 与 command_mode 的【严重度轴】正交，两轴各管一件事，避免把"要不要问"和
# "命中危险规则后怎么处理"混成一个开关：
#   permission_level  问不问、问多宽   —— ask_every / auto / full
#   command_mode      命中危险模式怎么办 —— block / ask / pass
# 地板不可越：选了「完全访问」，command_mode=block 下致命命令与保护路径写入**仍然直接拒绝**。
PERMISSION_LEVELS = ("ask_every", "auto", "full")

# 写类工具 → 人话动作（审批弹窗标题用）。未列出的按「执行 <工具名>」兜底，
# 让弹窗对用户可读——给用户看 click_wecom_element 这种名字等于没给。
_TOOL_ACTION_LABELS = {
    "click_wecom_element": "点击企微页面元素",
    "type_wecom_text": "在企微页面输入文字",
    "select_wecom_option": "选择企微页面下拉项",
    "scroll_wecom_window": "滚动企微页面",
    "type_wecom_document": "编辑企微在线文档",
    "upload_wecom_file": "上传文件到企微页面",
    "download_wecom_file": "从企微页面下载文件",
    "send_wecom_message": "发送企微消息",
    "send_wecom_webhook": "发送企微群机器人消息",
    "close_wecom_page": "关闭企微窗口/标签页",
    "open_wecom_silent_page": "打开企微静默页面",
    "wecom_run_js": "在企微页面执行脚本",
    # v1.0.614：批审原先不在表里 → 审批弹窗上显示的是裸工具名"执行 run_audit_flow"，
    # 而这恰恰是全项目唯一会"真实提交审批单"的工具。名字必须说人话。
    "run_audit_flow": "批量审核分支机构月报（脚本流水线）",
    "save_workspace_file": "写入产物库文件",
    "save_file": "写入产物库文件",
    "create_excel": "生成 Excel 文件",
    "create_docx": "生成 Word 文档",
    "create_pptx": "生成 PPT 文件",
    "pc_run": "执行本机命令",
    "pc_files": "读写/删除本机文件",
    "pc_clipboard": "读写本机剪贴板",
    "pc_program": "启动/关闭本机程序",
    "save_experience": "写入经验库",
    "dsh_run": "委托外部 agent 执行电脑任务",
}

# 无风险可言的交互/收尾类工具：即使在「每次询问」档也不该拦它们，
# 否则 agent 每轮收尾都要用户点一次确定，直接不可用。
_NEVER_GATE_TOOLS = {"finish", "ask_user", "ask_judge", "request_skill",
                     "import_skill_source", "append_progress", "list_skills"}


def permission_level(cfg=None):
    """取权限级别；缺失/非法一律回退 auto（历史行为，不会因为配置脏而放宽）。"""
    try:
        sec = (cfg or {}).get("security") or {}
        lv = str(sec.get("permission_level") or "auto").strip().lower()
        return lv if lv in PERMISSION_LEVELS else "auto"
    except Exception:  # noqa: BLE001
        return "auto"


def tool_action_label(name, args=None):
    """工具调用的用户可读描述（审批弹窗/审计日志用）。"""
    act = _TOOL_ACTION_LABELS.get(name) or ("执行 " + str(name))
    tgt = ""
    try:
        a = args or {}
        tgt = str(a.get("target") or a.get("path") or a.get("command")
                  or a.get("text") or a.get("name") or "").strip()
    except Exception:  # noqa: BLE001
        tgt = ""
    if tgt:
        act += "：" + (tgt[:80] + "…" if len(tgt) > 80 else tgt)
    # v1.0.614：把"会造成什么真实后果"显形。原实现只取 target/path/command/text/name，
    # 而 run_audit_flow 的参数是 {flow, auto_approve} —— 一个都不落在标签里，
    # 用户在审批弹窗上看到的只是"执行 run_audit_flow"，看不出批准=真点「通过」提交 N 张单据。
    try:
        a = args or {}
        if a.get("auto_approve"):
            act += "｜⚠️ 判定通过的单据将**真实点击「通过」提交审批**，事后不可撤销"
        if a.get("flow"):
            act += "｜流程：" + str(a["flow"])[:24]
        # v1.0.632（B1）：审批词命中时，把"为什么这次一定要问"写在弹窗标题上——
        # 只说"执行 click_wecom_element"用户看不出点的是"同意"。
        _w = approval_hit_word(name, a)
        if _w:
            act += "｜⚠️ 目标含审批动作「%s」，该点击会真实改变单据状态、事后不可撤销" % _w[:24]
    except Exception:  # noqa: BLE001
        pass
    return act


# v1.0.614：**参数级**的"无论如何都要问"名单——某些工具本身是正常用途，但带上某个参数
# 之后就是不可撤销的真实业务写入。这类风险不该取决于用户选的是哪一档权限，
# 也不该只靠工具描述里一句"仅用户明确要求时传 true"（那是提示词约束，不是代码约束）。
_ALWAYS_ASK = {
    "run_audit_flow": ("auto_approve",),
}

# v1.0.632（B1）：**审批词闸门**——把"绝不自动点同意/提交"这条业务红线从提示词落到代码层。
# 此前红线只有两处：`run_audit_flow(auto_approve)` 的参数级名单 + 工具描述里的一句话。
# 而 `click_wecom_element` 点"同意"、`wecom_run_js("…querySelector('[data-btn=approve]').click()")`
# 都不经过任何按**语义**设的拦截——auto 档和定时/刷课等后台会话可以全程无审批
# 提交真实财务单据。这里按目标文案/表达式里的词判定，命中即走既有的
# "挂起→弹窗→一次性令牌→重试"管道（后台无弹窗则直接拒绝），不新增第二套审批机制。
# 词表刻意保守：只收"点了就改变单据状态/动钱"的动词，不收"通过/确定/提交"这类
# 在答题选项、普通表单里高频出现的泛词（那会把跑课与填表功能实质打死）。
_APPROVAL_TEXT = re.compile(
    r"(审批通过|审核通过|批准通过|同意通过|同意支付|同意付款|准予|驳回|退回重报|否决|"
    r"提交审批|提交付款|确认支付|确认付款|立即支付|去支付|结算|"
    r"\b(?:approve|agreed?|reject|pay\s*now|settle)\b)", re.I)
# 单独成词的「同意」「批准」在按钮文案里就是审批动作，但作为**子串**出现在长句里太常见
# （"不同意该方案，请补充说明" 这类意见文本），所以只对整段取值恰好等于它的文案字段生效。
_APPROVAL_LABELS = {"同意", "同意全部", "全部同意", "批准", "驳回", "退回", "否决",
                    "审批通过", "审核通过", "确认支付", "确认付款", "立即支付", "去支付",
                    "付款", "结算", "提交审批", "提交付款",
                    "approve", "agree", "agreed", "reject"}
# 只有"会真实操作页面"的工具参与文案判定；输入类工具填"同意"到意见框不是提交动作。
_CLICK_LIKE_TOOLS = ("click_wecom_element", "wecom_run_js", "click_wecom_text")


def _clickable_text(args):
    """把点击/JS 参数里可能承载"按钮文案或选择器"的部分拼成一段文本供审批词判定。"""
    a = args or {}
    parts = []
    t = a.get("target")
    if isinstance(t, dict):
        parts += [str(v) for v in t.values()]
    elif t is not None:
        parts.append(str(t))
    for k in ("expression", "text", "label", "selector", "xpath", "content"):
        v = a.get(k)
        if v is not None:
            parts.append(str(v))
    return " ".join(parts)[:4000]


_LABEL_KEYS = ("text", "label", "name", "title")


def _clickable_labels(args):
    """只取"按钮文案"级别的字段（text/label/name/title），不取 XPath/JS 表达式。

    表达式里的 token 是选择器片段，整段等于"同意"这种情况不该由它触发精确匹配；
    反之文案字段是模型对着界面抄的字面标签，精确匹配可靠。"""
    a = args or {}
    out = []
    t = a.get("target")
    if isinstance(t, dict):
        out += [str(t[k]) for k in _LABEL_KEYS if t.get(k) is not None]
    for k in _LABEL_KEYS:
        if a.get(k) is not None:
            out.append(str(a[k]))
    return out


def approval_hit_word(name, args):
    """命中则返回那个审批词（供弹窗说明"为什么要问"），未命中返回 ""。"""
    if name not in _CLICK_LIKE_TOOLS:
        return ""
    try:
        m = _APPROVAL_TEXT.search(_clickable_text(args))
        if m:
            return m.group(0)
        for s in _clickable_labels(args):
            key = s.strip().lower()
            if key in _APPROVAL_LABELS:
                return s.strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def approval_word_hit(name, args):
    """点击目标/JS 表达式里出现审批动作 ⇒ True（无论哪一档权限都要问）。"""
    return bool(approval_hit_word(name, args))


def _always_ask_hit(name, args):
    keys = _ALWAYS_ASK.get(name)
    if keys:
        try:
            if any((args or {}).get(k) for k in keys):
                return True
        except Exception:  # noqa: BLE001
            pass
    return approval_word_hit(name, args)


def gate_write_tool(name, args, cfg, level=None, is_read=False, background=False):
    """「每次询问」档的统一闸门——集中一处，取代散落在各执行器里的判断。

    返回 None 表示放行；返回 dict 为 __approval_needed__ 形态，直接复用 loop 已有的
    挂起 → 审批弹窗 → /api/security/grant 一次性令牌 → 原样重试 管道（与危险命令、
    批量删除同一条路，不再新增第二套审批机制）。
    auto/full 档不在此新增询问：风险询问由 check_command / check_delete / check_paths 负责。
    is_read 由调用方（agent.loop，它才有 is_read_tool 口径）传入——读类/查询类不询问：
    该档的语义是"动手前先问"，不是"每看一眼都要点确定"。
    background（v1.0.614）：定时任务/刷课等**没有弹窗可点**的会话。命中 _ALWAYS_ASK 的
    不可撤销写操作在这里**直接拒绝并说明原因**，而不是挂起——挂起等于静默结束任务。"""
    lv = level or permission_level(cfg)
    _aa = _always_ask_hit(name, args) and not is_read
    if _aa and lv != "full":
        # 不可撤销的真实业务写入：auto 档也要问（v1.0.614 起，此前只有 ask_every 档问）。
        if background:
            audit("permission", tool_action_label(name, args), False, "后台会话无法逐次确认")
            return {"ok": False, "error": "该操作会真实提交审批单（不可撤销），而当前是定时任务/后台会话——"
                                 "没有弹窗可以确认，已拒绝执行。请在软件界面里手动发起这条任务。"}
        lv = "ask_every"   # 借道下面的统一询问路径（同一套令牌与审计，不另起机制）
    if lv != "ask_every" or is_read or name in _NEVER_GATE_TOOLS:
        return None
    h = args_hash("perm", name, json.dumps(args or {}, ensure_ascii=False,
                                           sort_keys=True, default=str))
    if consume(h):
        audit("permission", tool_action_label(name, args), True, "用户已批准（一次性令牌）")
        return None
    reason = ("该操作含不可撤销的真实业务写入（参数级风险），任何权限档都要确认"
              if _aa else "当前权限级别为「每次询问」")
    audit("permission", tool_action_label(name, args), False, reason)
    return {"__approval_needed__": True, "kind": "tool", "tool": name,
            "command": tool_action_label(name, args), "path": "",
            "hash": h, "reason": reason}


def audit_jsonl():
    # v1.0.348 路径修复：paths.workspace_root() 已返回 <app>/产物库，旧实现再拼一层 "产物库"
    # 导致审计日志写进 产物库/产物库/日志/ 嵌套目录（实测发现）。
    return os.path.join(paths.workspace_root(), "日志", "安全审计.jsonl")


def audit(tool, summary, allowed, reason=""):
    """追加一条安全审计（JSONL）。永不抛异常。
    v1.0.510 优化（P2-12）：append-only 无界增长（实测 111KB 且持续涨）——写入前
    超 5MB 轮转为 .1（只保留一代），审计历史仍可在轮转档里回溯。"""
    try:
        p = audit_jsonl()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        rec = {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "tool": tool, "summary": str(summary)[:300],
               "allowed": bool(allowed), "reason": str(reason)[:200]}
        with _lock:
            _rotate_if_large(p)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        try:
            log_event(f"[security] 审计写入失败：{e}")
        except Exception:  # noqa: BLE001
            pass


_AUDIT_ROTATE_BYTES = 5 * 1024 * 1024


def _rotate_if_large(p):
    """审计日志超限轮转：安全审计.jsonl → 安全审计.1.jsonl（覆盖上一代）。"""
    try:
        if os.path.getsize(p) >= _AUDIT_ROTATE_BYTES:
            rotated = p.replace(".jsonl", ".1.jsonl")
            try:
                os.remove(rotated)
            except OSError:
                pass
            os.replace(p, rotated)
    except OSError:
        pass  # 轮转失败不影响本次写入


def audit_read(limit=20):
    try:
        p = audit_jsonl()
        if not os.path.isfile(p):
            return []
        with open(p, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()][::-1]
    except Exception:  # noqa: BLE001
        return []


def audit_clear():
    try:
        p = audit_jsonl()
        if os.path.isfile(p):
            os.remove(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def audit_export():
    """导出为可读 txt 到 产物库/产出/，返回路径。
    v1.0.361 审查修复（S5）：paths.workspace_root() 已含「产物库」，原再拼一层导致导出写进
    产物库/产物库/产出/（用户不可见）——与 v1.0.348 的 audit_jsonl 同款修复。"""
    p = audit_jsonl()
    out_dir = os.path.join(paths.workspace_root(), "产出")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"安全审计导出_{datetime.datetime.now().strftime('%m%d_%H%M')}.txt")
    with open(p, encoding="utf-8", errors="ignore") if os.path.isfile(p) else open(os.devnull, encoding="utf-8") as f:
        lines = f.read().splitlines()
    with open(out, "w", encoding="utf-8", newline="\n") as w:
        w.write(f"安全审计导出（{len(lines)} 条）\n{'=' * 40}\n")
        for l in lines:
            try:
                r = json.loads(l)
                w.write(f"[{r['ts']}] {'放行' if r['allowed'] else '拦截'} {r['tool']}：{r['summary']}"
                        f"{'（' + r['reason'] + '）' if r.get('reason') else ''}\n")
            except Exception:  # noqa: BLE001
                w.write(l + "\n")
    return out


# ---------------- 命令安全 ----------------
def check_command(command, cfg):
    """返回 (verdict, reason)。verdict: pass | ask | block。"""
    sec = sec_cfg(cfg)
    cmd = str(command or "")
    # 白名单优先
    for pat in (sec.get("cmd_whitelist") or []):
        try:
            rx = re.compile(pat, re.I)
        except re.error:
            continue
        if rx.search(_OVERBROAD_PROBE):
            # 过泛规则会放行一切（含 format/rd /s），等同静默关闭命令管控——跳过并留痕一次
            if pat not in _overbroad_warned:
                _overbroad_warned.add(pat)
                audit("check_command", f"忽略过泛放行规则 {pat!r}", False,
                      "该放行正则连典型灾难命令都能命中，等于关闭命令管控；请改窄（如 ^git\\b）")
            continue
        if rx.search(cmd):
            return "pass", "命中放行名单"
    danger = list(_BUILTIN_DANGEROUS) + list(sec.get("cmd_blacklist") or [])
    _lv = permission_level(cfg)
    for pat in danger:
        try:
            if re.search(pat, cmd, re.I):
                mode = sec.get("command_mode") or "block"
                if mode == "pass":
                    return "pass", "命中危险模式但模式为放行"
                if mode == "ask" and _lv == "full":
                    # 权限级别「完全访问」只降级 ask 档；block 档（默认，含致命命令地板）
                    # 不受权限级别影响——完全访问 ≠ 无兜底。
                    return "pass", "权限级别为完全访问：危险命令「弹窗审批」已降级放行"
                return ("ask" if mode == "ask" else "block"), f"命中危险命令模式：{pat}"
        except re.error:
            continue
    return "pass", ""


# ---------------- 文件路径保护 ----------------
def check_paths(paths, cfg):
    """写/删/移动目标保护。返回 (allowed, 命中的保护前缀)。
    v1.0.373 审查修复（P1-2）：_normalize_for_protect 展开 8.3 短名（原 abspath 不展开，
    C:\\Progra~1\\x.dll 可绕过 C:\\Program Files 保护写系统目录）。
    v1.0.470 修复（P1-3）：补 realpath 双路检查——abspath/GetLongPathName 都不解析
    NTFS junction/symlink，指向受保护目录的目录联接可绕过前缀比对（如用户目录下
    link → C:\\Windows）。归一化路径与 realpath 解析路径任一命中保护前缀即拒绝。"""
    sec = sec_cfg(cfg)
    protected = list(_DEFAULT_PROTECTED) + list(sec.get("protected_paths") or [])
    for raw in paths:
        if not raw:
            continue
        candidates = {_normalize_for_protect(raw).rstrip("\\").lower()}
        try:
            candidates.add(os.path.realpath(str(raw)).rstrip("\\").lower())
        except Exception:  # noqa: BLE001
            pass
        for p in candidates:
            for pre in protected:
                pre_low = pre.lower().rstrip("\\")
                if p.startswith(pre_low + os.sep) or p == pre_low:
                    return False, pre
    return True, ""


# ---------------- 自动备份 ----------------
def backup_file(path, cfg):
    """覆盖/删除前备份单文件到 产物库/备份/<YYYYMMDD>/；超限裁最旧日期目录。永不抛异常。"""
    try:
        sec = sec_cfg(cfg)
        if not sec.get("auto_backup", True):
            return None
        if not path or not os.path.isfile(str(path)):
            return None
        # v1.0.361 审查修复（S5）：去掉多余一层「产物库」（workspace_root 已含）
        root = os.path.join(paths.workspace_root(), "备份")
        day = datetime.datetime.now().strftime("%Y%m%d")
        day_dir = os.path.join(root, day)
        os.makedirs(day_dir, exist_ok=True)
        base = os.path.basename(str(path))
        dst = os.path.join(day_dir, f"{int(time.time())}_{base}")
        import shutil
        shutil.copy2(str(path), dst)
        _trim_backup(root, float(sec.get("backup_limit_mb") or 500))
        return dst
    except Exception as e:  # noqa: BLE001
        try:
            log_event(f"[security] 备份失败（忽略）：{e}")
        except Exception:  # noqa: BLE001
            pass
        return None


def _trim_backup(root, limit_mb):
    total = 0
    days = []
    for d in os.listdir(root):
        dd = os.path.join(root, d)
        if not os.path.isdir(dd):
            continue
        sz = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(dd) for f in fs)
        days.append((dd, sz))
        total += sz
    if total <= limit_mb * 1048576:
        return
    for dd, sz in sorted(days):          # 最旧日期目录在前（YYYYMMDD 名有序）
        if total <= limit_mb * 1048576:
            break
        import shutil
        shutil.rmtree(dd, ignore_errors=True)
        total -= sz


# ---------------- 删除审批 ----------------
def args_hash(*parts):
    return hashlib.sha1("\x00".join(str(x) for x in parts).encode("utf-8")).hexdigest()[:16]


def _prune_stale_locked():
    now = time.time()
    if len(_grants) > 64:
        for k in [k for k, v in _grants.items() if v < now]:
            _grants.pop(k, None)
    for k in [k for k, v in _pending.items() if v[1] < now]:
        _pending.pop(k, None)


def begin_approval(h, ttl=600):
    """登记一次「待用户批准」的审批请求，返回随机 nonce（只随 SSE 发给前端弹窗）。

    v1.0.652（审计 P0-1）：审批令牌改双因子。hash 是 args 的确定性函数
    （sha1("perm"|name|args)），模型/本机进程都能自行计算——旧实现 grant(h) 对任意
    hash 直接发令牌，提示注入或失控模型拿可自算的 hash POST /api/security/grant 即可
    自批自放，整套 _ALWAYS_ASK / 审批闸 / ask_every 全部绕过。现在 grant 必须出示
    本函数签发的 nonce（只出现在前端弹窗的 SSE 事件里，不进任何模型可见内容）。"""
    nonce = secrets.token_hex(16)
    with _lock:
        _prune_stale_locked()
        _pending[str(h)] = (nonce, time.time() + ttl)
    return nonce


def grant(h, nonce=None):
    """发放一次性审批令牌。必须出示 begin_approval 签发的 nonce（用户在弹窗点
    「批准」时由前端回传）。校验失败返回 False（不发令牌）。
    v1.0.652（审计 P0-1）：见 begin_approval——单凭 hash 不再可自铸。"""
    h = str(h or "")
    with _lock:
        rec = _pending.pop(h, None)
    if not rec:
        return False
    _nonce, exp = rec
    if exp < time.time():
        return False
    if not nonce or not hmac.compare_digest(str(nonce), _nonce):
        return False
    with _lock:
        _prune_stale_locked()
        _grants[h] = time.time() + 600
    return True


def consume(h):
    """一次性消费审批令牌。命中返回 True。"""
    with _lock:
        exp = _grants.pop(h, None)
    return exp is not None and exp >= time.time()


def _del_window_count():
    now = time.time()
    while _del_window and now - _del_window[0] > 60:
        _del_window.pop(0)
    return len(_del_window)


def check_delete(path, cfg):
    """删除安全检查（pc_files delete 调用）。返回 (verdict, reason, args_hash)。
    verdict: pass=直接执行 ask=需审批 block=拒绝。"""
    sec = sec_cfg(cfg)
    h = args_hash("pc_files:delete", path)
    # v1.0.652（审计 P0-1）：保护路径检查放在令牌消费**之前**——旧序是"持令牌先放行"，
    # 一枚令牌即可穿透 protected_paths 地板。令牌只豁免下方的删除计数审批阈值，
    # 永不豁免保护路径（与 command_mode=block 的地板语义同级）。
    allowed, hit = check_paths([path], cfg)
    if not allowed:
        return "block", f"命中保护路径：{hit}", h
    if consume(h):
        return "pass", "用户已批准（令牌）", h
    with _lock:
        _del_window.append(time.time())
        cnt = _del_window_count()
    limit = int(sec.get("delete_confirm_limit") or 10)
    # v1.0.588：移除 command_mode=="pass" 的早退。那是设置页的「危险命令」模式开关，
    # 帮助文档（index.html 安全中心）把「删除审批」列为独立能力，命令模式不该顺带
    # 关掉批量删除闸门。要免审批请调高 delete_confirm_limit，而非借用命令模式。
    if cnt > limit:
        if permission_level(cfg) == "full":
            # 完全访问档：批量删除的「询问」降级放行（上方 check_paths 的保护路径
            # block 在此之前已判定，不受此影响）
            return "pass", "权限级别为完全访问：批量删除审批已降级放行", h
        return "ask", f"60 秒内已删除 {cnt - 1} 次，超过审批阈值（{limit}）", h
    return "pass", "", h


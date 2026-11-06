"""OS 窗口域：webhook / 窗口树摘要 / Win32 枚举与最小化 / 收纳授权登记
（v1.0.656 自 windows.py 拆出）。

兼容口径：对 windows 命名空间的运行期引用统一走 `_w.<name>` 动态属性
（`from . import windows as _w`），保证 monkeypatch.setattr(wecom_bridge.windows, "X")
与拆分前「windows.py 模块内直调」语义完全一致（含本模块内互相调用）。
"""

import ctypes
import json
import os
import threading
import time

from .cdp_client import DEFAULT_PORT
from . import windows as _w

# 企业微信 webhook 机器人仅支持 text / markdown 两种消息类型；其余类型统一回退 text，避免 payload 结构错误
WEBHOOK_MSGTYPES = ("text", "markdown")


def send_webhook(webhook_url, content, msgtype="text"):
    if not webhook_url:
        return {"ok": False, "error": "未配置 webhook（在设置里填入群机器人地址）"}
    if msgtype not in _w.WEBHOOK_MSGTYPES:
        msgtype = "text"  # 白名单外的 msgtype 一律回退 text，防止构造非法 payload
    payload = {"msgtype": msgtype, msgtype: {"content": content}}
    try:
        r = _w.requests.post(webhook_url, json=payload, timeout=10)
        try:
            body = r.json()
        except ValueError:
            return {"ok": False, "error": f"企微返回了无法解析的响应（HTTP {r.status_code}）：{r.text[:200]}"}
        # 企微 webhook 规范：成功返回 errcode=0；其它码返回友好提示
        if isinstance(body, dict) and body.get("errcode") in (0, None):
            return body
        err = (body.get("errmsg") if isinstance(body, dict) else None) or f"HTTP {r.status_code}"
        return {"ok": False, "error": f"企微 webhook 发送失败：{err}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _fetch_target_parents(port=DEFAULT_PORT, timeout=2.0):
    """经 CDP browser 会话一次性查询所有 page target 的 openerId（谁打开了谁），
    用于建立「页面 A 触发页面 B → A 为父、B 为子」的窗口父子关系。
    单次 Target.getTargets(flatten=True) 返回的 TargetInfo 即含 openerId，
    避免逐窗口建连接，开销可控。返回 {targetId: openerId}；
    CDP 不可用/失败返回 None（调用方降级为无父子信息，不破坏旧功能）。"""
    targets = _w.list_targets(port)
    if isinstance(targets, dict) or not _w._pages(targets):
        return None
    try:
        r = _w.requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        r.raise_for_status()
        ws_url = r.json().get("webSocketDebuggerUrl")
    except Exception:  # noqa: BLE001
        return None
    if not ws_url:
        return None
    ws = _w._open_ws(ws_url, timeout)
    if isinstance(ws, dict):  # _open_ws 失败返回 {"ok": False, "error": ...}
        return None
    try:
        ws.send(json.dumps({"id": 1, "method": "Target.getTargets", "params": {"flatten": True}}))
        msg = _w._recv_until(ws, 1)
        if msg is None:
            return None
        infos = msg.get("result", {}).get("targetInfos") or []
        return {t.get("targetId"): t.get("openerId") for t in infos}
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass  # silent-ok: 会话关闭失败不致命，底层连接会自动回收


def window_list_summary(port=DEFAULT_PORT):
    """列出可操作窗口摘要，并附带父子关系。index 与 pick_window/_page_ws 的 pick
    语义完全一致：都基于过滤后的 _pages() 连续索引，避免 CDP /json 存在非 page target
    时 index 错位导致 agent 用 index 作 pick 选错窗口。
    新增字段（向后兼容，旧字段不变）：
      parent   : 父窗口的 index；无父窗口(根窗口)为 -1
      children : 子窗口 index 列表（升序）
    父子关系来自 CDP openerId（一个页面触发打开另一个页面时被打开的页面 openerId
    指向触发它的页面）；CDP 未提供时全部为根窗口，不影响原有功能。"""
    targets = _w.list_targets(port)
    if isinstance(targets, dict):
        return targets
    pages = _w._pages(targets)
    parents = _w._fetch_target_parents(port)
    if not isinstance(parents, dict):
        parents = None
    _ap = _w._get_auto_pick()
    out = []
    for i, t in enumerate(pages):
        out.append({"index": i, "title": (t.get("title") or "")[:60],
                    "url": (t.get("url") or "")[:90], "type": t.get("type"),
                    "parent": -1, "children": [],
                    # v1.0.192：当前跟随窗口标记（点击链接自动跟随的窗口；用于多页面
                    # 流程识别"现在操作的是哪个页面"，模型切回列表页后该标记自然转移）
                    "current": (i == _ap)})
    if _ap is not None and not any(o["current"] for o in out):
        # 跟随窗口已关闭/失效：清掉标记，避免悬空
        _w._set_auto_pick(None)
        for o in out:
            o["current"] = False
    if parents is not None:
        idx_by_tid = {t.get("id"): i for i, t in enumerate(pages) if t.get("id")}
        for i, t in enumerate(pages):
            opener = parents.get(t.get("id")) if t.get("id") else None
            pid = idx_by_tid.get(opener)
            if pid is not None:
                out[i]["parent"] = pid
                out[pid]["children"].append(i)
        for o in out:
            o["children"].sort()
    return out


def window_tree_text(port=DEFAULT_PORT):
    """生成面向 agent 的窗口父子关系摘要文本（无 CDP 父子信息时退化为普通列表）。
    供 agent 启动上下文/工具返回使用，让 agent 一眼看清哪个窗口由哪个窗口触发，
    从而高效规划「进详情 → 回父窗口继续」的跨页流程。"""
    try:
        wins = _w.window_list_summary(port)
    except Exception:  # noqa: BLE001
        return "（获取窗口列表失败）"
    if isinstance(wins, dict):
        return "（CDP 不可用，无窗口列表）"
    if not wins:
        return "（当前无可用企微窗口）"
    by_idx = {w["index"]: w for w in wins}
    child_map = {}
    for w in wins:
        child_map.setdefault(w["parent"], []).append(w["index"])
    for k in child_map:
        child_map[k].sort()
    has_rel = any(w["parent"] >= 0 for w in wins)
    lines = ["企微当前窗口（index | 标题 | 父子关系）："]
    shown = set()

    def _fmt(w):
        rel = f"  由窗口{w['parent']}打开" if w["parent"] >= 0 else "  根窗口"
        kids = child_map.get(w["index"]) or []
        kid_txt = (", ".join(f"{k}" for k in kids)) if kids else ""
        line = f"[{w['index']}] {w['title'][:40]}{rel}"
        if kid_txt:
            line += f"；子窗口={kid_txt}"
        return line

    def _walk(idx, depth):
        """递归渲染整棵父子树，任意深度链路都逐层缩进。
        v1.0.652：visited 防环——openerId 理论无环，但 /json 异常数据/竞态构造出环时
        旧实现无限递归 RecursionError 打断 window_list_summary。"""
        if idx in shown:
            return
        w = by_idx.get(idx)
        if w is None:
            return
        shown.add(idx)
        lines.append("  " * depth + _fmt(w))
        for k in child_map.get(idx) or []:
            _walk(k, depth + 1)

    if has_rel:
        # 有父子关系：先渲染所有根窗口，再按父链递归缩进子窗口
        roots = sorted(w["index"] for w in wins if w["parent"] < 0)
        for ridx in roots:
            _walk(ridx, 0)
        # 孤儿节点（父不在列表/父链断裂）兜底补全，避免遗漏
        for w in wins:
            if w["index"] not in shown:
                lines.append("  " + _fmt(w))
    else:
        for w in wins:
            lines.append(f"[{w['index']}] {w['title'][:40]}")
    lines.append("提示：详情/附件等子窗口由父窗口触发打开，父窗口仍在列表保留；处理完子窗口直接切回父窗口序号继续，比在页面内反复找返回按钮更高效。")
    return "\n".join(lines)


def _stage_guard(fn, timeout=5.0, label="stage"):
    """v1.0.282：把「可能永久阻塞」的 Win32/CDP 诊断调用放进一次性线程 + 超时守卫。
    返回 (status, result)：status ∈ "ok" | "timeout" | "error"。
    背景（2026-09-04 实测）：v1.0.279 的 GetUserObjectInformationW 在 exe（session 1
    交互桌面）内直接崩 libffi-8.dll（WER BEX64, c0000409）→ WER 冻结全进程 → 8787
    所有端点挂起。该 API 已永久移除；本守卫防其余 user32/CDP 调用（如
    GetWindowTextW 跨线程死等挂起窗口属主线程）再把诊断端点或 window-tucker 循环挂死。
    局限：①超时后 worker 线程泄漏（内核调用不可 kill）——诊断端点低频、tucker 每新
    窗口才触发一次，泄漏率≈0，可接受；②崩进程级异常（如 libffi 崩溃）超时救不了，
    只能靠「移除已知危险 API」根治。"""
    import threading as _th
    box = {}

    def _run():
        try:
            box["ok"] = fn()
        except Exception as e:  # noqa: BLE001
            box["err"] = f"{type(e).__name__}: {e}"

    t = _th.Thread(target=_run, name=f"guard-{label}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return "timeout", f"阶段超时 >{timeout:g}s（疑似 Win32 调用挂起/安全软件钩子，worker 线程已泄漏）"
    if "err" in box:
        return "error", box["err"]
    return "ok", box.get("ok")


def _enum_windows(cb, extra_setup=None):
    """v1.0.355：EnumWindows 样板收敛（原 4 处各抄 WNDENUMPROC+argtypes ~80 行）。
    cb(hwnd, u32) 返回 None（异常自动收集不中断枚举）；extra_setup(u32, ctypes, wintypes)
    供调用点追加声明（如 GetWindowThreadProcessId）。返回 (enum_ok, u32, errs)；
    非 Windows 返回 (False, None, [])。"""
    if not _w._WIN32:
        return False, None, []
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        u32.EnumWindows.restype = wintypes.BOOL
        # 64 位下 HWND 是 64 位句柄：显式声明 argtypes，避免 ctypes 默认把
        # Python int 压成 32 位 C int 截断句柄。
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsWindowVisible.restype = wintypes.BOOL
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowTextW.restype = ctypes.c_int
        if extra_setup:
            extra_setup(u32, ctypes, wintypes)
        errs = []

        def _thunk(h, l):
            # 所有路径必须 return True（WNDENUMPROC 的 BOOL 结果，提前 return 会
            # 返回 None → ctypes 转换 TypeError 并中断枚举，实测踩坑）
            try:
                cb(h, u32)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")
            return True

        _ref = WNDENUMPROC(_thunk)  # 持引用防 GC 先回收回调
        ok = bool(u32.EnumWindows(_ref, 0))
        return ok, u32, errs
    except Exception:  # noqa: BLE001
        return False, None, []


def list_os_window_titles():
    """枚举当前桌面可见顶层窗口标题（Windows 专用）。只读，供 window_diag 诊断端点
    比对「CDP 页面标题 vs OS 窗口标题」。
    v1.0.275 返回 dict：{"titles": [...], "error": str|None}——枚举为空或异常时
    error 给出疑似原因（进程不在交互会话 / 安全软件拦截等），便于远程定位。"""
    titles = []

    def _cb(h, u32):
        if u32.IsWindowVisible(h):
            n = u32.GetWindowTextW(h, None, 0)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(h, buf, n + 1)
                if buf.value:
                    titles.append(buf.value)

    ok, u32, errs = _w._enum_windows(_cb)
    if u32 is None:
        return {"titles": [], "error": "非 Windows 平台"}
    err = None
    if errs:
        err = f"回调异常 ×{len(errs)}：{errs[:3]}"
    elif not titles:
        err = ("枚举到 0 个可见窗口——本进程很可能不在交互桌面会话"
               "（Session 0 / 非 Default 桌面），或被安全软件（如火绒）拦截枚举；"
               "对照 process.session_id 与 process.desktop 判断")
    return {"titles": titles, "error": err, "enum_returned": bool(ok)}


def process_session_info():
    """诊断（v1.0.275，只读）：本进程 PID / Windows 会话 ID / 当前线程桌面名。
    os_windows 为空时用它判断：SessionId>0 且 desktop 为 Default（或中文桌面名）
    = 在交互会话（枚举空则怀疑安全软件钩子）；SessionId=0 = 服务会话，
    EnumWindows 天然看不到用户桌面窗口（启动方式问题，需从桌面会话启动 exe）。
    v1.0.279：桌面名曾用 user32!GetUserObjectInformationW(UOI_NAME=2)——
    v1.0.282 永久移除：exe 内（session 1 交互桌面）实测该调用崩 libffi-8.dll
    （2026-09-04 WER BEX64 / c0000409，疑安全软件钩子），WER 随后冻结全进程、
    8787 全部端点挂起。桌面名对诊断无增量价值（EnumWindows 本就枚举当前线程
    桌面），session_id 足以定案，不再取。"""
    info = {"pid": os.getpid()}
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        sid = wintypes.DWORD(0)
        if k32.ProcessIdToSessionId(wintypes.DWORD(info["pid"]), ctypes.byref(sid)):
            info["session_id"] = sid.value
        else:
            info["session_id"] = None
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
    info["desktop"] = "(v1.0.282 起不再获取——GetUserObjectInformationW 在本环境崩进程，已移除)"
    return info


def _title_match(os_title, target_title):
    """OS 窗口标题 与 CDP 页面标题 模糊匹配：归一化后双向包含，较短一方 ≥4 字。
    企微 OS 窗口标题可能与 CDP title 不完全一致（前缀/后缀差异），双向包含兜底。"""
    a = (os_title or "").strip()
    b = (target_title or "").strip()
    if not a or not b:
        return False
    if min(len(a), len(b)) < 4:
        return False
    return a in b or b in a


def new_window_baseline():
    """自动化开页**前**拍一张「企微名下可见窗口」快照（hwnd 集合）。

    与 tuck_new_windows 配对：只有在这之后新出现的窗口才会被收纳，
    用户自己早就开着的窗口永远不在差集里——这是产品红线，也是 v1.0.594
    把 busy 门禁做进 _tuck_fresh_hwnds 的原因。异常返回空集（调用方再降级）。
    """
    try:
        return set(_w._wxwork_visible_hwnds() or [])
    except Exception:  # noqa: BLE001
        return set()


def foreground_hwnd():
    """当前前台窗口 hwnd（字符串），拿不到返回 None。"""
    try:
        import ctypes
        h = ctypes.windll.user32.GetForegroundWindow()
        return str(int(h)) if h else None
    except Exception:  # noqa: BLE001
        return None


def restore_foreground(hwnd):
    """把前台焦点还给指定窗口（best-effort）。

    SetForegroundWindow 受系统"焦点锁"限制（非前台进程常被拒），失败退到
    SwitchToThisWindow——那是系统认可的用户级切换路径。"""
    if not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        h = wintypes.HWND(int(hwnd))
        if not u32.IsWindow(h):
            return False
        if u32.GetForegroundWindow() == h:
            return True
        if u32.SetForegroundWindow(h):
            return True
        u32.SwitchToThisWindow(h, True)
        return True
    except Exception:  # noqa: BLE001
        return False


def minimize_windows_by_title(title, only=None):
    """把标题模糊匹配的可见顶层窗口最小化（Windows 专用，不抢焦点）。
    ShowWindow(SW_MINIMIZE) 只作用于目标窗口，不影响当前前台。
    返回实际最小化的 OS 标题列表（空列表=未找到/非 Windows）。

    v1.0.632（B4）：新增 `only`（hwnd 字符串集合）——按标题匹配原本是**全局**的，
    用户自己开着的同名窗口（同一张 OA 单据开了两个窗口）会被一起最小化。
    静默收纳现在只在"本软件开页动作之后新出现的窗口"里挑，标题路径也不例外。"""
    if not _w._WIN32 or not title:
        return []
    SW_MINIMIZE = 6
    hit = []
    _only = None if only is None else {str(x) for x in only}

    def _cb(h, u32):
        if _only is not None and str(int(h)) not in _only:
            return
        if u32.IsWindowVisible(h):
            n = u32.GetWindowTextW(h, None, 0)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(h, buf, n + 1)
                if _w._title_match(buf.value, title):
                    u32.ShowWindow(h, SW_MINIMIZE)
                    hit.append(buf.value)

    _w._enum_windows(_cb)
    return hit


def _wxwork_visible_hwnds():
    """v1.0.285：枚举属主进程为 WXWork/WXWorkWeb 的可见顶层窗口（不依赖标题）。
    返回 hwnd 字符串列表。用途：GetWindowTextW 被安全软件拦截的环境（本机
    2026-09-04 探针实测：枚举 1130 窗/39 可见/0 可读标题）里，标题匹配必败，
    改走「PID 属主 + 新出现」捕捉新学习窗口。GetWindowThreadProcessId 已经
    window_probe 验证可用。任何异常返回 []（tucker 侧再降级）。"""
    found = []

    def _extra(u32, ctypes, wintypes):
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def _cb(h, u32):
        from ctypes import wintypes
        if u32.IsWindowVisible(h):
            pid = wintypes.DWORD(0)
            u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value in wx_pids:
                found.append(str(int(h)))

    try:
        from .wecom_proc import find_wecom_all_pids
        wx_pids = find_wecom_all_pids()
        if not wx_pids:
            return []
        _w._enum_windows(_cb, _extra)
        return found
    except Exception:  # noqa: BLE001
        return []


def _minimize_hwnds(hwnds):
    """v1.0.285：对给定 hwnd 逐个 ShowWindow(SW_MINIMIZE=6)。返回成功个数。"""
    if not hwnds:
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wintypes.BOOL
        n = 0
        for hs in hwnds:
            if u32.ShowWindow(wintypes.HWND(int(hs)), 6):
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return 0


def _title_path_ok():
    """v1.0.285：探测 GetWindowTextW 是否可用（读任务栏标题，正常应非 0）。
    被安全软件拦截时返回 False → tucker 启动即走 PID 关联路径，日志只说一次。"""
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        u32.GetShellWindow.argtypes = []
        u32.GetShellWindow.restype = wintypes.HWND
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowTextW.restype = ctypes.c_int
        h = u32.GetShellWindow()
        return bool(h and u32.GetWindowTextW(h, None, 0) > 0)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 收纳授权（v1.0.629）
# 产品红线：用户自己在企微里点开的图片/网页窗口，本软件一个都不许最小化。
# v1.0.594 只把门禁做成「agent 任务执行中」——而"任务中"覆盖了几十分钟的整个
# LLM 流（实测 13:08 一次生成 PPT 的思考死循环里，13:11/13:13 用户自己点的 5 个
# 企微窗口被连着收掉），且它压根不看"这窗口是谁开的"。现在收纳唯一的许可来源是
# **本软件自己的开页动作**（open_silent_page / 点击确认产出了新 target）：
#   ① 动作前拍一张企微可见窗口基线；
#   ② 动作确实产出了新页面才登记（基线 + target id + 有效期）；
#   ③ 守护线程只对「登记之后新出现的窗口」动手，且必须先确认本软件真的绑定了企微。
_OPEN_MARK_TTL = 25.0        # 一次开页登记后，多久内出现的新窗口还认作"本软件开的"
_OPEN_MARK_MAX = 8           # 登记队列上限（防异常堆积）
_tuck_lock = threading.Lock()
_open_marks = []             # [{baseline:set, target_id, url, why, until}]
_unbound_warned = {"flag": False}
_bind_probe = {"ts": 0.0, "ok": False}


def note_agent_open(baseline, target_id=None, url="", why=""):
    """登记「这个新企微窗口是本软件自己刚开的」——静默收纳的唯一许可来源。

    baseline 必须是**开页动作之前**拍的企微可见窗口快照（new_window_baseline()）；
    传 None 表示拍快照失败，此时登记照旧但收纳只能走标题路径（PID 差分会拿不到基准，
    宁可不收，也不许把用户已有窗口算进来）。"""
    if baseline is not None and not isinstance(baseline, (set, frozenset, list)):
        return
    with _w._tuck_lock:
        # baseline=None 必须**原样存 None**：它表示"开页前没拍到快照"，此时窗口差分
        # 没有基准，若退化成空集就等于"当前所有企微窗口都是本软件开的"——正是要防的误收。
        _w._open_marks.append({"baseline": None if baseline is None else set(baseline),
                               "target_id": target_id or "",
                               "url": url or "",
                               "why": why or "",
                               "ts": time.monotonic(),   # v1.0.652（审计 P1-15）：登记时刻，供差分兜底的可信时窗判定
                               "until": time.monotonic() + _w._OPEN_MARK_TTL})
        if len(_w._open_marks) > _w._OPEN_MARK_MAX:
            # 与拆分前 `_open_marks = _open_marks[-MAX:]` 等价的就地裁剪（保留末尾 MAX 条）
            del _w._open_marks[:-_w._OPEN_MARK_MAX]


def _pop_open_marks():
    """取走当前仍然有效的开页登记（过期的丢弃）。取出即消耗——同一页不该反复收。"""
    now = time.monotonic()
    with _w._tuck_lock:
        live = [m for m in _w._open_marks if m["until"] > now]
        del _w._open_marks[:]
    return live


def _peek_open_marks():
    now = time.monotonic()
    with _w._tuck_lock:
        return [m for m in _w._open_marks if m["until"] > now]

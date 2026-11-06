# -*- coding: utf-8 -*-
"""save_dialog_auto — 「另存为」对话框自动应答（v1.0.413，下载零交互的终极兜底）

背景：agent 抓取企微网页附件时，企微 Electron 主进程自管下载（CDP 的
Browser.setDownloadBehavior 实测返回成功仍弹原生「另存为」，Network 事件也抓不到
请求 URL——下载走主进程私有通道）。页内 fetch 直传只覆盖 OA KM 标准结构，
OA 页面结构千变万化，点击触发下载的场景永远存在。

方案：后台 Watcher 线程轮询 EnumWindows——发现**企微进程**弹出的「另存为」对话框
（#32770，标题含 另存为/Save As）→ 自动在文件名框填入 产物库/下载/<原文件名>
（保留用户在对话框里已输入/默认的文件名，仅替换目录）→ 模拟点击「保存」。
对话框在用户眼前一闪而过（或来不及显示完），全程零手工交互。

安全边界：
  - 只处理 owner 进程为 WXWork.exe / WeComCompanion.exe 的对话框（不碰用户的
    浏览器/Office 自己的另存为）；
  - 只替换**目录**部分，文件名保留对话框默认值（不猜测、不覆盖已有文件由
    Windows 自身的重名确认兜底）；
  - 开关：config.feature_flags.auto_save_dialogs（默认开）；
  - 每个对话框只处理一次（handle 去重），处理失败不影响对话框本身可手动操作。

用法（backend 启动时）：
    from desktop import save_dialog_auto
    save_dialog_auto.start_daemon()   # 读配置决定是否启用，重复调用安全
"""
import os
import time
import ctypes
import threading
from ctypes import wintypes

from core_utils import log_event, workspace_root

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# v1.0.425：独立原型——close_guard 会全局污染 user32.SendMessageW.argtypes
# 为整数签名（LPARAM=c_ssize_t），我们的 WM_SETTEXT 要传字符串路径必炸。
# 模块加载时即绑定专用指针（WPARAM/LPARAM 用 c_void_p 宽容整数与指针）。
_SendMessageText = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_wchar_p
)(("SendMessageW", _user32))
_SendMessageClick = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)(("SendMessageW", _user32))


def _bind(dll, name, restype, *argtypes):
    """v1.0.604：把本模块用到的每个 Win32 调用都绑成**模块级独立指针**。
    为什么必须这样做：日志里 [save-dialog] watcher 异常共 803 条
    「argument 4: TypeError: 'str'/'c_wchar_p' object cannot be interpreted as an
    integer」——ctypes.windll.user32 是**进程内共享单例**，close_guard 等模块给
    SendMessageW 等全局改 argtypes 后，任何按位置传字符串的调用都会被当成整数槽位
    直接抛（实测 _user32.FindWindowExW(hwnd, None, "Button", cap) 就是这么炸的）。
    独立指针在导入期就固化签名，后续谁改全局原型都影响不到（同 SendMessageW 双指针）。"""
    p = ctypes.WINFUNCTYPE(restype, *argtypes)((name, dll))
    return p


_H = wintypes.HWND
_P = ctypes.c_wchar_p
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, _H, wintypes.LPARAM)   # 枚举回调类型（绑一次，共用）


# v1.0.617（审查 P2）：回调护栏。`@_WNDENUMPROC` 装饰的函数里抛出异常**不会**正常传回
# Python——ctypes 只把它打成 Unraisable 写进（GUI exe 里根本不存在的）stderr，枚举还会中途
# 停住；本模块的后果就是"另存为对话框里的控件找不全"，而现场一条日志都没有。逐窗兜住、
# 记下第一条，由 watcher 每轮排空一次。
_CB_ERRORS = []


def _guard_cb(fn):
    @_WNDENUMPROC
    def _w(hwnd, l):
        try:
            return fn(hwnd, l)
        except Exception as e:  # noqa: BLE001
            if not _CB_ERRORS:
                _CB_ERRORS.append("%s %s: %s" % (getattr(fn, "__name__", "?"), type(e).__name__, str(e)[:120]))
            return True     # 继续枚举：一窗出错不该让整个对话框被判定为"没有"
    return _w


def _drain_cb_errors():
    if _CB_ERRORS:
        log_event(f"[save-dialog] 窗口回调异常（本轮枚举结果可能不完整）：{_CB_ERRORS[0]}")
        del _CB_ERRORS[:]

_IsWindowVisible = _bind(_user32, "IsWindowVisible", ctypes.c_int, _H)
_GetClassNameW = _bind(_user32, "GetClassNameW", ctypes.c_int, _H, _P, ctypes.c_int)
_GetWindowTextW = _bind(_user32, "GetWindowTextW", ctypes.c_int, _H, _P, ctypes.c_int)
_GetWindowTextLengthW = _bind(_user32, "GetWindowTextLengthW", ctypes.c_int, _H)
_EnumWindows = _bind(_user32, "EnumWindows", ctypes.c_int, _WNDENUMPROC, wintypes.LPARAM)
_EnumChildWindows = _bind(_user32, "EnumChildWindows", ctypes.c_int, _H, _WNDENUMPROC, wintypes.LPARAM)
_FindWindowExW = _bind(_user32, "FindWindowExW", ctypes.c_void_p, _H, _H, _P, _P)
_GetWindowRect = _bind(_user32, "GetWindowRect", ctypes.c_int, _H, ctypes.c_void_p)
_GetWindowThreadProcessId = _bind(_user32, "GetWindowThreadProcessId", wintypes.DWORD,
                                  _H, ctypes.POINTER(wintypes.DWORD))
_OpenProcess = _bind(_kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD,
                     ctypes.c_int, wintypes.DWORD)
_QueryFullProcessImageNameW = _bind(_kernel32, "QueryFullProcessImageNameW", ctypes.c_int,
                                    wintypes.HANDLE, wintypes.DWORD, _P,
                                    ctypes.POINTER(wintypes.DWORD))
_CloseHandle = _bind(_kernel32, "CloseHandle", ctypes.c_int, wintypes.HANDLE)

_POLL_INTERVAL = 1.0          # 对话框出现探测间隔（秒；重试 15 次覆盖 15s 渲染窗口）
_HANDLED_TTL = 10 * 60        # 已处理句柄记忆时长（防同句柄重复点）
_MAX_TEXT = 512

_thread = None
_handled = {}                 # hwnd -> epoch（本次会话内处理过的对话框）
_fails = {}                   # hwnd -> 连续失败次数（渲染等待重试用）
_pending_name = {"v": None}   # v1.0.425：下一个保存框应使用的文件名（调用方 set_next_filename 注入；
                              # 控件树实测文件名框跨进程读文本为空，故不再依赖读框取默认名）


def set_next_filename(name):
    """设置下一次另存为对话框自动应答使用的文件名（batch_flow 附件下载前调用）。"""
    _pending_name["v"] = str(name or "").strip() or None
_last_err = {"n": 0}


def _is_wecom_dialog(hwnd):
    """对话框是否属于企微进程（WXWork.exe / WXWorkWeb.exe）。"""
    pid = wintypes.DWORD()
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid.value)
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if not _QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return False
        name = (buf.value or "").rsplit("\\", 1)[-1].lower()
        return name in ("wxwork.exe", "wxworkweb.exe", "wecomcompanion.exe")
    finally:
        _CloseHandle(h)


def _enum_wecom_save_dialogs():
    """枚举所有「另存为」对话框句柄（标题匹配 + 企微进程）。"""
    out = []

    @_guard_cb
    def _cb(hwnd, _l):
        # 只看可见的顶级对话框
        if not _IsWindowVisible(hwnd):
            return True
        # 类名 #32770 = 标准对话框
        cls = ctypes.create_unicode_buffer(64)
        _GetClassNameW(hwnd, cls, 64)
        if cls.value != "#32770":
            return True
        title = ctypes.create_unicode_buffer(_MAX_TEXT)
        _GetWindowTextW(hwnd, title, _MAX_TEXT)
        t = title.value or ""
        # v1.0.425（用户反馈弹框仍在）：放宽为企微进程的所有 #32770 对话框——
        # 企微的下载框标题未必含"另存为"（实测可能是自定义标题），标题只作记录。
        if not _is_wecom_dialog(hwnd):
            return True
        if t.strip() in ("", "任务对话", "TaskDialog"):
            return True   # 无标题的通用容器跳过（避免误触主窗体内的伪对话框）
        out.append((hwnd, t))
        return True

    _EnumWindows(_cb, 0)
    return out


def _find_save_button(hwnd):
    """在对话框里找「保存」按钮（含 (&S) 变体）。"""
    for cap in ("保存(&S)", "保存(S)", "保存(&S)(&S)", "保存", "Save(&S)", "Save"):
        btn = _FindWindowExW(hwnd, None, "Button", cap)
        if btn:
            return btn
    return None


def _find_filename_edit(hwnd):
    """找文件名输入框：对话框里宽度最大的 Edit 类控件（标准另存为的文件名框）。"""
    best = [None, 0]

    @_guard_cb
    def _cb(child, _l):
        cls = ctypes.create_unicode_buffer(64)
        _GetClassNameW(child, cls, 64)
        if cls.value not in ("Edit", "RichEdit20W"):
            return True
        rect = wintypes.RECT()
        if _GetWindowRect(child, ctypes.byref(rect)) and (rect.right - rect.left) > best[1]:
            best[0] = child
            best[1] = rect.right - rect.left
        return True

    _EnumChildWindows(hwnd, _cb, 0)
    return best[0]


def _read_edit_text(edit):
    """读文件名框当前文本（取默认文件名）。"""
    n = _GetWindowTextLengthW(edit)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _GetWindowTextW(edit, buf, n + 1)
    return buf.value or ""


def _dump_dialog_controls(hwnd):
    """诊断：枚举对话框全部子控件（类名/宽度/文本前 30），现场识别文件名框。"""
    out = []

    @_guard_cb
    def _cb(child, _l):
        cls = ctypes.create_unicode_buffer(64)
        _GetClassNameW(child, cls, 64)
        rect = wintypes.RECT()
        w = 0
        if _GetWindowRect(child, ctypes.byref(rect)):
            w = rect.right - rect.left
        txt = ctypes.create_unicode_buffer(64)
        _GetWindowTextW(child, txt, 64)
        out.append(f"{cls.value}({w}px):{txt.value[:30]}")
        return True

    _EnumChildWindows(hwnd, _cb, 0)
    return " | ".join(out[:12])


def _click_yes_button(hwnd):
    """v1.0.425：「确认另存为」覆盖确认框自动点「是(Y)」（审计重下同名附件时
    Windows 必弹；点否=放弃下载）。
    v1.0.470 修复（P1-15）：收窄匹配——原实现对企微进程任何含「是」的按钮 BM_CLICK
    （设置确认框/登录框/任意确认框都会被误点=静默批准）。现要求标准覆盖确认框形态：
    对话框同时存在「是」与「否」两个按钮且文本精确匹配（是/是(&Y)/是(Y)/&Yes 等）。
    「确定/取消」结构的普通确认框不再命中。"""
    _YES = {"是(&y)", "是(y)", "是", "&yes", "yes"}
    _NO = {"否(&n)", "否(n)", "否", "&no", "no"}
    buttons = []   # (hwnd, 文本 lower)

    @_guard_cb
    def _cb(child, _l):
        cls = ctypes.create_unicode_buffer(64)
        _GetClassNameW(child, cls, 64)
        if cls.value != "Button":
            return True
        n = _GetWindowTextLengthW(child)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _GetWindowTextW(child, buf, n + 1)
        buttons.append((child, buf.value.strip().lower()))
        return True

    _EnumChildWindows(hwnd, _cb, 0)
    texts = {t for _, t in buttons}
    has_yes = any(t in _YES for t in texts)
    has_no = any(t in _NO for t in texts)
    if not (has_yes and has_no):
        return False   # 非标准是/否确认框，绝不代点
    for bh, t in buttons:
        if t in _YES:
            _SendMessageClick(bh, 0x00F5, 0, 0)   # BM_CLICK
            return True
    return False


def _backup_existing(path):
    """v1.0.470（P1-15）：同名文件被覆盖前先把旧文件改名为 <名>.bak_<ts><扩展名>——
    自动点「保存/是」导致的覆盖不再销毁旧数据。返回备份路径或 None。"""
    try:
        if os.path.isfile(path):
            b, ext = os.path.splitext(path)
            dst = f"{b}.bak_{int(time.time())}{ext}"
            os.rename(path, dst)
            return dst
    except Exception:  # noqa: BLE001
        pass
    return None


def _handle_dialog(hwnd, target_dir):
    """自动应答一个另存为对话框：目录替换为产物库下载目录 + 点保存。返回 (ok, note)。"""
    edit = _find_filename_edit(hwnd)
    if not edit:
        # v1.0.470（P1-15）：覆盖确认框点「是」前先备份旧文件——覆盖不再销毁旧数据；
        # 且 _click_yes_button 已收窄为标准是/否确认框形态
        pending = _pending_name.get("v")
        if pending:
            _backup_existing(os.path.join(target_dir, os.path.basename(pending)))
        if _click_yes_button(hwnd):
            return True, "覆盖确认已自动点「是」（旧文件已备份为 .bak_*）"
        return False, "未找到文件名输入框"
    cur = _read_edit_text(edit).strip()
    pending = _pending_name.get("v")
    if not cur and not pending:
        return False, "文件名框为空且无预置文件名（对话框未就绪）"
    # v1.0.425：文件名优先用调用方预置（正文正则提取的附件名）——控件树实测
    # DirectUI 对话框的文件名框跨进程 GetWindowText 读不到默认值；框里有值则
    # 保留其文件名部分（兼容手动场景）。
    fname = os.path.basename(cur.replace("/", "\\")) if cur else (pending or "")
    fname = os.path.basename(fname.replace("/", "\\"))
    if not fname or fname.lower().startswith("c:"):
        fname = "download_%d.bin" % int(time.time())
    full = os.path.join(target_dir, fname)
    os.makedirs(target_dir, exist_ok=True)
    # v1.0.470（P1-15）：目标同名旧文件先备份——顺带让 Windows 的覆盖确认框
    # 不再弹出（文件已不存在），整条下载链路无覆盖丢数据窗口
    _backup_existing(full)
    WM_SETTEXT = 0x000C
    # v1.0.425：用模块级独立指针（close_guard 全局污染 user32 原型为整数签名）
    _SendMessageText(edit, WM_SETTEXT, 0, full)
    btn = _find_save_button(hwnd)
    if not btn:
        return False, "未找到保存按钮"
    BM_CLICK = 0x00F5
    _SendMessageClick(btn, BM_CLICK, 0, 0)
    _pending_name["v"] = None   # 一次性：防下一个无关弹框误用旧文件名
    return True, fname


def _loop(target_dir):
    # v1.0.653 瘦身：_stop Event 删除——全仓无任何 .set()（线程即 daemon 随进程退），
    # `while not _stop.is_set()` 恒等于 while True，等待改 sleep（省一个死开关）。
    while True:
        try:
            now = time.time()
            _dialogs = _enum_wecom_save_dialogs()
            _drain_cb_errors()      # 枚举回调里被 ctypes 吞掉的异常在这里现形（否则永远看不见）
            for hwnd, title in _dialogs:
                if now - _handled.get(hwnd, 0) < _HANDLED_TTL:
                    continue
                ok, note = _handle_dialog(hwnd, target_dir)
                if ok:
                    _handled[hwnd] = now
                    log_event(f"[save-dialog] 已自动应答对话框「{title[:20]}」→ 产物库/下载/{note}")
                else:
                    # v1.0.425：失败不标记——0.6s 后重试（对话框渲染中文件名框暂空
                    # 是主因）；同 hwnd 失败 8 次才放弃（避免对永久异常窗口死循环）
                    _fails[hwnd] = _fails.get(hwnd, 0) + 1
                    if _fails[hwnd] == 1:
                        log_event(f"[save-dialog] 检测到企微对话框「{title[:24]}」（{note}），将自动重试")
                    if _fails[hwnd] == 2:
                        # 诊断：dump 对话框控件树——现场识别真实文件名框的类名/ID
                        try:
                            tree = _dump_dialog_controls(hwnd)
                            log_event(f"[save-dialog] 控件树：{tree[:400]}")
                        except Exception:  # noqa: BLE001
                            pass
                    if _fails[hwnd] >= 15:
                        _handled[hwnd] = now
                        _fails.pop(hwnd, None)
                        _last_err["n"] += 1
                        if _last_err["n"] <= 3:
                            log_event(f"[save-dialog] 重试 15 次仍未成功（{note}）——放弃，可手动操作")
            # 清理过期句柄记忆
            if len(_handled) > 64:
                cut = now - _HANDLED_TTL
                for k in [k for k, v in _handled.items() if v < cut]:
                    _handled.pop(k, None)
                for k in [k for k, v in _fails.items() if k not in _handled and v >= 8]:
                    _fails.pop(k, None)
        except Exception as e:  # noqa: BLE001
            # v1.0.604：原只记 str(e)——803 条「argument 4 TypeError」刷了几个月，
            # 栈信息丢了没人知道炸在哪个调用。现带 traceback 尾部，一次定位。
            import traceback
            _tb = traceback.format_exc()[-240:].replace(chr(10), " ← ")
            log_event("[save-dialog] watcher 异常（继续轮询）：%s | %s" % (str(e)[:80], _tb))
        time.sleep(_POLL_INTERVAL)


def start_daemon(target_dir=None, enabled=True):
    """启动另存为自动应答线程（backend 启动时调用一次；重复调用安全）。
    enabled=False（config.feature_flags.auto_save_dialogs=false）时仅记录不启动。"""
    global _thread
    if not enabled:
        return
    if _thread and _thread.is_alive():
        return
    if target_dir is None:
        target_dir = os.path.join(workspace_root(), "下载")
    os.makedirs(target_dir, exist_ok=True)
    _thread = threading.Thread(target=_loop, args=(target_dir,), daemon=True, name="save-dialog-auto")
    _thread.start()
    log_event("[save-dialog] 另存为自动应答已启用（企微弹出的保存框自动存入 产物库/下载）")



# -*- coding: utf-8 -*-
"""win_send — 企微主窗口发消息（OS 级键盘自动化）。

v1.0.468（④ send_wecom_message 底座）：企微主聊天窗口不在 CDP target 里
（/json/list 只有内嵌浏览器页），只能 OS 级驱动：找主窗口 → 前台化 →
Ctrl+F 搜索 → 输入对象 → Enter 开聊天 → 输入文本 → Enter 发送。

前置条件：企微主窗口必须在桌面上可见（最小化到托盘时先由调用方提示用户点开）。
window.close 的同源经验：脚本只能动"允许动"的窗口；SetForegroundWindow 失败时
返回结构化错误，绝不盲打键盘（防输入串到别的窗口）。
"""
import ctypes
import subprocess
import time
from ctypes import wintypes

from core_utils import log_event   # v1.0.617：窗口枚举异常必须留痕（GUI exe 里 Unraisable 没人看得见）

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_CONTROL = 0x11
_VK_RETURN = 0x0D
_VK_SHIFT = 0x10
_KEYF = 0x46
_SW_RESTORE = 9     # ShowWindow 命令码（此前这里写字面量 9，同文件其余调用已用命名常量）


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_key(vk):
    _press(_INPUT_KEYBOARD, wintypes.WORD(vk), 0)
    _press(_INPUT_KEYBOARD, wintypes.WORD(vk), _KEYEVENTF_KEYUP)


def _combo_ctrl(vk):
    """v1.0.470 修复（P0-5）：真正的 Ctrl+vk 组合键序列。
    原实现 _send_key(_VK_CONTROL) + _type_text("f") —— Ctrl 在打字前已弹起，
    实际输入的是裸字符 "f"，搜索快捷键从未触发（搜索框收到 f 前缀污染的查询词）。"""
    _press(_INPUT_KEYBOARD, wintypes.WORD(_VK_CONTROL), 0)          # Ctrl 按下
    _press(_INPUT_KEYBOARD, wintypes.WORD(vk), 0)                   # vk 按下
    _press(_INPUT_KEYBOARD, wintypes.WORD(vk), _KEYEVENTF_KEYUP)    # vk 释放
    _press(_INPUT_KEYBOARD, wintypes.WORD(_VK_CONTROL), _KEYEVENTF_KEYUP)  # Ctrl 释放


def _shift_enter():
    """Shift+Enter：聊天输入框内软换行（不发送）。
    v1.0.652（审计 P1-12）：多行消息行间插入——KEYEVENTF_UNICODE 注入 \\n 会被企微
    当 Enter 提前发送/截断，Shift+Enter 才是"消息内换行"的正确键序。"""
    _press(_INPUT_KEYBOARD, wintypes.WORD(_VK_SHIFT), 0)
    _press(_INPUT_KEYBOARD, wintypes.WORD(_VK_RETURN), 0)
    _press(_INPUT_KEYBOARD, wintypes.WORD(_VK_RETURN), _KEYEVENTF_KEYUP)
    _press(_INPUT_KEYBOARD, wintypes.WORD(_VK_SHIFT), _KEYEVENTF_KEYUP)


def _press(itype, w, flags):
    inp = _INPUT(type=itype)
    inp.u.ki = _KEYBDINPUT(wVk=w, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _type_text(text):
    """Unicode 逐字符输入（KEYEVENTF_UNICODE，免中文输入法依赖）。"""
    for ch in text:
        code = ord(ch)
        _press(_INPUT_KEYBOARD, wintypes.WORD(code), _KEYEVENTF_UNICODE)
        _press(_INPUT_KEYBOARD, wintypes.WORD(code), _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)


def _wecom_pids():
    # v1.0.470：加 CREATE_NO_WINDOW（console=False 的 exe 里每次发消息都闪黑框）+
    # 异常兜底（原 timeout 超时抛 TimeoutExpired 直接逃逸出 send_wecom_message）。
    try:
        out = subprocess.run(["tasklist", "/fo", "csv"], capture_output=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             ).stdout.decode("gbk", "replace")
    except Exception:  # noqa: BLE001
        return set()
    pids = set()
    for ln in out.splitlines():
        parts = [p.strip('"') for p in ln.split('","')]
        if len(parts) >= 2 and parts[0] in ("WXWork.exe", "WXWorkWeb.exe"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def _focus_belongs_to(hwnd):
    """v1.0.470（P0-5）焦点校验：当前键盘焦点控件是否属于 hwnd（或其子窗口）。
    用于确认 Ctrl+F 后搜索框真正获得焦点 / 输入不会打到别的窗口。"""
    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    tid = _user32.GetWindowThreadProcessId(hwnd, None)
    if not _user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return False
    h = gti.hwndFocus or gti.hwndActive
    if not h:
        return False
    # 焦点控件必须就是 hwnd 本身或其后代
    walk, hops = h, 0
    while walk and hops < 16:
        if walk == hwnd:
            return True
        walk = _user32.GetParent(walk)
        hops += 1
    return False


def _find_main_window():
    """找企微主窗口：WXWork 进程 + 可见 + 标题含「企业微信」或最大可见窗。返回 hwnd 或 0。"""
    pids = _wecom_pids()
    best, best_area = 0, 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        # 同 os_ops：回调里抛异常在 GUI exe 里变成 Unraisable（无人看见），枚举还可能中途
        # 停住，症状就是"找不到企微主窗口"却没有任何原因。逐窗兜住，异常只记第一条。
        nonlocal best, best_area, cb_err
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            title = ctypes.create_unicode_buffer(128)
            _user32.GetWindowTextW(hwnd, title, 128)
            r = wintypes.RECT()
            if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if "企业微信" in title.value:
                best, best_area = hwnd, 1 << 60   # 标题精确命中=最高优先
                return True
            if area > best_area and area > 100000:   # 备选：最大的可见企微窗
                best, best_area = hwnd, area
            return True
        except Exception as e:  # noqa: BLE001
            if cb_err is None:
                cb_err = f"{type(e).__name__}: {e}"
            return True

    cb_err = None
    try:
        _user32.EnumWindows(cb, 0)
    except Exception as e:  # noqa: BLE001
        cb_err = cb_err or f"EnumWindows {type(e).__name__}: {e}"
    if cb_err:
        try:
            log_event(f"[win_send] 枚举企微主窗口异常（返回 0 由调用方判未绑定）：{cb_err}")
        except Exception:  # noqa: BLE001
            pass
    return best


def _foreground_is(hwnd):
    return _user32.GetForegroundWindow() == hwnd


def send_wecom_message(target, text, port=None):
    """向企微联系人/群发送消息。返回 {"ok": True} 或 {"ok": False, "error": ...}。
    步骤：找主窗口 → 前台化 → Ctrl+F → 输入对象 → Enter 开聊天 → 输入文本 → Enter。
    前置：企微主窗口在桌面上可见（最小化到托盘时报错提示用户点开）。
    v1.0.523 静默模式：OS 级 SendInput 结构性需要前台（主窗口不在 CDP target，
    无法后台化）——执行前等用户空闲（键盘/鼠标停 2s，最多等 120s）；等不到
    返回结构化错误让 agent 决定改用 webhook（零抢占）或稍后重试，
    绝不在用户打字时抢前台劫持键盘。"""
    if not target or not text:
        return {"ok": False, "error": "target 与 text 均不能为空"}
    try:
        from core.config_store import silent_mode
        if silent_mode():
            from core.idle import wait_user_idle
            if not wait_user_idle(max_wait_s=120.0, poll_s=3.0):
                return {"ok": False, "error": "用户正在使用电脑（键盘/鼠标持续活跃），静默模式已取消本次发送——"
                                     "可改用 webhook 通知（零前台抢占）或稍后重试"}
    except Exception:  # noqa: BLE001
        pass
    hwnd = _find_main_window()
    if not hwnd:
        return {"ok": False, "error": "未找到企微主窗口（可能最小化到托盘）——请点开企微主界面后重试"}
    # 前台化（最小化则先还原）
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.5)
    # 绕前台锁：先发一个 ALT 键再 SetForegroundWindow（Windows 经典技巧）
    _user32.keybd_event(0x12, 0, 0, 0)
    _user32.SetForegroundWindow(hwnd)
    _user32.keybd_event(0x12, 0, 2, 0)
    time.sleep(0.5)
    if not _foreground_is(hwnd):
        return {"ok": False, "error": "企微主窗口无法置于前台（用户正在操作其他窗口）——稍后重试"}
    # Ctrl+F 聚焦搜索（v1.0.470：真组合键序列，原实现 Ctrl 先弹起、"f" 成裸字符输入）
    _combo_ctrl(_KEYF)
    time.sleep(0.4)
    # v1.0.470（P0-5）：搜索框焦点校验——焦点没落在企微窗口内说明快捷键未生效，
    # 此时继续输入会把对象名打进无关焦点控件，必须中止而不是继续假成功。
    if not _focus_belongs_to(hwnd):
        return {"ok": False, "error": "Ctrl+F 后焦点未落在企微窗口（搜索框未聚焦）——请确认企微主界面可交互后重试"}
    # 兼容：Ctrl+F 若未聚焦搜索框，Esc 后直接打字（企微主界面默认搜索快捷键亦生效）
    _type_text(str(target))
    time.sleep(1.4)
    _send_key(_VK_RETURN)
    time.sleep(1.6)
    if not _foreground_is(hwnd):
        return {"ok": False, "error": "打开会话后窗口失去前台——稍后重试"}
    # v1.0.470（P0-5）：Enter 后焦点仍须在企微窗口内（搜索无结果时 Enter 不会打开
    # 会话，后续文本会打进搜索框——原实现此时仍返回 ok，属假成功）。
    if not _focus_belongs_to(hwnd):
        return {"ok": False, "error": f"未找到会话对象「{target}」（搜索无结果或未打开聊天窗口）——请核对名称与企微里的显示名一致"}
    # v1.0.652（审计 P1-12）：多行文本逐行键入、行间 Shift+Enter 软换行。
    # 旧实现把 \n 当 Unicode 字符注入——企微当 Enter 提前发送/截断成多条，
    # 且照样返回 ok:true（消息完整性静默被破坏，agent 无从纠正）。
    _lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for _li, _line in enumerate(_lines):
        if _li:
            _shift_enter()
            time.sleep(0.15)
        if _line:
            _type_text(_line)
    time.sleep(0.4)
    _send_key(_VK_RETURN)
    time.sleep(0.4)
    # v1.0.470（P0-5）：发送后再校验一次前台——输入过程中用户切走窗口时不得报成功。
    if not _foreground_is(hwnd):
        return {"ok": False, "error": "发送时窗口失去前台，消息可能未发出——请重新发送"}
    return {"ok": True, "target": str(target), "sent_len": len(str(text))}

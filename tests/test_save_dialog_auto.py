# -*- coding: utf-8 -*-
"""tests/test_save_dialog_auto.py — v1.0.413 另存为自动应答单元测试（纯逻辑，不碰真窗）。

用假 hwnd 对象 mock Win32 交互面（EnumWindows/FindWindowEx/SendMessage），验证：
①文件名保留+目录替换正确；②企微进程过滤；③空文件名框跳过；④去重不重复处理。

v1.0.604：生产代码把每个 Win32 调用绑成了模块级独立指针（_IsWindowVisible 等），
原因是 ctypes.windll.user32 是进程内共享单例，close_guard 等模块全局改 argtypes 后
按位置传字符串的调用会炸——日志里 803 条「argument 4 TypeError」即此。
测试桩因此改为**逐个替换模块指针名**（与本模块调用点一一对应），不再 patch _user32 整体。
"""
import os
import sys
import ctypes
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop import save_dialog_auto as sda

_U32_NAMES = ["IsWindowVisible", "GetClassNameW", "GetWindowTextW", "GetWindowTextLengthW",
              "EnumWindows", "EnumChildWindows", "FindWindowExW", "GetWindowRect",
              "GetWindowThreadProcessId"]
_K32_NAMES = ["OpenProcess", "QueryFullProcessImageNameW", "CloseHandle"]


class _FakeWin:
    """假 user32。dialogs: {hwnd: {"title","cls","pid","children"}}"""

    def __init__(self, dialogs, edits, buttons):
        self.dialogs = dialogs
        self.edits = edits          # hwnd -> text
        self.buttons = buttons      # hwnd -> click count
        self.sent = []

    def EnumWindows(self, cb, _l):
        for hwnd, d in self.dialogs.items():
            cb(hwnd, 0)
        return True

    def EnumChildWindows(self, hwnd, cb, _l):
        for c in self.dialogs.get(hwnd, {}).get("children", []):
            cb(c, 0)
        return True

    def IsWindowVisible(self, hwnd):
        return True

    def GetClassNameW(self, hwnd, buf, n):
        d = self.dialogs.get(hwnd)
        buf.value = d.get("cls", "") if d else "Edit"
        return n

    def GetWindowTextW(self, hwnd, buf, n):
        d = self.dialogs.get(hwnd)
        buf.value = d.get("title", "") if d else self.edits.get(hwnd, "")
        return n

    def GetWindowTextLengthW(self, hwnd):
        return len(self.edits.get(hwnd, ""))

    def GetWindowRect(self, hwnd, rect_ref):
        # rect_ref 是 byref(RECT) 的 CArgObject——cast 取回原始对象再写
        obj = ctypes.cast(rect_ref, ctypes.POINTER(wintypes.RECT)).contents
        obj.right, obj.left = 400, 0
        return True

    def GetWindowThreadProcessId(self, hwnd, pid):
        # 生产代码传的是 ctypes.byref(DWORD)（CArgObject），真句柄才要解引用
        getattr(pid, "_obj", pid).value = self.dialogs.get(hwnd, {}).get("pid", 0)
        return 1

    def FindWindowExW(self, hwnd, after, cls, cap):
        for c in self.dialogs.get(hwnd, {}).get("children", []):
            if self.buttons.get(c) is not None and cap in ("保存(&S)", "保存"):
                return c
        return None

    def SendMessageW(self, hwnd, msg, w, l):
        self.sent.append((hwnd, msg, l))
        return 1


class _FakeK32:
    """pid → 进程名。OpenProcess 把 pid 当句柄回传，Query 再按句柄取真实进程名，
    这样「非企微进程」才真的走到名字判断那一步（旧桩恒返回 wxwork.exe，等于没测）。"""

    def __init__(self, pid_names):
        self.pid_names = pid_names

    def OpenProcess(self, a, b, pid):
        return pid if pid in self.pid_names else 0

    def QueryFullProcessImageNameW(self, h, flag, buf, size):
        getattr(buf, "_obj", buf).value = "C:\\x\\" + self.pid_names.get(h, "chrome.exe")
        return True

    def CloseHandle(self, h):
        return True


def _patch(monkeypatch, fake, k32):
    """把假实现绑到生产代码实际调用的模块级指针名上。"""
    for n in _U32_NAMES:
        monkeypatch.setattr(sda, "_" + n, getattr(fake, n))
    for n in _K32_NAMES:
        monkeypatch.setattr(sda, "_" + n, getattr(k32, n))
    monkeypatch.setattr(sda, "_SendMessageText",
                        lambda hwnd, msg, wp, lp: fake.sent.append((hwnd, msg, lp)) or 1)
    monkeypatch.setattr(sda, "_SendMessageClick",
                        lambda hwnd, msg, wp, lp: fake.sent.append((hwnd, msg, lp)) or 1)


def test_fill_and_click(monkeypatch):
    """目录替换 + 文件名保留 + 点保存。"""
    edit_h, btn_h, dlg_h = 100, 200, 300
    fake = _FakeWin(
        dialogs={dlg_h: {"title": "另存为", "cls": "#32770", "pid": 1,
                         "children": [edit_h, btn_h]}},
        edits={edit_h: "C:\\Users\\x\\Downloads\\设备清单2026.8.31.xlsx"},
        buttons={btn_h: 0},
    )
    _patch(monkeypatch, fake, _FakeK32({1: "wxwork.exe"}))
    ok, note = sda._handle_dialog(dlg_h, os.path.abspath("tests/_dl_out"))
    assert ok, note
    assert note == "设备清单2026.8.31.xlsx", note   # 文件名保留
    assert fake.sent, "未点击保存"
    # 写入的完整路径含产物库目录 + 原文件名（WM_SETTEXT=0x000C 的 lParam 是路径）
    settext = [(l.value if hasattr(l, "value") else str(l)) for (_h, msg, l) in fake.sent if msg == 0x000C]
    assert settext, "没有 WM_SETTEXT 写入: %s" % (fake.sent,)
    assert "tests" in settext[0] and "_dl_out" in settext[0] and note in settext[0], settext[0]


def test_non_wecom_dialog_ignored(monkeypatch):
    """非企微进程的另存为不处理。"""
    dlg_h, edit_h = 301, 101
    fake = _FakeWin(
        dialogs={dlg_h: {"title": "另存为", "cls": "#32770", "pid": 2, "children": [edit_h]}},
        edits={edit_h: "x.xlsx"},
        buttons={},
    )
    _patch(monkeypatch, fake, _FakeK32({1: "wxwork.exe", 2: "chrome.exe"}))
    assert sda._enum_wecom_save_dialogs() == [], "非企微对话框不应被枚举"


def test_empty_edit_skipped(monkeypatch):
    """文件名框为空（对话框未就绪）→ 跳过不写。"""
    dlg_h, edit_h, btn_h = 302, 102, 202
    fake = _FakeWin(
        dialogs={dlg_h: {"title": "另存为", "cls": "#32770", "pid": 1,
                         "children": [edit_h, btn_h]}},
        edits={edit_h: ""},
        buttons={btn_h: 0},
    )
    _patch(monkeypatch, fake, _FakeK32({1: "wxwork.exe"}))
    ok, note = sda._handle_dialog(dlg_h, ".")
    assert not ok and "为空" in note, (ok, note)
    assert not fake.sent, "空框不应写路径"


def test_pointers_are_module_bound():
    """回归锁：Win32 调用必须走模块级独立指针，不得退回 ctypes.windll 全局对象——
    全局 user32 的 argtypes 会被 close_guard 改写（实测 803 条 watcher 异常）。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "desktop", "save_dialog_auto.py"), encoding="utf-8").read()
    body = src.split("def _is_wecom_dialog", 1)[1]
    for name in _U32_NAMES + _K32_NAMES:
        assert "_user32.%s(" % name not in body, "仍在裸用 _user32.%s" % name
        assert "_kernel32.%s(" % name not in body, "仍在裸用 _kernel32.%s" % name
    assert "_WNDENUMPROC" in src and "_bind(" in src, "指针绑定结构被拆了"

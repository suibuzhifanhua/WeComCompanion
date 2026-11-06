"""
close_guard.py — 拦截主窗口 X 关闭，弹「最小化到托盘 / 退出程序」选择
====================================================================
pywebview(EdgeChromium/WinForms) 的 X 关闭会直接销毁窗口并让 webview.start() 返回、
进程退出，没有公开的"取消关闭"钩子。这里在 Windows 上用 ctypes 子类化主窗口的
WNDPROC，只拦截 WM_CLOSE：
  - 选「最小化到托盘」 → ShowWindow(SW_HIDE) 隐藏窗口并吞掉 WM_CLOSE（进程常驻托盘）
  - 选「退出程序」      → 把 WM_CLOSE 转发给原 WinForms 处理程序，正常关闭→进程退出
除 WM_CLOSE 外的所有消息一律转发给"原来的" WNDPROC（WinForms 托管处理程序，
而不是 DefWindowProcW），从而不会破坏 WinForms 自身的消息处理。

自定义按钮的关闭选择对话框是纯 Win32 自绘的（CreateWindowEx + 内置 BUTTON/STATIC 类，
无 comctl32 v6 manifest 依赖；按钮文字完全自定义：最小化到托盘 / 退出程序）。
字体用 GetStockObject(DEFAULT_GUI_FONT)（微软系统默认 GUI 字体），与资源管理器一致。

【关键防御】WM_CLOSE 回调内任何异常都不允许导致窗口直接关闭：
  - _ask_close_choice 全链路 try/except，任何失败降级 MessageBoxW（文字"是/否"）
  - MessageBoxW 也失败 → 返回 True（最小化到托盘），进程不退出
  - proc 外层再包一层 try，未捕获异常默认最小化（不转发 WM_CLOSE）
历史教训：
  - v1.0.181 曾因 WM_CREATE 内 SendMessageW(WM_SETFONT) 未包 try，
    CreateWindowExW 创建窗口时回调异常未被捕获，proc 异常后 WM_CLOSE 被默认处理直接关窗。
  - v1.0.276 实测：选「最小化到托盘」后窗口隐藏成功但进程秒退——根因是
    经典嵌套循环用 PostQuitMessage(0) 退出，而 pywebview 6.x 主循环是【无参
    Application.Run()】，对 quit 状态敏感，外层 .NET 泵退出 → webview.start()
    返回 → backend 进程退出。v1.0.277 起对话框改 PeekMessageW 非阻塞泵 +
    MsgWaitForMultipleObjects 超时轮询 choice，全程零 WM_QUIT/PostQuitMessage。
"""
import ctypes
import os
import time

user32 = ctypes.windll.user32
# GetStockObject 属于 gdi32.dll（user32 里没有！v1.0.181 误用 user32.GetStockObject
# 导致 _setup_prototypes 抛 AttributeError → install 失败 → 点 X 直接关窗）
gdi32 = ctypes.windll.gdi32

# 消息
WM_CLOSE = 0x0010
WM_CREATE = 0x0001
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_KEYDOWN = 0x0100
WM_SETFONT = 0x0030
# 通用窗口样式
GWLP_WNDPROC = -4
SW_HIDE = 0
SW_SHOW = 5
DEFAULT_GUI_FONT = 13  # GetStockObject(13) 微软系统默认 GUI 字体
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
WS_GROUP = 0x00020000
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_TOPMOST = 0x00000008
# 按钮样式
BS_PUSHBUTTON = 0x00000000
BS_DEFPUSHBUTTON = 0x00000001
BS_AUTOCHECKBOX = 0x00000003   # v1.0.556「下次不再提示」复选框样式（漏定义曾致 WM_CREATE 抛 NameError）
# 静态文本样式
SS_LEFT = 0x00000000
SS_NOPREFIX = 0x00000080
# 系统颜色/键
COLOR_3DFACE = 15
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
# 消息等待掩码（MsgWaitForMultipleObjects：等待任意输入事件）
QS_ALLINPUT = 0x04FF
# DrawTextW 标志
DT_WORDBREAK = 0x0010
DT_CALCRECT = 0x0400
DT_NOPREFIX = 0x0800
# CreateFontW
FW_NORMAL = 400
FW_BOLD = 700
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5
# 控件 ID
IDC_BTN_MINIMIZE = 101
IDC_BTN_EXIT = 102
IDC_CHK_REMEMBER = 103        # v1.0.556：「下次不再提示」复选框
BM_GETCHECK = 0x00F0          # 按钮消息：读复选框状态
BST_CHECKED = 0x0001
# 窗口类名
_DIALOG_CLASS = "WeComCloseDialog"
_BTN_CLASS = "BUTTON"
_STATIC_CLASS = "STATIC"
# MessageBoxW（降级兜底）
MB_YESNO = 0x0004
MB_ICONQUESTION = 0x0020
IDYES = 6
IDNO = 7


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_size_t, ctypes.c_ssize_t)


# 防 GC（ctypes 回调必须长期存活）
_CBS = []


def _setup_prototypes():
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
    user32.SetActiveWindow.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.c_int
    user32.EnableWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.EnableWindow.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.TranslateMessage.restype = ctypes.c_int
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = ctypes.c_void_p
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    user32.PeekMessageW.restype = ctypes.c_int
    user32.MsgWaitForMultipleObjects.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    user32.MsgWaitForMultipleObjects.restype = ctypes.c_uint
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.DestroyWindow.restype = ctypes.c_int
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
    user32.RegisterClassExW.restype = ctypes.c_ushort
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = ctypes.c_void_p
    # v1.0.341：对话框排版所需（DPI 感知 + 文字实测 + 真字体）
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.DrawTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                                 ctypes.POINTER(RECT), ctypes.c_uint]
    user32.DrawTextW.restype = ctypes.c_int
    user32.AdjustWindowRectEx.argtypes = [ctypes.POINTER(RECT), ctypes.c_uint,
                                          ctypes.c_int, ctypes.c_uint]
    user32.AdjustWindowRectEx.restype = ctypes.c_int
    user32.SetFocus.argtypes = [ctypes.c_void_p]
    user32.SetFocus.restype = ctypes.c_void_p
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 13 + [ctypes.c_wchar_p]
    gdi32.CreateFontW.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_int


# ---------- 自绘最小化/退出选择对话框 ----------
# 模态对话框全局状态：None=未选择 True=最小化 False=退出；
# remember=「下次不再提示」勾选状态（v1.0.556），由 install 的关闭拦截读取后持久化
_DIALOG_RESULT = {"choice": None, "remember": False}
# 复选框控件句柄（WM_CREATE 时写入，按钮点击时 BM_GETCHECK 读取）
_DIALOG_CTRL = {"chk": None}

# v1.0.341：对话框文案与布局全局状态。
# 旧版布局写死 96-DPI 像素坐标、字体用 DEFAULT_GUI_FONT（中文系统上度量不匹配）、
# 说明文字控件高度不随实际换行数变——高 DPI / 实际字体更宽时文字互相重叠、
# 按钮越界出窗（v1.0.340 用户截图实锤）。新版：
#   1. 按 GetDpiForSystem 缩放全部坐标（DPI 不感知进程返回 96，DWM 拉伸同样成立）
#   2. 显式创建「Microsoft YaHei UI」9pt 字体（失败回退 DEFAULT_GUI_FONT）
#   3. 说明文字用 DrawTextW(DT_CALCRECT) 实测高度，窗口按内容自适应
#   4. 按钮水平居中、贴底排布，不再硬编码绝对坐标
_Q_TEXT = "关闭窗口后想做什么？"
_D_TEXT = ("「最小化到托盘」会让程序保留在后台运行，下次从系统托盘图标双击打开；\n"
           "「退出程序」则会完全关闭，下次需要手动重新启动。")
_LAYOUT = {"dpi": 96, "win_w": 480, "win_h": 240, "client_w": 452, "client_h": 228,
           "q": (24, 15, 404, 24), "d": (24, 45, 404, 52),
           "b1": (100, 170, 148, 34), "b2": (264, 170, 148, 34),
           "font": None, "font_bold": None, "fonts_owned": False}


def _system_dpi():
    """系统 DPI（Win10 1607+ 用 GetDpiForSystem；老系统回退 96）。"""
    try:
        f = user32.GetDpiForSystem
    except AttributeError:
        return 96
    try:
        return int(f()) or 96
    except Exception:  # noqa: BLE001
        return 96


def _make_gui_font(dpi, bold=False):
    """创建「Microsoft YaHei UI」9pt 字体；失败回退 DEFAULT_GUI_FONT（不可 Delete）。
    返回 (字体句柄, 是否需要 DeleteObject)。"""
    try:
        h = -round(9 * dpi / 72)  # 9pt：96dpi 时 -12
        f = gdi32.CreateFontW(
            h, 0, 0, 0, FW_BOLD if bold else FW_NORMAL,
            0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
            0, "Microsoft YaHei UI")
        if f:
            return f, True
    except Exception:  # noqa: BLE001
        pass
    try:
        return gdi32.GetStockObject(DEFAULT_GUI_FONT), False
    except Exception:  # noqa: BLE001
        return None, False


def _measure_text(text, width_px, dpi, bold=False):
    """DrawTextW(DT_CALCRECT) 实测：text 在 width_px 宽内按实际字体需要多高。"""
    try:
        hdc = user32.GetDC(None)
        if not hdc:
            return 0
        font, owned = _make_gui_font(dpi, bold)
        prev = gdi32.SelectObject(hdc, font)
        r = RECT(0, 0, int(width_px), 0)
        user32.DrawTextW(hdc, text, -1, ctypes.byref(r),
                         DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX)
        gdi32.SelectObject(hdc, prev)
        if owned:
            gdi32.DeleteObject(font)
        user32.ReleaseDC(None, hdc)
        return max(0, r.bottom - r.top)
    except Exception:  # noqa: BLE001
        return 0


def _compute_layout():
    """按当前 DPI + 实测文字高度计算对话框全部布局，写入 _LAYOUT。"""
    dpi = _system_dpi()
    d = dpi / 96.0
    margin = max(12, round(24 * d))
    client_w = max(300, round(432 * d))
    btn_w = max(96, round(148 * d))
    btn_h = max(26, round(34 * d))
    gap_btn = max(10, round(16 * d))
    content_w = client_w - 2 * margin
    q_h = _measure_text(_Q_TEXT, content_w, dpi, bold=True)
    if q_h <= 0:
        q_h = round(22 * d)  # 实测失败兜底
    q_h += max(2, round(4 * d))
    desc_h = _measure_text(_D_TEXT, content_w, dpi)
    if desc_h <= 0:
        desc_h = round(52 * d)
    gap1 = max(4, round(8 * d))
    gap_chk = max(6, round(12 * d))   # 说明文字与复选框间距
    chk_h = max(16, round(20 * d))    # 「下次不再提示」复选框行高
    gap2 = max(16, round(30 * d))
    bottom = max(10, round(22 * d))
    qy = margin
    dy = qy + q_h + gap1
    cy = dy + desc_h + gap_chk        # 复选框 y
    client_h = cy + chk_h + gap2 + btn_h + bottom
    total = 2 * btn_w + gap_btn
    bx = max(margin, (client_w - total) // 2)
    by = client_h - bottom - btn_h
    _LAYOUT.update(
        dpi=dpi, client_w=client_w, client_h=client_h,
        q=(margin, qy, content_w, q_h),
        d=(margin, dy, content_w, desc_h),
        c=(margin, cy, content_w, chk_h),
        b1=(bx, by, btn_w, btn_h),
        b2=(bx + btn_w + gap_btn, by, btn_w, btn_h),
    )


def _msgbox_fallback(hwnd):
    """最后防线：任何自绘失败都退回系统 MessageBoxW（按钮文字固定"是/否"）。
    返回 True=最小化到托盘，False=退出。MessageBoxW 本身失败时默认 True（不退出）。"""
    try:
        r = user32.MessageBoxW(
            hwnd, "要最小化到托盘继续后台运行，还是退出程序？",
            "企业微信伴侣", MB_YESNO | MB_ICONQUESTION)
        return r == IDYES
    except Exception:  # noqa: BLE001
        return True  # 极端情况：默认最小化（进程不退出更安全）


def _dialog_wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_CREATE:
        try:
            L = _LAYOUT
            # 字体在 _ask_close_choice 里创建好（含 DPI 缩放）；缺失则现场兜底
            font = L.get("font")
            if not font:
                font, owned = _make_gui_font(L.get("dpi", 96))
                L["font"], L["fonts_owned"] = font, owned
            qx, qy, qw, qh = L["q"]
            dx, dy, dw, dh = L["d"]
            cx, cy, cw, chh = L.get("c", (dx, dy + dh + 10, dw, 20))
            b1x, b1y, b1w, b1h = L["b1"]
            b2x, b2y, b2w, b2h = L["b2"]
            h1 = user32.CreateWindowExW(
                0, _STATIC_CLASS, _Q_TEXT,
                WS_CHILD | WS_VISIBLE | SS_LEFT | SS_NOPREFIX,
                qx, qy, qw, qh, hwnd, None, 0, None,
            )
            h2 = user32.CreateWindowExW(
                0, _STATIC_CLASS, _D_TEXT,
                WS_CHILD | WS_VISIBLE | SS_LEFT | SS_NOPREFIX,
                dx, dy, dw, dh, hwnd, None, 0, None,
            )
            h3 = user32.CreateWindowExW(
                0, _BTN_CLASS, "最小化到托盘",
                WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON | WS_TABSTOP,
                b1x, b1y, b1w, b1h, hwnd, IDC_BTN_MINIMIZE, 0, None,
            )
            h4 = user32.CreateWindowExW(
                0, _BTN_CLASS, "退出程序",
                WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON | WS_TABSTOP,
                b2x, b2y, b2w, b2h, hwnd, IDC_BTN_EXIT, 0, None,
            )
            # v1.0.556：「下次不再提示」复选框（勾选 + 点击任一按钮 = 记住该动作，
            # 之后点 X 直接执行不再弹窗；由 install 的关闭拦截持久化到 ui.close_action）
            h5 = user32.CreateWindowExW(
                0, _BTN_CLASS, "下次不再提示（记住本次选择）",
                WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX | WS_TABSTOP,
                cx, cy, cw, chh, hwnd, IDC_CHK_REMEMBER, 0, None,
            )
            _DIALOG_CTRL["chk"] = h5
            for h in (h1, h2, h3, h4, h5):
                if h:
                    user32.SendMessageW(h, WM_SETFONT, font, 1)  # lParam=1 立即重绘
            if h3:
                try:
                    user32.SetFocus(h3)  # 焦点给默认按钮：回车=最小化（防误退）
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass  # 字体/子控件创建失败不致命：窗口本体已在显示，按钮缺失仅影响交互
        return 0
    if msg == WM_COMMAND:
        cid = wparam & 0xFFFF
        # v1.0.556：按钮点击时读取复选框状态，随 choice 一起交给关闭拦截持久化
        _remember = False
        try:
            chk = _DIALOG_CTRL.get("chk")
            if chk and user32.SendMessageW(chk, BM_GETCHECK, 0, 0) == BST_CHECKED:
                _remember = True
        except Exception:  # noqa: BLE001
            pass
        _DIALOG_RESULT["remember"] = _remember
        if cid == IDC_BTN_MINIMIZE:
            # 【v1.0.277】只置 choice，绝不调 PostQuitMessage——
            # PostQuitMessage 的 quit 状态会被外层 pywebview 的无参 Application.Run()
            # （.NET 消息泵）感知 → GUI 线程退出 → webview.start() 返回 → 进程 os._exit。
            # 嵌套循环改由 _ask_close_choice 的 PeekMessageW 泵直接轮询 choice 退出。
            _DIALOG_RESULT["choice"] = True
            return 0
        if cid == IDC_BTN_EXIT:
            _DIALOG_RESULT["choice"] = False
            return 0
    if msg == WM_KEYDOWN:
        # 回车=最小化（更安全，避免误退）；Esc=取消关闭=最小化
        if wparam == VK_RETURN or wparam == VK_ESCAPE:
            _DIALOG_RESULT["choice"] = True
            return 0
    if msg == WM_CLOSE:
        # 用户按 X 关闭对话框 = 取消关闭 = 最小化
        _DIALOG_RESULT["choice"] = True
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_DIALOG_WNDPROC = WNDPROC(_dialog_wndproc)
_CBS.append(_DIALOG_WNDPROC)  # 防 GC


_dialog_class_registered = False


def _register_dialog_class():
    global _dialog_class_registered
    if _dialog_class_registered:
        return
    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(wc)
    wc.lpfnWndProc = ctypes.cast(_DIALOG_WNDPROC, ctypes.c_void_p)
    wc.hInstance = 0
    wc.hbrBackground = ctypes.c_void_p(COLOR_3DFACE + 1)
    wc.lpszClassName = _DIALOG_CLASS
    user32.RegisterClassExW(ctypes.byref(wc))
    _dialog_class_registered = True


def _ask_close_choice(hwnd):
    """弹自绘模态对话框。返回 True=最小化到托盘，False=退出程序。
    全链路防御：任何一步失败都不抛异常（降级 MessageBoxW 或默认最小化）。"""
    fonts_owned = [False]
    try:
        _setup_prototypes()
        try:
            _register_dialog_class()
        except Exception:  # noqa: BLE001
            pass  # 注册失败继续走 CreateWindowEx（大概率也失败，会走 fallback）
        # v1.0.341：先按 DPI + 实测文字算布局，再经 AdjustWindowRectEx 反推窗口外框尺寸
        try:
            _compute_layout()
            font, owned = _make_gui_font(_LAYOUT["dpi"])
            font_b, owned_b = _make_gui_font(_LAYOUT["dpi"], bold=True)
            _LAYOUT["font"], _LAYOUT["font_bold"] = font, font_b
            _LAYOUT["fonts_owned"] = bool(owned or owned_b)
            fonts_owned[0] = _LAYOUT["fonts_owned"]
        except Exception:  # noqa: BLE001
            pass  # 布局失败沿用 _LAYOUT 里的静态兜底值
        try:
            rc = RECT(0, 0, _LAYOUT["client_w"], _LAYOUT["client_h"])
            user32.AdjustWindowRectEx(
                ctypes.byref(rc), WS_CAPTION | WS_SYSMENU, 0, WS_EX_DLGMODALFRAME)
            w = rc.right - rc.left
            h = rc.bottom - rc.top
        except Exception:  # noqa: BLE001
            w, h = 480, 260  # AdjustWindowRectEx 失败兜底
        w = max(280, w)
        h = max(180, h)
        # 按主显示器屏幕居中（父窗口 rect 在多屏/缩放下可能越界或不准）
        sw = user32.GetSystemMetrics(0x00)  # SM_CXSCREEN
        sh = user32.GetSystemMetrics(0x01)  # SM_CYSCREEN
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        # 禁用父窗口实现模态
        user32.EnableWindow(hwnd, False)
        _DIALOG_RESULT["choice"] = None
        _DIALOG_RESULT["remember"] = False
        hdlg = user32.CreateWindowExW(
            WS_EX_DLGMODALFRAME | WS_EX_TOPMOST,
            _DIALOG_CLASS, "企业微信伴侣",
            WS_CAPTION | WS_SYSMENU | WS_VISIBLE,
            x, y, w, h,
            hwnd, None, 0, None,
        )
        if not hdlg:
            user32.EnableWindow(hwnd, True)
            return _msgbox_fallback(hwnd)
        user32.SetActiveWindow(hdlg)
        # 进入消息循环（模态阻塞到 choice 被设置）。
        # 【v1.0.277 根治】禁用 GetMessageW + PostQuitMessage 的经典嵌套循环模式：
        # pywebview 6.x 主循环是【无参 Application.Run()】，对线程 quit 状态敏感，
        # 对话框 PostQuitMessage(0) 即使被本循环的 GetMessageW 消费，外层 .NET 泵
        # 仍会退出 → webview.start() 返回 → backend._exit_app() 进程死亡
        # （v1.0.276 实测：选「最小化到托盘」窗口隐藏成功但进程秒退）。
        # 改为非阻塞 PeekMessageW 泵 + MsgWaitForMultipleObjects 100ms 超时：
        # 按钮点击在 DispatchMessageW 内直接置 choice，循环条件随之退出，全程零 WM_QUIT。
        msg = MSG()
        while _DIALOG_RESULT["choice"] is None:
            try:
                user32.MsgWaitForMultipleObjects(0, None, False, 100, QS_ALLINPUT)
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1) > 0:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            except Exception:  # noqa: BLE001
                break  # 消息泵异常：跳出循环，按未选择处理
        try:
            user32.DestroyWindow(hdlg)
        except Exception:  # noqa: BLE001
            pass
        # v1.0.341：回收自建字体（stock 字体不可删，fonts_owned 只在含自建时为 True）
        if fonts_owned[0]:
            try:
                for k in ("font", "font_bold"):
                    if _LAYOUT.get(k):
                        gdi32.DeleteObject(_LAYOUT[k])
                _LAYOUT["font"] = _LAYOUT["font_bold"] = None
                _LAYOUT["fonts_owned"] = False
            except Exception:  # noqa: BLE001
                pass
        try:
            user32.EnableWindow(hwnd, True)
            user32.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        return _DIALOG_RESULT["choice"] if _DIALOG_RESULT["choice"] is not None else True
    except Exception:  # noqa: BLE001
        # 兜底：任何未预期异常都回退 MessageBoxW（再失败默认最小化）
        try:
            user32.EnableWindow(hwnd, True)
        except Exception:  # noqa: BLE001
            pass
        return _msgbox_fallback(hwnd)


def find_main_hwnd(title, retries=120, delay=0.25):
    """按窗口标题找主窗口句柄（WinForms 表单创建后即可找到，含尚未 Show 的情况）。
    重试 120 次 × 0.25s = 30s：pywebview 窗口初始化可能较慢，给足时间避免
    install 线程先跑完导致关闭拦截没装上（点 X 直接退出）。"""
    for _ in range(retries):
        try:
            h = user32.FindWindowW(None, title)
        except Exception:  # noqa: BLE001
            h = None
        if h:
            return h
        time.sleep(delay)
    return None


_WM_SETICON = 0x80
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x10


def _apply_icon(hwnd, icon_path):
    """给窗口设置小/大图标（任务栏按钮、标题栏、Alt-Tab）。
    注意：HICON 是 64 位句柄——必须显式 restype=c_void_p，否则 ctypes 默认
    32 位截断导致图标静默失效（v1.0.324 首版踩坑）。用独立 user32 实例，
    不污染 close_guard 全局原型。"""
    import ctypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.LoadImageW.restype = ctypes.c_void_p
    u32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u32.GetSystemMetrics.restype = ctypes.c_int
    u32.GetSystemMetrics.argtypes = [ctypes.c_int]
    u32.SendMessageW.restype = ctypes.c_void_p
    u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_void_p, ctypes.c_void_p]
    sm_cxsm = u32.GetSystemMetrics(49) or 16   # SM_CXSMICON
    sm_cx = u32.GetSystemMetrics(11) or 32     # SM_CXICON
    hicon_sm = u32.LoadImageW(None, icon_path, _IMAGE_ICON, sm_cxsm, sm_cxsm, _LR_LOADFROMFILE)
    hicon_big = u32.LoadImageW(None, icon_path, _IMAGE_ICON, sm_cx, sm_cx, _LR_LOADFROMFILE)
    sent = 0
    if hicon_sm:
        u32.SendMessageW(hwnd, _WM_SETICON, ctypes.c_void_p(0), ctypes.c_void_p(hicon_sm))
        sent += 1
    if hicon_big:
        u32.SendMessageW(hwnd, _WM_SETICON, ctypes.c_void_p(1), ctypes.c_void_p(hicon_big))
        sent += 1
    return sent


# ---------- v1.0.573：web 层关闭确认（HTML 弹窗，软件同款字体/风格） ----------
# 点 X 时设置 pending 标志并吞掉 WM_CLOSE；前端经既有轮询（/api/close_ask）发现
# pending 后在网页内弹确认层，选择 POST /api/close_ask/choice 回传执行。
# v1.0.580 教训：不要在 WM_CLOSE 钩子线程里同步调 evaluate_js——钩子跑在
# pywebview GUI 主线程上，evaluate_js 内部再等主线程 = 自我死锁（界面冻结、
# 确认层点不动的实锤）。HTTP 轮询桥完全绕开跨线程调用。
# 降级链：15s 内前端未回传选择（页面崩了/轮询失效）→ 后台线程弹原自绘对话框。
_WEB_ASK = {"pending_hwnd": None, "once": None, "remember": False}   # v1.0.581：web 关闭确认状态
_WEB_APPLY = {"fn": None}              # install 闭包注册的执行回调
_WEB_ASK_FALLBACK_S = 15.0             # 前端未响应的降级时限


def close_ask_pending():
    """供 /api/close_ask 查询：是否存在等待用户确认的关闭请求。"""
    return _WEB_ASK.get("pending_hwnd") is not None


def apply_web_choice(minimize, remember):
    """选择回传入口（/api/close_ask/choice POST 调这里，HTTP 线程）。
    v1.0.581：**不在此线程直接执行窗口操作**——CoreWebView2 只能在 UI 线程访问，
    跨线程调 prev_fn/ShowWindow/window.hide() 会弹 .NET 未处理异常（用户截图实锤）。
    改为：把 {once 动作, remember} 存入 _WEB_ASK → PostMessage 投递 WM_CLOSE 给
    本窗口 → 钩子在 GUI 线程收到后按 once 执行（持久化 + 动作），天然线程安全。"""
    fn = _WEB_APPLY.get("fn")
    if not fn:
        return
    hwnd = _WEB_ASK.get("pending_hwnd")
    _WEB_ASK["once"] = "minimize" if minimize else "exit"
    _WEB_ASK["remember"] = bool(remember)
    _WEB_ASK["pending_hwnd"] = None            # 清 pending（前端弹层自动收起）
    try:
        import ctypes as _ct
        user32.PostMessageW(_ct.c_void_p(hwnd or 0), WM_CLOSE, 0, 0)
    except Exception as e:  # noqa: BLE001
        # PostMessage 失败（极端）→ 降级直接执行（可能跨线程异常，总比无响应好）
        try:
            from core_utils import log_event
            log_event(f"[close-guard] PostMessage 失败，降级直接执行：{e}")
        except Exception:  # noqa: BLE001
            pass
        fn(bool(minimize), bool(remember))


def install(title, on_minimize, icon_path=None, on_hotkey=None,
            saved_action=None, save_action=None):
    """安装关闭拦截。on_minimize() 在用户选「最小化到托盘」时于 UI 线程被调用。
    成功返回安装的 hwnd；失败返回 None。建议在独立线程中调用以免阻塞启动。
    icon_path（v1.0.324）：传入 .ico 路径时用 WM_SETICON 设置窗口大小图标——
    pywebview 的 start(icon=) 在 Windows/WinForms 后端被忽略，任务栏/Alt-Tab
    图标必须自行设置。
    on_hotkey（v1.0.373）：dict{"stop": fn}——Ctrl+Alt+S 全局热键触发的停止回调。
    安装成功后自动添加托盘图标（左键还原）+ 注册全局热键。
    v1.0.556（用户需求）：关闭对话框加「下次不再提示」复选框——
    saved_action：callable → None/"minimize"/"exit"，返回已记住的关闭动作
    （非 None 时点 X 直接执行，不再弹窗）；
    save_action：callable(minimize: bool)——用户勾选「下次不再提示」并选择后，
    把动作持久化（由调用方写 config，本模块不碰配置层）。"""
    try:
        _setup_prototypes()
    except Exception:  # noqa: BLE001
        return None
    hwnd = find_main_hwnd(title)
    if not hwnd:
        return None
    if icon_path:
        try:
            _apply_icon(hwnd, icon_path)
        except Exception:  # noqa: BLE001
            pass  # 图标失败不影响关闭拦截
    try:
        prev = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
        if not prev:
            return None
        prev_fn = WNDPROC(prev)  # 原 WinForms 托管处理程序，非 WM_CLOSE 消息都转发给它

        state = {"hwnd": hwnd, "prev": prev, "prev_fn": prev_fn,
                 "on_min": on_minimize}

        def _do_minimize(_h):
            """最小化+藏任务栏+通知回调（v1.0.654 B9：原三处逐字复制的收尾动作）。"""
            try:
                user32.ShowWindow(_h, SW_HIDE)
            except Exception:  # noqa: BLE001
                pass
            try:
                hide_from_taskbar(_h)
            except Exception:  # noqa: BLE001
                pass
            try:
                on_minimize()
            except Exception:  # noqa: BLE001
                pass

        def _do_exit(_h, _msg, _wp, _lp):
            """退出：转发原处理程序（v1.0.654 B9：原三处逐字复制）。"""
            try:
                return state["prev_fn"](_h, _msg, _wp, _lp)
            except Exception:  # noqa: BLE001
                return 0

        # v1.0.573：web 层关闭确认的选择回调（js_api apply_close_choice → 模块级
        # apply_web_choice → 本闭包）。执行动作与原自绘对话框的选择路径一致。
        def _apply_web_choice(minimize, remember):
            try:
                if remember and save_action:
                    try:
                        save_action(bool(minimize))
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            if minimize:
                _do_minimize(state["hwnd"])
            else:
                # 退出：转发 WM_CLOSE 给原处理程序（正常关闭 → 进程退出）
                _do_exit(state["hwnd"], WM_CLOSE, 0, 0)
        _WEB_APPLY["fn"] = _apply_web_choice

        @WNDPROC
        def proc(h, msg, wp, lp):
            if msg == WM_CLOSE:
                # v1.0.653 瘦身：state["busy"] 死机制删除——全部赋值点恒 False（v1.0.582
                # 起注释明令"严禁置 True"），恒不命中的拦截分支连同状态位一并移除。
                # v1.0.556：已记住动作（勾选过「下次不再提示」）→ 直接执行不弹窗
                try:
                    _act = saved_action() if saved_action else None
                except Exception:  # noqa: BLE001
                    _act = None
                if _act == "minimize":
                    _do_minimize(h)
                    return 0
                if _act == "exit":
                    return _do_exit(h, msg, wp, lp)
                # v1.0.581：web 确认层的一次性动作回放——apply_web_choice（HTTP
                # 线程）不直接执行窗口操作（CoreWebView2 只能在 UI 线程访问，
                # 跨线程直接调 prev_fn/ShowWindow 会弹 .NET 未处理异常），改为把
                # 动作存 _WEB_ASK["once"] 并 PostMessage 投递 WM_CLOSE 回 GUI
                # 线程，在此处按 once 执行——天然在 UI 线程，零跨线程异常。
                _once = None
                try:
                    _once = _WEB_ASK.get("once")
                except Exception:  # noqa: BLE001
                    pass  # silent-ok: 取不到 once 标记即按“本次不弹窗”处理
                if _once:
                    _WEB_ASK["once"] = None
                    _WEB_ASK["pending_hwnd"] = None
                    # v1.0.581：勾选「不再询问」→ 在 GUI 线程持久化（save_action 写 config）
                    if _WEB_ASK.get("remember") and save_action:
                        try:
                            save_action(_once == "minimize")
                        except Exception:  # noqa: BLE001
                            pass
                    _WEB_ASK["remember"] = False
                    if _once == "minimize":
                        _do_minimize(h)
                        return 0
                    if _once == "exit":
                        return _do_exit(h, msg, wp, lp)
                # v1.0.573→582（用户反馈：原生弹窗字体不一致 + web 层死锁 + 退出无反应）：
                # 设置 pending 标志并吞掉本次 WM_CLOSE——前端 500ms 常驻轮询发现后在
                # 网页内弹确认层，选择经 /api/close_ask/choice 回传，PostMessage 投回
                # WM_CLOSE 由本钩子的 once 回放执行。
                # **严禁在此线程调 evaluate_js**（死锁，v1.0.580 实锤）。
                # 防重复用 pending 标志：确认层已弹出时再点 X 直接忽略。
                if _WEB_ASK.get("pending_hwnd") is not None:
                    return 0   # 确认层已弹，忽略重复点 X
                _WEB_ASK["pending_hwnd"] = h

                def _web_ask_fallback(_h=h):
                    import time as _t
                    from core_utils import log_event  # 本模块不顶层导入，与 :617 同款局部导入
                    deadline = _t.time() + _WEB_ASK_FALLBACK_S
                    while _t.time() < deadline and _WEB_ASK.get("pending_hwnd") == _h:
                        _t.sleep(0.5)
                    if _WEB_ASK.get("pending_hwnd") != _h:
                        return   # 前端已回传选择，无需降级
                    try:
                        log_event("[close-guard] web 确认层 15s 未响应，降级自绘对话框")
                        minimize = _ask_close_choice(_h)
                        apply_web_choice(minimize, bool(_DIALOG_RESULT.get("remember")))
                    except Exception as e:  # noqa: BLE001
                        try:
                            log_event(f"[close-guard] 降级对话框异常（默认最小化）：{e}")
                        except Exception:  # noqa: BLE001
                            pass

                import threading as _th_fb
                _th_fb.Thread(target=_web_ask_fallback, daemon=True,
                              name="close-ask-fallback").start()
                return 0   # 吞掉本次关闭；等前端回传或 15s 降级
            if msg == WM_HOTKEY:
                # v1.0.373 全局热键：Ctrl+Alt+Space 唤起/隐藏；Ctrl+Alt+S 停止所有任务
                if wp == _HK_TOGGLE:
                    try:
                        u = _tray_user32()
                        if u.IsWindowVisible(h):
                            user32.ShowWindow(h, SW_HIDE)
                        else:
                            show_window(h)
                    except Exception:  # noqa: BLE001
                        pass
                elif wp == _HK_STOP:
                    fn = _on_hotkey_cb.get("stop")
                    if fn:
                        try:
                            fn()
                        except Exception:  # noqa: BLE001
                            pass
                return 0
            # 其它所有消息：原样转发给原 WinForms 处理程序
            try:
                return state["prev_fn"](h, msg, wp, lp)
            except Exception:  # noqa: BLE001
                return 0

        state["cb"] = proc
        _CBS.append(state)  # 防 GC
        user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, proc)
        register_hotkeys(hwnd, (on_hotkey or {}).get("stop"))
        # v1.0.373 收尾（双图标根治）：托盘图标统一由 tray.py（pystray）负责。
        # 此前两套并存——pystray 的「企业微信伴侣 vX」+ 本模块的「企业微信伴侣
        # （双击打开）」——任务栏永远两个图标（用户 hover 截图确认，2026-09-25）。
        # pystray 成熟库自带 TaskbarCreated 重建与右键菜单，统一归它；
        # 本模块仅保留：X 关闭拦截 / 最小化隐藏 / 全局热键 / 气泡通知（委托 tray.py）。
        return hwnd
    except Exception:  # noqa: BLE001
        return None


def show_window(hwnd):
    """把隐藏的主窗口重新显示并置于前台（托盘「打开伴侣」调用）。"""
    try:
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


# ==================== v1.0.373 托盘图标 + 气泡通知 + 全局热键 ====================
# 此前「最小化到托盘」只是隐藏窗口（无真托盘图标），用户只能重启找回——补齐：
#   Shell_NotifyIconW 托盘图标（左键还原）+ NIF_INFO 气泡通知（任务完成提醒）
#   + RegisterHotKey 全局热键（Ctrl+Alt+Space 唤起/隐藏、Ctrl+Alt+S 停止所有任务）。
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL = 0x1, 0x2
VK_SPACE = 0x20
_HK_TOGGLE, _HK_STOP = 1, 2


_on_hotkey_cb = {"stop": None}      # backend 注入：停止所有任务（Ctrl+Alt+S）
_user32_tray = None


def _tray_user32():
    """独立的 user32 实例并配好 RegisterHotKey 原型（不污染全局）。
    v1.0.498：Shell_NotifyIconW 原型随 ctypes 自注册托盘簇删除（托盘统一归 tray.py/pystray）。"""
    global _user32_tray
    if _user32_tray is not None:
        return _user32_tray
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    u.RegisterHotKey.restype = ctypes.c_int
    u.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.IsWindowVisible.restype = ctypes.c_int
    _user32_tray = u
    return u


def tray_notify(title, msg):
    """托盘气泡通知（任务完成/失败提醒）。
    v1.0.373 收尾：托盘统一归 tray.py（pystray），气泡委托其 notify——
    此前本模块用自注册托盘的 NIM_MODIFY 发气泡，双图标根源已移除。"""
    try:
        from desktop import tray
        return tray.notify(title, msg)
    except Exception:  # noqa: BLE001
        return False


def remove_tray_icon():
    """进程退出前清理托盘图标（防 os._exit 硬杀残留）。
    v1.0.373 收尾：托盘统一归 tray.py（pystray），委托其 shutdown；
    本模块的 ctypes 自注册托盘已移除（双图标根治，2026-09-25）。"""
    try:
        from desktop import tray
        tray.shutdown()
    except Exception:  # noqa: BLE001
        pass


def activate_existing(title):
    """把已在运行实例的主窗口激活到前台（单实例二次启动时调用，
    避免『以为没开再点一次』造成多开困惑）。"""
    try:
        _setup_prototypes()
        h = user32.FindWindowW(None, title)
        if h:
            show_window(h)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def hide_from_taskbar(hwnd):
    """v1.0.373（用户需求7）：窗口隐藏时强制从任务栏移除按钮——WinForms 隐藏后
    任务栏按钮偶有残留（用户实测显示两个图标）。经 ITaskbarList::DeleteTab（COM
    vtable 直调：槽 3=HrInit，5=DeleteTab）。失败静默。"""
    try:
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        ole32.CoInitialize.argtypes = [ctypes.c_void_p]
        ole32.CoInitialize.restype = ctypes.c_int
        ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.c_int
        ole32.CoUninitialize.restype = None
        CLSCTX_INPROC_SERVER = 1
        clsid = (ctypes.c_ubyte * 16)(0x44, 0xF3, 0xFD, 0x56, 0x6D, 0xFD, 0xD0, 0x11,
                                      0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90)
        iid = (ctypes.c_ubyte * 16)(0x42, 0xF3, 0xFD, 0x56, 0x6D, 0xFD, 0xD0, 0x11,
                                    0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90)
        ole32.CoInitialize(None)
        taskbar = ctypes.c_void_p()
        if ole32.CoCreateInstance(clsid, None, CLSCTX_INPROC_SERVER, iid, ctypes.byref(taskbar)) != 0 or not taskbar:
            ole32.CoUninitialize()
            return
        vtbl = ctypes.c_void_p.from_address(taskbar.value).value
        if not vtbl:
            ole32.CoUninitialize()
            return
        slot = ctypes.sizeof(ctypes.c_void_p)
        hr_init = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(
            ctypes.c_void_p.from_address(vtbl.value + 3 * slot).value)
        del_tab = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(
            ctypes.c_void_p.from_address(vtbl.value + 5 * slot).value)
        release = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(
            ctypes.c_void_p.from_address(vtbl.value + 2 * slot).value)
        hr_init(taskbar)
        del_tab(taskbar, hwnd)
        release(taskbar)
        ole32.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def register_hotkeys(hwnd, on_stop):
    """注册全局热键：Ctrl+Alt+Space 唤起/隐藏主窗口；Ctrl+Alt+S 停止所有任务。"""
    try:
        u = _tray_user32()
        _on_hotkey_cb["stop"] = on_stop
        ok1 = u.RegisterHotKey(hwnd, _HK_TOGGLE, MOD_CONTROL | MOD_ALT, VK_SPACE)
        ok2 = u.RegisterHotKey(hwnd, _HK_STOP, MOD_CONTROL | MOD_ALT, ord("S"))
        return ok1 and ok2
    except Exception:  # noqa: BLE001
        return False
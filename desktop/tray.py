"""
tray.py — 系统托盘（最小化到后台任务栏 / 通知区域）
====================================================
企微伴侣本身是「Flask 后端 + 浏览器窗口」，没有传统 GUI 主窗。
这里用 pystray 在任务栏通知区域放一个图标：
  - 双击图标 / 「打开伴侣」  → 打开浏览器到 http://127.0.0.1:8787
  - 「退出」               → 停止托盘并结束整个进程
这样关掉浏览器也不会杀掉应用，应用常驻后台。

纯 Windows 行为由 pystray 封装（底层 ctypes），打包进单文件 exe 无额外依赖。
"""
import os
import sys
import webbrowser

APP_URL = "http://127.0.0.1:8787/"
APP_NAME = "企业微信伴侣"

_ICON = None   # pystray Icon 实例（start_tray 运行期可用；托盘唯一来源）


def notify(title, msg):
    """托盘气泡通知（NIF_INFO 经 pystray）。托盘未运行/失败返回 False。
    v1.0.373 收尾：全软件的气泡通知统一走这里（close_guard.tray_notify 委托）。"""
    ic = _ICON
    if ic is None:
        return False
    try:
        ic.notify(str(msg or "")[:250], str(title or "")[:63])
        return True
    except Exception:  # noqa: BLE001
        return False


def shutdown():
    """停止托盘图标（进程退出前调用，防 os._exit 硬杀残留图标；跨线程安全）。"""
    ic = _ICON
    if ic is not None:
        try:
            ic.stop()
        except Exception:  # noqa: BLE001
            pass


def _safe_open():
    """打开伴侣页面，异常静默，绝不因浏览器缺失而阻塞/崩溃。"""
    try:
        webbrowser.open(APP_URL)
    except Exception:  # noqa: BLE001
        pass


def resource_path(rel):
    """与 core.paths.resource_path 同一口径（v1.0.619 收敛：原先两处各写一遍
    `getattr(sys, "_MEIPASS", 上一级目录)`，改冻结路径规则时要记得改两处）。"""
    from core.paths import resource_path as _rp
    return _rp(rel)


def _make_icon():
    from PIL import Image, ImageDraw
    # 优先用打包好的 app.ico；缺失则用绿色圆兜底
    p = resource_path("app.ico")
    try:
        if os.path.exists(p):
            return Image.open(p).convert("RGBA")
    except Exception:  # noqa: BLE001
        pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(22, 163, 74, 255))
    d.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 230))
    return img


def start_tray(version, on_exit, get_show_fn=None):
    """在调用线程里启动托盘图标（阻塞直到退出）。
    保证无论用户点击退出还是 run() 异常，on_exit 都恰好触发一次，避免进程残留/重复清理。
    get_show_fn(): 若提供则返回「还原主窗口」回调；“打开伴侣/双击”优先还原主窗口，
    拿不到时退回用系统浏览器打开（兼容无主窗口的极端情况）。
    """
    import pystray

    exited = {"done": False}

    def _do_exit():
        if not exited["done"]:
            exited["done"] = True
            try:
                on_exit()
            except Exception:  # noqa: BLE001
                pass

    def _show_main():
        fn = None
        try:
            fn = get_show_fn() if get_show_fn else None
        except Exception:  # noqa: BLE001
            fn = None
        if fn:
            try:
                fn()
                return
            except Exception:  # noqa: BLE001
                pass
        _safe_open()  # 兜底：无主窗口句柄时用浏览器打开

    def on_open(icon, item):
        _show_main()

    def on_quit(icon, item):
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
        _do_exit()

    menu = pystray.Menu(
        # v1.0.470 修复（P2）：「打开伴侣」设为 default 项——pystray.Icon 不支持
        # on_activate 参数（构造时被静默丢弃），且 Windows 上双击行为绑定 default=True
        # 菜单项。原实现双击托盘图标从不触发任何动作。
        pystray.MenuItem("打开伴侣", on_open, default=True),
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon(
        "WeComCompanion",
        _make_icon(),
        f"{APP_NAME} v{version}",
        menu,
    )
    global _ICON
    _ICON = icon   # 气泡通知入口（tray.notify）就绪
    try:
        icon.run()  # 阻塞；退出后返回
    finally:
        _ICON = None
        # run() 异常退出也要兜底触发 on_exit，确保资源释放、进程不残留
        _do_exit()

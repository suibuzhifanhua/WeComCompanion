# -*- coding: utf-8 -*-
"""core.notify — Windows 原生 Toast（自 core_utils 拆分 v1.0.402）

v1.0.498：通知中心文件簇（通知.json + push/list/mark）整簇删除——写入方唯一
（scheduler 定时任务完成），读取方（list/mark + 前端 UI）自 v1.0.402 起从未存在，
纯堆垃圾数据；送达通道收敛为 native_toast 单通道。
"""
import os
import sys
from core.logging import log_event  # noqa


# ---------------- v1.0.373（用户需求5）Windows 原生 Toast 推送 ----------------
# v1.0.556（用户截图实锤）：原 Toast 借 PowerShell 的 AUMID 发送，通知横幅来源
# 显示成「Windows PowerShell」而非软件名。注册自有 AppUserModelId（HKCU 注册表，
# 免管理员）后来源=「企业微信伴侣」；冻结态顺带把 exe 图标挂到横幅上。
_AUMID = "WeComCompanion"
_AUMID_STATE = {"done": False, "ok": False}


def _ensure_aumid():
    """注册自有 AUMID（进程内只写一次）。返回是否成功；失败回退 PowerShell AUMID。"""
    if _AUMID_STATE["done"]:
        return _AUMID_STATE["ok"]
    _AUMID_STATE["done"] = True
    if os.name != "nt":
        return False
    try:
        import winreg
        key_path = r"SOFTWARE\Classes\AppUserModelId\{}".format(_AUMID)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "企业微信伴侣")
            if getattr(sys, "frozen", False):   # 开发态是 python.exe，图标无意义
                try:
                    exe = sys.executable or ""
                    if exe.lower().endswith(".exe"):
                        winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, exe)
                except Exception:  # noqa: BLE001
                    pass
        _AUMID_STATE["ok"] = True
    except Exception as e:  # noqa: BLE001
        try:
            log_event(f"[notify] 自有 AUMID 注册失败（Toast 来源将显示 PowerShell）：{e}")
        except Exception:  # noqa: BLE001
            pass
    return _AUMID_STATE["ok"]


def native_toast(title, msg):
    """Windows 10/11 系统原生通知（右下角 Toast）。经 PowerShell 加载 WinRT，
    无第三方依赖；失败静默（托盘气泡 tray_notify 是兜底通道）。异步不阻塞调用方。
    v1.0.556：通知来源优先用自有 AUMID（横幅显示「企业微信伴侣」，原显示
    「Windows PowerShell」）；未注册成功时回退旧通道。"""
    try:
        import subprocess
        if os.name != "nt":
            return False
        esc = lambda s: str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")
        t, m = esc(title)[:60], esc(msg)[:180]
        notifier_id = (_AUMID if _ensure_aumid()
                       else "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe")
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] | Out-Null; "
            "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            "$t = $x.GetElementsByTagName('text'); "
            f"$t.Item(0).AppendChild($x.CreateTextNode('{t}')) | Out-Null; "
            f"$t.Item(1).AppendChild($x.CreateTextNode('{m}')) | Out-Null; "
            "$n = [Windows.UI.Notifications.ToastNotification]::new($x); "
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            f"'{notifier_id}').Show($n);"
        )
        # v1.0.533：提权链下 -WindowStyle Hidden 不可靠（用户实测控制台窗口可见）——
        # 叠加 startupinfo SW_HIDE 双保险，控制台窗口彻底不可见。
        si = subprocess.STARTUPINFO()
        try:
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0   # SW_HIDE
        except Exception:  # noqa: BLE001
            pass
        subprocess.Popen(["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         startupinfo=si)
        return True
    except Exception as e:  # noqa: BLE001
        try:
            log_event(f"[notify] 原生 Toast 发送失败（忽略）：{e}")
        except Exception:  # noqa: BLE001
            pass
        return False

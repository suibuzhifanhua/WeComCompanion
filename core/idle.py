# -*- coding: utf-8 -*-
"""core.idle — 空闲判定合集：「界面空闲」（v1.0.614）+「用户前台占用检测」（v1.0.523 自 idle_detect 并入）

两个空闲概念此前拆成 idle.py / idle_detect.py 两个微型文件（43+64 行），语义同属
「什么时候适合干活」，导航成本高。现合并为单模块：
  · touch/idle_seconds/wait_until_idle —— 界面（HTTP 请求）空闲；
  · user_busy/wait_user_idle —— OS 级用户输入空闲。

为什么要它：开机后有两件重活要跑（OCR 真实识别自检 ~3~6 秒 CPU、更新缓存预热 ~35 秒网络），
它们原先按固定延迟（20s / 35s）起跑。实测对着正式包点「运行组件」时，
**冷启动窗口里 `/api/components/status` 最慢要 14.48 秒**（预热后 0.9~1.7 秒）——
不是网络慢，是自家后台任务和界面请求抢同一段 CPU/GIL。

固定延迟治不了这个：用户手快，20 秒时正好在点界面。这里改成**等界面空下来再干活**：
每次 HTTP 请求都记一次时间戳，后台任务等到"最近 N 秒没有任何请求"才继续，
并有最长等待兜底（不能让自检永远跑不上）。
"""
import sys
import threading
import time

_lock = threading.Lock()
_last = {"ts": 0.0}


def touch():
    """记录一次界面活动（Flask before_request 调）。"""
    with _lock:
        _last["ts"] = time.time()


def idle_seconds():
    """距离上一次界面请求过了多少秒；从未有过请求时返回一个很大的数。"""
    with _lock:
        ts = _last["ts"]
    return (time.time() - ts) if ts else 1e9


def wait_until_idle(min_wait=0.0, idle_for=3.0, deadline=240.0, poll=1.0):
    """等到「至少跑了 min_wait 秒」且「界面已空闲 idle_for 秒」；超 deadline 就不再等。
    返回实际等待秒数。永不抛异常（后台任务的收尾不该把主流程带崩）。"""
    t0 = time.time()
    while True:
        waited = time.time() - t0
        if waited >= deadline:
            return waited
        if waited >= min_wait and idle_seconds() >= idle_for:
            return waited
        time.sleep(poll)


# =====================================================================
# 用户前台占用检测（v1.0.523 静默模式，原 core/idle_detect.py 并入）
#
# 目标：任务执行中的「必须前台」步骤（OS 级键盘输入等）在用户正在打字/操作鼠标时
# 自动延迟到空闲时段，确保自动化对用户完全无感知。
# 判据：GetLastInputInfo（系统级最后输入时刻，含键盘+鼠标，不受焦点窗口影响）。
# 空闲时长 = GetTickCount() - dwTime（DWORD 环绕用 & 0xFFFFFFFF 处理，49.7 天一循环）。
# 检测失败时的降级方向是「当作空闲放行」——能检测时不打扰，检测不了退回即时执行，
# 绝不因检测失效卡死前台步骤（功能优先）。
# =====================================================================
_WIN32 = (sys.platform == "win32")


def _user_input_idle_seconds():
    """系统级输入空闲秒数；获取失败返回 None（调用方按保守处理）。"""
    if not _WIN32:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        u32 = ctypes.windll.user32
        if not u32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now_tick = u32.GetTickCount() & 0xFFFFFFFF
        delta = (now_tick - (info.dwTime & 0xFFFFFFFF)) & 0xFFFFFFFF
        return delta / 1000.0
    except Exception:  # noqa: BLE001
        return None


def user_busy(idle_threshold=2.0):
    """用户是否正在活跃使用电脑（键盘/鼠标 idle < threshold 秒）。
    检测失败（GetLastInputInfo 被钩子拦截/非 Windows）→ 返回 False（当作空闲）：
    功能优先——否则 send_wecom_message 等前台步骤会在静默模式下永久坏死。"""
    idle = _user_input_idle_seconds()
    if idle is None:
        return False
    return idle < float(idle_threshold)


def wait_user_idle(max_wait_s=120.0, poll_s=3.0, idle_threshold=2.0):
    """阻塞等待用户进入空闲（最多 max_wait_s 秒）。
    返回 True=已空闲可执行；False=等待超时（调用方应放弃或降级，绝不强抢前台）。"""
    deadline = time.time() + float(max_wait_s)
    while time.time() < deadline:
        if not user_busy(idle_threshold):
            return True
        time.sleep(float(poll_s))
    return not user_busy(idle_threshold)

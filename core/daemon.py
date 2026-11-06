# -*- coding: utf-8 -*-
"""core.daemon — 守护线程骨架单源（审计 A13+B1）
=================================================================
收敛服务层六套守护循环与两处看门狗各抄一份的线程样板
（Thread+Event.wait+while+try/except 吞异常）：

- start_periodic：周期 tick 守护线程。内部 is_alive 判重（重复调用安全）、
  daemon 线程、Event.wait(interval) 挂起不占 CPU、tick_fn 异常吞掉留痕
  （log_event）、gate 返回 False 的轮次跳过 tick_fn、first_delay 控制首拍延时。
  返回线程对象，附 stop_event 供可停场景——调用方原有可停/不可停现状不变。
- spawn_deadline_watchdog：一次性看门狗守护线程，seconds 内未取消则调
  on_fire()；返回 (线程, cancel_event)，cancel_event.set() 提前取消。

行为与被收敛的原实现逐一等价（tick 间隔/判重/异常吞噬留痕/gate 跳过）。
"""
import threading

from core.logging import log_event

_REG_LOCK = threading.Lock()
_THREADS = {}          # name -> 线程（is_alive 判重；死线程启动时清出登记表）


def start_periodic(name, tick_fn, interval_s, gate=None, first_delay=0.0):
    """启动（或复用）名为 name 的周期守护线程：先等 first_delay，此后每
    interval_s 调一次 tick_fn；gate 返回 False 的轮次跳过 tick_fn。同名线程仍在
    运行时直接返回它（重复调用安全）。tick_fn/gate 异常一律吞掉并留痕，循环不退。
    返回线程对象（附 stop_event，置位即停）。"""
    with _REG_LOCK:
        th = _THREADS.get(name)
        if th is not None and th.is_alive():
            return th
        for k in [k for k, v in _THREADS.items() if not v.is_alive()]:
            _THREADS.pop(k, None)
        stop_event = threading.Event()

        def _loop():
            if first_delay > 0 and stop_event.wait(first_delay):
                return
            while not stop_event.is_set():
                try:
                    if gate is None or gate():
                        tick_fn()
                except Exception as e:  # noqa: BLE001
                    log_event(f"[daemon:{name}] tick 异常：{type(e).__name__}: {e}")
                if stop_event.wait(interval_s):
                    return

        th = threading.Thread(target=_loop, name=name, daemon=True)
        th.stop_event = stop_event
        _THREADS[name] = th
        th.start()
    return th


def spawn_deadline_watchdog(seconds, on_fire, name="deadline-watchdog"):
    """一次性看门狗守护线程：seconds 秒内未取消则调 on_fire()（触发后不再动作）。
    返回 (线程, cancel_event)——cancel_event.set() 提前取消，到期不触发。"""
    cancel = threading.Event()

    def _run():
        if cancel.wait(seconds):
            return
        try:
            on_fire()
        except Exception as e:  # noqa: BLE001
            log_event(f"[daemon:{name}] 看门狗触发异常：{type(e).__name__}: {e}")

    th = threading.Thread(target=_run, name=name, daemon=True)
    th.cancel_event = cancel
    th.start()
    return th, cancel

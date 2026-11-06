# -*- coding: utf-8 -*-
"""services.todo_watch — 企微待办变更轻提醒（v1.0.523-4）
=================================================================
定时（默认 10 分钟）读待办列表页的链接条目集合，与上次快照差分：
出现新条目 → 托盘气泡轻提醒（不弹 Toast 横幅、不抢焦点，静默模式兼容）。
- 定位：复用批审的待办页查找（标题含「待办/任务」）
- 取条目：list_links（链接标题集合）——纯 CDP 后台读，不依赖窗口可见
- 快照：产物库/台账/待办快照.json（条目集合 + 更新时间）
- 开关/间隔：config todo_watch {enabled: False, interval_min: 10}（默认关——
  需要企微已绑定且开着待办页才有效，由用户显式开启）
边界：CDP 不可用/待办页不存在 → 静默跳过本轮（不提醒不可用，避免打扰）。
"""
import json
import os
import time

from core_utils import log_event, workspace_root
from core.daemon import start_periodic   # A13+B1：守护骨架收敛

POLL_INTERVAL = 60            # 主循环 60s tick；实际读取按 interval_min 节流
_state = {"_stop": None, "_thread": None, "_last_read": 0.0}


def _snapshot_path():
    d = os.path.join(workspace_root(), "台账")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "待办快照.json")


def _load_snapshot():
    from core.files import read_json   # v1.0.654 A8：句柄样板收敛
    return read_json(_snapshot_path(), {"items": [], "ts": ""}) or {"items": [], "ts": ""}


def _save_snapshot(items):
    from core.config import atomic_write_json
    # v1.0.652（审计 P2-D4）：快照不再截断——sorted()[:200] 会把挤出窗口的旧条目
    # 下次读取时变成"新增待办"（幽灵提醒）；且 sorted 偏字典序而非时间序。
    # 快照是差分基线，必须完整（条目为标题级短串，全量保留体积可控）。
    atomic_write_json(_snapshot_path(),
                      {"items": sorted(items),
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")})


def _cfg(cfg):
    tw = cfg.get("todo_watch")
    if not isinstance(tw, dict):
        tw = {}
    try:
        iv = max(2, int(tw.get("interval_min") or 10))
    except (TypeError, ValueError):
        iv = 10
    return {"enabled": bool(tw.get("enabled", False)), "interval_min": iv}


def _list_page_items(port):
    """读待办列表页条目集合：返回 (标题集合, 页面标题) 或 None（页面不在/CDP 断）。"""
    try:
        import wecom_bridge as wb
        from services.batch_flow import _find_list_page
        title, _pages = _find_list_page(port)
        if not title:
            return None
        r = wb.list_links(title, port=port)
        links = r.get("links") if isinstance(r, dict) else None
        if not isinstance(links, list):
            return None
        items = set()
        for lk in links:
            if isinstance(lk, dict):
                t = str(lk.get("text") or lk.get("title") or "").strip()
            else:
                t = str(lk or "").strip()
            if len(t) >= 4:                      # 过滤空链接/图标链接
                items.add(t[:60])
        return (items, title)
    except Exception as e:  # noqa: BLE001
        log_event(f"[todo-watch] 读取失败（本轮跳过）：{e}")
        return None


def check_once(port=None, notify=True):
    """执行一次对比。返回 {"new": [新增条目], "total": n, "skipped": bool}。
    notify=False 时只更新快照不提醒（供测试/首次建基线）。"""
    if port is None:
        # v1.0.588：改用统一口径。原 `.get("cdp_port", 9222)` 只在键**缺失**时给默认值，
        # 配置里 cdp_port 存在但为空串/None 时会把空值当端口传下去。
        from core.config_store import cdp_port
        port = cdp_port()
    got = _list_page_items(port)
    if got is None:
        return {"new": [], "total": 0, "skipped": True}
    items, page_title = got
    prev = _load_snapshot()
    prev_items = set(prev.get("items") or [])
    fresh = sorted(items - prev_items) if prev_items else []
    _save_snapshot(items)
    if fresh and notify and prev_items:
        # prev_items 为空=首次建基线，不提醒（存量不算"新"）
        try:
            from desktop import close_guard
            head = fresh[0][:20]
            more = f" 等 {len(fresh)} 项" if len(fresh) > 1 else ""
            close_guard.tray_notify("待办有更新", f"新增：{head}{more}")
            log_event(f"[todo-watch] 新待办 {len(fresh)} 项：{'、'.join(fresh[:3])}")
        except Exception as e:  # noqa: BLE001
            log_event(f"[todo-watch] 提醒失败（不影响快照）：{e}")
    return {"new": fresh, "total": len(items), "skipped": False}


def _tick():
    cfg = _cfg(_load_cfg_safe())
    if not cfg["enabled"]:
        return
    now = time.time()
    if now - _state["_last_read"] < cfg["interval_min"] * 60:
        return
    _state["_last_read"] = now
    check_once()


def _load_cfg_safe():
    try:
        from core.config_store import load_config
        return load_config()
    except Exception:  # noqa: BLE001
        return {}


def start_daemon():
    """启动待办监视线程（60s tick，interval_min 节流实际读取）。"""
    if _state["_thread"] is not None and _state["_thread"].is_alive():
        return   # v1.0.652（审计 P2-B2）：is_alive 判重——旧 is not None 让 stop 后无法重启
    # v1.0.656（B1）：守护循环骨架收敛到 core.daemon.start_periodic
    # （原 _loop 的 while + try/except 吞异常 + _stop.wait(POLL_INTERVAL)）。
    _state["_thread"] = start_periodic("todo-watch", _tick, POLL_INTERVAL)
    _state["_stop"] = _state["_thread"].stop_event
    log_event("[todo-watch] 待办变更监视线程已启动（默认关，config todo_watch.enabled 开启）")


def stop_daemon():
    if _state["_stop"] is not None:
        _state["_stop"].set()

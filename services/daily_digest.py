# -*- coding: utf-8 -*-
"""services.daily_digest — 每日工作小结（v1.0.523-3）
=================================================================
每天一次（默认 18:30，可配 digests.daily.time）静默汇总当日工作：
- 任务会话：今日新建数、活跃会话的最新动态（取每个会话最后一条 user 消息摘要）
- 定时任务：今日运行成败
- Token 用量：今日 LLM 消耗
- 知识库：今日新增文件
产出：产物库/日报/工作小结_YYYY-MM-DD.md（LLM 润色生成；LLM 不可用时降级为纯数据版）
静默：全程不弹任何通知，写完只记日志；执行前确认用户空闲（复用 core.idle.user_busy）。
开关：config digests.daily.enabled（默认开）。
"""
import io
import json
import os
import time
from datetime import date, datetime

from core_utils import log_event, workspace_root
from core.daemon import start_periodic   # A13+B1：守护骨架收敛

POLL_INTERVAL = 300          # 5 分钟 tick 一次（到点窗口内触发一次即可）
DEFAULT_TIME = "18:30"
_MIN_ROOM = 40               # 小结正文本最低字符数——太短说明今日几乎无活动，跳过

_state = {"last_date": None, "_stop": None, "_thread": None}


def _digest_dir():
    d = os.path.join(workspace_root(), "日报")
    os.makedirs(d, exist_ok=True)
    return d


def _cfg(cfg):
    dg = cfg.get("digests")
    if not isinstance(dg, dict):
        dg = {}
    daily = dg.get("daily")
    if not isinstance(daily, dict):
        daily = {}
    return {
        "enabled": bool(daily.get("enabled", True)),
        "time": str(daily.get("time") or DEFAULT_TIME)[:5],
    }


# ---------- 数据采集 ----------
def _collect(cfg):
    """汇总今日数据（每个块失败独立兜底，坏一块不拖垮整份小结）。"""
    today = date.today().strftime("%Y-%m-%d")
    out = {"date": today, "conversations": [], "conv_new": 0,
           "schedules": [], "tokens": {}, "kb_new": []}
    # 1) 今日会话：mtime 在今天 + 每个会话的最后一条 user 消息（截 80 字）
    try:
        from services.conversations import conv_dir
        d = conv_dir()
        for n in os.listdir(d):
            if not n.endswith(".json"):
                continue
            p = os.path.join(d, n)
            try:
                if time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p))) != today:
                    continue
            except OSError:
                continue
            out["conv_new"] += 1
            try:
                data = json.load(io.open(p, encoding="utf-8"))
                msgs = [m for m in (data.get("messages") or []) if m.get("role") == "user"]
                last_user = str(msgs[-1].get("content") or "").strip()[:80] if msgs else ""
                title = str(data.get("title") or "").strip() or last_user[:30] or n[:-5]
                out["conversations"].append({"title": title[:40], "last_user": last_user})
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        log_event(f"[digest] 会话采集失败（跳过该块）：{e}")
    # 2) 定时任务今日运行
    try:
        from services.scheduler import list_schedules
        for x in (list_schedules() or []):
            lr = str(x.get("last_run") or "")
            if lr.startswith(today):
                out["schedules"].append({"name": x.get("name") or x.get("id", ""),
                                         "result": str(x.get("last_result") or "")[:80]})
    except Exception as e:  # noqa: BLE001
        log_event(f"[digest] 定时任务采集失败（跳过该块）：{e}")
    # 3) 今日 token
    try:
        from services import usage_stats
        out["tokens"] = usage_stats.summary(1).get("today", {})
    except Exception as e:  # noqa: BLE001
        log_event(f"[digest] 用量采集失败（跳过该块）：{e}")
    # 4) 知识库今日新增（扫描全库 mtime）
    try:
        kb_root = os.path.join(workspace_root(), "知识库")
        if os.path.isdir(kb_root):
            for root, _dirs, files in os.walk(kb_root):
                for f in files:
                    if not f.lower().endswith((".md", ".txt")):
                        continue
                    p = os.path.join(root, f)
                    try:
                        if time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p))) == today:
                            out["kb_new"].append(os.path.relpath(p, kb_root))
                    except OSError:
                        continue
    except Exception as e:  # noqa: BLE001
        log_event(f"[digest] 知识库采集失败（跳过该块）：{e}")
    return out


def _fallback_md(data):
    """LLM 不可用时的纯数据版小结（保证产物一定存在）。"""
    L = [f"# 工作小结 {data['date']}", ""]
    if data["conversations"]:
        L.append(f"## 任务（今日 {len(data['conversations'])} 个会话活跃）")
        for c in data["conversations"][:10]:
            L.append(f"- {c['title']}" + (f"：{c['last_user']}" if c["last_user"] else ""))
        L.append("")
    if data["schedules"]:
        L.append("## 定时任务（今日运行）")
        for s in data["schedules"]:
            L.append(f"- {s['name']}：{s['result']}")
        L.append("")
    if data["kb_new"]:
        L.append(f"## 知识库新增（{len(data['kb_new'])} 个文件）")
        for k in data["kb_new"][:10]:
            L.append(f"- {k}")
        L.append("")
    t = data.get("tokens") or {}
    if t:
        L.append(f"## Token 用量：{t.get('total', 0)}（{t.get('requests', 0)} 次调用，"
                 f"输入 {t.get('prompt', 0)} / 输出 {t.get('completion', 0)}）")
    if len(L) <= 2:
        L.append("今日无任务活动记录。")
    return "\n".join(L) + "\n"


_PROMPT = """你是工作日报助手。下面是用户今天使用「企微伴侣」自动化助手的原始活动数据。
请写一份简洁的中文工作小结（Markdown，200 字以内）：提炼今天做了哪些事、完成情况、
值得注意的异常。只依据数据，不要编造没有的事。直接输出正文，不要客套开场白。

{data}"""


def _llm_md(data):
    """LLM 润色版；失败返回 None（调用方降级 _fallback_md）。"""
    try:
        from core.config_store import load_config
        from llm_client import resolve_model, _llm_complete
        cfg = load_config()
        model = resolve_model(cfg)
        if not model:
            return None
        raw = json.dumps(data, ensure_ascii=False)[:4000]
        out = (_llm_complete(model, [{"role": "user", "content": _PROMPT.format(data=raw)}],
                             disable_thinking=True) or "").strip()
        return out if len(out) >= _MIN_ROOM else None
    except Exception as e:  # noqa: BLE001
        log_event(f"[digest] LLM 生成失败（降级数据版）：{e}")
        return None


# ---------- 主流程 ----------
def generate_today(force=False):
    """生成今日小结（已存在且非 force 则跳过）。返回 (path, mode) 或 None。"""
    data = _collect({})
    path = os.path.join(_digest_dir(), f"工作小结_{data['date']}.md")
    if os.path.exists(path) and not force:
        return None
    mode = "llm"
    md = _llm_md(data)
    if not md:
        md, mode = _fallback_md(data), "fallback"
    from core.config import atomic_write_text
    atomic_write_text(path, md)
    log_event(f"[digest] 每日小结已生成（{mode}）：{os.path.basename(path)}")
    return path, mode


def _due_now(hhmm):
    """当前时间是否落在 [hhmm, hhmm+2×POLL_INTERVAL) 窗口内（tick 5 分钟一次）。
    v1.0.652（审计 P2-B1）：窗口放宽到 2 个 tick——旧 `busy 就推迟到下一个 tick` 的
    注释与 `< POLL_INTERVAL` 的窗口矛盾：busy 后的下一个 tick 恰在 target+300s，
    被窗口排除 → 当天日报静默丢失。放宽后 busy 推迟一拍仍在窗口内。"""
    try:
        h, m = map(int, str(hhmm).split(":"))
    except (ValueError, AttributeError):
        h, m = 18, 30
    now = datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return 0 <= (now - target).total_seconds() < 2 * POLL_INTERVAL


def _tick():
    cfg = _cfg(_load_cfg_safe())
    if not cfg["enabled"]:
        return
    today = date.today().strftime("%Y-%m-%d")
    if _state["last_date"] == today:
        return                       # 今天已生成
    if not _due_now(cfg["time"]):
        return
    # 静默约定：用户正在用电脑时推迟到下一个 tick（不抢任何注意力）
    try:
        from core.idle import user_busy
        if user_busy(2.0):
            return
    except Exception:  # noqa: BLE001
        pass
    r = generate_today()
    if r:
        _state["last_date"] = today


def _load_cfg_safe():
    try:
        from core.config_store import load_config
        return load_config()
    except Exception:  # noqa: BLE001
        return {}


def start_daemon():
    """启动每日小结 daemon（backend 启动时调用一次；重复调用幂等）。"""
    if _state["_thread"] is not None and _state["_thread"].is_alive():
        return   # v1.0.652（审计 P2-B2）：is_alive 判重——旧 is not None 让 stop 后无法重启
    # v1.0.656（B1）：守护循环骨架收敛到 core.daemon.start_periodic
    # （原 _loop 的 while + try/except 吞异常 + _stop.wait(POLL_INTERVAL)）。
    _state["_thread"] = start_periodic("daily-digest", _tick, POLL_INTERVAL)
    _state["_stop"] = _state["_thread"].stop_event
    log_event(f"[digest] 每日小结线程已启动（默认 {_cfg(_load_cfg_safe())['time']}，开关在 config digests.daily）")


def stop_daemon():
    if _state["_stop"] is not None:
        _state["_stop"].set()

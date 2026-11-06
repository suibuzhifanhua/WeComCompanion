# -*- coding: utf-8 -*-
"""Token 用量统计（v1.0.252）

每次 LLM 调用收到 usage 时追加一条 JSONL 记录，按月分档独立存档：
    产物库/日志/usage_YYYYMM.jsonl   （v1.0.275 起；原 dist/logs/，旧文件由 migrate_legacy_layout 迁移）
（运行日志独立保存，不混入 companion.log；build.py 已把 产物库/ 整体列入 DIST_PRESERVE）

记录字段（OpenAI 兼容）：
    {"ts": "2026-09-02T08:45:00", "date": "2026-09-02", "model": "qwen3.8/latest",
     "prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801, "kind": "chat"}

兼容 Ollama 原生字段（prompt_eval_count / eval_count）。
summary(days) 返回前端渲染所需的聚合数据。
"""
import json
import os
import threading
from datetime import date, datetime, timedelta

# v1.0.256：LOG_DIR 必须指向 exe 所在目录而非 __file__ 所在目录——
# PyInstaller onefile 下 __file__ 指向 _MEI 临时解压目录，写入的数据随进程退出
# 一起被清理（P0：统计存档会全部丢失）。开发态跑源码则用 __file__ 定位。
# 两种模式最终都落在「dist/logs/」：frozen → <exe目录>/logs；源码 → <项目根>/dist/logs
import sys
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    # v1.0.652（审计 P2-F2）：源码模式取**项目根**（services/ 的上一级）——旧取
    # services/ 自身目录，开发态用量日志散落到 services/产物库/日志，与
    # core.paths.workspace_root()（项目根/产物库）口径分裂，relocate 随迁清单也盖不到。
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# v1.0.275：用量存档并入 产物库/日志/（原 frozen→<exe目录>/logs，源码→<项目根>/dist/logs，
# 两处散落且源码模式还写进了 exe 的 dist 目录；旧文件由 core_utils.migrate_legacy_layout 迁移）
LOG_DIR = os.path.join(_BASE, "产物库", "日志")
_lock = threading.Lock()

# v1.0.510 优化（P2-12）：月度用量档保留策略——原 append-only 永不清理
# （实测 ~500KB/月，数年累积可观）。保留最近 RETAIN_MONTHS 个月，更早的档在
# 本进程首次记录用量时静默清理（summary 最多回看 365 天，6 个月外的数据已不可达）。
RETAIN_MONTHS = 6
_cleanup_done = False


def _cleanup_old_locked():
    """删除超过保留期（RETAIN_MONTHS 个月）的 usage_YYYYMM.jsonl。持 _lock 调用。"""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True   # 先置位：即使本次清理抛异常也不反复重试（下次进程再试）
    cutoff = (date.today().replace(day=1) - timedelta(days=31 * RETAIN_MONTHS))
    cut_tag = cutoff.strftime("%Y%m")
    try:
        for n in os.listdir(LOG_DIR):
            if not (n.startswith("usage_") and n.endswith(".jsonl")):
                continue
            tag = n[len("usage_"):-len(".jsonl")]
            if len(tag) == 6 and tag.isdigit() and tag < cut_tag:
                try:
                    os.remove(os.path.join(LOG_DIR, n))
                except OSError:
                    continue
    except OSError:
        pass


def _month_path(d):
    return os.path.join(LOG_DIR, f"usage_{d.strftime('%Y%m')}.jsonl")


def record_usage(model_name, usage, kind="chat"):
    """记一条用量。usage 兼容 OpenAI / Olloma 字段；非法或缺 token 数时静默跳过。"""
    if not isinstance(usage, dict):
        return
    pt = usage.get("prompt_tokens", usage.get("prompt_eval_count"))
    ct = usage.get("completion_tokens", usage.get("eval_count"))
    try:
        pt = int(pt) if pt is not None else 0
        ct = int(ct) if ct is not None else 0
    except (TypeError, ValueError):
        return
    if pt <= 0 and ct <= 0:
        return
    now = datetime.now()
    rec = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "model": str(model_name or "unknown"),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
        "kind": kind,
    }
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with _lock:
            _cleanup_old_locked()   # v1.0.510（P2-12）：进程内首次记录时清理过期月档
            with open(_month_path(now), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  统计失败绝不影响主流程
        pass


def _iter_records(days):
    """读取覆盖 days 天范围的所有月度文件，逐条 yield 合法记录。"""
    if days <= 0:
        days = 14
    start = date.today() - timedelta(days=days - 1)
    months = []
    cur = start.replace(day=1)
    while cur <= date.today():
        months.append(cur)
        cur = (cur + timedelta(days=32)).replace(day=1)
    for m in months:
        p = _month_path(m)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(r, dict) and r.get("date", "") >= start.strftime("%Y-%m-%d"):
                        yield r
        except OSError:
            continue


def clear():
    """清空全部用量存档（v1.0.270）：删除所有 usage_*.jsonl。
    返回 {"removed": 文件数, "records": 记录条数, "freed_bytes": 释放字节}。
    v1.0.364 审查修复（M12）：全程持 _lock——原实现删除与 record_usage 的 append
    并发时（Windows 删打开中的文件抛 OSError）计数不符/产生残档。"""
    removed = 0
    records = 0
    freed = 0
    try:
        names = [n for n in os.listdir(LOG_DIR) if n.startswith("usage_") and n.endswith(".jsonl")]
    except OSError:
        names = []
    with _lock:
        for n in names:
            p = os.path.join(LOG_DIR, n)
            try:
                if not os.path.isfile(p):
                    continue
                freed += os.path.getsize(p)
                with open(p, encoding="utf-8") as f:
                    records += sum(1 for line in f if line.strip())
                os.remove(p)
                removed += 1
            except OSError:
                continue
    return {"removed": removed, "records": records, "freed_bytes": freed}


def summary(days=14):
    """聚合约 days 天用量：按日序列（空天补零）+ 模型排行 + 汇总。"""
    days = max(1, min(365, int(days or 14)))
    start = date.today() - timedelta(days=days - 1)

    by_day = {}
    by_model = {}
    tot = {"requests": 0, "prompt": 0, "completion": 0, "total": 0}
    today_str = date.today().strftime("%Y-%m-%d")
    today = {"requests": 0, "prompt": 0, "completion": 0, "total": 0}

    for r in _iter_records(days):
        d = r.get("date", "")
        m = r.get("model", "unknown")
        pt = int(r.get("prompt_tokens") or 0)
        ct = int(r.get("completion_tokens") or 0)
        tt = int(r.get("total_tokens") or (pt + ct))
        day = by_day.setdefault(d, {"requests": 0, "prompt": 0, "completion": 0, "total": 0})
        day["requests"] += 1; day["prompt"] += pt; day["completion"] += ct; day["total"] += tt
        mod = by_model.setdefault(m, {"requests": 0, "prompt": 0, "completion": 0, "total": 0})
        mod["requests"] += 1; mod["prompt"] += pt; mod["completion"] += ct; mod["total"] += tt
        tot["requests"] += 1; tot["prompt"] += pt; tot["completion"] += ct; tot["total"] += tt
        if d == today_str:
            today["requests"] += 1; today["prompt"] += pt; today["completion"] += ct; today["total"] += tt

    # 按日连续序列（空天补零），前端画柱状图直接用
    day_series = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        v = by_day.get(d)
        day_series.append({
            "date": d,
            "requests": v["requests"] if v else 0,
            "prompt": v["prompt"] if v else 0,
            "completion": v["completion"] if v else 0,
            "total": v["total"] if v else 0,
        })

    models = sorted(
        ({"model": k, **v} for k, v in by_model.items()),
        key=lambda x: x["total"], reverse=True,
    )
    return {
        "days": days,
        "today": today,
        "totals": tot,
        "day_series": day_series,
        "models": models,
        "avg_daily_total": round(tot["total"] / days) if days else 0,
    }

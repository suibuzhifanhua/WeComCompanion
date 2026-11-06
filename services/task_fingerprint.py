# -*- coding: utf-8 -*-
"""services.task_fingerprint — 相似任务检测与技能自动沉淀建议（v1.0.582，用户需求）
====================================================================
用户多次提交相似任务（prompt 语义近似 + 执行工具序列一致）且结果良好时，
主动建议"固化为技能"，固化后可一句话复用，不必每次重新描述。

【机制】
  - 指纹 = 规范化任务文本（去数字/日期/空白，lower）的 md5 前 12 位——
    「审计 10 月月报」与「审计 11 月月报」同指纹（月份数字被归一化）；
  - 存储：产物库/任务指纹.json（随 relocate 迁移，用户数据）；格式：
    {"<fp>": {"title", "prompt", "tools", "count", "last_ts", "asked"}}
  - 检测：同指纹 count >= SUGGEST_AFTER 次且未询问过（asked）→ 返回建议
    并标记 asked（同一指纹只问一次，避免打扰）；
  - 固化：前端确认 → POST /api/skills/auto_create → 写 cfg['custom_skills']
    （agent_tools._custom_skills 的标准格式），技能中心立即可见。
"""
import hashlib
import json
import os
import re
import time

SUGGEST_AFTER = 3          # 同指纹执行满 N 次才建议固化
MAX_RECORDS = 50           # 指纹记录上限（LRU 淘汰最旧）
FILE_NAME = "任务指纹.json"


def _store_path():
    from core.paths import workspace_root
    return os.path.join(workspace_root(), FILE_NAME)


def _load():
    try:
        with open(_store_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d):
    try:
        p = _store_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        try:
            from core_utils import log_event
            log_event(f"[task-fingerprint] 存储失败（忽略）：{e}")
        except Exception:  # noqa: BLE001
            pass


def _fingerprint(task_text, tool_names):
    """规范化任务文本 + 工具名序列 → 稳定指纹。数字/日期归一化让
    「审计 10 月月报」≈「审计 11 月月报」；工具序列参与哈希（执行路径一致）。"""
    t = re.sub(r"\s+", "", str(task_text or ""))
    t = re.sub(r"\d+", "N", t.lower())            # 数字归一（10 月/11 月 → N 月）
    t = re.sub(r"[一二三四五六七八九十百]+", "N", t)  # 中文数字归一
    tools = ",".join(sorted(set(str(x) for x in (tool_names or []) if x)))
    raw = f"{t}|{tools}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def record_and_check(task_text, tool_names):
    """任务完成时调用：记录指纹，达到阈值且未询问过 → 返回建议 dict（并标记 asked）。
    task_text 用全部 user 消息拼接（agent_tools._extract_task_text 的口径）；
    无工具调用的纯对话不记录（闲聊没有沉淀价值）。"""
    tools = [str(x) for x in (tool_names or []) if x]
    if not tools:
        return None
    text = str(task_text or "").strip()
    if len(text) < 6:
        return None   # 过短（"继续"/"好"）无指纹价值
    fp = _fingerprint(text, tools)
    d = _load()
    rec = d.get(fp) or {}
    rec["count"] = int(rec.get("count") or 0) + 1
    rec["title"] = (rec.get("title") or re.sub(r"\s+", " ", text)[:24])
    rec["prompt"] = text[:500]
    rec["tools"] = tools[:20]
    rec["last_ts"] = time.time()
    suggest = None
    if rec["count"] >= SUGGEST_AFTER and not rec.get("asked"):
        rec["asked"] = True
        suggest = {"suggest": True, "count": rec["count"], "title": rec["title"],
                   "prompt": rec["prompt"], "tools": tools[:20]}
    d[fp] = rec
    # LRU 淘汰最旧（防文件无限增长）
    if len(d) > MAX_RECORDS:
        keep = sorted(d.items(), key=lambda kv: kv[1].get("last_ts") or 0, reverse=True)[:MAX_RECORDS]
        d = dict(keep)
    _save(d)
    return suggest

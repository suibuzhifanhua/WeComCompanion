# -*- coding: utf-8 -*-
"""skill_versions - skill prompt version history & rollback (v1.0.349).

Every prompt-changing event (skill-center manual edit, self-repair lesson append,
override clear, rollback itself) snapshots the PREVIOUS full prompt before the
change is applied. Rollback = restore an old snapshot (current state is snapshotted
first, so a rollback is itself reversible).

Store: workspace/skill/versions/skill_versions.json
  {skill_id: [{ts, source, note, prompt}, ...]}  newest last, cap 20 per skill.
source keys: manual_edit | self_repair | override_clear | delete | rollback_pre
"""
import io
import json
import os
import threading
import time

from core_utils import log_event
from core.config import atomic_write_json  # v1.0.510（P2-13）
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）

_LOCK = threading.Lock()
_CAP = 20   # per skill


def _store_path():
    return os.path.join(paths.workspace_root(), "\u6280\u80fd", "\u7248\u672c",
                         "skill_versions.json")


def _load():
    try:
        with io.open(_store_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d):
    p = _store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # v1.0.510（P2-13）：tmp+os.replace 样板收敛到 atomic_write_json
    atomic_write_json(p, d, indent=1)


def record_version(skill_id, prompt, source, note=""):
    """Snapshot prompt (the state BEFORE the pending change). No-op when empty
    or unchanged since the latest snapshot. Thread-safe."""
    if not prompt:
        return False
    with _LOCK:
        d = _load()
        arr = d.setdefault(skill_id, [])
        if arr and arr[-1].get("prompt") == prompt:
            return False   # unchanged since last snapshot
        arr.append({"ts": 0, "source": str(source),
                    "note": str(note or "")[:120], "prompt": prompt})
        # v1.0.349：ts 技能内单调递增——同一秒内连续两次变更会产生相同时间戳，
        # 按秒取版本会产生歧义（实测回滚拿错版本）。
        prev_ts = arr[-2]["ts"] if len(arr) >= 2 else 0
        now = int(time.time())
        arr[-1]["ts"] = max(now, prev_ts + 1)
        del arr[:-_CAP]
        try:
            _save(d)
        except Exception as e:  # noqa: BLE001
            log_event("[skillver] save failed (ignored): " + str(e))
            return False
    log_event("[skillver] snapshot " + str(skill_id) + " source=" + str(source))
    return True


def list_versions(skill_id, with_prompt=False):
    """Newest first. prompt only included when with_prompt=True (list view sends head)."""
    with _LOCK:
        d = _load()
    out = []
    for v in reversed(d.get(skill_id, [])):
        item = {"ts": v.get("ts"), "source": v.get("source", ""),
                "note": v.get("note", ""), "head": (v.get("prompt") or "")[:120],
                "len": len(v.get("prompt") or "")}
        if with_prompt:
            item["prompt"] = v.get("prompt", "")
        out.append(item)
    return out


def get_version(skill_id, ts):
    with _LOCK:
        d = _load()
    try:
        ts = int(ts)
    except Exception:  # noqa: BLE001
        return None
    for v in reversed(d.get(skill_id, [])):
        if int(v.get("ts", -1)) == ts:
            return dict(v)
    return None

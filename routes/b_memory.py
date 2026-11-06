# -*- coding: utf-8 -*-
"""routes.b_memory — 记忆管理 API（列表/删行/清空）
"""
# -*- coding: utf-8 -*-
import os
import re

from flask import Blueprint, request, jsonify


from version import get as _version_get  # v1.0.510（P2-13）：版本兜底块收敛到 version.get
VERSION = _version_get()


bp = Blueprint("mgmt_memory", __name__)

# ============================================================
# 本地记忆沉淀（v1.0.312）：设置页 查看/清空；
# 提炼钩子在 routes/chat.py（会话完成后 memory_ops.remember_async）。
# ============================================================
@bp.route("/memory/list")
def api_memory_list():
    """v1.0.328：记忆逐条列表（供前端单条删除/查看评分）。"""
    from services.memory_ops import memory_path
    p = memory_path()
    items = []
    if os.path.isfile(p):
        for idx, line in enumerate(open(p, encoding="utf-8", errors="ignore").read().splitlines()):
            if not line.strip():
                continue
            m = re.match(r"^\-\s*\[([^\]]+)\]\s*(.*)$", line.strip())
            ts, body = (m.group(1), m.group(2)) if m else ("", line.strip())
            sm = re.match(r"^\((\d+)分\)\s*(.*)$", body)
            score = int(sm.group(1)) if sm else None
            text = sm.group(2) if sm else body
            items.append({"idx": idx, "ts": ts, "score": score, "text": text})
    return jsonify({"ok": True, "items": items, "path": p})


@bp.route("/memory/delete_line", methods=["POST"])
def api_memory_delete_line():
    """v1.0.328：删除单条记忆（按行序号，原子写回）。"""
    from services.memory_ops import memory_path
    data = request.get_json(force=True, silent=True) or {}
    idx = data.get("idx")
    p = memory_path()
    if not os.path.isfile(p) or idx is None:
        return jsonify({"ok": False, "error": "参数错误或记忆为空"}), 400
    lines = []
    # v1.0.361 审查修复（P18）：with 关闭句柄（原 open().read() 依赖 GC）
    with open(p, encoding="utf-8", errors="ignore") as f:
        lines = [l for l in f.read().splitlines()]
    # idx 是"非空行序号"——映射回实际行
    nonempty = [i for i, l in enumerate(lines) if l.strip()]
    if not isinstance(idx, int) or idx < 0 or idx >= len(nonempty):
        return jsonify({"ok": False, "error": "序号越界"}), 400
    del lines[nonempty[idx]]
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, p)
    return jsonify({"ok": True, "remaining": len([l for l in lines if l.strip()])})


@bp.route("/memory/clear", methods=["POST"])
def api_memory_clear():
    from services.memory_ops import clear_memory
    n = clear_memory()
    return jsonify({"ok": True, "removed": n})

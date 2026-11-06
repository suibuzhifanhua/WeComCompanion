# -*- coding: utf-8 -*-
"""routes/conversations.py — 会话 CRUD API（v1.0.327）"""
from flask import Blueprint, request, jsonify

from services import conversations

bp = Blueprint("conversations", __name__, url_prefix="/api")


@bp.route("/conversations", methods=["GET"])
def api_conv_list():
    return jsonify({"ok": True, "conversations": conversations.list_all()})


@bp.route("/conversations/create", methods=["POST"])
def api_conv_create():
    data = request.get_json(force=True, silent=True) or {}
    cid = conversations.create(title=data.get("title") or "")
    return jsonify({"ok": True, "id": cid})


@bp.route("/conversations/messages", methods=["GET"])
def api_conv_messages():
    cid = request.args.get("id") or ""
    data = conversations.load(cid)
    if not data:
        return jsonify({"ok": False, "error": "会话不存在"}), 404
    return jsonify({"ok": True, "id": cid, "title": data.get("title", ""),
                    "archived": data.get("archived", False),
                    "messages": data.get("messages", [])})


@bp.route("/conversations/unarchive", methods=["POST"])
def api_conv_unarchive():
    """v1.0.642：把归档的任务放回任务模块（入口在「设置 → 已归档任务」）。"""
    data = request.get_json(force=True, silent=True) or {}
    cid = data.get("id") or ""
    if not cid:
        return jsonify({"ok": False, "error": "缺少 id"}), 400
    ok = conversations.unarchive(cid)
    return jsonify({"ok": ok}), (200 if ok else 404)


@bp.route("/conversations/delete", methods=["POST"])
def api_conv_delete():
    data = request.get_json(force=True, silent=True) or {}
    cid = data.get("id") or ""
    if not cid:
        return jsonify({"ok": False, "error": "缺少 id"}), 400
    ok = conversations.delete(cid)
    return jsonify({"ok": ok}), (200 if ok else 404)


@bp.route("/conversations/batch_delete", methods=["POST"])
def api_conv_batch_delete():
    """v1.0.375 会话批量删除（列表多选/全选配套）：{ids:[...]}。
    逐条删除并分别汇报结果；单次上限 200 条防滥用。"""
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "缺少 ids"}), 400
    ids = [str(i) for i in ids if i][:200]
    deleted, missing = [], []
    for cid in ids:
        (deleted if conversations.delete(cid) else missing).append(cid)
    return jsonify({"ok": True, "deleted": deleted, "missing": missing})


@bp.route("/conversations/archive", methods=["POST"])
def api_conv_archive():
    data = request.get_json(force=True, silent=True) or {}
    cid = data.get("id") or ""
    if not cid:
        return jsonify({"ok": False, "error": "缺少 id"}), 400
    ok, note = conversations.archive(cid, summarize=True)
    if not ok:
        return jsonify({"ok": False, "error": "会话不存在"}), 404
    return jsonify({"ok": True, "summary_note": (note or "")[:200]})


@bp.route("/conversations/clear", methods=["POST"])
def api_conv_clear():
    """v1.0.502：清空会话全部消息（composer「🗑 清空」对具名会话直通，
    取代旧「引导去 ⋯ → 删除」提示）。会话与标题保留，消息清零。"""
    data = request.get_json(force=True, silent=True) or {}
    cid = data.get("id") or ""
    if not cid:
        return jsonify({"ok": False, "error": "缺少 id"}), 400
    ok, err = conversations.clear_messages(cid)
    return jsonify({"ok": ok, "error": err}), (200 if ok else 404)


@bp.route("/conversations/update_message", methods=["POST"])
def api_conv_update_message():
    """气泡编辑（v1.0.334）：按 role+ordinal 定位更新内容。"""
    data = request.get_json(force=True, silent=True) or {}
    cid = data.get("id") or ""
    role = data.get("role") or ""
    ordinal = data.get("ordinal")
    content = data.get("content")
    if not cid or role not in ("user", "agent") or ordinal is None:
        return jsonify({"ok": False, "error": "参数不全（id/role/ordinal/content）"}), 400
    if content is None:
        return jsonify({"ok": False, "error": "缺少 content"}), 400
    try:
        ordinal_i = int(ordinal)
    except (TypeError, ValueError):
        # v1.0.470（P2-17）：ordinal 非数字返回 400——原 int() 直接抛 ValueError 500
        return jsonify({"ok": False, "error": "ordinal 必须是整数"}), 400
    ok, err = conversations.mutate_message(cid, role, ordinal_i, content=str(content))
    return jsonify({"ok": ok, "error": err}), (200 if ok else 400)


@bp.route("/conversations/delete_message", methods=["POST"])
def api_conv_delete_message():
    """气泡删除（v1.0.334）：按 role+ordinal 定位删除。"""
    data = request.get_json(force=True, silent=True) or {}
    cid = data.get("id") or ""
    role = data.get("role") or ""
    ordinal = data.get("ordinal")
    if not cid or role not in ("user", "agent") or ordinal is None:
        return jsonify({"ok": False, "error": "参数不全（id/role/ordinal）"}), 400
    try:
        ordinal_i = int(ordinal)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "ordinal 必须是整数"}), 400   # v1.0.470（P2-17）
    ok, err = conversations.mutate_message(cid, role, ordinal_i, delete=True)
    return jsonify({"ok": ok, "error": err}), (200 if ok else 400)

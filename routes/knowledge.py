"""
routes/knowledge — 知识库（产物库/知识库/课件·题库·制度）文件树 / 读取
====================================================================
v1.0.294 新增。知识库是产物库下的嵌套分类目录：
  课件 = 课程课件 OCR 原文（learn_capture 产出，考试素材）
  题库 = 考试答题记录（exam-solver 产出，试错台账）
  制度 = 公司制度/管理办法原文（预留，人工上传）
独立于 /api/workspace（后者只列 5 个扁平子目录），供前端「📚知识库」视图分门别类展示。
"""
import os
import datetime

from flask import Blueprint, request, jsonify

from core_utils import (log_event, KB_ROOT, KB_CATEGORIES)
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）
from core import safe_open  # v1.0.646：打开动作唯一收口

bp = Blueprint("knowledge", __name__, url_prefix="/api")

# v1.0.301：「消化」列只读展示（AI 每日消化产出，不在三分类写入白名单内）
_DIGEST_CAT = "消化"
_READABLE_CATS = KB_CATEGORIES + (_DIGEST_CAT,)


def _kb_tree():
    """返回知识库分类文件树（name/size/mtime/count），供前端分栏展示。
    v1.0.301：末尾追加「消化」列（只读，展示 知识库/消化/ 下的每日消化文件）。"""
    root = paths.workspace_root()
    cats = []
    for cat in KB_CATEGORIES:
        d = os.path.join(root, KB_ROOT, cat)
        files = []
        try:
            for fn in sorted(os.listdir(d)):
                fp = os.path.join(d, fn)
                if os.path.isfile(fp):
                    st = os.stat(fp)
                    files.append({"name": fn, "size": st.st_size,
                                  "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        except Exception as e:  # noqa: BLE001
            log_event(f"[knowledge] 枚举 {cat} 失败：{e}")
        cats.append({"cat": cat, "dir": d, "count": len(files), "files": files})
    return {"root": root, "kb_root": os.path.join(root, KB_ROOT), "categories": cats}


@bp.route("/knowledge", methods=["GET"])
def api_knowledge():
    """GET /api/knowledge → 知识库三分类文件树。"""
    return jsonify(_kb_tree())


@bp.route("/knowledge/file", methods=["GET"])
def api_knowledge_file():
    """GET /api/knowledge/file?cat=课件&name=xxx.md → 读知识库某文件内容（>5MB 只给预览）。"""
    cat = request.args.get("cat", "")
    name = os.path.basename(request.args.get("name", ""))
    if cat not in _READABLE_CATS or not name:
        return jsonify({"ok": False, "error": "参数无效"}), 400
    fp = os.path.join(paths.workspace_root(), KB_ROOT, cat, name)
    if not os.path.isfile(fp):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        # v1.0.614：预览截断收敛到 core.files.read_preview（原来这里和
        # routes/workspace.py 各写一遍，阈值也各写一遍）
        from core.files import read_preview
        content, size, truncated = read_preview(fp)
        return jsonify({"ok": True, "name": name, "cat": cat, "content": content,
                        "size": size, "truncated": truncated})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/knowledge/open", methods=["POST"])
def api_knowledge_open():
    """POST /api/knowledge/open {cat?} → 用资源管理器打开知识库根目录或某分类文件夹。"""
    data = request.get_json(force=True, silent=True) or {}
    cat = data.get("cat")
    try:
        base = os.path.join(paths.workspace_root(), KB_ROOT)
        target = os.path.join(base, cat) if cat in KB_CATEGORIES else base
        if os.path.isdir(target):
            # v1.0.646：走 core/safe_open 收口（此前这里是 os.startfile 裸调用）
            ok, err = safe_open.open_path(target, roots=[paths.workspace_root()])
            if not ok:
                return jsonify({"ok": False, "error": err}), 400
            return jsonify({"ok": True, "opened": target})
        return jsonify({"ok": False, "error": "目录不存在"}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"打开失败：{e}"}), 500


# ---------- v1.0.301：后台维护（空闲整理 + 每日消化） ----------
@bp.route("/knowledge/maintenance/status", methods=["GET"])
def api_knowledge_maintenance_status():
    """GET /api/knowledge/maintenance/status → 知识库维护守护状态（空闲/上次整理/今日消化/失败次数）。"""
    try:
        from services import knowledge_ops  # 惰性导入防循环
        return jsonify({"ok": True, **knowledge_ops.keeper.status()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/knowledge/maintenance/run", methods=["POST"])
def api_knowledge_maintenance_run():
    """POST /api/knowledge/maintenance/run {action: organize|digest, force?}
    → 手动立即触发（后台线程执行，立即返回 accepted；进度看 status 轮询）。
    digest force=true 覆盖当天已有消化文件。"""
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action", "")
    force = bool(data.get("force", False))
    if action not in ("organize", "digest"):
        return jsonify({"ok": False, "error": "action 必须是 organize 或 digest"}), 400
    try:
        from services import knowledge_ops  # 惰性导入防循环
        return jsonify(knowledge_ops.keeper.run_manual(action, force=force))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------- v1.0.477 企微聊天文件静默分析（知识库） ----------------
@bp.route("/wecom_archive/status", methods=["GET"])
def api_wecom_archive_status():
    """企微文件知识库状态：账号/源文件数/已归档数/上次扫描（消息文本加密边界见 services/wecom_archive.py）。"""
    from services import wecom_archive
    return jsonify(wecom_archive.status())


@bp.route("/wecom_archive/scan", methods=["POST"])
def api_wecom_archive_scan():
    """手动触发一次静默分析（后台线程，立即返回；进度看 status）。
    v1.0.588：未授权时 archive_once 直接返回 {ok:False, need_consent:True}，不搬运任何文件。"""
    from services import wecom_archive
    return jsonify(wecom_archive.archive_async())


@bp.route("/wecom_archive/consent", methods=["POST"])
def api_wecom_archive_consent():
    """v1.0.588 合规闸：解析本机企微聊天附件正文需用户明确授权（该功能会把聊天文件
    内容复制进产物库/知识库）。授权状态持久化，可随时撤回。"""
    from services import wecom_archive
    data = request.get_json(force=True, silent=True) or {}
    value = bool(data.get("consent"))
    ok = wecom_archive.set_consent(value)
    return jsonify({"ok": ok, "consent": value})

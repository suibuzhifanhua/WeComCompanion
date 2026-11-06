"""
routes/workspace — 产物库文件树/上传/读取/打开/大小
====================================================================
从 backend.py 逐字迁入（T05）。R7 修复：补 workspace_size_info import（原缺失致 /api/workspace/size 500）。
"""
import os
import datetime

from flask import Blueprint, request, jsonify

from core_utils import (log_event, ws_rel_path, _timestamp, _parse_file_content, workspace_size_info, dnd_allow_paths, dnd_allowed)
from core.workspace_io import archive_progress_round   # v1.0.650 F5：进度台账开新一轮
from core.result import err_msg
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）
from core import safe_open  # v1.0.646：打开动作唯一收口（startfile 判据不再散落）

bp = Blueprint("workspace", __name__, url_prefix="/api")


@bp.route("/workspace/allow", methods=["POST"])
def api_workspace_allow():
    """v1.0.369 拖拽路径白名单登记：前端把用户拖入聊天框的本地文件/文件夹绝对路径
    上报至此，agent 只读工具（read_workspace_file / list_workspace_files）据此放行原路径。
    只读放行（写类工具仍限制产物库内）；白名单进程级内存、重启清空。"""
    data = request.get_json(force=True, silent=True) or {}
    paths = data.get("paths") or []
    n = dnd_allow_paths(paths)
    if n == 0:
        return jsonify({"ok": False, "error": "无可登记的有效路径"}), 400
    # v1.0.370：返回登记成功的条目（realpath + 类型），前端据此渲染附件 chip
    # v1.0.405（H4 纵深防御）：登记条目必须真实存在于文件系统——伪造路径（如 XSS
    # 直接 POST 任意路径）不再进白名单；配合 API 鉴权（H1）后跨站请求本身已 401。
    items = []
    for p in paths[:100]:
        if not isinstance(p, str) or not p.strip():
            continue
        rp = os.path.realpath(p)
        if os.path.exists(rp) and dnd_allowed(rp):
            items.append({"path": rp, "is_dir": os.path.isdir(rp),
                          "name": os.path.basename(rp.rstrip("\\/")) or rp})
    log_event(f"[workspace] 拖拽白名单登记 {n} 个路径")
    return jsonify({"ok": True, "allowed": n, "items": items})

# ---------- 产物库：文件树 / 读取 / 打开 ----------
def _ws_sub_dir(sub):
    """展示/读取用的目录解析。不能走 ws_sub()：它对不在写入名单里的段（台账/日志）
    静默回退成「产出」，会把只读段读成别的目录。"""
    return os.path.join(paths.workspace_root(), str(sub))


def _ws_tree():
    root = paths.workspace_root()
    tree = []
    for sub in paths.tree_subs():          # v1.0.616：动态段（日报/备份/自修复… 自动可见）
        d = _ws_sub_dir(sub)
        files = []
        try:
            for fn in sorted(os.listdir(d)):
                fp = os.path.join(d, fn)
                if os.path.isfile(fp):
                    st = os.stat(fp)
                    files.append({"name": fn, "size": st.st_size,
                                  "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        except Exception as e:  # noqa: BLE001
            log_event(f"[workspace] 枚举文件失败：{e}")
        tree.append({"sub": sub, "dir": d, "files": files,
                     "readonly": sub not in paths._WORKSPACE_SUBS})
    return {"root": root, "tree": tree}


@bp.route("/workspace/size", methods=["GET"])
def api_workspace_size():
    """P1-7: 产物库大小信息（含分目录与大文件清单），供前端提示清理。
    v1.0.614：统计含 组件/（运行时）后总口径必然超过"该清理"阈值，故 over_limit 只看
    可清理部分（总量减运行时）；?refresh=1 强制重新遍历（组件装完/删完核对用）。"""
    info = workspace_size_info(refresh=bool(request.args.get("refresh")))
    cleanable = info["total_mb"] - info.get("runtime_mb", 0.0)
    info["cleanable_mb"] = round(cleanable, 2)
    info["over_limit"] = cleanable >= 500
    return jsonify(info)


@bp.route("/workspace", methods=["GET"])
def api_workspace():
    return jsonify(_ws_tree())


@bp.route("/workspace/upload", methods=["POST"])
def api_workspace_upload():
    """上传文件到产物库/上传，返回保存路径与解析预览（供 agent 分析）。"""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "缺少 file 字段"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "文件名为空"}), 400
    try:
        filename = os.path.basename(f.filename) or "upload.bin"
        path = ws_rel_path("上传", filename)
        if os.path.exists(path):
            base, ext = os.path.splitext(filename)
            path = ws_rel_path("上传", f"{base}_{_timestamp()}{ext}")
        f.save(path)
        size = os.path.getsize(path)
        # 解析预览：前 3000 字符，便于前端/agent 快速了解内容
        kind, content, note = _parse_file_content(path, size)
        preview = ""
        if content:
            preview = content[:3000]
            if len(content) > 3000:
                preview += "\n…(预览截断)"
        return jsonify({"ok": True, "path": path, "filename": os.path.basename(path),
                        "size": size, "kind": kind or "unknown",
                        "note": note or "", "preview": preview})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"上传失败：{e}"}), 500


@bp.route("/workspace/file", methods=["GET"])
def api_workspace_file():
    """读产物文件的文本内容，给界面内预览用。

    v1.0.645（用户："产生的 md 文件不能在对话界面展开看详情"）：新增 `ref` 参数——
    对话里的文件行手上只有一句路径（`产物库/产出/x.md`、`产出/x.md`、甚至桌面绝对路径），
    走 `resolve_workspace_ref` 这**同一道**安全闸（与「定位/打开」完全同源，不另起一份放行规则：
    两处各判一次，迟早一处松一处紧）。非文本扩展名直接 415，让前端把按钮让位给「📄 打开」。
    """
    ref = request.args.get("ref")
    if ref:
        rp = resolve_workspace_ref(ref)
        if not rp or not os.path.isfile(rp) or not _previewable_path(rp):
            # 三种拒绝合并成一句：不区分"不存在"和"不许读"，免得这个接口变成产物库探测器
            return jsonify({"ok": False, "error": "路径不在可预览范围内（或文件已不存在）"}), 404
        ext = os.path.splitext(rp)[1].lstrip(".").lower()
        if ext not in _PREVIEW_TEXT_EXT:
            return jsonify({"ok": False, "binary": True,
                            "error": "%s 文件不支持在软件里预览，请用「📄 打开」" % ext}), 415
        return _file_payload(rp, name=os.path.basename(rp), sub="")
    sub = request.args.get("sub", "产出")
    name = os.path.basename(request.args.get("name", ""))
    if sub not in paths.tree_subs() or not name:
        return jsonify({"ok": False, "error": "参数无效"}), 400
    fp = os.path.join(_ws_sub_dir(sub), name)
    if not os.path.isfile(fp):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    return _file_payload(fp, name=name, sub=sub)


def _file_payload(fp, name, sub):
    try:
        # 大小上限：超大文件只返回预览，避免全量读入内存拖垮后端（完整内容请用 read_workspace_file 工具分片读取）
        # v1.0.614：截断逻辑收敛到 core.files.read_preview（阈值与提示文案只有一份）
        from core.files import read_preview
        content, size, truncated = read_preview(
            fp,
            truncated_hint="\n…（文件过大 {size} 字节，仅预览前 {limit} 字节。"
                           "如需完整内容，请在对话中让 agent 用 read_workspace_file 工具分片读取）")
        return jsonify({"ok": True, "name": name, "sub": sub, "content": content,
                        "size": size, "truncated": truncated,
                        "ext": os.path.splitext(fp)[1].lstrip(".").lower()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/workspace/new-round", methods=["POST"])
def api_workspace_new_round():
    """v1.0.650（F5）：进度台账「开新一轮」——把 产出/<key>_进度.md 改名归档。
    与 agent 工具 new_progress_round 走同一个 core.workspace_io.archive_progress_round，
    两条路各写一遍归档规则必漂（一处改名、另一处还在读旧名）。"""
    data = request.get_json(force=True, silent=True) or {}
    out = archive_progress_round(str(data.get("sub") or "产出"), str(data.get("name") or ""))
    if not (isinstance(out, dict) and out.get("ok")):
        return jsonify({"ok": False, "error": err_msg(out or {}) or "开新一轮失败"}), 400
    return jsonify(out)


@bp.route("/workspace/open", methods=["POST"])
def api_workspace_open():
    """用系统资源管理器打开产物库文件夹，或打开某个具体文件。"""
    data = request.get_json(force=True, silent=True) or {}
    sub = data.get("sub")
    name = data.get("name")
    try:
        # v1.0.306：设置页「打开日志文件夹」——产物库/日志/（companion.log 与用量存档所在地）
        if data.get("dir") == "logs":
            d = os.path.join(paths.workspace_root(), "日志")
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            ok, err = safe_open.open_path(d, roots=[paths.workspace_root()])
            if not ok:
                return jsonify({"ok": False, "error": err}), 400
            return jsonify({"ok": True, "opened": d})
        if name and sub in paths.tree_subs():
            fp = os.path.join(_ws_sub_dir(sub), os.path.basename(name))
            if os.path.exists(fp):
                ok, err = safe_open.open_path(fp, roots=[paths.workspace_root()])
                if not ok:
                    return jsonify({"ok": False, "error": err}), 400
                return jsonify({"ok": True, "opened": fp})
        root = paths.workspace_root()
        ok, err = safe_open.open_path(root, roots=[root])
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "opened": root})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"打开失败：{e}"}), 500


# ---------------------------------------------------------------- 聊天里的"跳到文件"
def _norm_ref(s):
    """去掉模型/界面常带的包裹引号、反斜杠与首尾空白；空或含 NUL 直接 None。"""
    s = str(s or "").strip().strip('"').strip("'")
    if not s or "\x00" in s:
        return None
    return s


# 聊天里"跳到文件"能定位的顶层目录黑名单：`系统/`（api_token 等本机凭据）、
# `组件/`（便携运行时，几万文件，用户在聊天里永远不会需要跳到它）。
# 不用 `paths._TREE_HIDE` 代替：那份清单为"产物库界面不列噪音"而设计，里面还含
# 知识库/，而报告类文件正是要从知识库跳过去的，照抄会把功能砍掉一块。
_LOCATE_DENY_TOP = ("系统", "组件")


def _under_root_and_visible(rp, root):
    """在产物库内 **且**不在上面那份黑名单顶层目录里，才算可定位。"""
    if not (rp == root or rp.startswith(root + os.sep)):
        return False
    rel = os.path.relpath(rp, root).replace("\\", "/")
    top = rel.split("/", 1)[0]
    return top not in _LOCATE_DENY_TOP


# v1.0.641（用户实报：付款凭证汇总.pdf / 发票汇总.pdf 生成在**桌面**，聊天里既进不了
# 「本次生成的文件」清单、路径也点不动）：产物库外的落点也要能跳。
# 但不是"任意路径"——只放开用户自己的产出落点，且解析后必须仍在其中：
# 这个入口挂在聊天气泡上，ref 来自模型写出的文本，必须假设它能被提示注入伪造。
_OUTSIDE_ALLOW_NAMES = ("Desktop", "Downloads", "Documents")


def _outside_allow_dirs():
    home = os.path.expanduser("~")
    cands = [os.path.join(home, n) for n in _OUTSIDE_ALLOW_NAMES]
    pub = os.environ.get("PUBLIC") or ""
    if pub:
        cands.append(os.path.join(pub, "Desktop"))     # 公用桌面（部分机器落到这里）
    out = []
    for c in cands:
        try:
            if c and os.path.isdir(c):
                out.append(os.path.realpath(c))
        except OSError:
            continue
    return out


# `action="open"` 走的是 os.startfile = 真的会执行关联程序。所以即便文件就在桌面上，
# 可执行与脚本类扩展名也一律不开——用户要跳的是"我刚生成的那份 PDF/表格"，不是程序。
_LOCATE_DENY_EXT = frozenset("""exe bat cmd ps1 psm1 vbs vbe js jse wsf msi msp scf scr pif
 hta cpl lnk url reg jar py pyw pyz dll sys msu""".split())


# v1.0.645：能在软件里**当文本预览**的扩展名。与 _LOCATE_DENY_EXT 是两回事——
# 那条管"别 startfile 一个脚本"，这条管"读出来是不是一堆可读字符"。
# 所以 .py/.js/.css 在这里是允许的（看源码不执行），而 pdf/office/图片一律 415。
_PREVIEW_TEXT_EXT = frozenset("""md markdown txt csv tsv json jsonl yaml yml ini conf toml
 xml html htm log py js ts css sql bak""".split())

# 预览的落点比"定位"更窄：定位只是打开资源管理器，预览是把**字节读进界面**。
# _under_root_and_visible 用的是黑名单（只挡 系统/组件），照它放行就等于能读
# 产物库/会话/*.json——那里面是全部对话原文。所以预览只认这几个交付段，
# 日志段也不给（companion.log 里可能有 token 与路径，且它不是交付物）。
_PREVIEW_ALLOW_TOPS = ("产出", "下载", "知识库", "台账", "归档")


def _locate_roots():
    """「跳到文件」允许打开的根：产物库本身 + 用户产出目录（桌面/下载/文档）。

    v1.0.646：与 _locate_* 那套判据同源，给 core/safe_open 做前缀校验用——
    之前 startfile 直接吃 fp，扩展名黑名单之外没有第二道（根前缀）防线。"""
    return [paths.workspace_root()] + list(_outside_allow_dirs())


def _previewable_path(rp):
    root = os.path.realpath(paths.workspace_root())
    if rp == root or rp.startswith(root + os.sep):
        top = os.path.relpath(rp, root).replace("\\", "/").split("/", 1)[0]
        return top in _PREVIEW_ALLOW_TOPS
    return _locatable_outside(rp)


def _locatable_outside(rp):
    """产物库外的路径要可定位：扩展名不是可执行/脚本 **且** 解析后仍在用户产出目录内。

    但**先排除软件自己的目录树**：本机 dist/产物库 就住在 `Desktop\\企微伴侣\\` 底下，
    不排掉的话"放开桌面"会连带把 `产物库/系统/api_token.txt`（凭据）也放开——
    探针实测确实放开了（v1.0.641 自查）。产物库内的路径一律只走产物库那套规则。"""
    ext = os.path.splitext(rp)[1].lstrip(".").lower()
    if not ext or ext in _LOCATE_DENY_EXT:
        return False
    root = os.path.realpath(paths.workspace_root())
    app_dir = os.path.dirname(root)
    if rp == root or rp.startswith(root + os.sep):
        return False
    if rp == app_dir or rp.startswith(app_dir + os.sep):
        return False
    return any(rp == d or rp.startswith(d + os.sep) for d in _outside_allow_dirs())


def resolve_workspace_ref(ref):
    """把界面上拿到的一句路径解析成**产物库内**的绝对路径；解析不到或越界返回 None。

    接受四种写法：绝对路径、`产物库/产出/x.md`（相对运行目录）、`产出/x.md`
    （相对产物库）、以及只给文件名（在产物库各可见目录里唯一命中才算，重名不猜）。

    产物库**外**只有桌面/下载/文档这几个用户产出目录、且扩展名不是可执行/脚本时才认
    （v1.0.641：AI 把交付 PDF 直接写到桌面是常见形态，之前一律 404，聊天里点不动）。

    为什么要 realpath 后再比前缀 + 查黑名单顶层：这个入口挂在聊天气泡上，`ref` 来自
    模型写出的文本或前端 DOM，必须假设它可以被伪造（提示注入 / 跨站请求）。越界一律拒；
    `系统/`（api_token）虽然也在产物库内，但它是本机凭据，不开放。
    """
    s = _norm_ref(ref)
    if not s:
        return None
    root = os.path.realpath(paths.workspace_root())
    flat = s.replace("\\", "/")
    cands = [s] if os.path.isabs(s) else [
        os.path.join(root, flat),
        os.path.join(os.path.dirname(root), flat),      # 「产物库/产出/x.md」这种带前缀的写法
    ]
    for c in cands:
        try:
            rp = os.path.realpath(c)
        except OSError:
            continue
        if not os.path.exists(rp):
            continue
        if _under_root_and_visible(rp, root):
            return rp
        # v1.0.641：产物库外但落在用户产出目录（桌面/下载/文档）里的交付物也认——
        # 前提是扩展名不是可执行/脚本（见 _locatable_outside）。
        if _locatable_outside(rp):
            return rp
    base = flat.rsplit("/", 1)[-1]
    if base and "/" not in flat:
        hits = []
        for sub in paths.tree_subs():
            if sub in _LOCATE_DENY_TOP:
                continue
            try:
                d = _ws_sub_dir(sub)
            except Exception:  # noqa: BLE001
                continue
            p = os.path.join(d, base)
            if os.path.exists(p):
                hits.append(os.path.realpath(p))
        if len(set(hits)) == 1:
            return hits[0]
    return None


@bp.route("/workspace/locate", methods=["POST"])
def api_workspace_locate():
    """v1.0.627：AI 产出的文件在聊天里一键跳到它所在的位置。

    `action="reveal"`（默认）= 打开资源管理器并**选中**该文件——用户要的"方便找到生成的
    文件"就是这一下；`action="open"` = 用系统关联程序直接打开。目录则两种都是打开目录。
    只接受 `resolve_workspace_ref` 能解析到产物库内的路径，否则 404：宁可不响应，
    也不给前端一个能开任意路径的口子。
    """
    data = request.get_json(force=True, silent=True) or {}
    action = str(data.get("action") or "reveal").strip().lower()
    fp = resolve_workspace_ref(data.get("ref"))
    if not fp:
        return jsonify({"ok": False, "error": "没找到该文件，或它不在产物库内"}), 404
    try:
        if action == "open" or os.path.isdir(fp):
            # v1.0.646：唯一收口——扩展名黑名单/存在性/根前缀判据都在 core/safe_open
            ok, err = safe_open.open_path(fp, roots=_locate_roots())
            if not ok:
                return jsonify({"ok": False, "error": err}), 400
        else:
            import subprocess
            # explorer /select, 即使成功也常返回退出码 1（Shell 的既有行为），
            # 所以只看"能不能启动进程"，不看退出码——判退出码会把成功当失败。
            subprocess.Popen(["explorer", "/select,", fp.replace("/", os.sep)],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log_event(f"[workspace] 定位产出文件：action={action} path={fp}")
        return jsonify({"ok": True, "path": fp, "action": action})
    except Exception as e:  # noqa: BLE001
        log_event(f"[workspace] 定位文件失败（{fp}）：{e}")
        return jsonify({"ok": False, "error": f"打开失败：{e}"}), 500
# -*- coding: utf-8 -*-
"""routes.b_repair — 自我修复 API（体检/报告/取消/一键打包）
"""
# -*- coding: utf-8 -*-
import json
import os
import re
import sys
import threading

from flask import Blueprint, request, jsonify

from core_utils import log_event
from core.config_store import load_config

from version import get as _version_get  # v1.0.510（P2-13）：版本兜底块收敛到 version.get
VERSION = _version_get()


bp = Blueprint("mgmt_repair", __name__)

# ============================================================
# 自我修复（v1.0.348）：错误日志+经验库 → LLM 体检 → 白名单自动修复 + 报告落盘。
# 安全边界：skill_lesson/experience 可自动落地（均可逆），code_fix/config_suggest 只出报告。
# ============================================================

# ---- v1.0.407 修复：版本管理端点接线 ----
# 前端版本管理页（js/11 loadVersionList/switchVersion）调 GET /api/versions 与
# POST /api/versions/switch，业务层 services/version_store 的 list_versions/
# switch_version 早已完整，但 HTTP 层从未注册——版本列表与一键回退一直 404
#（前后端契约校验抓出）。与 /repair/rebuild 同源（版本/rebuild 都操作源码+dist）。
@bp.route("/versions", methods=["GET"])
def api_versions_list():
    from services.version_store import list_versions
    try:
        return jsonify(list_versions())
    except Exception as e:  # noqa: BLE001
        log_event(f"[versions] 列表读取失败：{e}")
        return jsonify({"current": "", "versions": [], "error": str(e)})


@bp.route("/versions/switch", methods=["POST"])
def api_versions_switch():
    """一键回退到登记版本（快照当前 → git 回源码 → 换 exe，重启生效）。"""
    from services.version_store import switch_version
    data = request.get_json(force=True, silent=True) or {}
    ver = str(data.get("version") or "").strip()
    if not ver:
        return jsonify({"ok": False, "error": "缺少 version 参数"}), 400
    res = switch_version(ver)
    if res.get("ok"):
        log_event(f"[versions] 已切换到 v{ver}（重启生效）")
    return jsonify(res)


@bp.route("/repair/run", methods=["POST"])
def api_repair_run():
    """手动触发一次自我体检与修复。
    background=true（前端默认用）：后台线程执行立即返回——LLM 重试链在模型不可达时
    可能超过 3 分钟，同步等待会把按钮挂死；结果照常落 产物库/自修复/ 报告，前端轮询。"""
    from services.self_repair import run_self_repair
    data = request.get_json(force=True, silent=True) or {}
    if data.get("background"):
        threading.Thread(target=run_self_repair, args=(load_config(),),
                         daemon=True, name="self-repair-run").start()
        return jsonify({"ok": True, "started": True})
    try:
        return jsonify(run_self_repair(load_config()))
    except Exception as e:
        return jsonify({"ok": False, "error": f"体检失败：{e}"}), 500


@bp.route("/repair/status")
def api_repair_status():
    """体检三合一状态（v1.0.655 B6：原 /repair/last + /repair/progress + /repair/latest
    合并——前端轮询原本每拍 2 个往返，现在 1 个）。返回 {last, progress, latest}
    三键，各键内部形状与原端点逐字节一致（latest 错误降级为子对象 ok 标志，
    不再整体 500——三段独立成败）。run/cancel/rebuild 异质动作端点保留。"""
    from services.self_repair import _report_dir
    from core.files import read_json   # v1.0.654 A8：句柄样板收敛
    d = _report_dir()
    p_last = os.path.join(d, "last_run.json")
    p_prog = os.path.join(d, "progress.json")
    if os.path.isfile(p_last):
        last = read_json(p_last, {})
    else:
        last = {"ok": False, "ts": 0, "error": "尚未运行过体检"}
    if os.path.isfile(p_prog):
        progress = read_json(p_prog, {})
    else:
        progress = {"phase": "idle", "tokens": 0, "elapsed_s": 0}
    try:
        reps = [f for f in os.listdir(d) if f.startswith("健康报告_") and f.endswith(".md")]
        if not reps:
            latest = {"ok": True, "content": "", "name": ""}
        else:
            name = max(reps)
            with open(os.path.join(d, name), encoding="utf-8", errors="ignore") as _f:
                latest = {"ok": True, "content": _f.read(), "name": name}
    except Exception as e:  # noqa: BLE001
        latest = {"ok": False, "error": str(e)}
    return jsonify({"last": last, "progress": progress, "latest": latest})


@bp.route("/repair/cancel", methods=["POST"])
def api_repair_cancel():
    """v1.0.356：请求取消正在运行的体检（流式阶段即时中断，结果落 last_run）。"""
    from services.self_repair import request_repair_cancel
    was_running = request_repair_cancel()
    return jsonify({"ok": True, "was_running": bool(was_running),
                    "note": "" if was_running else "当前没有正在运行的体检"})


# ---- v1.0.373 一键打包并重启（确认式）：自我修复/版本管理页触发 ----
# 软件无法在运行中替换自身 exe（Windows 文件锁 + 打包需杀进程），因此由
# 延迟批处理接管：等 2s（让 HTTP 响应返回）→ 杀进程 → build.py --force 打包 → 启动新 exe。
# 打包失败时 dist 里仍是上一版 exe，脚本照常启动（自然回滚语义）。

def _rebuild_script(src_dir, python_exe, dist_exe, note):
    """生成延迟打包重启批处理。note 过滤 cmd 元字符（引号/换行/管道等）。"""
    safe_note = re.sub(r'["%\r\n|&<>^!]', "", note).strip()[:120] or "软件内一键打包"
    return "\r\n".join([
        "@echo off",
        "rem WeComCompanion one-click rebuild (auto-generated)",
        "timeout /t 2 /nobreak >nul",
        f'taskkill /f /im "{os.path.basename(dist_exe)}" >nul 2>&1',
        f'cd /d "{src_dir}"',
        f'"{python_exe}" build.py --yes --force --note="{safe_note}"',
        f'start "" "{dist_exe}"',
    ])


_LAST_REBUILD_TS = 0.0   # v1.0.373（P3-15）：一键打包 60s 限频

# v1.0.373（用户反馈）：一键打包防空转——源码相对「当前 exe 构建时登记的 git hash」
# 无任何未打包改动时拒绝打包（否则产物与运行版本零差异，只有版本号在空转 +1）。
# 判定优先级：
#   ① git 可用且登记过 hash：HEAD 变了 / 工作树脏 → 有改动；
#   ② 否则按**源码 mtime vs 上次构建时间**判（v1.0.619 新增，见 _newest_source_mtime）。
# v1.0.619（审查⑥）为什么补 ②：这套源码平时**不是 git 仓库**（用户机器上就是裸目录），
# 旧实现在 `not gi or not build_hash` 时直接「保守放行」——于是防空转门禁一直是空的，
# 每次点一键打包都照打不误，版本号 +1、产物零差异，正是当初要拦的那个现象。
# 注意：不能复用 version_store._APP_ROOT——frozen 下 __file__ 指向解包临时目录，
# versions.json 必须按 dist 上级的源码根定位（与 _rebuild_script 的 src_dir 同源）。
_REBUILD_NOISE_FILES = ("config.json",)   # 运行时改写、不进包体，不视为"代码改动"
# 参与"有没有改动"比对的源码域（运行时数据一律排除：产物库/dist/build/组件/知识库）
_REBUILD_SCOPES = ("core", "services", "routes", "agent", "agent_tools",
                   "wecom_bridge", "desktop", "js", "css")
_REBUILD_SCOPES_EXT = (".py", ".js", ".css", ".html")
_REBUILD_ROOT_FILES = ("backend.py", "core_utils.py", "llm_client.py", "index.html",
                       "security.py", "security_ops.py", "version.py", "build.py",
                       "requirements-dev.txt")


def _newest_source_mtime(src_root):
    """源码里最新的修改时间。返回 (mtime, 相对路径)，一个源码文件都没有时返回 (0, "")。"""
    best, best_rel = 0.0, ""
    for sub in _REBUILD_SCOPES:
        d = os.path.join(src_root, sub)
        if not os.path.isdir(d):
            continue
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(_REBUILD_SCOPES_EXT):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    m = os.path.getmtime(fp)
                except OSError:  # noqa: BLE001
                    continue
                if m > best:
                    best, best_rel = m, os.path.relpath(fp, src_root)
    for fn in _REBUILD_ROOT_FILES:
        fp = os.path.join(src_root, fn)
        try:
            m = os.path.getmtime(fp)
        except OSError:  # noqa: BLE001
            continue
        if m > best:
            best, best_rel = m, fn
    return best, best_rel.replace("\\", "/")


def _source_has_changes(src_root):
    """返回 (changed, reason)。"""
    db = os.path.join(src_root, "产物库", "版本", "versions.json")
    build_hash, latest_ver, built_ts = "", "", 0
    try:
        with open(db, encoding="utf-8") as f:
            recs = json.load(f)
        recs = recs if isinstance(recs, list) else recs.get("versions", [])
        if recs:
            build_hash = (recs[0].get("hash") or "").strip()
            latest_ver = (recs[0].get("version") or "").strip()
            try:
                built_ts = int(recs[0].get("ts") or 0)
            except Exception:  # noqa: BLE001
                built_ts = 0
    except Exception:  # noqa: BLE001
        pass
    from core import portable as _pb
    gi = _pb.find_git()      # v1.0.612：三处重复的 git 定位收敛到 core.portable
    if not (gi and build_hash):
        # 没有 git 或没登记过 hash —— 走 mtime 比对，不再"保守放行"（那样门禁恒为空转）
        m, rel = _newest_source_mtime(src_root)
        if not built_ts:
            return True, "没有可比对的构建记录（版本台账为空），照常打包"
        if not rel:
            return True, "在 %s 找不到可比对的源码文件，照常打包" % src_root
        if m > built_ts:
            import datetime as _dt
            return True, "上次构建后有源码改动（最新：%s，%s）" % (
                rel, _dt.datetime.fromtimestamp(m).strftime("%m-%d %H:%M"))
        return False, "源码与当前运行版本（v%s）之后没有任何改动，没有需要打包的" % (latest_ver or "?")
    try:
        import subprocess as _sp

        def _git(*args):
            r = _sp.run([gi, "-C", src_root] + list(args), capture_output=True, timeout=30)
            return (r.stdout or b"").decode("utf-8", "replace").strip() if r.returncode == 0 else ""

        head = _git("rev-parse", "--short", "HEAD")
        if head and head != build_hash:
            return True, "源码有新提交（%s → %s）" % (build_hash, head)
        dirty = [ln for ln in (_git("status", "--porcelain") or "").splitlines()
                 if ln.strip() and not ln.strip().endswith(_REBUILD_NOISE_FILES)]
        if dirty:
            return True, "工作树有未提交改动（%d 个文件）" % len(dirty)
        return False, "源码与当前运行版本（v%s）完全一致，没有需要打包的改动" % (latest_ver or "?")
    except Exception as e:  # noqa: BLE001
        return True, "改动检测异常（%s），保守放行" % e


@bp.route("/repair/rebuild", methods=["POST"])
def api_repair_rebuild():
    """一键打包并重启：确认式（前端二次确认后调用）。仅打包版且本机有构建环境时可用。"""
    import subprocess
    import tempfile
    # v1.0.373（P3-15）：60s 限频——防本机脚本反复触发"杀进程→重打包"
    global _LAST_REBUILD_TS
    import time as _time
    now = _time.time()
    if now - _LAST_REBUILD_TS < 60:
        return jsonify({"ok": False, "error": "刚刚已触发过打包，请等 60 秒后再试"}), 429
    data = request.get_json(force=True, silent=True) or {}
    note = str(data.get("note") or "软件内一键打包")[:120]
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return jsonify({"ok": False, "error": "仅打包版支持一键打包；开发模式请直接运行 build.py"}), 400
    dist_exe = os.path.realpath(sys.executable)
    src_dir = os.path.dirname(os.path.dirname(dist_exe))   # dist/ 的上级 = 源码目录
    if not os.path.isfile(os.path.join(src_dir, "build.py")):
        return jsonify({"ok": False, "error": "未找到 build.py（软件需在源码目录结构内运行才能一键打包）"}), 400
    # v1.0.373（用户反馈）：源码相对当前运行版本无未打包改动 → 拒绝空转打包
    # （自我修复落地的新提交 / 未提交的手工改动都会放行；纯版本号 +1 没有意义）
    try:
        changed, why = _source_has_changes(src_dir)
        if not changed:
            return jsonify({"ok": False,
                            "error": "无需打包：%s。只有当源码出现新改动（如自我修复落地、手工修改）后才需要重新打包。" % why}), 400
        log_event(f"[repair] 一键打包放行：{why}")
    except Exception as e:  # noqa: BLE001
        log_event(f"[repair] 改动检测异常（保守放行）：{e}")
    # 构建用解释器：统一走 b_ocr_admin._locate_build_python（三处口径一致）
    # v1.0.470 修复（P1-19）：删除硬编码他人机器路径。
    # v1.0.473 修复：frozen exe 的 sys.executable 是软件自己不是 python——原探测把
    # WeComCompanion.exe 当解释器写进打包脚本（用户实测 16:40）。
    try:
        from .b_ocr_admin import _locate_build_python
        py_exe, _pi = _locate_build_python()
    except Exception:  # noqa: BLE001
        py_exe = ""
    if not py_exe:
        py_exe = "python"
    try:
        script = _rebuild_script(src_dir, py_exe, dist_exe, note)
        bat = os.path.join(tempfile.gettempdir(), "wecom_rebuild.bat")
        with open(bat, "w", encoding="gbk", errors="replace") as f:   # cmd 按 ANSI/GBK 解析 bat
            f.write(script)
        subprocess.Popen(["cmd", "/c", bat], cwd=src_dir,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"触发打包失败：{e}"}), 500
    _LAST_REBUILD_TS = now
    log_event(f"[repair] 一键打包并重启已触发（note={note}；2s 后杀进程 → build --force → 重启）")
    return jsonify({"ok": True,
                    "hint": "程序将在 2 秒后退出，打包约 1-2 分钟（期间窗口消失），完成后自动启动新版本"})


# ---- v1.0.373 管理员模式：状态查询 + 以管理员身份重启（UAC 提权） ----

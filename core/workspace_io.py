# -*- coding: utf-8 -*-
"""core.workspace_io — 产物库写入/迁移/台账/拖拽白名单（自 core_utils 拆分 v1.0.402）
"""
# -*- coding: utf-8 -*-
import os
import re
import json
import time
import shutil
import datetime
import threading
from core import paths  # noqa: F401（路径函数走属性查找，可被测试 patch）
from core.paths import WORKSPACE_NAME, KB_ROOT, KB_CATEGORIES, _KB_SUBS, _ALLOWED_SUBS, _app_dir, BASE  # noqa
from core.logging import log_event, _timestamp, _LOG_BACKUP_COUNT  # noqa
# v1.0.470 修复（P1-1）：原漏导 _LOG_BACKUP_COUNT——migrate_legacy_layout 第 1 步轮转
# 备份迁移处 NameError 被外层 except 吞掉，.log.1~.log.5 及后续步骤从未执行过。
from core.config import config_path  # noqa

# ---------- v1.0.275：运行时产物整合（原先散落在应用根目录） ----------
# 旧布局（顶层杂乱）：<appdir>/companion.log[.1-.5]、<appdir>/logs/usage_*.jsonl、<appdir>/experiences.json
# 新布局（全部归入产物库）：<appdir>/产物库/日志/companion.log[.1-.5]、<appdir>/产物库/日志/usage_*.jsonl、<appdir>/产物库/experiences.json


def migrate_legacy_layout(data_dir=None):
    """v1.0.275：一次性把旧布局的散落运行时产物并入产物库（幂等，失败不影响启动）。
    规则：旧文件存在且新路径不存在 → move；新路径已存在 → 不动（保留两边，旧文件人工删）。
    全程不删除任何文件。返回已迁移的相对路径列表（空 = 无需迁移）。"""
    try:
        if not data_dir:
            data_dir = os.path.dirname(config_path())
        ws = os.path.join(data_dir, WORKSPACE_NAME)
        logdir = os.path.join(ws, "日志")
        os.makedirs(logdir, exist_ok=True)
        moved = []

        def _move_once(src, dst):
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.move(src, dst)
                    moved.append(os.path.relpath(dst, data_dir))
                except Exception:  # noqa: BLE001
                    pass  # 被占用（另有实例在跑）就跳过，下次启动再试

        # 1) companion.log + 轮转备份
        _move_once(os.path.join(data_dir, "companion.log"), os.path.join(logdir, "companion.log"))
        for i in range(1, _LOG_BACKUP_COUNT + 1):
            _move_once(os.path.join(data_dir, f"companion.log.{i}"), os.path.join(logdir, f"companion.log.{i}"))
        # 2) experiences.json
        _move_once(os.path.join(data_dir, "experiences.json"), os.path.join(ws, "experiences.json"))
        # 3) logs/usage_*.jsonl（含源码模式旧位置 <data_dir>/dist/logs，v1.0.252 起写那里）
        for logs_dir in (os.path.join(data_dir, "logs"), os.path.join(data_dir, "dist", "logs")):
            if not os.path.isdir(logs_dir):
                continue
            for fn in sorted(os.listdir(logs_dir)):
                if fn.startswith("usage_") and fn.endswith(".jsonl"):
                    _move_once(os.path.join(logs_dir, fn), os.path.join(logdir, fn))
            try:
                if not os.listdir(logs_dir):
                    os.rmdir(logs_dir)  # 只删空目录
            except Exception:  # noqa: BLE001
                pass
        return moved
    except Exception:  # noqa: BLE001
        return []


def migrate_knowledge_base(data_dir=None):
    """v1.0.294：一次性把散落旧位置的知识产物归入 产物库/知识库/ 分类子目录（幂等，只移不删）。
    规则（旧位置存在且新位置无同名 → move；否则不动，保留两边供人工核对）：
      - 产物库/知识/*.md            → 知识库/课件/   （课程课件 OCR）
      - 产物库/产出/答题记录_*.md    → 知识库/题库/   （考试答题记录）
      - 知识库/制度/                 → 确保存在（预留）
    全程不删除任何文件；失败静默不阻塞启动。返回已迁移的相对路径列表（空=无需迁移）。"""
    try:
        if not data_dir:
            data_dir = os.path.dirname(config_path())
        ws = os.path.join(data_dir, WORKSPACE_NAME)
        cat_course = os.path.join(ws, KB_ROOT, "课件")
        cat_quiz = os.path.join(ws, KB_ROOT, "题库")
        cat_policy = os.path.join(ws, KB_ROOT, "制度")
        for d in (cat_course, cat_quiz, cat_policy):
            os.makedirs(d, exist_ok=True)
        moved = []

        def _move_once(src, dst):
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.move(src, dst)
                    moved.append(os.path.relpath(dst, data_dir))
                except Exception:  # noqa: BLE001
                    pass  # 被占用/权限问题就跳过，下次启动再试

        # 1) 旧 知识/ 目录的 .md（课程课件）→ 知识库/课件/
        old_know = os.path.join(ws, "知识")
        if os.path.isdir(old_know):
            for fn in sorted(os.listdir(old_know)):
                if fn.lower().endswith(".md"):
                    _move_once(os.path.join(old_know, fn), os.path.join(cat_course, fn))
            # 知识/ 清空后删空目录（不删非空目录，残留文件留给人工）
            try:
                if not os.listdir(old_know):
                    os.rmdir(old_know)
            except Exception:  # noqa: BLE001
                pass
        # 2) 产出/答题记录_*.md → 知识库/题库/
        out_dir = os.path.join(ws, "产出")
        if os.path.isdir(out_dir):
            for fn in sorted(os.listdir(out_dir)):
                if fn.startswith("答题记录_") and fn.lower().endswith(".md"):
                    _move_once(os.path.join(out_dir, fn), os.path.join(cat_quiz, fn))
        return moved
    except Exception:  # noqa: BLE001
        return []
def save_workspace_file(sub, filename, content, append=False):
    """把内容写入产物库。content 可为 str 或 bytes。返回保存后的绝对路径。"""
    path = paths.ws_rel_path(sub, filename)
    try:
        if isinstance(content, bytes):
            mode = "ab" if append else "wb"
            with open(path, mode) as f:
                f.write(content)
        else:
            mode = "a" if append else "w"
            with open(path, mode, encoding="utf-8") as f:
                f.write(content)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"保存失败：{e}"}
    return {"ok": True, "path": path, "sub": sub, "filename": os.path.basename(path)}


# v1.0.650（F5，用户"全部优化"）：进度台账的"轮次"概念。
# `append_progress` 一直往同名 `<key>_进度.md` 纯追加、从不归档，而快捷入口的提示词
# 写着"已记过的课/单直接跳过"（js/02-nav-quicklinks.js），清断点又只删 断点/*.json
# 不碰 md（agent/session.py:drop_chat_state）——于是上一轮的记录会把这一轮的"跳过"
# 喂错：新跑的一轮一开头就被提醒"这些都做过了"，要么空转要么谎报完成。
# 归档名故意用 `轮次归档_` 前缀，两个坑都绕开：
#   ① 提示词让模型读的是 `<key>_进度.md` 与 `<key>_*.md`（skills.py 那条"只读文件名
#      日期后缀最新的一份"）——归档若叫 `<key>_进度_<日期>.md` 正好命中这个通配，
#      等于把刚归档的上一轮又喂回去，跳过照旧；
#   ② 一次性迁移按 `fn.startswith("答题记录_")` 把产出/答题记录_*.md 挪进 知识库/题库/，
#      前缀放最后会被这条规则连带搬走，归档位置就不稳定了。
ROUND_ARCHIVE_PREFIX = "轮次归档_"
_PROGRESS_SUFFIX = "_进度.md"


def _progress_items(txt):
    """与 _run_append_progress 同一套计数口径（记录之间用 --- 分隔）。"""
    if not (txt or "").strip():
        return 0
    return max(1, txt.count("\n---\n") + 1)


def archive_progress_round(sub, filename):
    """把「本轮进度文件」改名归档，为同一 key 腾出干净的一轮。返回归档后的文件名与条目数。"""
    name = os.path.basename(str(filename or ""))
    if not name.endswith(_PROGRESS_SUFFIX):
        return {"ok": False, "error": f"只能归档以 {_PROGRESS_SUFFIX} 结尾的进度文件（收到：{name or '空文件名'}）"}
    src = paths.ws_rel_path(sub, name)
    if not os.path.isfile(src):
        return {"ok": False, "error": f"进度文件不存在：{sub}/{name}（本轮还没记过东西，不用开新一轮）"}
    stem = name[: -len(_PROGRESS_SUFFIX)] or "任务"
    try:
        with open(src, encoding="utf-8") as f:
            txt = f.read()
    except Exception:  # noqa: BLE001
        txt = ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = paths.ws_rel_path(sub, f"{ROUND_ARCHIVE_PREFIX}{stamp}_{stem}{_PROGRESS_SUFFIX}")
    _n = 0
    while os.path.exists(dst):          # 同一秒内连开两轮也不能把前一份覆盖掉
        _n += 1
        dst = paths.ws_rel_path(sub, f"{ROUND_ARCHIVE_PREFIX}{stamp}_{stem}{_PROGRESS_SUFFIX}")[:-3] + f"({_n}).md"
    try:
        os.rename(src, dst)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"归档失败：{e}"}
    items = _progress_items(txt)
    log_event(f"[workspace] 进度台账开新一轮：{sub}/{name} → {os.path.basename(dst)}（{items} 条）")
    return {"ok": True, "archived": os.path.basename(dst), "items": items,
            "sub": sub, "cleared": name}


_LEDGER_NAME = "产物台账.md"
_LEDGER_LOCK = threading.Lock()   # v1.0.510 优化（P1-8）：多线程追加台账行原无锁，可能交错


def _ledger_path():
    return os.path.join(paths.ws_sub("产出"), _LEDGER_NAME)


# ══════ 产物快照（v1.0.635，用户实报"清单漏了产出的 ppt 文件"） ══════
# 清单原先只认**工具结果里带 path** 的产出（save_file / create_pptx / download…）。
# 但 agent 常走另一条路：`save_file` 写一个 python-pptx 脚本 → `pc_run` 跑它 →
# .pptx 由脚本落盘。这条路上没有任何工具结果携带 pptx 的路径，于是清单只列出了
# 三个 .py，正主儿反而没有——用户要的恰恰是那一个。
# 所以补一层**磁盘差集**：任务开始时给产物库的"用户可取用"几段拍快照，收尾时再比一次，
# 新增或被改写的文件都算本次产出。工具上报的仍然优先（语义确定），差集负责兜住
# "脚本/外部进程写出来的文件"这类工具看不见的产出。
OUTPUT_SCAN_SUBS = ("产出", "下载", "知识库")


def snapshot_outputs():
    """{绝对路径: (mtime_ns, 字节数)}。本机实测 719 个文件 ~21ms，一次任务只拍两张。"""
    out = {}
    root = paths.workspace_root()
    ledger = _ledger_path()
    for sub in OUTPUT_SCAN_SUBS:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in ("组件", "__pycache__", "node_modules")]
            for f in fn:
                p = os.path.join(dp, f)
                if p == ledger:      # 台账是记账文件，每次 save_file 都会被追加——列出来纯噪声
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue         # 边扫边被删/占用：跳过即可，不影响其余
                out[p] = (st.st_mtime_ns, st.st_size)
    return out


def new_outputs(before):
    """与起始快照相比，**新增或被改写**的文件（按最后修改时间新→旧）。
    before 为 None（快照失败）时返回空表——清单退回"只列工具上报的产出"，不报错。"""
    if not isinstance(before, dict):
        return []
    now = snapshot_outputs()
    changed = [(stamp[0], p) for p, stamp in now.items()
               if before.get(p) != stamp]
    changed.sort(reverse=True)
    return [p for _t, p in changed]


def append_ledger(sub, filename, source="生成", note=""):
    """把一次产物落盘登记到「产物库/产出/产物台账.md」末尾（首次自动建表头）。
    沉淀每次生成的报告/记录，方便回溯「何时生成了什么」。失败不抛、不阻塞主流程，
    但 v1.0.510 起补日志（原完全静默——台账丢失无任何痕迹）。"""
    try:
        with _LEDGER_LOCK:
            path = _ledger_path()
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("# 企微伴侣产物台账\n\n")
                    f.write("| 时间 | 来源 | 子目录 | 文件 | 备注 |\n")
                    f.write("| --- | --- | --- | --- | --- |\n")
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"| {ts} | {source} | {sub} | {filename} | {note} |\n")
    except Exception as e:  # noqa: BLE001
        try:
            log_event(f"[ledger] 产物台账写入失败（不影响产物本身）：{sub}/{filename}：{e}")
        except Exception:  # noqa: BLE001
            pass  # silent-ok: 日志自身写入失败，不能再抛（台账属尽力而为）


_SIZE_TTL = 600.0          # 只用于组件目录（7 万文件，一次遍历实测 ~1.3s）
_comp_cache = {"ts": 0.0, "mb": None, "lock": threading.Lock()}
_RUNTIME_SUB = "组件"       # 便携 Node/Git/OCR/ASR 运行时：占盘最大，但不是"可清理的垃圾"


def invalidate_size_cache():
    """组件装完/卸完调用：让下一次体积统计重新遍历运行时目录。"""
    with _comp_cache["lock"]:
        _comp_cache["mb"] = None
        _comp_cache["ts"] = 0.0


def _components_mb(refresh=False):
    """组件/ 的体积——整个统计里唯一值得缓存的一段。返回 (MB, 是否取自缓存)。"""
    now = time.time()
    with _comp_cache["lock"]:
        if not refresh and _comp_cache["mb"] is not None and (now - _comp_cache["ts"]) < _SIZE_TTL:
            return _comp_cache["mb"], True
    tot = 0
    for dirpath, _, filenames in os.walk(os.path.join(paths.workspace_root(), _RUNTIME_SUB)):
        for fn in filenames:
            try:
                tot += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    mb = round(tot / 1048576.0, 2)
    with _comp_cache["lock"]:
        _comp_cache["mb"] = mb
        _comp_cache["ts"] = time.time()
    return mb, False


def workspace_size_info(refresh=False):
    """产物库体积：按顶层条目分组 + 大文件清单，供前端显示和清理提示。

    v1.0.614（审查 ㉜）：口径从「只累加 5 个写入子目录」换成「整个产物库」。原口径
    实测报 23.2MB，而产物库实际 1109.4MB——差的 1075.9MB 全在 组件/（一键安装下来的
    便携运行时），既不在目录里也不在树里，用户据此判断"没占多少盘"是错的。
    组件单列为 runtime_mb，且不进 large_files（那是"该清理的东西"清单，删运行时等于
    把刚装的 OCR/语音再下一遍）。
    v1.0.616（审查 ⑭）：缓存**只留给组件**。原先整份结果一起缓存 600s，用户手动删完
    大文件后最长 10 分钟仍报旧体积；除组件外的部分只有千级文件（实测 ~30ms），每次
    真走即可。`cached` 字段现在只描述组件那一段。
    """
    root = paths.workspace_root()
    result = {"total_mb": 0.0, "dirs": {}, "large_files": [], "runtime_mb": 0.0, "cached": False,
              "stat_at": datetime.datetime.now().strftime("%H:%M:%S")}
    total = 0
    runtime_mb, cached = _components_mb(refresh=refresh)
    result["cached"] = cached
    try:
        for name in sorted(os.listdir(root)):
            top = os.path.join(root, name)
            if os.path.isdir(top):
                if name == _RUNTIME_SUB:                 # 走缓存，不再逐文件遍历第二遍
                    result["dirs"][name] = runtime_mb
                    total += runtime_mb * 1048576.0
                    continue
                sub_total = 0
                for dirpath, _, filenames in os.walk(top):
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            continue
                        sub_total += sz
                        if sz >= 10 * 1048576:           # 组件不参与"可清理大文件"
                            result["large_files"].append({
                                "path": os.path.relpath(fp, top).replace("\\", "/"),
                                "sub": name, "size_mb": round(sz / 1048576.0, 2)})
            else:
                try:
                    sub_total = os.path.getsize(top)
                except OSError:
                    sub_total = 0
            total += sub_total
            result["dirs"][name] = round(sub_total / 1048576.0, 2)
        result["runtime_mb"] = result["dirs"].get(_RUNTIME_SUB, 0.0)
        result["total_mb"] = round(total / 1048576.0, 2)
        result["large_files"].sort(key=lambda x: x["size_mb"], reverse=True)
    except Exception as e:  # noqa: BLE001
        log_event(f"[workspace] 体积统计中断：{type(e).__name__}: {e}")
    return dict(result)


def save_chat_log(messages, title=None):
    """把一段对话(去掉 system)存为 markdown 到 对话记录。"""
    lines = ["# 企微伴侣对话记录", "", f"> 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            lines.append(f"**用户**：{(m.get('content') or '')}")
        elif role == "assistant":
            lines.append(f"**助手**：{(m.get('content') or '')}")
        elif role == "tool":
            c = m.get("content") or ""
            if len(c) > 2000:
                c = c[:2000] + " …(已截断)"
            lines.append(f"**工具结果**：{c}")
        lines.append("")
    fname = (title or "对话") + "_" + _timestamp() + ".md"
    out = save_workspace_file("对话记录", fname, "\n".join(lines))
    if isinstance(out, dict) and out.get("ok"):
        append_ledger("对话记录", out.get("filename", fname), source="对话记录", note=(title or "")[:40])
    return out


def save_template_result(name, data):
    """把结构化模板运行结果存为 JSON（并登记产物台账）。"""
    fname = re.sub(r'[\/\\:*?"<>|]', "_", str(name))[:60] + "_" + _timestamp() + ".json"
    out = save_workspace_file("产出", fname, json.dumps(data, ensure_ascii=False, indent=2))
    if isinstance(out, dict) and out.get("ok"):
        append_ledger("产出", out.get("filename", fname), source="模板运行", note=str(name)[:40])
    return out
def _safe_ws_path(path):
    """把 path 收敛到产物库内，防目录穿越（../、绝对路径越界一律拒绝）。返回绝对路径或 None。"""
    root = os.path.realpath(paths.workspace_root())
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(root, path)
    p = os.path.realpath(p)
    if p == root or p.startswith(root + os.sep):
        return p
    return None


# ---- v1.0.369 拖拽路径白名单：用户把本地文件/文件夹拖进聊天框时，前端上报绝对路径 ----
# agent 的只读工具（read_workspace_file / list_workspace_files）默认被 _safe_ws_path
# 限制在产物库内——拖入的外部路径不登记白名单就读不了。白名单只对读路径放行
# （写类工具 save_workspace_file 等不走这里，仍限制在产物库），进程级内存、重启清空。
# v1.0.373 审查修复（P2-5）：白名单条目带时间戳，TTL 30 分钟惰性过期——一次拖入
# 不再永久可读，降低被后续会话滥用的窗口。
_DND_ALLOWED = {}            # realpath -> 登记时间戳
_DND_ALLOWED_LOCK = threading.Lock()
_DND_TTL_SEC = 30 * 60

def dnd_allow_paths(paths):
    """登记拖拽白名单（绝对路径，realpath 归一）。返回成功登记数；脏数据静默丢弃。"""
    n = 0
    now = time.time()
    with _DND_ALLOWED_LOCK:
        for k in [k for k, ts in _DND_ALLOWED.items() if now - ts > _DND_TTL_SEC]:
            _DND_ALLOWED.pop(k, None)
        for p in (paths or [])[:100]:
            if not isinstance(p, str) or not p.strip() or len(p) > 500:
                continue
            try:
                rp = os.path.realpath(p)
            except Exception:  # noqa: BLE001
                continue
            if os.path.isabs(rp) and os.path.exists(rp):
                _DND_ALLOWED[rp] = now
                n += 1
    return n


def dnd_allowed(path):
    """path 是否在拖拽白名单（读放行）。
    v1.0.373 审查修复（P1-3）：目录条目对其**子树**同样放行——原精确匹配导致
    拖入文件夹后 list_workspace_files 能列目录、read_workspace_file 读子文件全被拒，
    「拖文件夹只引用分析」语义半失效。过期条目（TTL）视为不存在。"""
    try:
        rp = os.path.realpath(str(path))
        now = time.time()
        with _DND_ALLOWED_LOCK:
            expired = [k for k, ts in _DND_ALLOWED.items() if now - ts > _DND_TTL_SEC]
            for k in expired:
                _DND_ALLOWED.pop(k, None)
            if rp in _DND_ALLOWED:
                return True
            for d in _DND_ALLOWED:
                if os.path.isdir(d) and (rp + os.sep).startswith(d + os.sep):
                    return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _safe_ws_read_path(path):
    """只读工具专用路径解析：产物库内正常收敛；产物库外查拖拽白名单放行。

    与 _safe_ws_path 的区别仅在于白名单——命名带 read 以防未来被误用于写类工具。
    """
    p = _safe_ws_path(path)
    if p is not None:
        return p
    if path and dnd_allowed(path):
        return os.path.realpath(str(path))
    return None

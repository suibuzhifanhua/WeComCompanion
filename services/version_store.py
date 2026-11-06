"""version_store -- build version archive & in-app rollback (v1.0.358).

Keeps the last _KEEP released versions (see the constant below -- do not write a
number in prose, it rots: this line said "last 8" long after _KEEP dropped to 3):
source hash (git) + built exe backup
(build/_rollback/WeComCompanion_<ver>.exe) + a JSON index at
产物库/版本/versions.json.  The settings page lists versions and can switch:
source tree -> git reset --hard <hash>; running exe -> direct copy, or staged
copy (dist/WeComCompanion.exe.next + dist/.version_switch marker) applied on
next startup by apply_pending_switch().

Note: source is ASCII-only by design (Write-tool GBK safety); user-facing
Chinese strings use \\u escapes.
"""
import json
import os
import re
import shutil
import sys
import time

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # v1.0.401 归位：上一级为项目根
_DB_SUBDIR = "\u4ea7\u7269\u5e93/\u7248\u672c"          # 产物库/版本
_KEEP = 3   # v1.0.373：回滚备份保留最近 3 个版本（原 8 个占 340MB+，用户确认瘦身）
_VERSION_RE = re.compile(r'(?:__version__|VERSION)\s*=\s*["\']([^"\']+)["\']')


def _app_root():
    # v1.0.408 修复：frozen exe 下 __file__ 指向 PyInstaller 解包临时目录（_MEIxxxx），
    # 基于 _APP_ROOT 找 versions.json / version.py 全部落空 → 版本管理页空列表 +
    # current 恒为 "?"（b_repair._source_has_changes 注释里早警告过此坑但本模块未适配）。
    # frozen 态源码根 = dist 的上级 = sys.executable 所在目录的上级。
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    return _APP_ROOT


def _db_path():
    return os.path.join(_app_root(), _DB_SUBDIR.replace("/", os.sep), "versions.json")


def _rollback_dir():
    return os.path.join(_app_root(), "build", "_rollback")


def _dist_exe():
    return os.path.join(_app_root(), "dist", "WeComCompanion.exe")


def _current_version():
    try:
        with open(os.path.join(_app_root(), "version.py"), encoding="utf-8") as f:
            m = _VERSION_RE.search(f.read())
        return m.group(1) if m else "?"
    except Exception:  # noqa: BLE001
        return "?"


def _git_exe():
    # v1.0.612：定位收敛到 core.portable.find_git()（PATH → WorkBuddy 便携版 → 软件自装 MinGit）
    from core import portable
    return portable.find_git()


def _git(*args):
    """Run a local git command; returns stdout str or None on any failure."""
    gi = _git_exe()
    if not gi:
        return None
    try:
        import subprocess
        r = subprocess.run([gi, "-C", _app_root()] + list(args), capture_output=True, timeout=120,
                       creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode != 0:
            return None
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _load():
    try:
        with open(_db_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(recs):
    os.makedirs(os.path.dirname(_db_path()), exist_ok=True)
    # v1.0.652（审计 P2-F3）：原子写——旧裸 open("w") 截断即丢全部版本记录。
    _tmp = _db_path() + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False, indent=1)
    os.replace(_tmp, _db_path())


def _git_changelog(prev_hash):
    """v1.0.360：上一个登记版本 → 当前 HEAD 的提交主题列表（版本介绍的数据源）。
    每条 ≤120 字符、最多 10 条；git 不可用/无上一版本返回 []。"""
    if not prev_hash or not _git_exe():
        return []
    out = _git("log", prev_hash + "..HEAD", "--pretty=format:%s") or ""
    items = []
    for ln in out.splitlines():
        ln = ln.strip()
        if ln:
            items.append(ln[:120])
        if len(items) >= 10:
            break
    return items


def record_version(note=""):
    """Register the just-built version (called by build.py after a successful build).
    Dedupes by version; prunes history to the last _KEEP records and deletes
    the exe backups of pruned records.
    v1.0.360：自动生成版本介绍（相对上一个登记版本的提交主题列表）；若工作区有
    未提交改动，先自动提交（保证 hash 精确指向本版本源码，回滚不失真）。"""
    ver = _current_version()
    recs = _load()
    prev_hash = (recs[0].get("hash") or "") if recs else ""
    changes = []
    dist = _dist_exe()
    exe_rel = ""
    if os.path.isfile(dist):
        os.makedirs(_rollback_dir(), exist_ok=True)
        tgt = os.path.join(_rollback_dir(), "WeComCompanion_%s.exe" % ver)
        if not os.path.isfile(tgt):
            shutil.copy2(dist, tgt)
        exe_rel = "build/_rollback/" + os.path.basename(tgt)
    # v1.0.360：未提交改动先入库（build 先于 commit 的工作流也能精确回滚）
    if _git_exe():
        st = (_git("status", "--porcelain") or "")
        if st.strip():
            _git("-c", "user.name=version-store", "-c", "user.email=version-store@local",
                 "commit", "-am", ("v" + ver + " " + note) if note else ("v" + ver + " release"))
    h = (_git("rev-parse", "--short", "HEAD") or "").strip()
    changes = _git_changelog(prev_hash)
    if not changes:
        changes = [str(note)] if note else []
    entry = {"version": ver, "ts": int(time.time()), "hash": h, "exe": exe_rel,
             "note": str(note or ""), "changes": changes}
    recs = [r for r in recs if r.get("version") != ver]
    recs.insert(0, entry)
    pruned = recs[_KEEP:]
    recs = recs[:_KEEP]
    for r in pruned:
        p = r.get("exe") or ""
        if p:
            try:
                os.remove(os.path.join(_app_root(), p.replace("/", os.sep)))
            except OSError:  # noqa: BLE001
                pass
    _save(recs)
    return entry


def list_versions():
    return {"current": _current_version(), "versions": _load(), "keep": _KEEP}


def switch_version(ver):
    """Switch to a recorded version: snapshot current -> git reset --hard <hash>
    (source) -> swap dist exe (direct, or staged for next startup when the
    running exe locks the file). Restart required to take effect."""
    ver = str(ver or "").strip()
    rec = next((r for r in _load() if r.get("version") == ver), None)
    if not rec:
        return {"ok": False, "error": "\u672a\u627e\u5230\u8be5\u7248\u672c\u8bb0\u5f55"}
    cur = _current_version()
    if ver == cur:
        return {"ok": False, "error": "\u5df2\u7ecf\u662f\u5f53\u524d\u7248\u672c"}
    try:
        from services import self_repair
        if getattr(self_repair, "_RUN_ACTIVE", False):
            return {"ok": False, "error": "\u81ea\u6211\u4fee\u590d\u6b63\u5728\u8fd0\u884c\uff0c\u8bf7\u7a0d\u540e\u518d\u5207\u6362"}
    except Exception:  # noqa: BLE001
        pass
    # 1) always snapshot current state first -> switching is reversible
    record_version(note="\u5207\u6362\u524d\u81ea\u52a8\u5feb\u7167")
    # 2) source restore via git
    h = rec.get("hash") or ""
    if h and _git_exe():
        st = (_git("status", "--porcelain") or "")
        if st.strip():
            _git("-c", "user.name=version-store", "-c", "user.email=version-store@local",
                 "commit", "-am", "WIP before switch to " + ver)
        src_note = ("\u6e90\u7801\u5df2\u5207\u6362" if _git("reset", "--hard", h) is not None
                    else "\u6e90\u7801\u5207\u6362\u5931\u8d25\uff08git\uff09")
    else:
        src_note = "\u65e0 git/hash\uff0c\u4ec5\u5207\u6362 exe"
    # 3) exe swap
    exe_rel = rec.get("exe") or ""
    exe_path = os.path.join(_app_root(), exe_rel.replace("/", os.sep)) if exe_rel else ""
    exe_note = ""
    if exe_path and os.path.isfile(exe_path):
        try:
            shutil.copy2(exe_path, _dist_exe())
            exe_note = "exe \u5df2\u5207\u6362"
        except PermissionError:
            try:
                shutil.copy2(exe_path, _dist_exe() + ".next")
                with open(os.path.join(_app_root(), "dist", ".version_switch"), "w", encoding="utf-8") as f:
                    json.dump({"version": ver}, f)
                exe_note = "exe \u88ab\u8fd0\u884c\u4e2d\u8fdb\u7a0b\u5360\u7528\uff0c\u5df2\u6392\u961f\u4e0b\u6b21\u542f\u52a8\u65f6\u81ea\u52a8\u5b8c\u6210"
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": "exe \u5207\u6362\u5931\u8d25\uff1a" + str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "exe \u5207\u6362\u5931\u8d25\uff1a" + str(e)}
    else:
        exe_note = "\u672a\u627e\u5230\u8be5\u7248\u672c\u7684 exe \u5907\u4efd\uff0c\u4ec5\u5207\u6362\u4e86\u6e90\u7801"
    # 4) audit trail
    recs = _load()
    for r in recs:
        if r.get("version") == ver:
            r["note"] = (r.get("note") or "") + " \u2190 \u5f53\u524d"
    _save(recs)
    return {"ok": True, "version": ver,
            "detail": src_note + "\uff1b" + exe_note + "\uff1b\u91cd\u542f\u8f6f\u4ef6\u540e\u751f\u6548"}


def apply_pending_switch():
    """Called at app startup: finish a staged exe swap (running exe had locked the file).
    Returns True when a pending switch was applied."""
    marker = os.path.join(_app_root(), "dist", ".version_switch")
    if not os.path.isfile(marker):
        return False
    try:
        nxt = _dist_exe() + ".next"
        if os.path.isfile(nxt):
            shutil.copy2(nxt, _dist_exe())
            os.remove(nxt)
        os.remove(marker)
        try:
            from core_utils import log_event
            log_event("[version-store] \u5df2\u5b8c\u6210\u4e0a\u6b21\u6392\u961f\u7684\u7248\u672c\u5207\u6362\uff08exe \u5df2\u66ff\u6362\uff09")
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False

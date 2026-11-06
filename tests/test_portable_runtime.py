# -*- coding: utf-8 -*-
"""tests/test_portable_runtime.py — 按域归并的回归锁合集

v1.0.648 由若干"按版本号起的文件"按域合并而来。每条 class/函数的 docstring 保留
原始出处与事故现场——版本编号是这些锁的**理由**，抹掉等于把锁降级成断言。
版本出处见各段上方的 `# ---- 来自 xxx ----` 标记。
"""
import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import zipfile


# ============================================================ 来自 test_v559_relocate.py（v1.0.559）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from core import relocate as R


@pytest.fixture
def fake_desktop(tmp_path):
    d = tmp_path / "Desktop"
    d.mkdir()
    return str(d)


# ===================== 判定 =====================

def test_needs_relocation_desktop(fake_desktop):
    should, reason, target = R.needs_relocation(
        os.path.join(fake_desktop), desktop_dirs=[fake_desktop], downloads_dirs=[])
    assert should is True and reason == "桌面"
    assert target == os.path.join(fake_desktop, R.APP_FOLDER_NAME)


def test_needs_relocation_downloads(tmp_path):
    dl = str(tmp_path / "Downloads")
    os.makedirs(dl, exist_ok=True)
    should, reason, _ = R.needs_relocation(dl, desktop_dirs=[], downloads_dirs=[dl])
    assert should is True and reason == "下载文件夹"


def test_needs_relocation_drive_root(tmp_path):
    # 模拟盘根：把根目录判定逻辑抽出来测（用 tmp 下最短路径近似不可行，直接验证函数）
    assert R._is_drive_root("C:\\") is True
    assert R._is_drive_root("C:\\Users") is False
    assert R._is_drive_root("") is False


def test_needs_relocation_normal_dir(fake_desktop):
    normal = os.path.join(fake_desktop, " somewhere-else ", "app")
    should, reason, target = R.needs_relocation(
        normal, desktop_dirs=[fake_desktop], downloads_dirs=[])
    assert should is False and target == ""


def test_needs_relocation_skips_system_dirs(fake_desktop):
    sysd = os.path.join(fake_desktop, "Windows", "System32")
    should, _, _ = R.needs_relocation(sysd, desktop_dirs=[fake_desktop], downloads_dirs=[])
    assert should is False, "系统关键目录绝不迁移"


def test_needs_relocation_idempotent_when_target_exists(fake_desktop):
    # 目标文件夹里已有本软件 exe → 视为已归位，跳过（用户又复制了一份的场景）
    target = os.path.join(fake_desktop, R.APP_FOLDER_NAME)
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "WeComCompanion.exe"), "wb") as f:
        f.write(b"MZ")
    monkey_src = pytest.MonkeyPatch()
    monkey_src.setattr(sys, "executable", "C:\\fake\\WeComCompanion.exe")
    try:
        should, _, _ = R.needs_relocation(fake_desktop, desktop_dirs=[fake_desktop],
                                          downloads_dirs=[])
    finally:
        monkey_src.undo()
    assert should is False


# ===================== 迁移执行 =====================

def test_perform_relocation_moves_everything(tmp_path, monkeypatch):
    exe = tmp_path / "WeComCompanion.exe"
    exe.write_bytes(b"MZ" + os.urandom(64))
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "产物库").mkdir()
    (tmp_path / "产物库" / "产出").mkdir()
    (tmp_path / "产物库" / "产出" / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "ocr").mkdir()
    (tmp_path / "ocr" / "runtime.dll").write_text("d", encoding="utf-8")

    target = str(tmp_path / R.APP_FOLDER_NAME)
    new_exe, moved, errors, bak = R.perform_relocation(str(exe), target)

    assert errors == [], errors
    assert os.path.isfile(new_exe) and open(new_exe, "rb").read(2) == b"MZ"
    assert sorted(moved) == ["config.json", "ocr", "产物库"], moved
    assert os.path.isfile(os.path.join(target, "config.json"))
    assert os.path.isfile(os.path.join(target, "产物库", "产出", "a.txt"))
    assert os.path.isfile(os.path.join(target, "ocr", "runtime.dll"))
    # v1.0.561：不再 rename——bak 为空串，原位 exe 保留（进程退出后可手动删除）
    assert bak == ""
    assert exe.exists(), "原位 exe 保留（提示用户自行删除）"
    assert not os.path.exists(str(tmp_path / "config.json"))
    assert not os.path.exists(str(tmp_path / "产物库"))


def test_perform_relocation_copy_failure_aborts(tmp_path, monkeypatch):
    """copy 自身失败（磁盘满/占用）→ 整体放弃（抛错由入口降级），绝不半迁移。"""
    exe = tmp_path / "WeComCompanion.exe"
    exe.write_bytes(b"MZ")
    import shutil as _sh
    monkeypatch.setattr(_sh, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(RuntimeError, match="复制程序文件失败"):
        R.perform_relocation(str(exe), str(tmp_path / R.APP_FOLDER_NAME))
    # 放弃时不得留下半成品
    assert not (tmp_path / R.APP_FOLDER_NAME / "WeComCompanion.exe").exists()


def test_perform_relocation_companion_failure_nonfatal(tmp_path, monkeypatch):
    """单个伴随物搬不动（被占用）→ 记 error 继续搬其余，exe 照常归位。"""
    exe = tmp_path / "WeComCompanion.exe"
    exe.write_bytes(b"MZ" + os.urandom(32))
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    import shutil as _sh
    real_move = _sh.move

    def _flaky_move(src, dst, *a, **k):
        if str(src).endswith("config.json"):
            raise PermissionError("locked")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(_sh, "move", _flaky_move)
    _, moved, errors, _ = R.perform_relocation(str(exe), str(tmp_path / R.APP_FOLDER_NAME))
    assert any("config.json" in e for e in errors), errors
    assert os.path.isfile(os.path.join(str(tmp_path / R.APP_FOLDER_NAME), "WeComCompanion.exe"))


# ===================== 入口流程 =====================

def test_maybe_relocate_full_flow(tmp_path, monkeypatch):
    """桌面场景端到端（mock Popen/frozen/toast）：拉起新 exe + 带清理标记 + 返回 True。"""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    exe = desktop / "WeComCompanion.exe"
    exe.write_bytes(b"MZ" + os.urandom(64))
    (desktop / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(R, "_desktop_dirs", lambda: [str(desktop)])
    monkeypatch.setattr(R, "_downloads_dirs", lambda: [])
    boxes = []
    import ctypes as _ct
    monkeypatch.setattr(_ct.windll.user32, "MessageBoxW",
                        lambda h, msg, title, flags: boxes.append((msg, title, flags)) or 1)
    popped = []
    import subprocess as _sp
    monkeypatch.setattr(_sp, "Popen",
                        lambda cmd, **k: popped.append(cmd))
    exited = {}
    monkeypatch.setattr(os, "_exit", lambda code: exited.setdefault("code", code))

    assert R.maybe_relocate_and_relaunch() is True
    # v1.0.561：不自动重启（无 Popen），改弹信息框 + 退出
    assert popped == [], "不应自动拉起新实例（用户要求从新位置手动启动）"
    assert boxes, "必须弹信息框告知新位置"
    msg, title, flags = boxes[0]
    new_exe = os.path.join(str(desktop), R.APP_FOLDER_NAME, "WeComCompanion.exe")
    assert new_exe in msg, f"信息框应包含新 exe 完整路径：{msg}"
    assert "请从新位置启动" in msg and "删除原来位置的旧副本" in msg
    assert "已搬移" in title and flags == 0x40
    assert os.path.isfile(new_exe), "新 exe 必须已就位"
    assert exited.get("code") == 0, "用户点确定后老进程必须退出"
    # 原位 exe 保留（进程已退出即可删除）
    assert exe.exists()


def test_maybe_relocate_skips_dev_mode(monkeypatch):
    """开发模式（非 frozen）绝不迁移。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert R.maybe_relocate_and_relaunch() is False


def test_maybe_relocate_cleanup_bak(tmp_path, monkeypatch):
    """新实例收尾：env 带 WECOM_MIGRATE_BAK 时删除 .bak 并清 env，正常继续启动。"""
    bak = tmp_path / "WeComCompanion.exe.migrated.bak"
    bak.write_bytes(b"MZ")
    monkeypatch.setenv(R.ENV_MIGRATE_BAK, str(bak))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "WeComCompanion.exe"))
    # 正常目录（非桌面）→ 清理后走"无需迁移"返回 False
    assert R.maybe_relocate_and_relaunch() is False
    assert not bak.exists(), "收尾必须删除旧 .bak"
    assert R.ENV_MIGRATE_BAK not in os.environ

# ============================================================ 来自 test_v612_portable.py（v1.0.612）


import core.portable as P   # noqa: E402


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """把「运行目录」挪到 tmp：便携安装会真往 dist/组件 写东西，测试不能碰真目录。"""
    monkeypatch.setattr(P, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(P, "log_event", lambda *a, **k: None)
    P._STATE.clear()
    return tmp_path


# ---------------- 定位：PATH 优先，其次软件自装 ----------------

def test_find_node_prefers_system_then_portable(ws, monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda n: "C:/system/node.exe" if n == "node" else None)
    assert P.find_node() == "C:/system/node.exe"
    monkeypatch.setattr(P.shutil, "which", lambda n: None)
    assert P.find_node() is None                       # 两样都没有 → 不猜
    exe = ws / "组件" / "node" / "current" / "node-v22.20.0-win-x64" / "node.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    assert P.find_node() == str(exe)                   # zip 自带顶层目录也要找得到


def test_find_git_three_sources_in_order(ws, monkeypatch):
    hits = []
    monkeypatch.setattr(P.shutil, "which", lambda n: "C:/sys/git.exe" if n == "git" else None)
    assert P.find_git() == "C:/sys/git.exe"
    monkeypatch.setattr(P.shutil, "which", lambda n: None)
    wb = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "PortableGit",
                      "versions", "1.2.0", "cmd", "git.exe")
    monkeypatch.setattr(os.path, "isfile", lambda p: p == wb or p in hits)
    assert P.find_git() == wb                          # 其次 Qoder 自带的 PortableGit
    monkeypatch.setattr(os.path, "isfile", lambda p: p in hits)
    exe = ws / "组件" / "git" / "current" / "cmd" / "git.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    hits.append(str(exe))
    assert P.find_git() == str(exe)                    # 最后才是软件自装的 MinGit


def test_node_env_prepends_node_dir(ws, monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda n: None)
    exe = ws / "组件" / "node" / "current" / "node.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    env = P.node_env()
    assert env["PATH"].split(os.pathsep)[0] == str(exe.parent), env["PATH"][:120]


# ---------------- 解析与解压（不碰网） ----------------

def test_pick_url_chooses_newest(ws, monkeypatch):
    rows = [{"name": "node-v22.9.0-win-x64.zip", "url": "https://m/a.zip"},
            {"name": "node-v22.20.0-win-x64.zip", "url": "https://m/b.zip"},
            {"name": "node-v22.11.0-win-x64.zip", "url": "https://m/c.zip"},
            {"name": "node-v22.20.0-win-arm64.zip", "url": "https://m/d.zip"},
            {"name": "SHASUMS256.txt", "url": "https://m/e.txt"}]

    class R:
        status_code = 200

        @staticmethod
        def json():
            return rows

    monkeypatch.setattr(P, "_lazy_requests_get", lambda *a, **k: R(), raising=False)
    import core_utils as CU
    monkeypatch.setattr(CU, "_lazy_requests", type("X", (), {"get": staticmethod(lambda *a, **k: R())}))
    url, fn = P._pick_url("node")
    assert (url, fn) == ("https://m/b.zip", "node-v22.20.0-win-x64.zip"), (url, fn)   # 22.20 > 22.9（按段比大小）


def test_verkey_numeric_not_lexicographic():
    assert P._verkey("node-v22.20.0-win-x64.zip") > P._verkey("node-v22.9.0-win-x64.zip")


def test_safe_extract_blocks_zip_slip(ws):
    z = ws / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escaped.txt", "boom")
    with pytest.raises(RuntimeError) as e:
        P._safe_extract(zipfile.ZipFile(z), str(ws / "out"))
    assert "越界" in str(e.value)
    assert not (ws.parent / "escaped.txt").exists()


def test_install_worker_end_to_end_offline(ws, monkeypatch):
    """解析→下载→解压→原子提交→写标记，全链路用本地 zip 演一遍（不联网）。"""
    payload = ws / "payload.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("node-v22.20.0-win-x64/node.exe", b"fake-node")
        zf.writestr("node-v22.20.0-win-x64/npm.cmd", b"@echo off")

    def _fake_download(url, dest, name):
        import shutil as _s
        _s.copyfile(payload, dest)
        return url

    monkeypatch.setattr(P, "_pick_url", lambda name: ("https://m/node-v22.20.0-win-x64.zip",
                                                      "node-v22.20.0-win-x64.zip"))
    monkeypatch.setattr(P, "_download", _fake_download)
    # v1.0.623：_install_worker 装完会把便携目录注入**本进程** PATH（不写系统环境变量）。
    # 不先把 PATH 挂到 monkeypatch 上，这份临时目录就漏给整个测试会话——后面的
    # test_v619_bundle_imports 里 check_js_file 会把这坨 9 字节的假 node.EXE 当真 node 跑，
    # 于是"校验器存在但校验失败"，红得完全看不出跟本用例有关系。
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    P._STATE["node"] = {"running": True, "phase": "", "pct": 0, "ok": None, "error": "", "version": ""}
    P._install_worker("node")
    st = P.install_status("node")
    assert st["ok"] is True and st["running"] is False, st
    assert st["version"] == "22.20.0", st
    # Windows 上 shutil.which 是按 PATHEXT 拼名字返回的，大小写取 PATHEXT（".EXE"）不是盘上
    # 那份小写名——所以这里一律小写后比。用户哪天把 node 加进系统 PATH，find_node() 走 PATH
    # 分支也会拿到 `node.EXE`，写死字面量会误报。
    _nd = (P.find_node() or "").lower()
    _np = (P.find_npm() or "").lower()
    assert _nd.endswith("node.exe"), _nd
    assert _np.endswith("npm.cmd"), _np
    meta = json.load(io.open(P._marker("node"), encoding="utf-8"))
    assert meta["exe"].lower() == _nd, meta


def test_install_worker_failure_leaves_state(ws, monkeypatch):
    monkeypatch.setattr(P, "_pick_url", lambda name: (_ for _ in ()).throw(RuntimeError("索引 503")))
    P._STATE["git"] = {"running": True, "phase": "", "pct": 0, "ok": None, "error": "", "version": ""}
    P._install_worker("git")
    st = P.install_status("git")
    assert st["ok"] is False and "503" in st["error"], st
    assert st["running"] is False                      # 失败也要把 running 落下来，否则界面永远转圈


def test_install_async_idempotent(ws, monkeypatch):
    exe = ws / "组件" / "node" / "current" / "node.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    r = P.install_async("node")
    assert r.get("already_installed") is True, r
    assert P.install_async("nonexistent").get("ok") is False


# ---------------- 组件体检与修复动作 ----------------

def _mk_node_probe(monkeypatch, which_ret, portable_ret, running=None):
    import routes.b_ocr_admin as RO
    # `path=` 关键字要吃得下：v1.0.623 起「来源」判据走 portable.find_system_tool()，
    # 它把软件自装的便携目录从搜索路径里摘掉后再 which(cmd, path=...)——
    # 旧桩只收单参，注入 PATH 后那条判据一调就 TypeError，探测整行变「探测失败」。
    monkeypatch.setattr(P.shutil, "which", lambda n, **kw: which_ret if n in ("node", "git") else None)
    monkeypatch.setattr(P, "find_node", lambda: portable_ret)
    monkeypatch.setattr(P, "find_git", lambda: portable_ret)
    monkeypatch.setattr(P, "install_status",
                        lambda name: (running or {"running": False, "phase": "", "pct": 0}))
    monkeypatch.setattr(RO, "_which_version", lambda a, exe=None: (0, "v22.20.0"))
    return RO._probe_component


def test_node_missing_now_offers_in_app_install(monkeypatch):
    probe = _mk_node_probe(monkeypatch, None, None)
    c = probe("node")
    assert c["status"] == "missing", c
    a = c["actions"][0]
    assert (a["type"], a["api"], a["kind"], a["body"]) == ("api", "/api/components/fix", "install",
                                                           {"key": "node"}), a


def test_node_installing_shows_progress(monkeypatch):
    probe = _mk_node_probe(monkeypatch, None, None, running={"running": True, "phase": "下载", "pct": 37})
    c = probe("node")
    assert c["status"] == "installing" and "37%" in c["hint"], c


def test_portable_node_hint_says_it_does_not_touch_path(monkeypatch):
    probe = _mk_node_probe(monkeypatch, None, "C:/app/组件/node/current/node.exe")
    c = probe("node")
    assert c["status"] == "ok" and "不写系统 PATH" in c["hint"], c


def test_fix_route_dispatches_portable_install(monkeypatch):
    import routes.b_ocr_admin as RO
    import backend as B
    called = []
    monkeypatch.setattr(P, "install_async", lambda name: called.append(name) or {"ok": True, "msg": "开始"})
    with B.app.test_request_context("/api/components/fix", method="POST",
                                    json={"key": "node"}):
        r = RO.api_components_fix()
    assert called == ["node"], called
    assert r.get_json()["ok"] is True


def test_dsh_fix_waits_for_node_before_npm(monkeypatch):
    """单独点 dsh 的修复按钮也不能再要求用户先装 Node：先自装、等它就绪、再跑 npm。"""
    import routes.b_ocr_admin as RO
    import backend as B
    from core import portable as _p
    seq = {"n": 0}

    def _find_node():
        return None if seq["n"] == 0 else "C:/app/组件/node/current/node.exe"

    def _install_async(name):
        seq["n"] += 1
        _p.install_status = lambda n: {"running": False, "phase": "完成", "pct": 100}
        return {"ok": True}

    monkeypatch.setattr(_p, "find_node", _find_node)
    monkeypatch.setattr(_p, "install_async", _install_async)
    monkeypatch.setattr(_p, "install_status", lambda n: {"running": False, "phase": "", "pct": 100})
    import agent_tools.dsh_bridge as DB
    monkeypatch.setattr(DB, "dsh_prepare", lambda timeout=180: {"ok": True, "installed": True})
    with B.app.test_request_context("/api/components/fix", method="POST", json={"key": "dsh"}):
        r = RO.api_components_fix()
    assert seq["n"] == 1, "没先补装 Node 就去跑 npm（就是截图里那次失败）"
    assert r.get_json()["ok"] is True


def test_dsh_bridge_uses_portable_node_and_npm(monkeypatch):
    import agent_tools.dsh_bridge as DB
    from core import portable as _p
    monkeypatch.setattr(_p, "find_node", lambda: "C:/app/组件/node/current/node.exe")
    monkeypatch.setattr(_p, "find_npm", lambda: "C:/app/组件/node/current/npm.cmd")
    monkeypatch.setattr(_p, "node_env", lambda extra_path=None: {"PATH": "C:/app/组件/node/current"})
    runs = []

    def _fake_run(args, timeout, env=None):
        runs.append((args, env))
        return {"exit_code": 0, "stdout": "v22.20.0", "stderr": ""}

    monkeypatch.setattr(DB, "_run_captured", _fake_run)
    seq = {"n": 0}

    def _which():
        seq["n"] += 1
        return None if seq["n"] == 1 else "C:/app/组件/node/current/dsh.cmd"

    monkeypatch.setattr(DB, "_which_dsh", _which)      # 装之前找不到、装之后找得到
    info = DB._node_info()
    assert info["ok"] is True and info["path"].endswith("node.exe"), info
    r = DB.dsh_prepare(timeout=30)
    assert r["ok"] is True, r
    npm_call = [a for a, e in runs if a and str(a[0]).endswith("npm.cmd")]
    assert npm_call, "npm 没用绝对路径（便携版不在 PATH 上会 WinError 2）"
    assert any(e and "组件" in e.get("PATH", "") for a, e in runs), "npm 子进程没补 PATH"


def test_which_dsh_finds_shim_next_to_portable_node(monkeypatch):
    """npm -g 装进便携 node 目录，而那个目录**不在系统 PATH 上** ——
    只查 PATH 就会「装成功却报找不到 dsh」，所以要在 node 同目录再找一遍。"""
    import agent_tools.dsh_bridge as DB
    from core import portable as _p
    monkeypatch.setattr(DB.shutil, "which", lambda n: None)      # PATH 里没有
    monkeypatch.setattr(_p, "find_node", lambda: "C:/app/组件/node/current/node.exe")
    want = os.path.join("C:/app/组件/node/current", "dsh.cmd")
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda p: real_isfile(p) or p == want)
    assert DB._which_dsh() == want


# ---------------- 重复定位收敛（源码锁） ----------------

def test_dsh_probe_shares_the_same_locator(monkeypatch):
    """正式包实测抓出来的假阴性：dsh 已装好（109 秒那次，shim 就在便携 node 目录里），
    体检行却说「未安装」——因为探测分支自己抄了一遍 `shutil.which` 循环，
    而便携目录不在系统 PATH 上。判据必须与 dsh_bridge 同源。"""
    import routes.b_ocr_admin as RO
    import agent_tools.dsh_bridge as DB
    monkeypatch.setattr(DB, "_which_dsh", lambda: r"C:\app\组件\node\current\dsh.cmd")
    # v1.0.614 批1：行状态改用 dsh_check 的 ready（node + 凭据齐备），本用例只关心
    # 「dsh 定位是否与执行侧同源」，故把另外两个前置供齐，避免测成"环境有没有 node"。
    monkeypatch.setattr(DB, "_node_info", lambda: {"ok": True, "version": "v22.20.0",
                                                   "path": "C:/node/node.exe", "detail": ""})
    monkeypatch.setattr(RO, "load_config", lambda: {"dsh": {"base_url": "http://127.0.0.1:8100"}})
    c = RO._probe_component("dsh")
    assert c["status"] == "ok", c
    assert c["version"].startswith("dsh.cmd @ "), c["version"]     # 紧凑形式，不被 60 字截断
    assert len(c["version"]) <= 60, c["version"]
    monkeypatch.setattr(DB, "_which_dsh", lambda: None)
    assert RO._probe_component("dsh")["status"] == "degraded"


def test_dsh_probe_does_not_reimplement_which():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "routes", "b_ocr_admin.py"), encoding="utf-8").read()
    assert 'for n in ("dsh.cmd"' not in src, "探测里又自己抄了一份 dsh 定位，便携版会被判成未安装"


def test_git_locator_no_longer_duplicated():
    """`~/.workbuddy/binaries/PortableGit` 这段手抄路径原先散在 4 个生产文件里，
    加第三种来源要改 4 处 —— 现在只允许 core/portable.py 有。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prod = ["routes/b_ocr_admin.py", "routes/b_repair.py", "services/self_repair.py",
            "services/version_store.py"]
    bad = [f for f in prod
           if "PortableGit" in io.open(os.path.join(root, f), encoding="utf-8").read()]
    assert not bad, "git 定位又被人抄回业务代码里了：%s" % bad
    src = io.open(os.path.join(root, "core/portable.py"), encoding="utf-8").read()
    assert "PortableGit" in src and "def find_git" in src


# ============================================================ 来自 test_v632_portable_commit.py（v1.0.632）
import json


@pytest.fixture
def ws_v632(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(P, "log_event", lambda *a, **k: None)
    # 装完的两个"顺手刷新"跟本用例无关，挂掉以免碰真实 PATH/缓存
    monkeypatch.setattr(P, "apply_component_path", lambda: None)
    monkeypatch.setattr(P, "runtime_locations", lambda **k: {})
    P._STATE.clear()
    return tmp_path


def _mk_zip(ws_v632, members):
    payload = ws_v632 / "payload.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        for m in members:
            zf.writestr(m, b"x")
    return payload


def _wire(monkeypatch, ws_v632, members, fname="MinGit-2.56.0-64-bit.zip"):
    payload = _mk_zip(ws_v632, members)

    def _dl(url, dest, name):
        import shutil as _s
        _s.copyfile(payload, dest)
        return url

    monkeypatch.setattr(P, "_pick_url", lambda name: ("https://m/" + fname, fname))
    monkeypatch.setattr(P, "_download", _dl)


def _seed_old_runtime(name, rel, ver="2.56.0"):
    """演"机器上已经有一个可用的旧版本"——B5 要保住的正是这个。"""
    root = os.path.join(P.portable_root(name), "current")
    exe = os.path.join(root, rel)
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "wb") as f:
        f.write(b"old-but-working")
    with open(P._marker(name), "w", encoding="utf-8") as f:
        json.dump({"name": name, "version": ver, "exe": exe}, f)
    return exe


# ---------------------------------------------------------------- 核心：失败不再吃掉旧版本
def test_bad_package_keeps_previous_runtime(ws_v632, monkeypatch):
    old_exe = _seed_old_runtime("git", os.path.join("cmd", "git.exe"))
    # 包里没有 cmd/git.exe —— 正是旧代码会"删完才发现"的形态
    _wire(monkeypatch, ws_v632, ["README.txt"])
    P._STATE["git"] = {"running": True, "phase": "", "pct": 0,
                       "ok": None, "error": "", "version": ""}
    P._install_worker("git")

    st = P.install_status("git")
    assert st["ok"] is False and "没找到" in st["error"], st
    assert os.path.isfile(old_exe), "新包验不过却把旧 runtime 删了——用户两头落空"
    assert P.portable_exe("git") == old_exe, "标记还该指向那个仍然可用的旧版本"
    assert not os.path.exists(os.path.join(P.portable_root("git"), "_staging"))


def test_good_package_commits_and_records_final_path(ws_v632, monkeypatch):
    """成功路径行为不变，但标记里记的必须是 current/ 下的路径，不是 staging 的。"""
    _wire(monkeypatch, ws_v632, ["MinGit-2.56.0/cmd/git.exe", "MinGit-2.56.0/mingw64/bin/git.exe"])
    P._STATE["git"] = {"running": True, "phase": "", "pct": 0,
                       "ok": None, "error": "", "version": ""}
    P._install_worker("git")

    st = P.install_status("git")
    assert st["ok"] is True and st["version"] == "2.56.0", st
    meta = json.load(io.open(P._marker("git"), encoding="utf-8"))
    assert "_staging" not in meta["exe"].replace("\\", "/"), meta
    assert os.path.isfile(meta["exe"]), meta
    assert meta["exe"].startswith(os.path.join(P.portable_root("git"), "current")), meta
    assert P.portable_exe("git") == meta["exe"]


def test_nested_package_path_maps_through_rename(ws_v632, monkeypatch):
    """zip 自带一层顶层目录时（node 的真实形态），相对位置要跟着搬进 current/。"""
    _wire(monkeypatch, ws_v632, ["node-v22.20.0-win-x64/node.exe"], "node-v22.20.0-win-x64.zip")
    P._STATE["node"] = {"running": True, "phase": "", "pct": 0,
                        "ok": None, "error": "", "version": ""}
    P._install_worker("node")
    meta = json.load(io.open(P._marker("node"), encoding="utf-8"))
    assert meta["exe"].endswith(os.path.join("node.exe")), meta
    assert os.path.isfile(meta["exe"]), meta


# ---------------------------------------------------------------- 搜索根参数语义
def test_exe_candidates_with_base_ignores_stale_marker(ws_v632):
    """传了 base 就是"只验这批新文件"：旧 runtime 的文件存在不算证据。

    这是 B5 的关键——旧顺序若改成"先搜再换"但仍走默认根，标记里的旧路径会排在
    候选第一位，空 staging 也能"验通过"，等于把 bug 从明处搬进暗处。"""
    old_exe = _seed_old_runtime("git", os.path.join("cmd", "git.exe"))
    empty = os.path.join(str(ws_v632), "empty_stage")
    os.makedirs(empty, exist_ok=True)
    assert P._exe_candidates("git", os.path.join("cmd", "git.exe")) == [old_exe]
    assert P._exe_candidates("git", os.path.join("cmd", "git.exe"), base=empty) == []


def test_commit_order_is_validate_then_delete():
    """顺序锁：源码里 `shutil.rmtree(final)` 必须排在 staging 校验之后。

    这条比行为用例更防手滑——将来有人为了"少一次遍历"把校验挪回 rename 之后，
    行为用例里旧 runtime 依然会被删掉，而这条直接红。"""
    src = io.open(os.path.join(ROOT, "core", "portable.py"), encoding="utf-8").read()
    i = src.find("def _install_worker")
    body = src[i:src.find("def install_async", i)]
    a = body.find("base=stage")
    b = body.find("shutil.rmtree(final")
    assert a != -1 and b != -1, (a, b)
    assert a < b, "先删再验 = 失败的包会把上一个可用版本一起带走"
    assert body.find("os.rename(stage, final)") > b

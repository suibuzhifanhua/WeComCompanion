# -*- coding: utf-8 -*-
"""组件兼容性测试（v1.0.473）：三类验证——
① 探测逻辑分支（mock 文件系统/PATH，不依赖真实环境）；
② Requires-Python 判定（_pyver_satisfies 纯函数）；
③ 本机真实组件体检（真实跑 /api/components/status，输出兼容性报告）。
运行：pytest tests/test_components_compat.py -q
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import difflib
import json

import core  # noqa: E402
from unittest import mock  # noqa: E402

TMP = os.environ.get("WECOM_TEST_TMP") or os.path.join(os.path.dirname(__file__), "_comp_tmp")
os.makedirs(TMP, exist_ok=True)
core.paths.workspace_root = lambda: TMP


def test_pyver_satisfies():
    """Requires-Python 极简判定：>=/<=/</>/==/!= 逗号分段 + 位数截断语义。"""
    from core.ocr import _pyver_satisfies as s
    # 区间
    assert s(">=3.6,<3.13", (3, 12, 0))
    assert not s(">=3.6,<3.13", (3, 13, 0))
    assert s(">=3.6,<=3.10", (3, 10, 5))
    assert not s(">=3.6,<=3.10", (3, 11, 0))
    # 空/None 恒过
    assert s("", (3, 13, 0)) and s(None, (3, 13, 0))
    # 单边
    assert not s(">3.14", (3, 13, 0)) and s(">3.12", (3, 13, 0))
    assert s("<3.14", (3, 13, 0)) and not s("<3.13", (3, 13, 0))
    # ==/!= 位数截断（==3.13 匹配 3.13.x 任意 micro）
    assert s("==3.13", (3, 13, 5)) and not s("==3.13", (3, 14, 0))
    assert not s("!=3.13", (3, 13, 0)) and s("!=3.12", (3, 13, 0))
    # 非法分段宽松放行
    assert s(">=abc,<3.14", (3, 13, 0))
    print("PASS Requires-Python 判定")


def test_ocr_wheel_python_ok_metadata():
    """_ocr_wheel_python_ok 从 wheel(zip) 的 dist-info/METADATA 读 Requires-Python。"""
    import zipfile
    from core.ocr import _ocr_wheel_python_ok
    wp = os.path.join(TMP, "fake-1.4.4-py3-none-any.whl")
    with zipfile.ZipFile(wp, "w") as z:
        z.writestr("fake/__init__.py", "")
        z.writestr("fake-1.4.4.dist-info/METADATA",
                   "Metadata-Version: 2.1\nName: fake\nVersion: 1.4.4\nRequires-Python: >=3.6,<3.13\n")
    ok, req = _ocr_wheel_python_ok(wp)
    assert ok and req == ">=3.6,<3.13", (ok, req)
    # 无 METADATA 的坏包：宽松放行（True, None）
    wp2 = os.path.join(TMP, "empty.whl")
    with zipfile.ZipFile(wp2, "w") as z:
        z.writestr("x.txt", "hi")
    ok2, req2 = _ocr_wheel_python_ok(wp2)
    assert ok2 and req2 is None
    os.remove(wp); os.remove(wp2)
    print("PASS wheel Requires-Python 读取")


def test_component_probe_structures():
    """七类组件探测：结构契约（key/name/status 合法值/str 字段）——真实环境探测。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from routes.b_ocr_admin import _probe_component
    valid = {"ok", "degraded", "missing", "installing", "unknown"}
    for key in ("ocr", "asr", "git", "node", "build_env", "dsh", "wecom"):
        c = _probe_component(key)
        assert c["key"] == key
        assert c["status"] in valid, (key, c["status"])
        assert isinstance(c.get("name"), str) and c["name"]
        assert isinstance(c.get("hint"), str)
    # git/node 在开发机应可探测到（真实兼容性验证；CI 无此环境则跳过断言）
    git_c = _probe_component("git")
    if git_c["status"] == "ok":
        assert git_c["version"] and "git version" in git_c["version"].lower(), git_c["version"]
    print("PASS 组件探测结构契约（真实环境）")


def test_git_detection_paths():
    """Git 探测：PATH 命中 → ok；PATH 无 + PortableGit 无 → degraded（功能降级不失效）。
    v1.0.473：缺失时给出结构化 actions（官网 + 国内镜像下载链接）。"""
    from routes.b_ocr_admin import _probe_component
    with mock.patch("shutil.which", return_value=None), \
         mock.patch("os.path.isfile", return_value=False):
        c = _probe_component("git")
        assert c["status"] == "degraded", c
        links = [a for a in c.get("actions", []) if a["type"] == "link"]
        assert links and any("npmmirror" in a["url"] for a in links), c.get("actions")
    # PATH 有 git（fake 可执行由 --version 子进程模拟：mock run）
    import subprocess
    def _fake_run(*a, **k):
        class R:  # noqa: E306
            stdout = b"git version 2.55.0.windows.3\n"
            stderr = b""
        return R()
    with mock.patch("shutil.which", return_value="C:/fake/git.exe"), \
         mock.patch("subprocess.run", side_effect=_fake_run):
        c2 = _probe_component("git")
        assert c2["status"] == "ok" and "2.55.0" in (c2["version"] or ""), c2
    print("PASS Git 探测分支（缺失降级/命中取版本）")


def test_node_detection():
    """Node 探测：缺失 → missing，且**首选动作是软件内自装便携版**（v1.0.612），
    npmmirror 下载链接退为次要选项（想用自己装的人还有路）。"""
    from routes.b_ocr_admin import _probe_component
    from core import portable as _p
    with mock.patch("shutil.which", return_value=None), \
         mock.patch.object(_p, "portable_exe", return_value=None), \
         mock.patch.object(_p, "install_status", return_value={"running": False}):
        c = _probe_component("node")
        assert c["status"] == "missing", c
        a0 = (c.get("actions") or [{}])[0]
        assert (a0.get("type"), a0.get("api"), a0.get("kind")) == ("api", "/api/components/fix", "install"), a0
        assert (a0.get("body") or {}).get("key") == "node", a0
        links = [a for a in c.get("actions", []) if a["type"] == "link"]
        assert links and any("npmmirror" in a["url"] for a in links), c.get("actions")
    print("PASS Node 探测缺失分支（软件内自装优先）")


def test_dsh_detection():
    """dsh 探测：三候选名（dsh.cmd/dsh/dsh.ps1）任一命中 → ok。"""
    from routes.b_ocr_admin import _probe_component
    import routes.b_ocr_admin as RO
    import agent_tools.dsh_bridge as DB
    calls = []
    def _fake_which(n):
        calls.append(n)
        return "C:/npm/dsh.cmd" if n == "dsh.cmd" else None
    # v1.0.614 批1：状态升级为 dsh_check 的 ready（node + 凭据 + dsh 三项），本用例测的是
    # 候选名命中顺序，另外两项按"齐备"供给（shutil.which 已被打桩，node 必然探不到）。
    with mock.patch("shutil.which", side_effect=_fake_which), \
         mock.patch.object(DB, "_node_info",
                           return_value={"ok": True, "version": "v22.20.0",
                                         "path": "C:/node/node.exe", "detail": ""}), \
         mock.patch.object(RO, "load_config",
                           return_value={"dsh": {"base_url": "http://127.0.0.1:8100"}}):
        c = _probe_component("dsh")
        assert c["status"] == "ok", c
        assert "dsh.cmd" in (c["version"] or ""), c
    assert calls[:1] == ["dsh.cmd"], calls
    print("PASS dsh 探测候选顺序")


def test_wecom_detection_fallback():
    """企微探测：固定候选不存在 → 注册表回退 → None 时 missing + 指引。"""
    from routes.b_ocr_admin import _probe_component
    with mock.patch("wecom_bridge.wecom_proc.detect_wecom_exe", return_value=None):
        c = _probe_component("wecom")
        assert c["status"] == "missing" and "企业微信" in c["hint"], c
    with mock.patch("wecom_bridge.wecom_proc.detect_wecom_exe",
                    return_value=r"C:\Program Files (x86)\WXWork\WXWork.exe"):
        c2 = _probe_component("wecom")
        assert c2["status"] == "ok" and "WXWork.exe" in (c2["version"] or ""), c2
    print("PASS 企微探测分支")


if __name__ == "__main__":
    test_pyver_satisfies()
    test_ocr_wheel_python_ok_metadata()
    test_component_probe_structures()
    test_git_detection_paths()
    test_node_detection()
    test_dsh_detection()
    test_wecom_detection_fallback()
    print("== components compat ALL PASS ==")


# ============================================================ 来自 test_v608_ocr_selfcheck.py（v1.0.608）


import core.ocr as O   # noqa: E402


# ---------- 索引解析与 Requires-Python ----------

def test_anchors_unescape_requires_python():
    html = ('<a href="../../packages/aa/rapidocr_onnxruntime-1.4.4-py3-none-any.whl#sha256=x" '
            'data-requires-python="&lt;3.13,&gt;=3.6">1.4.4</a>'
            '<a href="../../packages/bb/rapidocr_onnxruntime-1.2.3-py3-none-any.whl">1.2.3</a>')
    a = O._ocr_anchors(html)
    assert len(a) == 2
    assert a[0][1] == "<3.13,>=3.6", a[0]        # 转义已还原
    assert a[1][1] == "", a[1]                    # 索引没给属性 → 视为无约束


def test_pick_wheel_returns_requires():
    anchors = [("x/rapidocr_onnxruntime-1.4.4-py3-none-any.whl#sha256=y", "<3.13,>=3.6")]
    href, req = O._ocr_pick_wheel(anchors, "rapidocr-onnxruntime", "1.4.4")
    assert href.endswith("1.4.4-py3-none-any.whl") and req == "<3.13,>=3.6"


def test_pyver_satisfies_cases():
    assert O._pyver_satisfies("<3.13,>=3.6", (3, 13, 12)) is False
    assert O._pyver_satisfies("<3.13,>=3.6", (3, 12, 4)) is True
    assert O._pyver_satisfies("", (3, 13, 12)) is True
    assert O._pyver_satisfies(">=3.8", (3, 13, 12)) is True


def test_install_blocks_before_download(monkeypatch):
    """解析期就拦下不兼容版本：绝不允许产生下载流量（原实现白下 ~15MB 才中止）。"""
    def boom(*a, **k):
        raise AssertionError("解析期拦下后不该再发任何下载请求")
    import requests
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(O, "_ocr_resolve_wheel",
                        lambda pkg, ver: ("http://x/rapidocr_onnxruntime-1.4.4-py3-none-any.whl", "<3.13,>=3.6"))
    O._OCR_DL.update(state="idle", progress=0, msg="", error="")
    O._ocr_install_worker(want={"rapidocr-onnxruntime": "1.4.4"}, need=["rapidocr-onnxruntime"])
    st = dict(O._OCR_DL)
    assert st["state"] == "failed", st
    assert "下载前已拦下" in st["error"], st["error"]
    assert "恢复推荐版本" in st["error"]


def test_latest_version_skips_incompatible(monkeypatch):
    """「最新版」必须是当前解释器真能用的最新版，不是"索引里最靠后的那个"。"""
    html = ('<a href="p/rapidocr_onnxruntime-1.4.4-py3-none-any.whl" data-requires-python="&lt;3.13,&gt;=3.6">x</a>'
            '<a href="p/rapidocr_onnxruntime-1.4.0-py3-none-any.whl" data-requires-python="&lt;3.14,&gt;=3.6">x</a>'
            '<a href="p/rapidocr_onnxruntime-1.3.9-py3-none-any.whl">x</a>')

    class R:
        status_code = 200
        text = html
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    got = O._ocr_latest_version("rapidocr-onnxruntime")
    assert got == "1.4.0", got        # 1.4.4 声明 <3.13 被跳过


# ---------- 真实识别自检 ----------

def test_selfcheck_reports_missing_runtime(monkeypatch):
    monkeypatch.setattr(O, "ensure_ocr_runtime", lambda: False)
    r = O.ocr_selfcheck(force=True)
    assert r["ok"] is False and "旁挂目录不存在" in r["error"], r


def test_selfcheck_accepts_confusable_misread():
    """实测 rapidocr 会把 OCR717 读成 0XCR717——判据必须扛得住字形混淆，
    否则好引擎会被自检误报成"坏了"。这里直接锁住归一化 + 相似度算法。"""
    exp, got = O._SC_TEXT, "0XCR717\n7SELFTEST 2026"
    ratio = difflib.SequenceMatcher(None, O._sc_norm(exp), O._sc_norm(got)).ratio()
    assert ratio >= O._SC_MIN_RATIO, ratio
    bad = difflib.SequenceMatcher(None, O._sc_norm(exp), O._sc_norm("完全无关的一句话")).ratio()
    assert bad < O._SC_MIN_RATIO, bad


def test_selfcheck_passes_on_real_engine(tmp_path, monkeypatch):
    """真跑一遍：造图 → 走生产 OCR 入口 → 必须通过（本机 dist/ocr 在位时）。
    旁挂目录不存在（源码树直跑）时跳过，不算失败。"""
    cfg = os.path.abspath(os.path.join("dist", "config.json"))
    if not os.path.isdir(os.path.join(os.path.dirname(cfg), "ocr")):
        import pytest
        pytest.skip("本机无 dist/ocr 旁挂目录，跳过真实引擎自检")
    import core.config as C
    monkeypatch.setattr(C, "config_path", lambda *a, **k: cfg)
    monkeypatch.setattr(O, "config_path", lambda *a, **k: cfg)
    monkeypatch.setattr(O, "_OCR_RUNTIME_STATE", None)
    r = O.ocr_selfcheck(force=True)
    assert r["ok"] is True, r
    assert r["ms"] and r["ms"] < 60000, r


# ---------- dsh 源顺序 ----------

def test_dsh_prepare_tries_mirror_first(monkeypatch):
    from agent_tools import dsh_bridge as D
    calls = []
    monkeypatch.setattr(D, "IS_WIN", True)
    monkeypatch.setattr(D, "_node_info", lambda: {"ok": True, "version": "v22.19.0", "detail": ""})
    seq = {"n": 0}

    def fake_which():
        seq["n"] += 1
        return None if seq["n"] == 1 else r"C:\npm\dsh.cmd"     # 先"未装"，装完"在 PATH"
    monkeypatch.setattr(D, "_which_dsh", fake_which)

    def fake_run(args, timeout=None, env=None):   # v1.0.612：npm 子进程要带补好的 PATH
        calls.append(list(args))
        return {"exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(D, "_run_captured", fake_run)
    r = D.dsh_prepare({})
    assert r["ok"] is True, r
    assert "--registry" in calls[0] and D.MIRROR in calls[0], calls[0]
    assert r["used_mirror"] is True, r


# ---------- 组件状态以自检为准 ----------

def test_probe_ocr_status_follows_selfcheck(monkeypatch):
    from routes import b_ocr_admin as B

    def fake_status():
        return {"state": "ready", "installed": True, "msg": "", "progress": 100,
                "selfcheck": {"state": "done", "ok": False, "ms": 1200, "saw": "",
                              "error": "识别结果为空（引擎没吐出任何文字）"}}
    import core_utils as CU
    monkeypatch.setattr(CU, "ocr_install_status", fake_status)
    monkeypatch.setattr(CU, "ocr_check_updates", lambda: {"packages": [], "updates_available": 0})
    c = B._probe_component("ocr")
    assert c["status"] == "degraded", c
    assert "自检未通过" in c["hint"], c
    assert any(a.get("kind") == "check" for a in c.get("actions") or []), c

# ============================================================ 来自 test_v609_versions_truth.py（v1.0.609）


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mk_ocr(tmp_path, dist_infos, recorded):
    ocr = tmp_path / "ocr"
    ocr.mkdir(parents=True)
    for name in dist_infos:
        (ocr / name).mkdir()
    (ocr / "versions.json").write_text(json.dumps(recorded, ensure_ascii=False), encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    return str(cfg)


def test_versions_prefer_disk_over_recorded(tmp_path, monkeypatch):
    cfg = _mk_ocr(tmp_path,
                  ["numpy-2.5.3.dist-info", "onnxruntime-1.30.0.dist-info",
                   "rapidocr_onnxruntime-1.4.4.dist-info"],
                  {"numpy": "2.5.2", "onnxruntime": "1.29.0", "rapidocr-onnxruntime": "1.2.3",
                   "opencv-python": "5.0.0.93"})
    monkeypatch.setattr(O, "config_path", lambda *a, **k: cfg)
    v = O.ocr_versions()
    assert v["numpy"] == "2.5.3" and v["onnxruntime"] == "1.30.0", v
    assert v["rapidocr-onnxruntime"] == "1.4.4", v          # 下划线包名归一成 dashed
    assert v["opencv-python"] == "5.0.0.93", v              # 磁盘无 dist-info → 回落记录值，不猜


def test_versions_on_disk_parses_wheel_names(tmp_path, monkeypatch):
    cfg = _mk_ocr(tmp_path, ["opencv_python-5.0.0.93.dist-info", "pyclipper-1.4.0.dist-info",
                             "not-a-dist-info.txt", "shapely-2.2.0rc1.dist-info"], {})
    monkeypatch.setattr(O, "config_path", lambda *a, **k: cfg)
    d = O.ocr_versions_on_disk()
    assert d == {"opencv-python": "5.0.0.93", "pyclipper": "1.4.0", "shapely": "2.2.0rc1"}, d


def test_no_recorded_file_is_safe(tmp_path, monkeypatch):
    ocr = tmp_path / "ocr"
    ocr.mkdir()
    (ocr / "numpy-2.5.3.dist-info").mkdir()
    monkeypatch.setattr(O, "config_path", lambda *a, **k: str(tmp_path / "config.json"))
    assert O.ocr_versions() == {"numpy": "2.5.3"}


def test_size_hint_reports_measured_disk_size(tmp_path, monkeypatch):
    ocr = tmp_path / "ocr"
    ocr.mkdir()
    (ocr / "blob.bin").write_bytes(b"x" * (3 * 1024 * 1024))
    monkeypatch.setattr(O, "config_path", lambda *a, **k: str(tmp_path / "config.json"))
    hint = O.ocr_size_hint()
    assert "3MB" in hint, hint          # 本机实测占用，不是写死的参考值
    assert "81MB" in hint, hint         # 下载量口径
    assert O.ocr_disk_size_mb() == 3


def test_stale_size_claims_gone_from_shipped_code():
    """9 处"约 130MB"/3 处"~213MB" 是散在代码里的旧口径，必须一起清掉——
    只改 UI 不改注释/日志，下一轮又会被人按注释里的旧数抄回去。"""
    targets = ["core/ocr.py", "routes/b_ocr_admin.py", "js/11-workspace-kb-study.js",
               "index.html", "build.py", "services/learn_capture.py", "core_utils.py"]
    bad = []
    for rel in targets:
        s = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for token in ("130MB", "213MB"):
            if token in s:
                bad.append("%s:%s" % (rel, token))
    assert not bad, "仍有旧体积口径残留：%s" % bad
    # 新口径要有出处，不能又变成一句无来源的数
    src = io.open(os.path.join(ROOT, "core/ocr.py"), encoding="utf-8").read()
    assert "_OCR_DL_MB = 81" in src and "_OCR_DISK_MB = 226" in src
    assert "def ocr_size_hint" in src


# ---- v1.0.610：体检行把"磁盘真值"摆到界面上（此前 OCR 行版本一直是空的）----

def _probe(monkeypatch, versions):
    import core_utils as CU
    monkeypatch.setattr(CU, "ocr_install_status",
                        lambda: {"state": "idle", "installed": True, "selfcheck": {}})
    monkeypatch.setattr(CU, "ocr_check_updates", lambda *a, **k: {"packages": []})
    monkeypatch.setattr(CU, "ocr_versions", lambda: dict(versions))
    monkeypatch.setattr(CU, "ocr_size_hint", lambda: "下载约 81MB、本机当前占用约 226MB")
    from routes.b_ocr_admin import _probe_component
    return _probe_component("ocr")


def test_probe_shows_installed_versions_as_badge(monkeypatch):
    c = _probe(monkeypatch, {"rapidocr-onnxruntime": "1.4.4", "onnxruntime": "1.30.0",
                             "numpy": "2.5.3", "shapely": "2.2.0rc1"})
    assert c["version"] == "rapidocr 1.4.4 · onnxruntime 1.30.0 · numpy 2.5.3", c["version"]
    assert len(c["version"]) <= 60, "前端 .comp-ver 只留 60 字，超了会被截成半截版本号"
    # 体积口径由后端一处给出，前端确认文案取这个字段，不再自抄
    assert c["size_hint"] == "下载约 81MB、本机当前占用约 226MB"


def test_probe_leaves_badge_empty_when_versions_unknown(monkeypatch):
    """读不到 dist-info 也不回落记录值时，宁可不显示，也不摆一个猜出来的版本。"""
    c = _probe(monkeypatch, {})
    assert c["version"] is None, c

# ============================================================ 来自 test_v611_asr_component.py（v1.0.611）


import pytest   # noqa: E402

from services import asr as A   # noqa: E402
import routes.b_ocr_admin as RO   # noqa: E402


@pytest.fixture
def _clean(monkeypatch):
    monkeypatch.setattr(A, "install_progress", lambda: {"running": False, "phase": "", "pct": 0})
    monkeypatch.setattr(A, "last_error", lambda: "")


def _mk(monkeypatch, complete, meta=None, running=False, pct=0, err=""):
    monkeypatch.setattr(A, "install_progress",
                        lambda: {"running": running, "phase": "pip 安装组件及全部依赖", "pct": pct})
    monkeypatch.setattr(A, "component_complete", lambda: complete)
    monkeypatch.setattr(A, "installed_meta", lambda: meta or {})
    monkeypatch.setattr(A, "last_error", lambda: err)
    monkeypatch.setattr(A, "install_progress",
                        lambda: {"running": running, "phase": "pip 安装组件及全部依赖", "pct": pct})
    monkeypatch.setattr(A, "component_complete", lambda: complete)
    monkeypatch.setattr(A, "installed_meta", lambda: meta or {})
    monkeypatch.setattr(A, "last_error", lambda: err)


def test_missing_asr_offers_in_app_install(monkeypatch):
    _mk(monkeypatch, False)
    c = RO._probe_component("asr")
    assert c["status"] == "missing", c
    acts = [(a.get("api"), a.get("kind")) for a in c["actions"]]
    assert ("/api/asr/install", "install") in acts, acts   # 参与「一键安装」，不是只给个链接


def test_complete_asr_shows_installed_packages(monkeypatch):
    _mk(monkeypatch, True, meta={"pkgs": ["faster-whisper==1.2.1", "av==16.1.0"]})
    c = RO._probe_component("asr")
    assert c["status"] == "ok", c
    assert "faster-whisper" in (c["version"] or ""), c
    assert c["actions"] == [], c


def test_running_install_reports_progress(monkeypatch):
    _mk(monkeypatch, False, running=True, pct=42)
    c = RO._probe_component("asr")
    assert c["status"] == "installing", c
    assert "42%" in c["hint"], c          # 组件页 3 秒轮询靠这个状态活着


def test_installed_but_broken_shows_real_reason(monkeypatch):
    """装好了 ≠ 能用：把上次模型加载的真实异常摆到体检行里，而不是只报一句「转写失败」。"""
    _mk(monkeypatch, True, meta={"pkgs": ["faster-whisper==1.2.1"]},
        err="HTTPError: 401 Client Error ... cas-server.xethub.hf.co")
    c = RO._probe_component("asr")
    assert c["status"] == "degraded", c
    assert "401" in c["hint"], c


def test_asr_is_in_the_component_summary(monkeypatch):
    monkeypatch.setattr(RO, "_probe_component",
                        lambda k: {"key": k, "name": k, "status": "ok", "hint": "", "actions": []})
    import backend as B
    with B.app.test_request_context("/api/components/status"):
        body = RO.api_components_status().get_json()
    assert len(body["components"]) == 7 and body["total"] == 7, body["total"]
    assert [c["key"] for c in body["components"]].count("asr") == 1


def test_transcribe_surfaces_failure_reason(monkeypatch):
    monkeypatch.setattr(A, "_get_model", lambda: None)
    A._state["_failed_msg"] = "OSError: 模型下载中断（hf-mirror 超时）"
    try:
        text, err = A.transcribe_file("whatever.wav")
        assert text is None
        assert "模型下载中断" in err, err      # 不再是"未安装或加载失败"这种含糊话
    finally:
        A._state["_failed_msg"] = ""


def test_component_path_falls_back_to_ocr_runtime(monkeypatch, tmp_path):
    """numpy 是 dist/ocr 旁挂的，组件目录里没有——导入失败时必须走 ensure_ocr_runtime()。
    用 sys.modules['numpy']=None 造出稳定的 ImportError（不必真把 numpy 从机器上搬走）。"""
    import core.ocr as O
    calls = []
    monkeypatch.setattr(O, "ensure_ocr_runtime", lambda: calls.append(1) or False)
    monkeypatch.setattr(A, "component_complete", lambda: True)
    monkeypatch.setattr(A, "component_dir", lambda: str(tmp_path))
    logged = []
    monkeypatch.setattr(A, "log_event", lambda *a, **k: logged.append(a[0] if a else ""))
    A._state["_path_ready"] = None
    real = sys.modules.get("numpy")
    sys.modules["numpy"] = None
    try:
        assert A._ensure_component_path() is True
    finally:
        if real is None:
            sys.modules.pop("numpy", None)
        else:
            sys.modules["numpy"] = real
        A._state["_path_ready"] = None
    assert calls == [1], "numpy 导入失败后没有去挂 OCR 旁挂目录"
    assert any("numpy" in str(x) for x in logged), logged   # 挂不上也要留痕，不能静默


def test_source_locks_ensure_call():
    """行为断言在 frozen 才成立（本机 venv 有 numpy），源码锁保证这条接线不被以后删掉。"""
    import io
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "services", "asr.py"), encoding="utf-8").read()
    i = src.find("def _ensure_component_path")
    body = src[i:src.find("\ndef ", i + 10)]
    assert "ensure_ocr_runtime" in body, "_ensure_component_path 丢了 OCR 旁挂路径兜底"

# ============================================================ 来自 test_v611_update_cache.py（v1.0.611）


def _fresh(monkeypatch, latest="9.9.9", calls=None, tmp_path=None):
    """把联网面（版本解析）换成计数器；磁盘面给一个固定的"已装 1.0.0 + 六个目录都在"。
    目录必须真造出来，否则 state 会是 missing（missing 不算可升级，断言就失去意义）。"""
    O._OCR_UPD["ts"] = 0.0
    O._OCR_UPD["data"] = None
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    for sub in O._OCR_SUB2PKG:
        (tmp_path / "ocr" / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(O, "config_path", lambda *a, **k: str(cfg))
    monkeypatch.setattr(O, "ocr_versions", lambda: {p: "1.0.0" for p in O._OCR_WHEELS})

    def _fake(pkg, py="cp313"):
        if calls is not None:
            calls.append(pkg)
        return latest

    monkeypatch.setattr(O, "_ocr_latest_version", _fake)


def test_second_call_is_cached(tmp_path, monkeypatch):
    calls = []
    _fresh(monkeypatch, calls=calls, tmp_path=tmp_path)
    a = O.ocr_check_updates(force=True)
    n1 = len(calls)
    b = O.ocr_check_updates()
    assert n1 == len(O._OCR_WHEELS), n1
    assert len(calls) == n1, "第二次调用不应再打镜像（缓存未生效）"
    assert b is a and b["updates_available"] == len(O._OCR_WHEELS)
    assert b.get("checked_at")


def test_ttl_expiry_rescans(tmp_path, monkeypatch):
    calls = []
    _fresh(monkeypatch, calls=calls, tmp_path=tmp_path)
    O.ocr_check_updates(force=True)
    n1 = len(calls)
    # 时间往前推过 TTL → 必须重算，否则"可升级"会永远停在旧镜像结果
    monkeypatch.setattr(O.time, "time", lambda: O._OCR_UPD["ts"] + O._OCR_UPD_TTL + 1)
    O.ocr_check_updates()
    assert len(calls) == n1 * 2, len(calls)


def test_invalidate_forces_rescan(tmp_path, monkeypatch):
    calls = []
    _fresh(monkeypatch, calls=calls, tmp_path=tmp_path)
    O.ocr_check_updates(force=True)
    n1 = len(calls)
    O.ocr_invalidate_updates()
    O.ocr_check_updates()
    assert len(calls) == n1 * 2, "装完/更新完不作废缓存，界面会继续报刚换掉的旧版本"


def test_cache_only_never_touches_network(tmp_path, monkeypatch):
    calls = []
    _fresh(monkeypatch, calls=calls, tmp_path=tmp_path)
    r = O.ocr_check_updates(cache_only=True)
    assert calls == []
    assert r["pending"] is True and r["packages"] == []
    # 有缓存时直接给缓存，仍然零请求
    O.ocr_check_updates(force=True)
    n1 = len(calls)
    r2 = O.ocr_check_updates(cache_only=True)
    assert r2["updates_available"] == len(O._OCR_WHEELS)
    assert len(calls) == n1


def test_probe_is_fast_when_cache_cold(tmp_path, monkeypatch):
    """进「运行组件」的探测必须与镜像往返解耦：缓存空着就暂不报可升级，而不是当场扫。"""
    calls = []
    _fresh(monkeypatch, calls=calls, tmp_path=tmp_path)
    import core_utils as CU
    monkeypatch.setattr(CU, "ocr_install_status", lambda: {"state": "idle", "installed": True})
    monkeypatch.setattr(CU, "ocr_size_hint", lambda: "下载约 81MB、本机当前占用约 226MB")
    from routes.b_ocr_admin import _probe_component
    c = _probe_component("ocr")
    assert calls == [], "界面路径打到了镜像（%d 次）" % len(calls)
    assert c["status"] != "outdated", c
    assert "可升级" not in (c.get("hint") or ""), c.get("hint")


def test_install_worker_invalidates_cache():
    """装完必须作废缓存（源码锁：worker 收尾流程里要有这一步）。"""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "core", "ocr.py"), encoding="utf-8").read()
    i = src.find("def _ocr_install_worker")
    body = src[i:src.find("\ndef ", i + 10)]
    assert "ocr_invalidate_updates()" in body


# ============================================================ 来自 test_v619_components_na.py（v1.0.619）

import pytest

sys.path.insert(0, ROOT)

from routes import b_ocr_admin as R    # noqa: E402


def _comp(client, key="build_env"):
    d = client.get("/api/components/status").get_json()
    return next(c for c in d["components"] if c["key"] == key), d


# ---------- 后端判定 ----------

def test_source_root_detects_manifest_for_real(tmp_path, monkeypatch):
    """判据本身（有没有 requirements-dev.txt）必须被真跑一遍：
    只 patch `_build_source_root` 的锁，改坏它内部的 isfile 判断也不会红。"""
    monkeypatch.setattr(R, "_source_root_dir", lambda: str(tmp_path))
    assert R._build_source_root() == (str(tmp_path), False)
    (tmp_path / "requirements-dev.txt").write_text("pyinstaller\n", encoding="utf-8")
    assert R._build_source_root() == (str(tmp_path), True)


def test_source_root_is_parent_of_packed_exe(monkeypatch, tmp_path):
    """归位后的真实布局：Desktop\\企微伴侣\\WeComCompanion.exe → 源码根 = Desktop
    （用户那条报错里的路径正是这么算出来的，所以它必然找不到依赖清单）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "企微伴侣" / "WeComCompanion.exe"))
    assert R._source_root_dir() == str(tmp_path)


def test_no_source_tree_marks_component_not_applicable(monkeypatch, app_client):
    monkeypatch.setattr(R, "_build_source_root", lambda: (r"C:\Users\someone\Desktop", False))
    c, d = _comp(app_client)
    assert c["status"] == "na", c
    assert "不适用" in c["hint"], c["hint"]
    assert not [a for a in (c.get("actions") or []) if a.get("kind") == "install"], \
        "不适用的组件还给安装按钮，一键安装就会把它捞进待办"
    # 汇总口径：na 不计入分母，单独报数
    assert d["total"] == len(d["components"]) - d["na_n"], d
    assert d["na_n"] >= 1


def test_source_tree_present_still_offers_upgrade(monkeypatch, app_client):
    """有源码的开发机不能顺手把按钮关掉——那才是真·缺依赖，必须还能一键装。"""
    monkeypatch.setattr(R, "_build_source_root", lambda: (ROOT, True))
    monkeypatch.setattr(R, "_locate_build_python", lambda: ("/no/such/python", ""))
    c, _ = _comp(app_client)
    assert c["status"] == "missing", c
    assert [a for a in c["actions"] if a.get("kind") == "install"], "开发机丢了「一键升级」入口"


def test_fix_endpoint_is_honest_without_source(monkeypatch, app_client):
    """面板此时已不给按钮；直接 POST 也要拿到人话，而不是带用户家目录的路径。"""
    monkeypatch.setattr(R, "_build_source_root", lambda: (r"C:\Users\user\Desktop", False))
    monkeypatch.setattr(R, "_locate_build_python", lambda: (sys.executable, ""))
    r = app_client.post("/api/components/fix", json={"key": "build_env"})
    assert r.status_code == 400, r.get_json()
    err = r.get_json()["error"]
    assert "不适用" in err, err
    assert "C:\\Users" not in err and "requirements-dev.txt" in err, err


# ---------- 前端呈现 ----------

_JS = io.open(os.path.join(ROOT, "js", "11-workspace-kb-study.js"), encoding="utf-8").read()
_CORE_JS = io.open(os.path.join(ROOT, "js", "01-core.js"), encoding="utf-8").read()


def test_js_renders_na_state():
    # v1.0.650 U6：状态图标/文案表从"每页手抄一份"收敛到 js/01-core.js 的 STATUS_*
    # （以前组件视图 ✅可用 / 同页汇总「全部就绪」/ 设置页绿点「已安装」三处三种叫法）。
    assert 'na:"➖"' in _CORE_JS, "状态图标表没有 na——会渲染成 ⚪ + 原始字符串"
    assert 'na:"不适用"' in _CORE_JS, "状态文案表没有 na"
    assert "const ic=STATUS_ICON, st=STATUS_WORD" in _JS, \
        "组件视图又改回本地手抄表——两张表各改各的，正是这次要收掉的漂移"


def test_js_excludes_na_everywhere_it_counts():
    """三处计数都要排除 na：待处理数、一键安装 todo、仍需手动 manual。
    逐条正则取整行来判——只断言"文件里出现过 c.status!==\"na\""会被其中一处满足，
    自检时改掉另一处仍是绿（第一版就是这么漏的）。"""
    import re
    patterns = {
        "need": r"const need=[^\n]*",
        "todo": r"const _todo=[^\n]*",
        "manual": r"const _manual=[^\n]*",
    }
    for what, pat in patterns.items():
        m = re.search(pat, _JS)
        assert m, f"找不到 {what} 那一行（改动结构时请同步本锁的正则）"
        assert '"na"' in m.group(0), f"{what} 没排除 na：{m.group(0)}"
        assert 'c.status!=="ok"' in m.group(0) or "!==\"ok\"" in m.group(0), m.group(0)


def test_na_component_visible_in_served_html(app_client):
    """走真实接口取一份 status，确认 na 项的字段齐（name/hint/status），
    前端拿到的不是半成品。"""
    d = app_client.get("/api/components/status").get_json()
    for c in d["components"]:
        assert c["status"] in ("ok", "degraded", "missing", "installing", "unknown",
                               "outdated", "na"), c
        if c["status"] == "na":
            assert c.get("hint") and c.get("name"), c


@pytest.fixture()
def app_client():
    import backend
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as c:
        yield c

# ============================================================ 来自 test_v623_comp_path.py（v1.0.623）


import core.portable as P        # noqa: E402
import agent_tools.os_ops as OS  # noqa: E402


@pytest.fixture
def fake_ws(tmp_path, monkeypatch):
    """把产物库根指到临时目录（不建 组件/，由用例自己决定造什么）。"""
    ws = tmp_path / "产物库"
    ws.mkdir()
    monkeypatch.setattr(P, "workspace_root", lambda: str(ws))
    return ws


def _mk_node(root, version_dir="node-v22.20.0-win-x64", with_marker=True):
    d = root / "组件" / "node" / "current" / version_dir
    d.mkdir(parents=True)
    exe = d / "node.exe"
    exe.write_bytes(b"MZ")
    (d / "npm.cmd").write_text("@echo off\n", encoding="utf-8")
    if with_marker:
        import json
        marker = root / "组件" / "node" / "_portable.json"
        marker.write_text(json.dumps({"name": "node", "exe": str(exe)}), encoding="utf-8")
    return d


def test_dirs_come_from_marker_and_from_walk(fake_ws):
    d = _mk_node(fake_ws)
    assert str(d) in P.component_bin_dirs(), "标记里记的 exe 目录没被认出来"
    # 标记丢了也要靠目录结构兜住（zip 自带一层顶层目录那种）
    (fake_ws / "组件" / "node" / "_portable.json").unlink()
    assert str(d) in P.component_bin_dirs(), "没标记时靠遍历也该找到 node.exe 所在目录"


def test_probe_never_creates_directories(tmp_path, monkeypatch):
    """首启闸门口径：只 stat，不许顺手 makedirs 造出 产物库/组件/node。"""
    ws = tmp_path / "产物库"          # 故意不存在
    monkeypatch.setattr(P, "workspace_root", lambda: str(ws))
    assert P.component_bin_dirs() == []
    assert not ws.exists(), "探测把产物库建出来了——v1.0.619 的延时创建白做"
    assert not (tmp_path / "产物库" / "组件").exists()


def test_apply_prepends_and_is_idempotent(fake_ws, monkeypatch):
    d = _mk_node(fake_ws)
    saved = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = saved
        added = P.apply_component_path()
        assert added == [str(d)], added
        assert os.environ["PATH"].split(os.pathsep)[0] == str(d), "没顶到最前面"
        again = P.apply_component_path()
        assert again == [], "第二次还把同一目录再塞一遍（PATH 会越拼越长）"
        assert os.environ["PATH"].split(os.pathsep).count(str(d)) == 1
    finally:
        os.environ["PATH"] = saved


def test_existing_entry_is_matched_loosely(fake_ws, monkeypatch):
    """已在 PATH 里（大小写/尾斜杠不同）就不该重复加。"""
    d = _mk_node(fake_ws)
    saved = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = str(d) + os.pathsep + saved
        assert P.apply_component_path() == []
        os.environ["PATH"] = (str(d) + "\\").lower() + os.pathsep + saved
        assert P.apply_component_path() == [], "尾斜杠/大小写差异被当成没装过"
    finally:
        os.environ["PATH"] = saved


def test_pc_run_makes_portable_node_resolvable(fake_ws):
    """端到端：跑 pc_run 时用 `where node` 验它真的在解析路径里（子进程继承本进程 env）。"""
    d = _mk_node(fake_ws)
    saved = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = saved
        r = OS.pc_run("where node", timeout=25)
        assert r.get("ok") or r.get("exit_code") == 0, r
        assert str(d).lower() in (r.get("stdout") or "").lower(), \
            "pc_run 里找不到软件自装的 node——用户「找了一圈」的那个坑没堵上"
    finally:
        os.environ["PATH"] = saved


def test_process_injection_never_touches_the_registry():
    """两段职责要分得开：进程内注入不碰注册表；碰注册表的只有 625「终端共享」。

    这条锁原本（623）写的是"core/portable.py 里不许出现 winreg/setx/subprocess"。
    625 按用户要求加了可选的用户 PATH 共享，winreg 因此合法出现——如果继续按"整份文件
    不许有 winreg"锁，就会变成"要么关掉新功能、要么把锁拆了"，两头都错。
    现在的判据按段落分：注入段一条都不许碰；共享段只许 HKCU（用户级，不要管理员）、
    只许 winreg（不许 shell out setx——setx 在 1024 字符截断会抹掉用户尾部条目）。
    """
    src = io.open(os.path.join(ROOT, "core", "portable.py"), encoding="utf-8").read()
    marker = "# ---------------------------------------------------------------- 终端共享"
    assert marker in src, "共享段与注入段的分界标记没了（这条锁会失去作用范围）"
    inject = src[:src.index(marker)]
    for banned in ("winreg", "SetEnvironmentVariable", "subprocess", "setx"):
        assert banned not in inject, "进程内注入段出现了 %r：它不该动用户环境变量" % banned
    body = src[src.index(marker):]
    assert "winreg.HKEY_CURRENT_USER" in body, "共享段不再写 HKCU，改动去向不明"
    assert "HKEY_LOCAL_MACHINE" not in src, "出现机器级写入：需要管理员且影响全机所有账户"
    assert "subprocess" not in src and "os.system" not in src, "shell out 改 PATH 的通道不该存在"


def test_install_finish_injects_immediately():
    """装完当场注入，不用等重启（否则"已安装"与"能直接用"之间隔一次重启）。"""
    src = io.open(os.path.join(ROOT, "core", "portable.py"), encoding="utf-8").read()
    w = src[src.index("def _install_worker"):]
    assert "apply_component_path()" in w, "安装成功后没注入 PATH"
    assert w.index("apply_component_path()") < w.index("except Exception"), "只在正常路径注入，失败分支不该跑"


def test_source_discriminator_ignores_our_own_portable_dir(fake_ws):
    """注入 PATH 之后，组件页「来源」判据不能把自己装的算成用户系统装的。

    老写法是 `_sh.which("node")` 有没有命中；便携目录一进 PATH，which 必然命中我们
    解压的那份，界面就会恒说「来源：系统 PATH」——用户以为软件没装、其实装了，
    反过来"缺组件"的提示也会失真。`find_system_tool()` 按便携目录黑名单排除。
    """
    d = _mk_node(fake_ws)
    saved = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = saved
        assert P.apply_component_path() == [str(d)]
        assert P.find_node(), "agent 侧该能找到（便携 node 已可用）"
        sysn = P.find_system_tool("node")
        assert not sysn or os.path.dirname(sysn).rstrip("\\").lower() != str(d).lower(), \
            "find_system_tool 把软件自装的便携版当成了系统安装"
    finally:
        os.environ["PATH"] = saved


def test_runtime_locations_reports_installed_tools_without_creating_anything(fake_ws):
    """「装完能不能第一时间找到」的判据：便携版就地报出路径，且探测不建目录。"""
    d = _mk_node(fake_ws)
    got = P.runtime_locations(refresh=True)
    assert got.get("node", "").lower().startswith(str(d).lower()), got


def test_runtime_locations_is_memoized_then_refreshed(fake_ws):
    """30s 记忆 + 安装完成主动作废：既要"刚装完立刻可见"，也不要每条消息都 stat。"""
    d = _mk_node(fake_ws)
    first = P.runtime_locations(refresh=True)
    assert first.get("node")
    (d / "node.exe").unlink()          # 文件"消失"，缓存不该立刻反映（省 stat）
    assert P.runtime_locations().get("node") == first["node"], "没走缓存"
    assert P.runtime_locations(refresh=True).get("node") != first["node"], "refresh 没作废缓存"


def test_runtime_hint_text_shape_and_empty_case(fake_ws, monkeypatch):
    monkeypatch.setattr(P, "runtime_locations", lambda refresh=False: {})
    assert P.runtime_hint_text() == "", "一个运行时都没有时不该往上下文塞废话"
    monkeypatch.setattr(P, "runtime_locations",
                        lambda refresh=False: {"node": r"C:\app\组件\node\current\node.exe"})
    t = P.runtime_hint_text()
    assert t.startswith("[本机运行时]"), t[:40]
    assert "node.exe" in t and "不必再去搜文件位置" in t, t
    assert "「没装 node」" in t or "没装 node" in t, "要正面否掉模型上次 where 落空得出的错误结论"


def test_prompt_anchors_the_runtime_hint(fake_ws, monkeypatch):
    """模型每条消息都该直接看到"node 在这儿"，而不是再 `where node` / 满盘 dir。"""
    import agent.context as ctx

    d = _mk_node(fake_ws)
    monkeypatch.setattr(P, "runtime_locations",
                        lambda refresh=False: {"node": str(d / "node.exe"),
                                               "npm": str(d / "npm.cmd")})
    msgs = ctx.build_chat_messages([{"role": "user", "content": "跑个 node 脚本"}],
                                   {}, 9222, None, False)
    joined = "\n".join(m.get("content", "") for m in msgs if m.get("role") == "system")
    assert "[本机运行时]" in joined, [m.get("content", "")[:50] for m in msgs]
    assert str(d) in joined and "不必再去搜文件位置" in joined


def test_install_worker_invalidates_the_hint_cache():
    """装完不刷新缓存的话，新装的 node 在提示词里还要"隐身"半分钟。"""
    src = io.open(os.path.join(ROOT, "core", "portable.py"), encoding="utf-8").read()
    w = src[src.index("def _install_worker"):src.index("def install_async")]
    assert "runtime_locations(refresh=True)" in w, w[-400:]


def test_component_page_wording_matches_reality():
    """组件页文案与行为同步 + 「来源」判据不再用裸 which。"""
    src = io.open(os.path.join(ROOT, "routes", "b_ocr_admin.py"), encoding="utf-8").read()
    assert "已注入本软件运行环境" in src, "组件页还停留在「只在本软件内可用」的旧口径"
    for stale in ('not _sh.which("node")', 'not _sh.which("git")'):
        assert stale not in src, \
            "来源判据仍是裸 which：%s —— 便携目录注入 PATH 后它恒报「系统 PATH」" % stale
    assert src.count("find_system_tool") >= 2, "git/node 两处来源判据都要换掉"

# ============================================================ 来自 test_v625_share_path.py（v1.0.625）


import core.portable as P   # noqa: E402

USER_ENTRIES = [
    r"C:\Python313\Scripts",
    r"%USERPROFILE%\AppData\Local\Microsoft\WindowsApps",   # 字面量：必须原样活着
    r"C:\Program Files\Git\cmd",
]
OURS = [r"C:\app\dist\产物库\组件\node\current\node-v22.20.0-win-x64"]


class FakeReg:
    """内存假注册表：记录读写次数与最后一次写入的值/类型，供断言。"""

    def __init__(self, parts=None, typ=1):        # 1=REG_SZ（本机实测类型）
        self.parts = list(parts if parts is not None else USER_ENTRIES)
        self.typ = typ
        self.writes = []
        self.broadcasts = 0

    def raw(self):
        return list(self.parts), self.typ

    def write(self, parts, typ):
        if len(";".join(parts)) > P._PATH_LIMIT:
            raise RuntimeError("用户 PATH 会超过 %d 字符，已拒绝写入" % P._PATH_LIMIT)
        self.writes.append((list(parts), typ))
        self.parts = list(parts)
        self.broadcasts += 1
        return ";".join(parts)


@pytest.fixture
def reg(monkeypatch, tmp_path):
    fr = FakeReg()
    monkeypatch.setattr(P, "_user_path_raw", fr.raw)
    monkeypatch.setattr(P, "_write_user_path", fr.write)
    monkeypatch.setattr(P, "component_bin_dirs", lambda: list(OURS))
    monkeypatch.setattr(P, "workspace_root", lambda: str(tmp_path))
    P._UP_CACHE["val"] = None
    return fr


def test_enable_appends_ours_and_keeps_user_entries_untouched(reg):
    r = P.share_user_path(on=True)
    assert r["ok"] and r["changed"] and r["added"] == OURS, r
    # 用户原有三条一字不动、顺序不变，我们的追加在最后
    assert reg.parts[:len(USER_ENTRIES)] == USER_ENTRIES, reg.parts
    # 类型保持 REG_SZ：改成 REG_EXPAND_SZ 会让 %USERPROFILE% 那条的展开语义偷偷变
    assert reg.writes[-1][1] == 1, reg.writes[-1]
    led = json.load(io.open(os.path.join(str(P.workspace_root()), "系统", "path_share.json"),
                            encoding="utf-8"))
    assert led["added"] == [o.rstrip("\\") for o in OURS], led


def test_enable_is_idempotent_and_reports_no_change(reg):
    assert P.share_user_path(on=True)["changed"] is True
    reg.writes.clear()
    r = P.share_user_path(on=True)
    assert r["ok"] and r["changed"] is False, r
    assert reg.writes == [], "已在 PATH 里还重复写注册表（并会再广播一次变更）"
    assert len([p for p in reg.parts if p in OURS]) == 1, "PATH 里出现重复条目"


def test_disable_removes_only_ours(reg):
    P.share_user_path(on=True)
    r = P.share_user_path(on=False)
    assert r["ok"] and r["removed"] == OURS, r
    assert reg.parts == USER_ENTRIES, "撤销把用户自己的条目也动了"


def test_disable_removes_ledger_orphans(reg, monkeypatch):
    """目录被删掉后，台账里那条残留也要能摘干净——不能留下 PATH 死链。"""
    old = r"C:\app\dist\产物库\组件\git\current\cmd"
    monkeypatch.setattr(P, "_ledger_read", lambda: [old])
    parts = list(reg.parts) + [old, OURS[0]]
    reg.parts = parts
    r = P.share_user_path(on=False)
    assert r["ok"] and old in r["removed"], r
    assert old not in reg.parts and OURS[0] not in reg.parts
    assert [p for p in reg.parts if p in USER_ENTRIES] == USER_ENTRIES


def test_disable_when_never_enabled_does_not_write(reg):
    reg.writes.clear()
    r = P.share_user_path(on=False)
    assert r["ok"] and r["changed"] is False, r
    assert reg.writes == [], "没东西可摘也去写注册表（还白广播一次）"


def test_state_flag_follows_registry_not_config(reg, monkeypatch):
    """开关状态以注册表为准：用户手工删掉那条，界面要如实回到未开启。"""
    assert P.user_path_shared() is False
    P.share_user_path(on=True)
    assert P.user_path_shared() is True, "写完还报未开启——按钮会停在旧文案，用户以为没生效"
    reg.parts = USER_ENTRIES          # 模拟用户在系统设置里手工删了
    P._UP_CACHE["val"] = None
    assert P.user_path_shared() is False


def test_partial_presence_counts_as_off(monkeypatch):
    """两个便携目录只进了一半时不能报"已开启"——按钮要说真话。"""
    extra = r"C:\app\dist\产物库\组件\git\current\cmd"
    reg_parts = USER_ENTRIES + [OURS[0]]
    monkeypatch.setattr(P, "_user_path_raw", lambda: (list(reg_parts), 1))
    monkeypatch.setattr(P, "component_bin_dirs", lambda: [OURS[0], extra])
    P._UP_CACHE.update({"val": None, "ts": 0.0})
    assert P.user_path_shared() is False


def test_over_limit_is_refused_not_truncated(reg):
    """拼出来超过上限时：拒绝写入 + 说原因，绝不静默截断（截断＝丢用户条目）。"""
    reg.parts = USER_ENTRIES + ["d:" + ("x" * 4000) + str(i) for i in range(6)]
    r = P.share_user_path(on=True)
    assert r["ok"] is False and "拒绝写入" in r.get("error", ""), r
    assert OURS[0] not in reg.parts, "超限了却还是把我们的目录塞进去了"


def test_real_write_keeps_type_and_broadcasts(monkeypatch):
    """用假 winreg 走**真** `_write_user_path`：类型原样回写 + 写完必须广播。

    上面那些用例把写函数本身换成了假表（便于断言条目顺序），这条则验真函数：
    读到什么类型就写回什么类型（本机是 REG_SZ，里面还有字面量 %USERPROFILE%），
    以及写完整件事才算完成——不广播的话新开的终端还是拿不到。
    """
    import types
    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER, fake.KEY_READ, fake.KEY_SET_VALUE = "HKCU", 1, 2
    fake.REG_SZ, fake.REG_EXPAND_SZ = 1, 2
    trace = []
    fake.OpenKey = lambda hive, key, res, access: (access,)
    fake.CloseKey = lambda h: trace.append("close")
    fake.QueryValueEx = lambda h, name: (r"C:\a;C:\b", fake.REG_SZ)

    def _set(h, name, res, typ, val):
        trace.append(("set", h[0], typ, val))

    fake.SetValueEx = _set
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(P, "_broadcast_env_change", lambda: trace.append("broadcast"))
    out = P._write_user_path([r"C:\a", r"C:\b", OURS[0]], fake.REG_EXPAND_SZ)
    assert out == r"C:\a;C:\b;" + OURS[0]
    writes = [t for t in trace if isinstance(t, tuple) and t[0] == "set"]
    assert writes and writes[0][2] == fake.REG_EXPAND_SZ, \
        "没按调用方传入的原类型回写（把 REG_EXPAND_SZ 拍成 REG_SZ 会改变 %VAR% 的展开语义）"
    assert writes[0][1] == fake.KEY_SET_VALUE, "写句柄权限不对"
    assert "broadcast" in trace, "写完没广播，新开的终端拿不到新 PATH"


def test_real_write_refuses_over_limit(monkeypatch):
    """超限走的是真函数：必须抛错而不是写半截（截断＝丢用户条目，不可逆）。"""
    import types
    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER, fake.KEY_SET_VALUE, fake.REG_SZ = "HKCU", 2, 1
    written = []
    fake.OpenKey = lambda *a: (a[3],)
    fake.CloseKey = lambda h: None
    fake.SetValueEx = lambda *a: written.append(a)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    with pytest.raises(Exception) as e:
        P._write_user_path(["x" * (P._PATH_LIMIT + 10)], fake.REG_SZ)
    assert "拒绝写入" in str(e.value), e.value
    assert written == [], "已经拒绝写入却仍动了注册表"


def test_writes_go_through_winreg_never_a_shell():
    """②③条规矩的源码侧护栏：写注册表用 SetValueEx，且这一段没有任何 shell-out 通道。

    不检查"有没有 setx 这个词"——解释性文字里本来就要写它（说明为什么不用），
    按词查只会误伤注释；真正的判据是**没有可执行外部命令的路径**：本模块不 import
    subprocess，`setx` 就算被写进字符串也跑不起来。
    """
    src = io.open(os.path.join(ROOT, "core", "portable.py"), encoding="utf-8").read()
    body = src[src.index("# ---------------------------------------------------------------- 终端共享"):]
    assert "SetValueEx" in body, "共享写回没走 winreg"
    assert "typ or winreg.REG_SZ" in body, "写回没保持原类型（本机是 REG_SZ）"
    assert body.count("import winreg") >= 2, "winreg 没按函数内导入处理（非 Windows 导入本模块会炸）"
    assert "subprocess" not in src, "core.portable 出现了 subprocess：外部命令改 PATH 的通道不该存在"


def test_share_action_shape_and_not_auto_installable(monkeypatch):
    """动作必须是 kind="share"：一键安装只认 kind="install"，绝不能把改用户 PATH 混进批量安装。"""
    import routes.b_ocr_admin as RO
    monkeypatch.setattr(P, "portable_exe", lambda name: r"C:\app\组件\node\current\node.exe")
    monkeypatch.setattr(P, "user_path_shared", lambda: False)
    act, on = RO._share_path_action("node", "node/npm")
    assert act and on is False
    assert act["kind"] == "share" and act["body"] == {"on": True}, act
    assert act["type"] == "api" and act["api"] == "/api/components/share-path"
    assert "HKCU" in act["confirm"] and "你已有的条目一条不动" in act["confirm"], "确认文案要如实说明副作用"
    monkeypatch.setattr(P, "user_path_shared", lambda: True)
    act2, on2 = RO._share_path_action("node", "node/npm")
    assert on2 is True and act2["body"] == {"on": False} and "撤销" in act2["label"]


def test_no_action_when_not_our_portable(monkeypatch):
    """git 来自系统 PATH 时不该出现这个开关——我们没装便携版，共享无意义。"""
    import routes.b_ocr_admin as RO
    monkeypatch.setattr(P, "portable_exe", lambda name: None)
    assert RO._share_path_action("git", "git") == (None, False)


def test_route_passes_flag_through(monkeypatch):
    """路由只做转发：开关语义与四条规矩都在 share_user_path 里，别在路由里各写一遍。"""
    import routes.b_ocr_admin as RO
    import backend as B
    seen = []
    monkeypatch.setattr(P, "share_user_path",
                        lambda on: seen.append(on) or {"ok": True, "changed": True, "msg": "已加入用户 PATH"})
    with B.app.test_request_context("/api/components/share-path", method="POST", json={"on": True}):
        r = RO.api_components_share_path()
    assert seen == [True], seen
    assert r.get_json()["ok"] is True

    seen.clear()
    monkeypatch.setattr(P, "share_user_path", lambda on: seen.append(on) or {"ok": False, "error": "超限"})
    with B.app.test_request_context("/api/components/share-path", method="POST", json={"on": False}):
        r2 = RO.api_components_share_path()
    assert seen == [False] and r2.get_json()["ok"] is False, r2.get_json()

# -*- coding: utf-8 -*-
"""v1.0.298：PC 接管通道（deepseek-harness / dsh）单元/控制流测试。

原则：
- 不真装 dsh、不真跑 dsh（沙箱装全局 npm 包慢且污染用户环境）：
  subprocess.run / subprocess.Popen / shutil.which / _node_info / _which_dsh
  全部 monkeypatch，只测命令构造、参数传递、超时/退出码分支与降级路径。
- Windows npm shim 是 .cmd → 重点验证「shim 解析出 JS 入口 → node 直跑」
  （cmd /c 的引号语义会拆碎含空格路径+任务，实证弃用，只留无空格回退）。
- 非 win32 优雅降级：IS_WIN=False 时全部返回结构化错误不崩。
运行：python tests/test_dsh_bridge.py
"""
import os
import sys
import shutil
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.result import is_err, err_msg  # noqa: E402
import agent_tools.dsh_bridge as dsh_bridge
import core
from agent_tools import build_agent_tools, dispatch_tool

TMP = tempfile.mkdtemp(prefix="dsh_bridge_test_")


def _cleanup():
    shutil.rmtree(TMP, ignore_errors=True)


CFG_BASE = {"enabled_skills": None, "skill_overrides": {}, "connectors": {}, "wecom": {}}


def _patch_dsh_bin(path):
    """把 dsh 探测结果固定为 path（或 None），返回恢复函数。"""
    orig = dsh_bridge._which_dsh
    dsh_bridge._which_dsh = lambda: path
    return lambda: setattr(dsh_bridge, "_which_dsh", orig)


def _patch_node(ok=True, version="v22.22.2"):
    orig = dsh_bridge._node_info
    dsh_bridge._node_info = lambda: {"ok": ok, "version": version,
                                     "detail": "" if ok else f"{version} 过低"}
    return lambda: setattr(dsh_bridge, "_node_info", orig)


# ---------- 装配 ----------
def test_build_agent_tools_dsh_mounted():
    cfg = dict(CFG_BASE)
    tools, prompt = build_agent_tools(cfg)
    names = {t["function"]["name"] for t in tools}
    expect = {"dsh_status", "dsh_prepare", "pc_takeover"}
    missing = expect - names
    assert not missing, f"dsh 接管工具未全挂载：{missing}"
    assert "pc_takeover" in prompt and "dsh_status" in prompt, "pc-ops prompt 未注入接管通道"
    print("PASS build_agent_tools：3 个 dsh 工具 + 接管通道 prompt 全挂载")


# ---------- 环境探测 ----------
def test_node_info_parse():
    orig = dsh_bridge._run_captured
    # v1.0.612：_node_info 先解析运行时路径（PATH → 软件便携版），没有 node 就直接短路，
    # 所以这里要把定位结果钉住，测的才是"版本串解析"本身。
    from core import portable as _p
    orig_find = _p.find_node
    _p.find_node = lambda: r"C:\node\node.exe"
    # 直接喂不同版本串测解析
    cases = [
        ("v22.22.2\n", True),
        ("v24.1.0\n", True),
        ("v22.18.3\n", False),
        ("v20.10.0\n", False),
    ]
    for out, want in cases:
        dsh_bridge._run_captured = lambda a, timeout, env=None, _o=out: {"exit_code": 0, "stdout": _o, "stderr": ""}
        r = dsh_bridge._node_info()
        assert r["ok"] is want, f"node 版本解析错：{out!r} → {r}"
        assert r["path"] == r"C:\node\node.exe", r      # 用的是解析出来的绝对路径
    dsh_bridge._run_captured = orig
    # 异常分支
    def _boom(args, timeout, env=None):
        raise RuntimeError("node 不可用")
    dsh_bridge._run_captured = _boom
    r = dsh_bridge._node_info()
    assert r["ok"] is False and "无法执行 node" in r["detail"], r
    dsh_bridge._run_captured = orig
    _p.find_node = orig_find
    print("PASS _node_info：版本边界（22.18/22.22/24.x）+ 异常分支")


def test_which_dsh_candidates():
    orig = dsh_bridge.shutil.which
    # v1.0.612 起 _which_dsh 还会查软件自装的便携 node 目录 —— 本机真装过一份，
    # 不钉住它"全缺失"这一档就永远测不到（测试必须是假的运行环境）。
    from core import portable as _p
    orig_find, orig_exe = _p.find_node, _p.portable_exe
    _p.find_node = lambda: None
    _p.portable_exe = lambda name: None
    hits = {"dsh.cmd": "C:\\Users\\user\\AppData\\Roaming\\npm\\dsh.cmd",
            "dsh": None, "dsh.ps1": None}
    dsh_bridge.shutil.which = lambda c: hits.get(c)
    assert dsh_bridge._which_dsh() == hits["dsh.cmd"], "应优先解析 dsh.cmd"
    hits["dsh.cmd"] = None
    hits["dsh"] = "C:\\dsh.exe"
    assert dsh_bridge._which_dsh() == "C:\\dsh.exe", "cmd 缺失应回退 dsh"
    hits["dsh"] = None
    assert dsh_bridge._which_dsh() is None, "全缺失应返回 None"
    _p.find_node, _p.portable_exe = orig_find, orig_exe
    dsh_bridge.shutil.which = orig
    print("PASS _which_dsh：dsh.cmd 优先 → dsh 回退 → None")


def test_dsh_check_ready():
    u1, u2 = _patch_node(True), _patch_dsh_bin("C:\\npm\\dsh.cmd")
    try:
        r = dsh_bridge.dsh_check({"dsh": {"base_url": "http://localhost:1234/v1",
                                          "api_key": "sk-x"}})
        assert r["ok"] and r["ready"] and not r["hints"], r
        assert r["base_url"] == "http://localhost:1234/v1" and r["has_api_key"], r
    finally:
        u1(); u2()
    print("PASS dsh_check：node+dsh+凭据齐备 → ready，无 hints")


def test_dsh_check_missing_all():
    u1, u2 = _patch_node(False, "v20.0.0"), _patch_dsh_bin(None)
    # v1.0.425：隔离环境变量——dsh_check 会读 DEEPSEEK_API_KEY/BASE_URL，
    # 开发机 shell 残留这些变量时凭据 hint 消失（实测 3 hints→2）
    saved_env = {k: os.environ.pop(k, None)
                 for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")}
    try:
        r = dsh_bridge.dsh_check({})
        assert not r["ready"], r
        assert len(r["hints"]) == 3, f"应给 3 条修复提示（node/安装/凭据）：{r['hints']}"
    finally:
        u1(); u2()
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
    print("PASS dsh_check：全缺 → 3 条 hints 全给出")


# ---------- 安装 ----------
def test_prepare_already_installed():
    u1, u2 = _patch_node(True), _patch_dsh_bin("C:\\npm\\dsh.cmd")
    orig = dsh_bridge._run_captured
    dsh_bridge._run_captured = lambda a, t: (_ for _ in ()).throw(AssertionError("不应触发 npm"))
    try:
        r = dsh_bridge.dsh_prepare({})
        assert r["ok"] and r.get("already_installed"), r
    finally:
        u1(); u2(); setattr(dsh_bridge, "_run_captured", orig)
    print("PASS dsh_prepare：已安装 → 直接返回，不跑 npm")


def test_prepare_mirror_first():
    """v1.0.608：第 1 次就用 npmmirror 国内镜像（官方源常"连得上但极慢"，
    旧顺序会让一键安装先卡满一次超时才轮到镜像）。"""
    state = {"n": 0}

    def _fake_which():
        state["n"] += 1
        return None if state["n"] == 1 else r"C:\\npm\\dsh.cmd"  # 装前无、装后有

    calls = []
    orig = dsh_bridge._run_captured

    def _fake_run(args, timeout, env=None):
        calls.append(list(args))
        return {"exit_code": 0, "stdout": "added 1 package", "stderr": ""}

    u1 = _patch_node(True)
    from core import portable as _p
    orig_npm = _p.find_npm
    _p.find_npm = lambda: r"C:/node/npm.cmd"      # v1.0.612：npm 走绝对路径（便携版不在 PATH）
    orig_which = dsh_bridge._which_dsh
    dsh_bridge._which_dsh, dsh_bridge._run_captured = _fake_which, _fake_run
    try:
        r = dsh_bridge.dsh_prepare({})
        assert r["ok"] and r.get("installed") and r.get("used_mirror"), r
        assert calls == [[r"C:/node/npm.cmd", "install", "-g", "@deepseek-ai/dsh",
                          "--registry", dsh_bridge.MIRROR]], calls
        assert any("npmmirror.com" in x for x in calls[0]), calls
    finally:
        u1(); _p.find_npm = orig_npm
        setattr(dsh_bridge, "_which_dsh", orig_which); setattr(dsh_bridge, "_run_captured", orig)
    print("PASS dsh_prepare：第 1 次即走 npmmirror 国内镜像，命令构造正确")


def test_prepare_official_fallback():
    """镜像真失败时才回落官方源（attempt=2、used_mirror=False）。"""
    state = {"n": 0}

    def _fake_which():
        # 调用序：#1 安装前检查(无) → npm×2 → #2 安装后检查(有)
        state["n"] += 1
        return None if state["n"] <= 1 else r"C:\\npm\\dsh.cmd"

    calls = []
    orig = dsh_bridge._run_captured

    def _fake_run(args, timeout, env=None):
        calls.append(list(args))
        return ({"exit_code": 1, "stdout": "", "stderr": "ETIMEDOUT npmmirror"}
                if len(calls) == 1
                else {"exit_code": 0, "stdout": "ok", "stderr": ""})

    u1 = _patch_node(True)
    from core import portable as _p
    orig_npm = _p.find_npm
    _p.find_npm = lambda: r"C:/node/npm.cmd"
    orig_which = dsh_bridge._which_dsh
    dsh_bridge._which_dsh, dsh_bridge._run_captured = _fake_which, _fake_run
    try:
        r = dsh_bridge.dsh_prepare({})
        assert r["ok"] and r["attempt"] == 2 and not r.get("used_mirror"), r
        assert all(c[0] == r"C:/node/npm.cmd" for c in calls), calls
        assert "--registry" in calls[0] and "--registry" not in calls[1], calls
    finally:
        u1(); _p.find_npm = orig_npm
        setattr(dsh_bridge, "_which_dsh", orig_which); setattr(dsh_bridge, "_run_captured", orig)
    print("PASS dsh_prepare：镜像失败 → 官方源兜底成功")
def test_prepare_node_missing():
    u1 = _patch_node(False, "v20.0.0")
    orig = dsh_bridge._run_captured
    dsh_bridge._run_captured = lambda a, t: (_ for _ in ()).throw(AssertionError("node 不达标不该跑 npm"))
    try:
        r = dsh_bridge.dsh_prepare({})
        assert not r["ok"] and "Node 环境不满足" in r.get("error", ""), r
    finally:
        u1(); setattr(dsh_bridge, "_run_captured", orig)
    print("PASS dsh_prepare：node 不达标 → 拒绝安装并说明原因")


# ---------- 一次性委托 ----------
def test_run_empty_task():
    r = dsh_bridge.dsh_run({}, "   ")
    assert is_err(r), r
    print("PASS dsh_run：空 task 直接拒绝")


def test_run_not_installed():
    u = _patch_dsh_bin(None)
    try:
        r = dsh_bridge.dsh_run({}, "把 C:\\proj 的测试跑一遍")
        assert is_err(r) and "dsh_prepare" in err_msg(r), r
    finally:
        u()
    print("PASS dsh_run：未安装 → 指引 dsh_prepare")


def test_run_bad_cwd():
    u = _patch_dsh_bin("C:\\npm\\dsh.cmd")
    try:
        r = dsh_bridge.dsh_run({}, "task", cwd="C:\\definitely_not_exist_9999")
        assert is_err(r) and "工作目录不存在" in err_msg(r), r
    finally:
        u()
    print("PASS dsh_run：cwd 不存在 → 结构化错误")


class _FakeProc:
    def __init__(self, out_b, err_b, rc):
        self._out, self._err, self._rc = out_b, err_b, rc
        self.pid = 4321
        self.returncode = rc
        self.killed = False

    def communicate(self, timeout=None):
        if isinstance(self._out, Exception):
            raise self._out
        self.returncode = self._rc
        return self._out, self._err

    def kill(self):
        self.killed = True


def test_resolve_shim_entry():
    """npm shim 解析：真实格式提取绝对 .js 入口；% 变量/不存在路径全排除。"""
    js_real = os.path.join(TMP, "real_entry.js")
    with open(js_real, "w") as f:
        f.write("console.log(1)")
    js_abs = js_real.replace(os.sep, "\\")
    shim = os.path.join(TMP, "fake.dsh.cmd")
    with open(shim, "w", encoding="utf-8") as f:
        f.write('@ECHO OFF\r\nSET "_node_module_folder=%~dp0..\\node_modules"\r\n'
                f'FOR %%F IN ("{js_abs}") DO @SET "_script=%%~sF"\r\n'
                '& "%_prog%" "%_script%" %*\r\n')
    assert dsh_bridge._resolve_shim_entry(shim) == js_real, "应解析出存在的绝对 .js 入口"
    # 只有 % 变量路径 → None
    shim2 = os.path.join(TMP, "fake2.cmd")
    with open(shim2, "w", encoding="utf-8") as f:
        f.write('@ECHO OFF\r\nFOR %%F IN ("%~dp0..\\x.js") DO @SET s=%%~sF\r\n')
    assert dsh_bridge._resolve_shim_entry(shim2) is None, "% 变量路径不应被采信"
    # .js 路径不存在 → None
    shim3 = os.path.join(TMP, "fake3.cmd")
    with open(shim3, "w", encoding="utf-8") as f:
        f.write('FOR %%F IN ("C:\\no_such_dir_xyz\\a.js") DO @SET s=%%~sF\r\n')
    assert dsh_bridge._resolve_shim_entry(shim3) is None, "不存在的入口应判 None"
    # shim 文件不存在 → None
    assert dsh_bridge._resolve_shim_entry(os.path.join(TMP, "nope.cmd")) is None
    print("PASS _resolve_shim_entry：真实格式提取 + 三类拒绝")


def test_run_success():
    # 构造真实格式的假 shim + 假 JS 入口，覆盖「shim 解析 → node 直跑」全链路
    js_entry = os.path.join(TMP, "dsh_entry.js").replace(os.sep, "\\")
    with open(js_entry, "w") as f:
        f.write("// stub entry")
    bin_path = os.path.join(TMP, "App Data", "npm", "dsh.cmd").replace(os.sep, "\\")
    os.makedirs(os.path.dirname(bin_path), exist_ok=True)
    with open(bin_path, "w", encoding="utf-8") as f:
        f.write(f'@ECHO OFF\r\nFOR %%F IN ("{js_entry}") DO @SET "_script=%%~sF"\r\n')

    cfg = dict(CFG_BASE, dsh={"base_url": "http://localhost:1234/v1", "api_key": "sk-test"})
    captured = {}
    fake = _FakeProc(b"all tests fixed, 12/12 green", b"dsh: reasoning: step1\nstep2", 0)
    orig_popen = subprocess.Popen

    def _fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured.update({k: kw.get(k) for k in ("cwd", "env", "creationflags")})
        return fake

    u1 = _patch_dsh_bin(bin_path)
    orig_root = core.paths.workspace_root
    core.paths.workspace_root = lambda: TMP
    dsh_bridge.subprocess.Popen = _fake_popen
    try:
        r = dsh_bridge.dsh_run(cfg, "把 C:\\proj 的测试跑一遍并修复失败用例")
        assert r["ok"] and r["answer"] == "all tests fixed, 12/12 green", r
        assert r["reasoning_tail"].startswith("dsh: reasoning:"), r
        # node 直跑（不经 cmd）：[node, shim 解析出的 js 入口, ...args]
        cmd = captured["cmd"]
        assert "node" in os.path.basename(cmd[0]).lower(), f"应直跑 node：{cmd}"
        assert os.path.normpath(cmd[1]) == os.path.normpath(js_entry), cmd
        assert cmd[-3:] == ["--profile", "headless", "把 C:\\proj 的测试跑一遍并修复失败用例"], cmd
        # env 注入 + 默认 cwd + 进程组
        assert captured["env"]["DEEPSEEK_BASE_URL"] == "http://localhost:1234/v1", captured["env"]
        assert captured["env"]["DEEPSEEK_API_KEY"] == "sk-test", captured["env"]
        assert captured["cwd"] == TMP, captured
        # v1.0.299：进程组（超时杀整树）+ 无窗口（不弹黑框）
        if dsh_bridge.IS_WIN:
            assert captured["creationflags"] == (0x00000200 | 0x08000000), captured
        else:
            assert captured["creationflags"] == 0, captured
        # 转写落盘 产出/dsh/
        assert r.get("transcript") and os.path.exists(r["transcript"]), r
        assert os.sep + "产出" in r["transcript"] and "dsh" in r["transcript"], r
        content = open(r["transcript"], encoding="utf-8").read()
        assert "修复失败用例" in content and "12/12 green" in content, content[:200]
    finally:
        u1()
        setattr(dsh_bridge, "subprocess", subprocess)
        dsh_bridge.subprocess.Popen = orig_popen
        setattr(dsh_bridge, "workspace_root", orig_root)
    print("PASS dsh_run：成功路径（shim 解析→node 直跑/env 注入/进程组/转写存档）")


def test_run_timeout():
    bin_path = "C:\\npm\\dsh.cmd"
    killed = []
    fake = _FakeProc(subprocess.TimeoutExpired("dsh", 60), b"", 0)
    orig_popen, orig_kill = subprocess.Popen, dsh_bridge._kill_tree

    def _fake_popen(cmd, **kw):
        return fake

    u1 = _patch_dsh_bin(bin_path)
    orig_root = dsh_bridge.workspace_root
    dsh_bridge.workspace_root = lambda: TMP
    dsh_bridge.subprocess.Popen = _fake_popen
    dsh_bridge._kill_tree = lambda p: killed.append(p.pid)
    try:
        r = dsh_bridge.dsh_run({"dsh": {}}, "长任务", timeout=61)
        assert not r["ok"] and r.get("timed_out"), r
        assert "超时" in r.get("error", ""), r
        assert killed == [4321], f"超时必须杀进程树（taskkill /T）：{killed}"
    finally:
        u1()
        setattr(dsh_bridge, "subprocess", subprocess)
        dsh_bridge.subprocess.Popen = orig_popen
        setattr(dsh_bridge, "_kill_tree", orig_kill)
        setattr(dsh_bridge, "workspace_root", orig_root)
    print("PASS dsh_run：超时 → 进程树强杀 + 结构化超时错误")


def test_run_exit1():
    bin_path = "C:\\npm\\dsh.cmd"
    fake = _FakeProc(b"", b"dsh: auth_error: invalid api key\nretry exhausted", 1)
    orig_popen = subprocess.Popen

    def _fake_popen(cmd, **kw):
        return fake

    u1 = _patch_dsh_bin(bin_path)
    orig_root = dsh_bridge.workspace_root
    dsh_bridge.workspace_root = lambda: TMP
    dsh_bridge.subprocess.Popen = _fake_popen
    try:
        r = dsh_bridge.dsh_run({"dsh": {}}, "task")
        assert not r["ok"] and r.get("exit_code") == 1, r
        assert "invalid api key" in r.get("error", ""), r
        assert r.get("hint") and "dsh_status" in r["hint"], r
    finally:
        u1()
        setattr(dsh_bridge, "subprocess", subprocess)
        dsh_bridge.subprocess.Popen = orig_popen
        setattr(dsh_bridge, "workspace_root", orig_root)
    print("PASS dsh_run：退出码 1 → 错误含 stderr 尾部 + 诊断 hint")


def test_non_win32_degrade():
    orig = dsh_bridge.IS_WIN
    dsh_bridge.IS_WIN = False
    try:
        r1 = dsh_bridge.dsh_check({})
        r2 = dsh_bridge.dsh_prepare({})
        r3 = dsh_bridge.dsh_run({}, "task")
        for r in (r1, r2, r3):
            assert is_err(r) and "仅支持 Windows" in err_msg(r), r
    finally:
        setattr(dsh_bridge, "IS_WIN", orig)
    print("PASS 非 win32：三个入口全部优雅降级")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    _cleanup()
    print(f"\n{'=' * 50}\n{len(fns) - failed}/{len(fns)} 通过"
          + (" —— 有失败用例！" if failed else " —— 全绿 ✓"))
    sys.exit(1 if failed else 0)

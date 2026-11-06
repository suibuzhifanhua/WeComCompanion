# -*- coding: utf-8 -*-
"""test_rebuild — 软件内一键打包并重启（v1.0.373）回归。

背景：自我修复/版本管理页新增「📦 打包并重启」。软件无法在运行中替换自身 exe，
由延迟批处理接管：等 2s → 杀进程 → build.py --force --note 打包 → start 新 exe。
--force 跳过对话活动检测（进程已死，保护无意义；人工打包不带 --force 仍受保护）。
v1.0.373 补：/repair/rebuild 前置 _source_has_changes 检测——源码相对当前 exe
构建 hash 无未打包改动（无新提交且工作树干净，config.json 运行时噪声除外）→
拒绝空转打包；有新提交（自我修复落地）/未提交改动 → 放行。

用例：
1. _rebuild_script：note 的 cmd 元字符（引号/%/换行/管道/&/<>/^/!）被过滤，路径正确嵌入
2. POST /api/repair/rebuild 开发模式 → 400（frozen 守卫；防开发进程误自杀）
3. _source_has_changes：HEAD==登记hash+干净 → 无改动；改文件 → 有；只改 config.json → 无
   （运行时噪声过滤）；HEAD 前进 → 有；versions.json 缺失 → 保守放行
"""
import os
import shutil as _shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.result import is_err, err_msg  # noqa: E402

from routes.management import _rebuild_script, _source_has_changes  # noqa: E402


def _git_exe():
    import shutil
    gi = shutil.which("git")
    if gi:
        return gi
    cand = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "PortableGit",
                        "versions", "1.2.0", "cmd", "git.exe")
    return cand if os.path.isfile(cand) else None


def _mk_repo(prefix="wec_rebuild_"):
    """建临时 git 仓库 + versions.json 登记 HEAD；返回 (src_root, head_hash)。
    .gitignore 与真实项目对齐（产物库/dist/build 忽略）——否则 versions.json
    未跟踪状态会污染 porcelain 判定。"""
    root = tempfile.mkdtemp(prefix=prefix)
    gi = _git_exe()
    assert gi, "本机无 git，无法跑该用例"
    def g(*a):
        subprocess.run([gi, "-C", root] + list(a), capture_output=True, timeout=60, check=True)
    os.makedirs(os.path.join(root, "产物库", "版本"))
    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as f:
        f.write("dist/\nbuild/\n产物库/\n__pycache__/\n*.pyc\n")
    with open(os.path.join(root, "code.py"), "w", encoding="utf-8") as f:
        f.write("print('v1')\n")
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
        f.write("{}\n")
    g("init", "-q")
    g("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    head = subprocess.run([gi, "-C", root, "rev-parse", "--short", "HEAD"],
                          capture_output=True, timeout=60).stdout.decode().strip()
    import json
    with open(os.path.join(root, "产物库", "版本", "versions.json"), "w", encoding="utf-8") as f:
        json.dump([{"version": "1.0.100", "hash": head}], f)
    return root, head


from routes.management import _rebuild_script  # noqa: E402


def test_rebuild_script_escapes_note():
    s = _rebuild_script(r"C:\src", r"C:\py\python.exe", r"C:\dist\WeComCompanion.exe",
                        '修复"引号" & 竖线|换行\n百分号% 脱字^ 感叹! <tag>')
    lines = s.split("\r\n")
    assert lines[0] == "@echo off"
    note_line = [l for l in lines if "build.py" in l][0]
    for bad in ('"', "%", "\n", "|", "&", "<", ">", "^", "!"):
        seg = note_line.split('--note="', 1)[1].rstrip('"')
        assert bad not in seg, (bad, note_line)
    assert r'"C:\py\python.exe" build.py --yes --force --note=' in note_line, note_line
    assert lines[-1] == 'start "" "C:\\dist\\WeComCompanion.exe"'
    print("PASS 批处理 note 元字符过滤 + --force + 启动行正确")


def test_rebuild_endpoint_guard_dev_mode():
    """测试进程非 frozen → 400 拒绝（防开发进程被误杀）。"""
    from flask import Flask
    from routes.management import bp as management_bp
    app = Flask(__name__)
    app.register_blueprint(management_bp)
    c = app.test_client()
    r = c.post("/api/repair/rebuild", json={"note": "x"})
    assert r.status_code == 400, r.status_code
    d = r.get_json()
    assert d.get("ok") is False and "打包版" in (d.get("error") or ""), d
    print("PASS POST /api/repair/rebuild 开发模式 400 守卫")


def test_source_has_changes_clean():
    """HEAD==登记hash 且工作树干净 → 无改动（拒绝空转打包的核心判定）。"""
    root, _h = _mk_repo()
    changed, why = _source_has_changes(root)
    assert changed is False, (changed, why)
    assert "一致" in why, why
    print("PASS 源码与运行版本一致 → 拒绝打包")


def test_source_has_changes_dirty_and_commit():
    root, head = _mk_repo()
    gi = _git_exe()
    # 未提交的代码改动 → 有改动
    with open(os.path.join(root, "code.py"), "w", encoding="utf-8") as f:
        f.write("print('v2')\n")
    changed, why = _source_has_changes(root)
    assert changed is True and "未提交" in why, (changed, why)
    # 提交后 HEAD 前进 → 有改动（新提交未打包）
    g = lambda *a: subprocess.run([gi, "-C", root, "-c", "user.name=t", "-c", "user.email=t@t"]
                                  + list(a), capture_output=True, timeout=60, check=True)
    g("add", "-A")
    g("commit", "-q", "-m", "v2")
    changed, why = _source_has_changes(root)
    assert changed is True and "新提交" in why, (changed, why)
    print("PASS 未提交改动/新提交 → 放行打包")


def test_source_has_changes_config_noise_filtered():
    """只改 config.json（运行时配置，不进包体）→ 不视为代码改动。"""
    root, _h = _mk_repo()
    with open(os.path.join(root, "config.json"), "a", encoding="utf-8") as f:
        f.write('{"x": 1}\n')
    changed, why = _source_has_changes(root)
    assert changed is False, (changed, why)
    print("PASS config.json 运行时噪声被过滤")


def test_source_has_changes_missing_record():
    """versions.json 缺失 → 保守放行（宁可多打不误拦）。"""
    root, _h = _mk_repo()
    os.remove(os.path.join(root, "产物库", "版本", "versions.json"))
    changed, why = _source_has_changes(root)
    assert changed is True, (changed, why)
    print("PASS 版本记录缺失 → 保守放行")


def test_ask_user_tool():
    """ask_user 工具：参数校验 + options 清洗（空串剔除/截断 60 字/上限 6 个）。"""
    from agent_tools.executors import _run_ask_user
    r = _run_ask_user({"question": "什么风格", "options": ["A", "  ", "B", "C" * 100]}, {})
    assert r.get("__ask_user__") is True, r
    assert r["options"] == ["A", "B", "C" * 60], r["options"]
    assert r["allow_text"] is True
    assert is_err(_run_ask_user({}, {})), "缺 question 应报错"
    r2 = _run_ask_user({"question": "q", "options": "bad"}, {})
    assert r2.get("__ask_user__") is True and r2["options"] == [], r2
    assert _run_ask_user({"question": "q", "allow_text": False}, {})["allow_text"] is False
    print("PASS ask_user 参数校验与 options 清洗")


def test_admin_restart_guard_dev_mode():
    """管理员重启：开发模式 400；status 永远可用（is_admin 布尔）。"""
    from flask import Flask
    from routes.management import bp as management_bp
    app = Flask(__name__)
    app.register_blueprint(management_bp)
    c = app.test_client()
    r = c.post("/api/admin/restart", json={})
    assert r.status_code == 400 and "打包版" in (r.get_json().get("error") or ""), r.get_json()
    d = c.get("/api/admin/status").get_json()
    assert d.get("ok") is True and isinstance(d.get("is_admin"), bool), d
    print("PASS /api/admin/restart 开发模式 400 / status 字段齐全")


def test_wecom_tools_filtered_when_unbound(monkeypatch):
    """v1.0.373（用户需求2）：企微未绑定 → 企微工具全部不注入（纯 agent 模式）。"""
    from agent_tools.dispatch import build_agent_tools, _wecom_ok_cache
    monkeypatch.setitem(_wecom_ok_cache, "v", False)
    monkeypatch.setitem(_wecom_ok_cache, "ts", float("inf"))
    cfg = {"enabled_skills": None, "skill_overrides": {}, "custom_skills": [],
           "connectors": {}, "models": [], "feature_flags": {"skills": True},
           "wecom": {}}
    tools, _ = build_agent_tools(cfg)
    names = {t.get("function", {}).get("name") for t in tools}
    wecom_names = [n for n in names if "wecom" in (n or "")]
    assert not wecom_names, wecom_names
    assert "finish" in names, "基础工具应保留"
    print("PASS 企微未绑定 → 企微工具全部过滤（纯 agent 模式）")


def test_wecom_tools_injected_when_bound(monkeypatch):
    """v1.0.373：绑定（缓存为真）→ 企微工具正常注入。"""
    from agent_tools.dispatch import build_agent_tools, _wecom_ok_cache
    monkeypatch.setitem(_wecom_ok_cache, "v", True)
    monkeypatch.setitem(_wecom_ok_cache, "ts", float("inf"))
    cfg = {"enabled_skills": None, "skill_overrides": {}, "custom_skills": [],
           "connectors": {}, "models": [], "feature_flags": {"skills": True},
           "wecom": {}}
    tools, _ = build_agent_tools(cfg)
    names = {t.get("function", {}).get("name") for t in tools}
    assert any("wecom" in (n or "") for n in names), names
    print("PASS 企微绑定 → 企微工具正常注入")


class _FakeFn:
    """可调用包装：允许 setattr(argtypes/restype)——真 ctypes 函数指针可设属性，
    普通 bound method 不行。"""
    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None
    def __call__(self, *a):
        return self._fn(*a)


class _FakeK32:
    """kernel32 假实现：前 fail_times 次 CreateMutexW 返回指定错误码 err，之后成功。"""
    def __init__(self, fail_times=1, err=183):
        self.n = 0
        self.fail_times = fail_times
        self.err = err
        self.CreateMutexW = _FakeFn(self._create)
    def _create(self, a, b, name):
        self.n += 1
        import ctypes as _ct
        _ct.set_last_error(self.err if self.n <= self.fail_times else 0)
        return 0x1234


def test_single_instance_admin_wait():
    """v1.0.373 修复回归：--admin 场景 mutex 被旧实例持有（1.2s 退出窗口）→
    轮询等待拿到锁（wait_grace），而不是立即失败——原 bug：新旧实例全部消失。"""
    from core_utils import acquire_single_instance_mutex
    k32 = _FakeK32(fail_times=1, err=183)
    sleeps = []
    ok, handle = acquire_single_instance_mutex("X", wait_grace=True,
                                               k32=k32, sleep_fn=sleeps.append)
    assert ok is True and handle == 0x1234, (ok, handle)
    assert k32.n == 2 and sleeps, (k32.n, sleeps)   # 等待过一次再拿锁
    print("PASS --admin 场景 mutex 占用 → 轮询等待后获取成功")


def test_single_instance_normal_no_wait():
    """普通启动（wait_grace=False）mutex 被持有 → 立即失败（不等待，原行为保持）。"""
    from core_utils import acquire_single_instance_mutex
    k32 = _FakeK32(fail_times=1, err=183)
    ok, handle = acquire_single_instance_mutex("X", wait_grace=False, k32=k32)
    assert ok is False and handle is None and k32.n == 1, (ok, handle, k32.n)
    print("PASS 普通启动 mutex 占用 → 立即失败不等待")


def test_single_instance_access_denied():
    """v1.0.373 关键修复：普通双击遇管理员实例运行时，CreateMutexW 被拒
    （ERROR_ACCESS_DENIED=5）——此前误判 err=5 为"拿到锁"→ 双实例双图标。
    现在 err=5 与 err=183 一样视为"已有实例在跑"。"""
    from core_utils import acquire_single_instance_mutex
    k32 = _FakeK32(fail_times=99, err=5)
    ok, handle = acquire_single_instance_mutex("X", wait_grace=False, k32=k32)
    assert ok is False and handle is None and k32.n == 1, (ok, handle, k32.n)
    print("PASS 管理员实例持锁时普通双击（err=5）→ 正确判定已有实例不双开")

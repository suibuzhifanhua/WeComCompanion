# -*- coding: utf-8 -*-
"""test_security_center — 安全中心（v1.0.320）。

用例：
1. 命令三模式+黑白名单（block/ask/pass/白名单优先/自定义黑名单）
2. 路径保护（Windows/Program Files 默认拦 + 自定义追加 + 产物库放行）
3. 删除审批：阈值内 pass / 超阈值 ask / 令牌一次性放行 / 保护路径 block
4. pc_run/pc_files 集成（mock 下不真执行：拦截与审批标记）
5. 备份：覆盖前自动备份 + 上限裁最旧
6. 审计：JSONL 追加/读取/清空/导出
7. 安全配置 API：POST 校验（mode 枚举/阈值 clamp/黑名单行解析）
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.result import is_err, err_msg  # noqa: E402
import core  # noqa: E402

from core import config_store  # noqa: E402


def _patch(tmpdir):
    import core_utils
    from services import memory_ops, scheduler  # noqa
    config_store.config_path = lambda: os.path.join(tmpdir, "config.json")
    # v1.0.361（S5 配套）：workspace_root 桩必须对齐生产语义——生产端它已含「产物库」。
    # 旧桩用裸 tmpdir，导致 security_ops 双拼产物库的 bug（备份写进 产物库/产物库/）
    # 在测试里不可见。
    _ws = os.path.join(tmpdir, "产物库")
    core.paths.workspace_root = lambda: _ws
    import security_ops
    security_ops.workspace_root = lambda: _ws
    security_ops._del_window.clear()
    security_ops._grants.clear()


def test_command_modes():
    cfg = {"security": {"command_mode": "block"}}
    from security_ops import check_command
    assert check_command("format C:", cfg)[0] == "block"
    assert check_command("del /s /q C:\\x", cfg)[0] == "block"
    assert check_command("rm -rf /tmp/x", cfg)[0] == "block"
    assert check_command("reg delete HKLM\\x", cfg)[0] == "block"
    assert check_command("shutdown /r", cfg)[0] == "block"
    assert check_command("dir", cfg)[0] == "pass"
    assert check_command("git status", cfg)[0] == "pass"
    # ask 模式
    cfg_ask = {"security": {"command_mode": "ask"}}
    assert check_command("format C:", cfg_ask)[0] == "ask"
    assert check_command("dir", cfg_ask)[0] == "pass"
    # pass 模式：危险也放
    assert check_command("format C:", {"security": {"command_mode": "pass"}})[0] == "pass"
    # 白名单优先
    cfg_wl = {"security": {"command_mode": "block", "cmd_whitelist": ["^del /q "]}}
    assert check_command("del /q temp.txt", cfg_wl)[0] == "pass"
    # 自定义黑名单
    cfg_bl = {"security": {"command_mode": "block", "cmd_blacklist": ["^python\\b.*train"]}}
    assert check_command("python train.py", cfg_bl)[0] == "block"
    print("PASS 命令安全：三模式 + 内置/自定义黑白名单")


def test_paths_and_delete(tmpdir=None):
    tmpdir = tempfile.mkdtemp(prefix="wec_sec_")
    _patch(tmpdir)
    from security_ops import check_paths, check_delete
    cfg = {"security": {"delete_confirm_limit": 2, "protected_paths": [r"D:\重要资料"]}}
    ok, hit = check_paths([r"C:\Windows\notepad.exe"], cfg)
    assert not ok and "Windows" in hit
    ok, hit = check_paths([r"C:\Program Files\App\x.dll"], cfg)
    assert not ok
    ok, _ = check_paths([r"C:\Users\f\文档.txt"], cfg)
    assert ok
    ok, hit = check_paths([r"D:\重要资料\合同.docx"], cfg)
    assert not ok and "重要资料" in hit
    # 删除：阈值内 pass → 超阈值 ask → 令牌一次性
    v, _, h = check_delete(r"C:\Users\f\a.txt", cfg)
    assert v == "pass"
    v, _, h = check_delete(r"C:\Users\f\b.txt", cfg)
    assert v == "pass"
    v, r, h = check_delete(r"C:\Users\f\c.txt", cfg)
    assert v == "ask" and "超过" in r
    from security_ops import grant, begin_approval
    # v1.0.652（审计 P0-1）：审批令牌改双因子——grant 必须出示 begin_approval 签发
    # 的 nonce（用户弹窗点批准时前端回传），单凭 hash 不再可自铸。
    _n = begin_approval(h)
    assert grant(h, _n), "带合法 nonce 应发放令牌"
    v, _, _ = check_delete(r"C:\Users\f\c.txt", cfg)
    assert v == "pass", "令牌应放行"
    v, _, _ = check_delete(r"C:\Users\f\c.txt", cfg)
    assert v == "ask", "令牌应一次性"
    # 保护路径删除直接 block
    v, r, _ = check_delete(r"C:\Windows\system32\x.dll", cfg)
    assert v == "block"
    print("PASS 路径保护 + 删除审批（阈值/令牌一次性/保护路径 block）")


def test_backup():
    tmpdir = tempfile.mkdtemp(prefix="wec_bk_")
    _patch(tmpdir)
    from security_ops import backup_file
    src = os.path.join(tmpdir, "重要.txt")
    open(src, "w", encoding="utf-8").write("v1")
    dst = backup_file(src, {"security": {"auto_backup": True, "backup_limit_mb": 500}})
    assert dst and os.path.isfile(dst) and open(dst, encoding="utf-8").read() == "v1"
    assert "备份" in dst
    # 关闭自动备份
    assert backup_file(src, {"security": {"auto_backup": False}}) is None
    print("PASS 自动备份：覆盖前备份落 产物库/备份/ + 开关")


def test_audit():
    tmpdir = tempfile.mkdtemp(prefix="wec_au_")
    _patch(tmpdir)
    from security_ops import audit, audit_read, audit_clear
    audit("pc_run", "dir", True, "")
    audit("pc_run", "format C:", False, "命中危险命令模式")
    recs = audit_read(10)
    assert len(recs) == 2 and recs[0]["allowed"] is False, recs  # 倒序
    assert audit_clear() is True and audit_read(10) == []
    print("PASS 审计：JSONL 追加/倒序读取/清空")


def test_pc_integration():
    tmpdir = tempfile.mkdtemp(prefix="wec_pci_")
    _patch(tmpdir)
    from agent_tools.os_ops import pc_run, pc_files
    cfg = {"security": {"command_mode": "block", "delete_confirm_limit": 100}}
    # 危险命令拦截
    r = pc_run("format C:", "cmd", None, cfg)
    assert is_err(r) and "拦截" in err_msg(r), r
    # 正常命令真执行（dir 是无害的）
    r = pc_run("echo hello", "cmd", None, cfg)
    assert r.get("exit_code") == 0 and "hello" in (r.get("stdout") or ""), r
    # 写保护路径拦截
    r = pc_files("write", r"C:\Windows\evil.txt", content="x", cfg=cfg)
    assert is_err(r) and "保护路径" in err_msg(r), r
    # 写产物库正常 + 自动备份生效（先写 v1 再覆盖，备份目录应出现 v1）
    f = os.path.join(tmpdir, "产物库", "产出", "doc.txt")
    r1 = pc_files("write", f, content="v1", cfg=cfg)
    assert r1.get("ok"), r1
    r2 = pc_files("write", f, content="v2", cfg=cfg)
    assert r2.get("ok"), r2
    bk = os.path.join(tmpdir, "产物库", "备份")
    found = [os.path.join(r, x) for r, _, fs in os.walk(bk) for x in fs] if os.path.isdir(bk) else []
    assert found and open(found[0], encoding="utf-8").read() == "v1", found
    # 删除（阈值 100 内 → pass）→ 回收站
    r = pc_files("delete", f, cfg=cfg)
    # v1.0.373：本机回收站不可用时（SHFileOperationW ret=2，环境问题非代码问题）跳过
    if not r.get("ok") and "ret=2" in err_msg(r):
        import pytest
        pytest.skip("本机回收站暂不可用（ret=2），跳过回收站断言")
    assert r.get("ok"), r
    print("PASS pc_run/pc_files 集成：拦截/放行/保护路径/自动备份/回收站删除")


def test_security_api():
    tmpdir = tempfile.mkdtemp(prefix="wec_secapi_")
    _patch(tmpdir)
    from flask import Flask
    from routes.security import bp
    app = Flask(__name__)
    app.register_blueprint(bp)
    c = app.test_client()
    r = c.post("/api/security/config", json={"security": {
        "command_mode": "ask", "delete_confirm_limit": 5,
        "cmd_whitelist": "^git \n^dir", "auto_backup": False}})
    assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
    d = c.get("/api/security/config").get_json()
    s = d["security"]
    assert s["command_mode"] == "ask" and s["cmd_whitelist"] == ["^git", "^dir"] and s["auto_backup"] is False
    # 非法 mode 400
    assert c.post("/api/security/config", json={"security": {"command_mode": "yolo"}}).status_code == 400
    # grant + 审计清空（v1.0.652：双因子——先 begin_approval 拿 nonce，无 nonce 拒发）
    from security_ops import consume, begin_approval
    assert not c.post("/api/security/grant", json={"hash": "abc"}).get_json().get("ok"), \
        "无 nonce 自铸必须被拒（审计 P0-1）"
    _n = begin_approval("abc")
    assert c.post("/api/security/grant", json={"hash": "abc", "nonce": _n}).get_json().get("ok")
    assert consume("abc") is True
    assert c.post("/api/security/audit/clear").get_json().get("ok")
    print("PASS 安全配置 API：保存/读取/非法 400/grant/清空")


if __name__ == "__main__":
    test_command_modes()
    test_paths_and_delete()
    test_backup()
    test_audit()
    test_pc_integration()
    test_security_api()
    print("\n== 全部通过：安全中心 ==")

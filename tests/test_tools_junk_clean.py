# -*- coding: utf-8 -*-
"""tools/junk_clean.py 的行为锁。

这个工具会删文件，所以它自己必须被测清楚：
① 默认（不带 --apply）一个字节都不动；
② 白名单命中才删，**黑名单里的哪怕长得像垃圾也不删**（`dist/**/__pycache__`、
   `build/_rollback/`、`产物库/`——这些是运行数据与无 git 时唯一的回滚手段）；
③ 日志只有显式 --include-logs 才进计划（本项目靠 `_pack_*.log` 里的 `[build] done ->`
   取证"后台打包到底成没成"，不能顺手清掉）。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.junk_clean as jc   # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """造一棵假项目树 + 假 TEMP，绝不碰真机目录。"""
    root = tmp_path / "proj"

    def mk(rel, content=b"x"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    mk("pkg/__pycache__/a.pyc")
    mk("dist/pkg/__pycache__/b.pyc")            # 黑名单：运行数据里的缓存也不碰
    mk("dist/pkg/mod.py")
    mk("build/_ui_verify/profile.bin", b"y" * 2048)
    mk("build/_cv_chrome/lockfile")
    mk("build/_rollback/WeComCompanion_1.0.618.exe", b"z" * 4096)
    mk("build/_dist_backup/产物库/日志/keep.log")
    mk("build/_chat_archive/会话/keep.json")
    mk("build/_shot_9_components.png", b"p" * 1024)
    mk("build/_pack_600.log", b"L" * 512)
    mk("产物库/日志/c.log", b"L" * 128)
    mk("docs/报告.md")

    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir()
    (fake_temp / "wc_fake_profile").mkdir()
    (fake_temp / "wc_fake_profile" / "state.js").write_text("s", encoding="utf-8")
    # 测试泄漏的合成工作区（真机上本机攒到 3390 个）：也必须被清掉
    (fake_temp / "wec_cfg_dsh_abc123").mkdir()
    (fake_temp / "wec_cfg_dsh_abc123" / "config.json").write_text("{}", encoding="utf-8")
    (fake_temp / "keepme").mkdir()              # 不匹配白名单，不该被动
    monkeypatch.setattr(jc, "TEMP", str(fake_temp))
    return root, fake_temp


def _exists(root, rel):
    return os.path.exists(str(root / rel))


def test_dry_run_touches_nothing(sandbox, capsys):
    root, fake_temp = sandbox
    rc = jc.main(["--root", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY-RUN" in out
    assert _exists(root, "pkg/__pycache__/a.pyc"), "预览模式竟然删了文件"
    assert _exists(root, "build/_ui_verify/profile.bin")
    assert _exists(fake_temp, "wc_fake_profile/state.js")
    assert "未删除任何文件" in out


def test_apply_removes_whitelist_only(sandbox):
    root, fake_temp = sandbox
    jc.main(["--root", str(root), "--apply"])
    # 白名单：项目内缓存 / Chrome profile / 截图 / 假 TEMP 里的台子 profile
    assert not _exists(root, "pkg/__pycache__")
    assert not _exists(root, "build/_ui_verify")
    assert not _exists(root, "build/_cv_chrome")
    assert not _exists(root, "build/_shot_9_components.png")
    assert not _exists(fake_temp, "wc_fake_profile")
    assert not _exists(fake_temp, "wec_cfg_dsh_abc123"), "测试泄漏的合成工作区没被清"
    # 黑名单：一个字都不该少
    assert _exists(root, "dist/pkg/__pycache__/b.pyc"), "误删 dist 里的运行数据"
    assert _exists(root, "dist/pkg/mod.py")
    assert _exists(root, "build/_rollback/WeComCompanion_1.0.618.exe"), "误删唯一回滚手段"
    assert _exists(root, "build/_dist_backup/产物库/日志/keep.log"), "误删打包还原源"
    assert _exists(root, "build/_chat_archive/会话/keep.json"), "误删归档对话"
    assert _exists(root, "产物库/日志/c.log"), "误删产物库"
    assert _exists(root, "docs/报告.md")
    assert _exists(fake_temp, "keepme")
    # 日志默认不删
    assert _exists(root, "build/_pack_600.log"), "--include-logs 没开却删了取证日志"


def test_include_logs_is_opt_in(sandbox):
    root, _ft = sandbox
    jc.main(["--root", str(root), "--apply", "--include-logs"])
    assert not _exists(root, "build/_pack_600.log"), "显式要求删日志却没删"
    assert _exists(root, "产物库/日志/c.log"), "顺手把产物库日志也删了"


def test_forbidden_list_is_the_real_guard():
    """黑名单本身不许被削弱——这五项没了就等于把运行数据/回滚手段暴露给删除。"""
    for must in ("dist", "产物库", "docs", "build/_rollback", "build/_dist_backup",
                 "build/_chat_archive"):
        assert must in jc.FORBIDDEN_SEGMENTS, "FORBIDDEN_SEGMENTS 少了 %s" % must
    assert jc.forbidden("build/_rollback/app.exe")
    assert jc.forbidden("dist/产物库/组件")
    assert not jc.forbidden("pkg/__pycache__")


def test_no_temp_flag_leaves_machine_temp_alone(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    ft = tmp_path / "t2"
    ft.mkdir()
    (ft / "wc_keep").mkdir(parents=True)
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"x")
    monkeypatch.setattr(jc, "TEMP", str(ft))
    jc.main(["--root", str(root), "--apply", "--no-temp"])
    assert not _exists(root, "pkg/__pycache")
    assert _exists(ft, "wc_keep"), "--no-temp 还去动了 TEMP"

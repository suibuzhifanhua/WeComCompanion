# -*- coding: utf-8 -*-
"""tests/test_safe_open.py — core/safe_open 收口的行为级锁（v1.0.646）。

要锁的不是一个函数，是"os.startfile 这类会执行的动作不再散落各处"这件事：
os.startfile 交给 Shell 解析 = 关联程序真跑起来。README v1.0.641 记录过一次
事故（api_token.txt 借桌面路径被放开），修法是就地补一条 if，判据仍然三份三处漂移。

三条必须同时成立：
  ① 危险扩展名打不开（不是"记一条日志就算了"）；
  ② 越出允许根的路径打不开（防"合法前缀放行可疑文件"那次的事故形态）；
  ③ 真的打不开时**绝不能 startfile**（这是本测试存在的全部理由）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import safe_open as S


def _stub_start(monkeypatch):
    """把 os.startfile 换成探针：测试期间它一旦被调用就是失败。"""
    calls = []

    def _boom(p):
        calls.append(p)
        raise AssertionError(f"不该调用 os.startfile：{p}")

    monkeypatch.setattr(S.os, "startfile", _boom)
    return calls


def test_executable_extensions_are_refused(monkeypatch, tmp_path):
    calls = _stub_start(monkeypatch)
    for fn in ("x.exe", "x.bat", "x.ps1", "x.lnk", "x.scr", "x.pyw", "x.js"):
        p = tmp_path / fn
        p.write_text("whatever", encoding="utf-8")
        ok, err = S.open_path(str(p), roots=[str(tmp_path)])
        assert ok is False, f"{fn} 被放行了 —— startfile 会把它执行起来"
        assert err
    assert not calls, f"拒绝路径下仍触发了 startfile：{calls}"


def test_plain_document_still_opens(monkeypatch, tmp_path):
    from unittest import mock

    p = tmp_path / "report.pdf"
    p.write_text("%PDF-1.4", encoding="utf-8")
    with mock.patch.object(S.os, "startfile", lambda x: None):
        ok, _err = S.open_path(str(p), roots=[str(tmp_path)])
    assert ok is True, "正常交付文档被误拦（功能回归）"


def test_path_outside_allowed_roots_is_refused(monkeypatch, tmp_path):
    """②历史事故形态：目标不在允许根内（哪怕扩展名无害）一律拒绝。"""
    calls = _stub_start(monkeypatch)
    inside = tmp_path / "in"
    outside = tmp_path / "out"
    for d in (inside, outside):
        d.mkdir()
    f = outside / "secret.txt"
    f.write_text("token", encoding="utf-8")
    ok, err = S.open_path(str(f), roots=[str(inside)])
    assert ok is False, "允许根之外的路径被放开了（凭据可借合法前缀被打开）"
    assert not calls


def test_missing_target_is_refused(monkeypatch, tmp_path):
    calls = _stub_start(monkeypatch)
    ok, _ = S.open_path(str(tmp_path / "nope.txt"), roots=[str(tmp_path)])
    assert ok is False
    assert not calls


def test_directory_opening_is_controllable(monkeypatch, tmp_path):
    """目录要不要能开由调用方声明：知识库入口要，跳文件入口的默认语义是选中。"""
    calls = _stub_start(monkeypatch)
    ok, _ = S.open_path(str(tmp_path), roots=[str(tmp_path)], allow_dirs=False)
    assert ok is False
    assert not calls
    from unittest import mock

    with mock.patch.object(S.os, "startfile", lambda x: None):
        assert S.open_path(str(tmp_path), roots=[str(tmp_path)], allow_dirs=True)[0] is True


def test_open_url_rejects_non_http_and_remote_hosts(monkeypatch):
    calls = _stub_start(monkeypatch)
    for bad in ("file:///C:/Windows/System32/calc.exe", "javascript:alert(1)",
                "\\\\server\\share\\x.exe", "http://evil.tld/x", ""):
        ok, _ = S.open_url(bad)
        assert ok is False, f"危险地址被放行：{bad!r}"
    assert not calls


def test_open_url_allows_local_host(monkeypatch):
    from unittest import mock

    with mock.patch.object(S.os, "startfile", lambda x: None):
        assert S.open_url("http://127.0.0.1:8787/")[0] is True
        assert S.open_url("http://localhost:8787/")[0] is True


def test_no_bare_startfile_left_in_routes():
    """③收口验收：routes/ 里不允许再有裸 os.startfile（新增入口必须过 safe_open）。"""
    import io as _io

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for _r, _ds, _fs in os.walk(os.path.join(root, "routes")):
        if "__pycache__" in _r:
            continue
        for fn in _fs:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(_r, fn)
            txt = _io.open(p, encoding="utf-8", errors="replace").read()
            for i, line in enumerate(txt.splitlines(), 1):
                if "os.startfile(" in line and "# noqa: S606  系统默认浏览器" not in line:
                    hits.append(f"{os.path.relpath(p, root)}:{i}")
    assert not hits, f"routes/ 仍有裸 os.startfile（判据会重新漂移）：{hits}"

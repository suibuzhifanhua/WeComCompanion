# -*- coding: utf-8 -*-
"""test_autostart — 开机自启接口（v1.0.369）回归。

背景：设置页新增「开机自动启动」开关。实现走 HKCU\\...\\Run 注册表
（键名 WeComCompanion，值为带引号的 exe 路径），仅打包版（sys.frozen）支持；
开发模式 GET 返回 supported=False，POST 显式 400（注册 python.exe 无意义）。

用例：
1. GET /api/autostart 返回 ok/supported/enabled 三字段，开发进程下 supported=False
2. POST enabled=true（开发模式）→ 400 + ok=False，错误信息含"打包版"
3. _autostart_status 在非 frozen 环境恒 (False, False)，不触注册表
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.management import _autostart_status, _parse_autostart_value  # noqa: E402


def test_parse_autostart_value():
    """v1.0.373：注册表值解析 → (enabled, hide_main)。--minimized 参数开关隐藏窗口。"""
    assert _parse_autostart_value('"C:\\a\\WeComCompanion.exe"') == (True, False)
    assert _parse_autostart_value('"C:\\a\\WeComCompanion.exe" --minimized') == (True, True)
    assert _parse_autostart_value("") == (False, False)
    assert _parse_autostart_value(None) == (False, False)
    print("PASS _parse_autostart_value 解析 enabled/hide_main")


def _make_client():
    from flask import Flask
    from routes.management import bp as management_bp
    app = Flask(__name__)
    app.register_blueprint(management_bp)
    return app.test_client()


def test_autostart_status_dev_mode():
    """测试进程非 frozen（未打包）→ 恒 (False, False, False)，不读注册表。"""
    supported, enabled, hide_main = _autostart_status()
    assert supported is False, "开发环境不应报告 supported=True"
    assert enabled is False and hide_main is False
    print("PASS _autostart_status 非 frozen 恒 (False, False, False)")


def test_autostart_get_shape():
    c = _make_client()
    r = c.get("/api/autostart")
    assert r.status_code == 200, r.status_code
    d = r.get_json()
    assert d.get("ok") is True, d
    assert "supported" in d and "enabled" in d and "hide_main" in d, d
    assert d["supported"] is False, "测试进程未打包，supported 必须为 False"
    print("PASS GET /api/autostart 字段齐全且 supported=False")


def test_autostart_post_guard_dev_mode():
    """开发模式 POST 开启 → 400 拒绝（防止把 python.exe 写进 Run 键）。"""
    c = _make_client()
    r = c.post("/api/autostart", json={"enabled": True})
    assert r.status_code == 400, r.status_code
    d = r.get_json()
    assert d.get("ok") is False, d
    assert "打包版" in (d.get("error") or ""), d
    # 关闭方向在开发模式同样拒绝（写路径整体不可用）
    r2 = c.post("/api/autostart", json={"enabled": False})
    assert r2.status_code == 400, r2.status_code
    print("PASS POST /api/autostart 开发模式 400 守卫（开/关均拒）")

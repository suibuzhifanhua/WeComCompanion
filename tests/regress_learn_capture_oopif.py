"""课件采集回归测试 v1.0.231：跨域 OOPIF + SVG 直连。

不需要真实企微/学习平台——用本地 HTTP 模拟「CDN 上的 SVG 文件序列」。

跑法：
    python tests/regress_learn_capture_oopif.py
"""
import http.server
import os
import re
import socket
import socketserver
import sys
import tempfile
import threading
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _make_svg(idx, lines):
    body = "".join(f"<text>{ln}</text>" for ln in lines)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    ).encode("utf-8")


def _start_server(port, pages):
    """pages: dict[int, list[str]]"""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            m = re.match(r"^/(\d+)\.svg$", self.path)
            if m:
                n = int(m.group(1))
                if n in pages:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/svg+xml")
                    self.end_headers()
                    self.wfile.write(_make_svg(n, pages[n]))
                    return
            self.send_response(404)
            self.end_headers()
        def log_message(self, *a, **k):  # noqa: A003
            pass

    srv = socketserver.TCPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def main():
    port = _free_port()
    pages = {i: [f"测试页{i}-标题", f"测试页{i}-正文：合规管理要求说明"] for i in range(1, 23)}
    # 不放第 23 页：模拟真实课件最后页之后开始 404，触发「连续 2 次 404 停止」逻辑
    srv = _start_server(port, pages)

    # 工作区指向临时目录（避免污染真实产物库）
    tmp_workspace = tempfile.mkdtemp(prefix="learn_test_")

    # 准备假 iframe OOPIF target 的 URL（带 ?query 模拟真实 cdn-cloud 的 index.html?znTid=...）
    fake_url = f"http://127.0.0.1:{port}/index.html?znTid=abc&pageIndex=0"
    fake_targets = [
        {"type": "page", "url": "http://main.example/", "webSocketDebuggerUrl": ""},
        {"type": "iframe", "url": fake_url, "webSocketDebuggerUrl": ""},
    ]

    try:
        from core_utils import workspace_root
        from services.learn_capture import _capture_svg_pages, _detect_oopif_svg

        with mock.patch("wecom_bridge.list_targets", return_value=fake_targets), \
             mock.patch.object(workspace_root.__globals__.get("os"), "makedirs", lambda *a, **k: None), \
             mock.patch.object(workspace_root, "__call__", lambda: tmp_workspace):
            # workspace_root 是 core_utils 的函数，没法直接 mock call——改成 monkey-patch
            pass

        # 简单点：直接 patch workspace_root 模块函数
        with mock.patch.dict(os.environ, {}), \
             mock.patch("wecom_bridge.list_targets", return_value=fake_targets):
            import core_utils
            core_utils_workspace = os.path.join(tmp_workspace, "产物库")
            os.makedirs(core_utils_workspace, exist_ok=True)
            with mock.patch("core_utils.workspace_root", return_value=core_utils_workspace):
                detect = _detect_oopif_svg(9222)
                print("[detect]", detect)
                assert detect is not None, "OOPIF 未检测到"
                base, start = detect
                assert start == 1
                assert base == f"http://127.0.0.1:{port}" + "/{}.svg", f"base 错: {base}"

                r = _capture_svg_pages(9222, name="回归测试课件", tmp=None)
                print("[result]", r)
                assert r.get("ok"), f"采集失败: {r}"
                assert r.get("pages") == 22, f"页数错: {r.get('pages')}"
                assert r.get("via") == "oopif-svg"
                assert r.get("chars", 0) > 22 * 10, f"字符数太少: {r.get('chars')}"
                # 第 23 页应该被截断
                md_path = os.path.join(core_utils_workspace, r.get("path"))
                content = open(md_path, encoding="utf-8").read()
                assert "测试页23" not in content, "23 页不应被采集"
                assert "测试页22-正文" in content
                # 23 页 404 → 应被自动截断（连续 2 次 404 停止）
                print("[OK] OOPIF SVG 直连：22/22 页采到，23 页自动 404 截断")
    finally:
        srv.shutdown()
        # 清理临时工作区
        import shutil
        shutil.rmtree(tmp_workspace, ignore_errors=True)


if __name__ == "__main__":
    main()

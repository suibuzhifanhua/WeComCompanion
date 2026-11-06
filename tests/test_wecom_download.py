# -*- coding: utf-8 -*-
"""tests/test_wecom_download.py — 企微下载域回归锁合集（v1.0.646 按域归并）。

两个文件都守 `wecom_bridge/download.py`：一个是"下载能不能不弹框"（通道），
一个是"下载下来的文件叫什么"（名字）。同属一次下载动作的两端，拆成两个版本
编号的文件反而看不出它们共用同一条链路。

| 原文件                        | 版本出处  | 守护对象                       | 用例数 |
|-------------------------------|-----------|--------------------------------|--------|
| test_download_behavior.py     | v1.0.412  | set_download_behavior 三级通道 |   3    |
| test_v605_attach_name.py      | v1.0.605  | 附件文件名规范化与优先级       |   6    |

v1.0.646 归并时清理：原 test_download_behavior.py 里的 `_isolate()` 是一条从未被调用的
快照函数（真正防污染的是 monkeypatch 本身），随合并删除。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.result import is_err, err_msg  # noqa: E402

import wecom_bridge.download as dl   # noqa: E402
from wecom_bridge.download import _decode_name, _name_from_cd   # noqa: E402


# ============================================================ v1.0.412 下载通道
class TestDownloadBehaviorChannel:
    """免弹框下载：set_download_behavior 三级通道。

    背景：Browser.setDownloadBehavior 只在 browser target（/json/version 的
    webSocketDebuggerUrl）全局生效；原实现发到 page 端点等于没设 → 企微 Electron
    默认下载流程接管 → 弹「另存为」对话框挂住任务。修复后必须优先 browser 端点，
    失败逐级回退 page-browser / page-legacy。
    """

    def test_browser_channel_preferred(self, monkeypatch):
        """browser 端点可用 → 命令发到 browser ws，且不碰 page 通道。"""
        sent = []

        def fake_browser_ws(port=dl.DEFAULT_PORT):
            return "ws://127.0.0.1:9222/devtools/browser/guid"

        def fake_cdp_command(ws_url, method, params=None, timeout=None):
            sent.append((ws_url, method))
            return {"ok": True}

        monkeypatch.setattr(dl, "_browser_ws_url", fake_browser_ws)
        monkeypatch.setattr(dl, "_cdp_command", fake_cdp_command)
        r = dl.set_download_behavior(os.path.join(os.path.dirname(__file__), "_dl_test"), pick=0)
        assert r.get("ok") and r.get("via") == "browser", r
        assert sent and sent[0][0].endswith("/devtools/browser/guid"), sent
        assert sent[0][1] == "Browser.setDownloadBehavior", sent
        assert len(sent) == 1, f"browser 通道成功后不应再发 page 命令: {sent}"

    def test_fallback_chain(self, monkeypatch):
        """browser 不可用 → page-browser；再失败 → page-legacy；全失败 → __error__。"""
        calls = []

        def fake_browser_ws(port=dl.DEFAULT_PORT):
            return None   # browser 端点拿不到

        def fake_cdp_command(ws_url, method, params=None, timeout=None):
            calls.append(method)
            if method == "Page.setDownloadBehavior":
                return {"ok": True}
            return {"__error__": "not supported"}

        def fake_page_ws(pick, port=dl.DEFAULT_PORT):
            return "ws://page", {"id": "p"}

        monkeypatch.setattr(dl, "_browser_ws_url", fake_browser_ws)
        monkeypatch.setattr(dl, "_cdp_command", fake_cdp_command)
        monkeypatch.setattr(dl, "_page_ws", fake_page_ws)
        monkeypatch.setattr(dl, "_require_page", lambda *a, **k: (lambda t: (None, t[0], t[1]))(fake_page_ws(*a, **k)))   # v1.0.654 A6 配套
        r = dl.set_download_behavior(os.path.join(os.path.dirname(__file__), "_dl_test2"), pick=0)
        assert r.get("ok") and r.get("via") == "page-legacy", r
        assert "Browser.setDownloadBehavior" in calls and "Page.setDownloadBehavior" in calls

        # 全失败 → 报错且错误信息含三种通道
        calls.clear()

        def fake_cdp_fail(ws_url, method, params=None, timeout=None):
            calls.append(method)
            return {"__error__": "boom"}

        monkeypatch.setattr(dl, "_cdp_command", fake_cdp_fail)
        r2 = dl.set_download_behavior(os.path.join(os.path.dirname(__file__), "_dl_test3"), pick=0)
        assert is_err(r2) and "browser" in err_msg(r2) and "page-legacy" in err_msg(r2), r2

    def test_browser_ws_url_none_on_http_fail(self, monkeypatch):
        """/json/version 不可达 → None（调用方走 page 回退，不抛异常）。"""
        class _R:
            def raise_for_status(self): pass
            def json(self): return {"webSocketDebuggerUrl": "ws://x"}

        def fake_get(url, timeout):
            raise OSError("conn refused")

        monkeypatch.setattr(dl.requests, "get", fake_get)
        assert dl._browser_ws_url() is None


# ============================================================ v1.0.605 附件文件名
class TestAttachmentName:
    """附件文件名规范化与优先级（本地实测 build/_test_silent_download_local.py 挖出）。

    实测现象：OA 的下载接口回 `Content-Disposition: attachment; filename*=UTF-8''%E8%AE%BE…`
    百分号形式，旧实现只做 latin-1→utf-8 修复，于是文件落成 `%E8%AE%BE%E5%A4%87…xlsx`；
    同时 `page_fetch_to_disk` 里服务端名字**盖过**调用方显式传入的名字，
    把批审按正文抓到的名字丢了——「同名预删」和跨张的前缀模糊命中口径都跟着失准。
    """

    def test_percent_form_decodes_to_chinese(self):
        cd = "attachment; filename*=UTF-8''%E8%AE%BE%E5%A4%87%E6%B8%85%E5%8D%95202609.xlsx"
        assert _name_from_cd(cd) == "设备清单202609.xlsx"

    def test_plain_and_quoted_forms(self):
        assert _name_from_cd('attachment; filename="庚分公司清单202607.xlsx"') == "庚分公司清单202607.xlsx"
        assert _name_from_cd("attachment; filename=report.pdf") == "report.pdf"

    def test_latin1_mojibake_repaired(self):
        """老服务器把中文名按 latin-1 塞进头部（无百分号）→ 修回 utf-8。"""
        raw = "设备清单.xlsx".encode("utf-8").decode("latin-1")
        assert _decode_name(raw) == "设备清单.xlsx"

    def test_no_path_traversal_and_illegal_chars(self):
        assert _decode_name("../../../../windows/win.ini") == "win.ini"
        assert _decode_name("C:\\下载\\a b.xlsx").endswith("a b.xlsx")
        assert ":" not in _decode_name("清单:2026.xlsx")

    def test_empty_cd_returns_empty(self):
        assert _name_from_cd("") == ""
        assert _decode_name(None) == ""

    def test_caller_name_wins_over_server_name(self):
        """优先级锁：page_fetch_to_disk 必须先用调用方给的名字，不能被服务端头部名盖掉。"""
        s = io.open(os.path.join(ROOT, "wecom_bridge", "download.py"), encoding="utf-8").read()
        assert 'd.get("fn") or filename' not in s, "仍是服务端名优先（旧 bug）"
        assert "_decode_name(filename) or _decode_name(d.get(\"fn\"))" in s
        # 三条通道都走同一套规范化，不许再各留一份解析
        assert s.count('filename\\*?=') <= 1, "Content-Disposition 解析没收敛到一处"

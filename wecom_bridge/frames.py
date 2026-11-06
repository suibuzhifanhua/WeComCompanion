# -*- coding: utf-8 -*-
"""wecom_bridge.frames — iframe 上下文（v1.0.230）

背景：企业学习平台的课件渲染在**同源 iframe** 内。
同源 iframe 不会在 /json/list 里单独出现（只有 OOPIF 才会），
主 frame 的 document.querySelectorAll 也看不到 iframe 内部 DOM，
所以 read_page / click_element 对课件一律失效（表现为"只采到第 1 页"）。

解决：一条 WebSocket 会话内
  1) Runtime.enable → 收集 Runtime.executionContextCreated 得到 frameId→contextId
  2) Page.getFrameTree → 得到 frameId→url/name 树
  3) Runtime.evaluate 带 contextId → 在指定 iframe 内执行 JS

本模块把这套流程封装成可复用 API，供 learn_capture 等使用。
"""
import json
import time   # v1.0.652：_wait/_enable 总时限用 monotonic

from .cdp_client import _open_ws
from core.result import err_msg

# 探针：返回该 frame 的内容特征，用于判断"课件在哪一层"
PROBE_JS = (
    "(() => {"
    " const b = document.body ? document.body.innerText : '';"
    " return JSON.stringify({"
    "  len: b.length,"
    "  canvas: document.querySelectorAll('canvas').length,"
    "  iframe: document.querySelectorAll('iframe').length,"
    "  video: document.querySelectorAll('video').length,"
    "  head: b.slice(0, 160)"
    " });"
    "})()"
)


class FrameSession(object):
    """一条 CDP WebSocket 会话，管理该页面的所有 frame 上下文。"""

    def __init__(self, ws_url, timeout=10):
        self.ws = _open_ws(ws_url, timeout)
        if isinstance(self.ws, dict):  # 连接失败
            raise RuntimeError(err_msg(self.ws))
        self.timeout = timeout
        self._id = 0
        self.ctxs = []        # [{id, frameId, name, origin}]
        self.frames = []      # [{id, name, url, depth}]
        self._enable()

    # ---------- 底层收发 ----------
    def _send(self, method, params=None):
        self._id += 1
        i = self._id
        msg = {"id": i, "method": method}
        if params is not None:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        return i

    def _wait(self, i):
        # v1.0.652（审计 P1-10）：补总时限——ws 空闲超时挡不住事件流不断（与
        # cdp_client._recv_until 同款缺陷），课件采集页事件多时会永久挂住。
        deadline = time.monotonic() + float(getattr(self, "timeout", 25) or 25)
        while True:
            _left = deadline - time.monotonic()
            if _left <= 0:
                return None
            try:
                self.ws.settimeout(min(float(self.ws.gettimeout() or _left), _left))
                m = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001
                return None
            if m.get("id") == i:
                return m

    def _enable(self):
        """Runtime.enable + 收集已存在的执行上下文（含所有同源 iframe）。"""
        i = self._send("Runtime.enable")
        deadline = time.monotonic() + float(getattr(self, "timeout", 25) or 25)   # v1.0.652
        while True:
            if time.monotonic() >= deadline:
                break
            try:
                m = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001
                break
            if m.get("id") == i:
                break
            if m.get("method") == "Runtime.executionContextCreated":
                c = m["params"]["context"]
                if c.get("type") != "other":
                    self.ctxs.append({
                        "id": c.get("id"),
                        "frameId": (c.get("auxData") or {}).get("frameId"),
                        "name": c.get("name") or "",
                        "origin": c.get("origin") or "",
                    })
        self._load_tree()

    def _load_tree(self):
        i = self._send("Page.getFrameTree")
        m = self._wait(i)
        if not m or "result" not in m:
            return

        def walk(node, dep=0):
            f = node.get("frame") or node
            self.frames.append({
                "id": f.get("id"),
                "name": f.get("name") or "",
                "url": f.get("url") or "",
                "depth": dep,
            })
            for c in (node.get("childFrames") or []):
                walk(c, dep + 1)

        walk(m["result"].get("frameTree") or {})

    # ---------- 对外 API ----------
    def eval(self, expr, ctx_id=None, timeout=None):
        """在指定 contextId（默认主 frame）内执行 JS，返回 Python 值。"""
        params = {"expression": expr, "returnByValue": True, "awaitPromise": True}
        if ctx_id is not None:
            params["contextId"] = ctx_id
        i = self._send("Runtime.evaluate", params)
        m = self._wait(i)
        if not m:
            return {"ok": False, "error": "evaluate 超时"}
        if "error" in m:
            return {"ok": False, "error": str(m["error"])}
        r = m.get("result", {}).get("result", {})
        if r.get("type") == "undefined":
            return None
        return r.get("value")

    def frame_of(self, ctx_id):
        for c in self.ctxs:
            if c["id"] == ctx_id:
                for f in self.frames:
                    if f["id"] == c["frameId"]:
                        return f
                return {"id": c["frameId"], "url": "", "name": c["name"], "depth": -1}
        return None

    def probe_all(self):
        """对所有 frame 跑探针，返回 [{ctx, frame, len, canvas, iframe, video, head}]。"""
        out = []
        for c in self.ctxs:
            v = self.eval(PROBE_JS, c["id"])
            info = {"ctx": c["id"], "frame": self.frame_of(c["id"]) or {}}
            try:
                info.update(json.loads(v)) if isinstance(v, str) else None
            except Exception:  # noqa: BLE001
                pass
            out.append(info)
        return out

    def pick_content_frame(self, want_canvas=False, want_video=False, min_len=0):
        """挑出"最像课件"的 frame context：优先 canvas/video 数量多的，其次正文长的。"""
        best, best_score = None, -1
        for info in self.probe_all():
            score = 0
            if want_canvas:
                score += (info.get("canvas") or 0) * 100
            if want_video:
                score += (info.get("video") or 0) * 100
            score += min(info.get("len") or 0, 2000) / 100.0
            if (info.get("len") or 0) < min_len:
                score -= 500
            if score > best_score:
                best, best_score = info, score
        return best

    def screenshot(self, fmt="png", front=None):
        """在本会话连接的窗口上截图，返回 PNG 字节（失败返回 b''）。

        为什么不复用 wecom_bridge.capture_screenshot：它带 4s 超时 + DOM 绘制降级，
        课件（canvas 渲染）超时降级后会返回不含最新画布的画面，表现为"多页 OCR 内容重复"。
        课件采集要求"截到的一定是当前帧"，宁可失败也不降级（v1.0.230 实测）。

        v1.0.523：front=None（默认）时按静默模式自动判定——静默模式（wecom.silent_mode
        默认开）下不 bringToFront（不抢前台焦点），课件采集可能拿到旧帧或空帧，
        由调用方 _take_shot 的既有失败处理兜底；显式传 True 强制前置（旧行为）。
        """
        if front is None:
            try:
                from core.config_store import silent_mode
                front = not silent_mode()
            except Exception:  # noqa: BLE001
                front = True
        if front:
            i = self._send("Page.bringToFront")
            self._wait(i)
        i = self._send("Page.captureScreenshot", {"format": fmt, "captureBeyondViewport": False})
        m = self._wait(i)
        if not m or "error" in m:
            return b""
        import base64
        data = (m.get("result") or {}).get("data") or ""
        try:
            return base64.b64decode(data)
        except Exception:  # noqa: BLE001
            return b""

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def open_session(port=None, pick=0):
    """按窗口序号打开 FrameSession。
    v1.0.364 审查修复（H3）：port=None 时从配置读取 cdp_port（原写死 9222，
    用户改端口后此路径失联）。v1.0.588：口径收敛到 core.config_store.cdp_port。"""
    if port is None:
        from core.config_store import cdp_port
        port = cdp_port()
    from .windows import list_targets, _pages, pick_window
    tg = list_targets(port)
    if isinstance(tg, dict):
        raise RuntimeError(err_msg(tg))
    # pick 语义与 read_page / click_element 一致：_pages() 过滤后的序号
    t = pick_window(tg, pick) if pick is not None else None
    if not t:
        pages = _pages(tg)
        if not pages:
            raise RuntimeError("没有可连接的窗口")
        t = pages[0]
    if not t.get("webSocketDebuggerUrl"):
        raise RuntimeError("该窗口无调试地址")
    return FrameSession(t["webSocketDebuggerUrl"])

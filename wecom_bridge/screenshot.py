"""企微窗口截图（CDP 像素截图 + DOM 绘制降级）（v1.0.656 自 windows.py 拆出）。

兼容口径：对 windows 命名空间的运行期引用统一走 `_w.<name>` 动态属性
（`from . import windows as _w`），保证 monkeypatch.setattr(wecom_bridge.windows, "X")
与拆分前「windows.py 模块内直调」语义完全一致（含本模块内互相调用）。
"""

import json

from .cdp_client import DEFAULT_PORT, CDP_EVAL_TIMEOUT, CDP_SCREENSHOT_TRY_TIMEOUT
from . import windows as _w
from core.result import is_err, err_msg

_CAPTURE_JS = r"""
(() => {
  try {
    const body = document.body ? document.body.innerText : '';
    const seen = new Set();
    const struct = [];
    const tags = ['h1','h2','h3','a','button','li','p','span','div'];
    for (const t of tags) {
      document.querySelectorAll(t).forEach(n => {
        const txt = (n.innerText || '').trim();
        if (txt && txt.length <= 120 && !seen.has(txt)) {
          seen.add(txt);
          struct.push({tag: t, text: txt, href: n.href || ''});
        }
      });
      if (struct.length > 600) break;
    }
    const norm = body.replace(/\s+/g, ' ').trim();
    return JSON.stringify({
      title: document.title,
      body: norm.slice(0, 14000),
      struct: struct.slice(0, 400),
      url: location.href
    });
  } catch (e) {
    return JSON.stringify({__error__: String(e)});
  }
})()
"""


def capture(pick=None, port=DEFAULT_PORT):
    targets = _w.list_targets(port)
    if is_err(targets):
        return {"ok": False, "error": err_msg(targets), "targets": []}
    page = _w.pick_window(targets, pick)
    if not page:
        return {"ok": False, "error": "没有可用的企微窗口 (targets 为空或全被 devTools:false 屏蔽)",
                "targets": targets if isinstance(targets, list) else []}
    try:
        raw = _w._cdp_eval(page["webSocketDebuggerUrl"], _w._CAPTURE_JS)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "title": page.get("title"), "url": page.get("url")}
    data = _w._json_result(raw, empty={})   # v1.0.654 A5：解析单源
    data["url"] = page.get("url")
    return data


def capture_screenshot(pick=None, port=DEFAULT_PORT, fmt="png", front=False):
    """对指定（或自动选定）的企微窗口截图，返回 base64 PNG 与窗口元信息。
    纯新增能力，不改动任何既有函数签名/返回结构；失败时返回 {"ok": False, "error": ...}。
    front (v1.0.274)：是否 Page.bringToFront 强制前置目标窗口。默认 False——
    前端预览 5s 轮询，旧版无条件前置导致企微窗口反复弹窗遮挡用户工作；
    前置会打断用户，故默认只 setWebLifecycleState=active 唤醒合成器，
    拿不到新帧时走既有「DOM 绘制截图」降级路径。"""
    _err, ws_url, page = _w._require_page(pick, port, with_windows=True)   # v1.0.654 A6
    if _err:
        return _err
    ws = _w._open_ws(ws_url, CDP_SCREENSHOT_TRY_TIMEOUT)
    if isinstance(ws, dict):
        return ws
    try:
        # 截图前先唤醒/前置目标页面并强制刷新合成帧：
        # 后台/隐藏页面可能返回缓存帧（真实页面已变、截图仍是旧画面）。
        # 逐条发送并按 id 回收响应，任一命令失败都不阻断后续截图。
        _next_id = 0
        def _cmd(method, params=None):
            nonlocal _next_id
            _next_id += 1
            ws.send(json.dumps({"id": _next_id, "method": method, "params": params or {}}))
            return _w._recv_until(ws, _next_id)
        if front:
            # 仅显式要求时前置（手动截图保存/课件采集）：激活目标窗口强制合成新帧
            try:
                _cmd("Page.bringToFront")
            except Exception:  # noqa: BLE001
                pass
        try:
            # 无论是否前置：页面生命周期置 active 唤醒合成器（不激活 OS 窗口、不抢焦点），
            # 降低「截到缓存旧帧」概率；确实拿不到新帧时下方走 DOM 绘制降级
            _cmd("Page.setWebLifecycleState", {"state": "active"})
        except Exception:  # noqa: BLE001
            pass
        try:
            msg = _cmd("Page.captureScreenshot",
                       {"format": fmt, "captureBeyondViewport": False})
        except Exception as e:
            # v1.0.361 审查修复（P9）：主截图命令异常（连接被页面导航/崩溃重置等）
            # 不再逃逸——降级 DOM 绘制或返回结构化错误（调用方按 __error__ 处理）
            _w._logw(f"[wecom] captureScreenshot 异常，降级 DOM 绘制：{type(e).__name__}: {e}")
            return _w._dom_render_screenshot(page, ws_url)
        if msg is None:
            # 合成帧挂起（后台页面/渲染异常时 Page.captureScreenshot 会一直等不到帧）：
            # 降级为页面内 DOM 绘制截图，保证预览仍能反映页面内容变化
            return _w._dom_render_screenshot(page, ws_url)
        if "error" in msg:
            # 有 error 时也尝试 DOM 降级，避免「截图不可用」把预览画面永久卡死
            fallback = _w._dom_render_screenshot(page, ws_url)
            if fallback.get("data"):
                return fallback
            return {"ok": False, "error": str(msg["error"])}
        data = (msg.get("result", {}) or {}).get("data")
        if not data:
            return _w._dom_render_screenshot(page, ws_url)
        return {"title": page.get("title"), "url": page.get("url"),
                "data": data, "format": fmt}
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass  # silent-ok: 会话关闭失败不致命，底层连接会自动回收


_DOM_RENDER_JS = r"""
(() => {
  try {
    const doc = document.documentElement;
    const w = Math.max(doc.scrollWidth, window.innerWidth) || 800;
    const h = Math.max(doc.scrollHeight, window.innerHeight) || 600;
    const scale = Math.min(1, 1280 / w);
    const cv = document.createElement('canvas');
    cv.width = Math.max(1, Math.floor(w * scale));
    cv.height = Math.max(1, Math.floor(h * scale));
    const ctx = cv.getContext('2d');
    ctx.scale(scale, scale);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, w, h);
    let n = 0;
    const all = document.querySelectorAll('body *');
    for (let i = 0; i < all.length; i++) {
      const el = all[i];
      if (n++ > 6000) break;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 1 || r.height < 1) continue;
      if (r.right < -20 || r.left > w + 40 || r.bottom < -20 || r.top > h + 40) continue;
      let cs; try { cs = window.getComputedStyle(el); } catch (e) { continue; }
      const bg = cs.backgroundColor;
      if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
        ctx.fillStyle = bg; ctx.fillRect(r.left, r.top, r.width, r.height);
      }
      const vis = !(cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity || '1') <= 0.05);
      if (!vis) continue;
      const bw = parseFloat(cs.borderTopWidth) || 0;
      if (bw > 0.5 && cs.borderTopStyle !== 'none') {
        try { ctx.strokeStyle = cs.borderTopColor || '#999'; ctx.lineWidth = bw;
              ctx.strokeRect(r.left + bw / 2, r.top + bw / 2, Math.max(0, r.width - bw), Math.max(0, r.height - bw)); } catch (e) {}
      }
      const txt = (el.childElementCount === 0 && (el.innerText || '').trim());
      if (txt) {
        ctx.fillStyle = cs.color || '#000';
        const fs = Math.min(parseFloat(cs.fontSize) || 13, 26);
        ctx.font = (cs.fontWeight === 'bold' ? 'bold ' : '') + fs + 'px sans-serif';
        ctx.fillText(txt.slice(0, 200), r.left + 2, r.top + fs);
      }
    }
    return cv.toDataURL('image/png');
  } catch (e) { return JSON.stringify({__error__: String(e)}); }
})()
"""


def _dom_render_screenshot(page, ws_url, timeout=CDP_EVAL_TIMEOUT):
    """降级截图：CDP Page.captureScreenshot 在合成帧挂起（后台/渲染异常页面）时
    长时间无响应，改为在页面内用 canvas 做 DOM 绘制，绕开合成器，仍能返回可辨识画面。
    跨域图片会污染 canvas（toDataURL 抛 SecurityError），故不绘制 img，
    仅覆盖背景色、文字、控件边框——对答题/表单等以文字为主的页面足够反映内容变化。
    返回结构与 capture_screenshot 一致，并带 render:"dom" 标记供前端区分。"""
    raw = _w._cdp_eval(ws_url, _w._DOM_RENDER_JS, timeout=timeout)
    if is_err(raw):
        return {"ok": False, "error": "DOM 渲染截图失败：" + err_msg(raw)}
    if isinstance(raw, str):
        if raw.startswith("data:image/png;base64,"):
            return {"title": page.get("title"), "url": page.get("url"),
                    "data": raw[len("data:image/png;base64,"):],
                    "format": "png", "render": "dom"}
        if raw.startswith('{"__error__"'):
            try:
                return {"ok": False, "error": "DOM 渲染截图失败：" + err_msg(json.loads(raw))}
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": "DOM 渲染截图失败：" + raw[:120]}
    return {"ok": False, "error": "DOM 渲染截图失败：无输出"}

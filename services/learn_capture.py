# -*- coding: utf-8 -*-
"""在线课程内容采集器（v1.0.229 → v1.0.230 → v1.0.231）。

场景：企业学习平台的课程是「在线 PDF 阅读器 + 视频」的形式，
内容渲染在 canvas / video 里，read_wecom_page 拿不到文字（DOM 只有页码）。
考试题又直接出自这些课件——不采集就只能靠模型瞎猜（实测 60 分，及格 90）。

v1.0.231 重要发现：学习平台的「PDF」其实是**跨域 OOPIF + 每页独立 SVG**
（type=iframe, URL 形如 <学习平台CDN域>/.../{N}.svg），比截图 OCR 快 50x：
  - SVG 公开 CDN，无需鉴权
  - SVG 里有 <text> 节点直接含课件文字，无需 OCR
  - 22 页课程 0.3s 全部下载解析完成，准确率 100%
  - 同时学到了「跨域 OOPIF：list_targets 出现但 main frame getFrameTree 看不到」

方案（按优先级尝试）：
  0) OOPIF SVG 直连（v1.0.231）：list_targets 中匹配 /{N}.svg，按 N 递增拉
  1) PDF：逐页「截图 → OCR → 点下一页」（同源 iframe 已解决，v1.0.230）
  2) 视频：播放中按间隔抽帧「截图 → OCR」

产出：产物库/知识库/课件/<课程名>.md —— 考试时 exam-solver 检索这个文件作答。
"""
import os
import re
import time
import base64
import datetime

# v1.0.510 优化（P2-13）：原模块级 _OCR 懒加载引擎（RapidOCR 第二实例）已删除，
# OCR 统一走 core.ocr 单例（见 _ocr_image）。保留 _get_ocr 兼容 shim 供潜在外部引用。
def _get_ocr():
    """兼容 shim：OCR 引擎统一由 core.ocr 管理，本模块不再自建。恒返回 None
    （调用方应改用 _ocr_image(path)，其内部已处理未就绪降级）。"""
    return None


def _ocr_image(path):
    """对图片做 OCR，返回识别文本（按行合并）。失败返回 ''。
    v1.0.510 优化（P2-13）：原在本模块自建第二套 RapidOCR 引擎（_get_ocr 懒加载），
    与 core/ocr.py 的全局单例并存——同一进程可能加载两份 ~226MB OCR 引擎。现统一
    委托 core.ocr._ocr_image_file（内含 ocr_ready_or_install 触发后台安装 + 单例
    双检锁），本模块不再自持引擎。"""
    from core.ocr import _ocr_image_file
    return _ocr_image_file(path)



def _save_bytes(raw, path):
    try:
        with open(path, "wb") as f:
            f.write(raw)
        return bool(raw)
    except Exception:  # noqa: BLE001
        return False


def _take_shot(sess, port, pick):
    """取当前画面 PNG 字节：优先用 frame 会话直连截图（不降级），失败回退公共截图接口。"""
    if sess is not None:
        raw = sess.screenshot()   # v1.0.523：front 未显式传——静默模式下不前置（frame 会话内部判定）
        if raw:
            return raw
    import wecom_bridge as wb
    # v1.0.274：公共截图默认不再前置窗口，课件采集要求「截到的一定是当前帧」，显式前置
    # v1.0.523：静默模式下不前置（可能拿到旧帧/走 DOM 降级）——不打断用户优先于采集画质
    try:
        from core.config_store import silent_mode
        _front = not silent_mode()
    except Exception:  # noqa: BLE001
        _front = True
    res = wb.capture_screenshot(pick, port=port, front=_front)
    data = res.get("data") if isinstance(res, dict) else None
    if not data:
        return b""
    try:
        return base64.b64decode(data)
    except Exception:  # noqa: BLE001
        return b""


def _cur_page(port, pick, sess=None):
    """从页面（含 iframe）文本解析当前页码（如 '1 / 22'）。返回 (cur, total)。"""
    try:
        if sess is not None:
            for info in sess.probe_all():
                head = info.get("head") or ""
                m = re.search(r"(\d+)\s*/\s*(\d+)", head)
                if m:
                    return int(m.group(1)), int(m.group(2))
        import wecom_bridge as wb
        d = wb.read_page(pick, port=port)
        body = (d.get("body") or "")
        m = re.search(r"(\d+)\s*/\s*(\d+)", body)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:  # noqa: BLE001
        pass
    return None, None


# --- 课件定位（v1.0.230）：课件渲染在同源 iframe 内，必须在 frame context 里操作 ---

_JS_CLICK_NEXT = r"""
(() => {
  const hit = [];
  const scan = (root, tag) => {
    root.querySelectorAll('button,[role=button],a,span,div,i,svg,use,path').forEach(e => {
      const tx = (e.innerText || e.textContent || '').trim();
      const cl = (e.className || '').toString();
      const ar = (e.getAttribute && (e.getAttribute('aria-label') || e.getAttribute('title'))) || '';
      const all = tx + ' ' + cl + ' ' + ar;
      if (/下一页|下页|后一页|page-next|pageNext|next-page|nextPage|icon-next|arrow-right|arrowRight/i.test(all) && tx.length < 16) {
        const r = e.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) hit.push({e: e, y: r.top + r.height / 2, x: r.left + r.width / 2});
      }
    });
  };
  scan(document, 'main');
  for (let i = 0; i < document.querySelectorAll('iframe').length; i++) {
    try { scan(document.querySelectorAll('iframe')[i].contentDocument, 'sub'); } catch (err) {}
  }
  if (!hit.length) return 'NO_TARGET';
  // 取最靠右/最靠下的（翻页控件通常在右下或右侧）
  hit.sort((a, b) => (b.x - a.x) || (b.y - a.y));
  const h = hit[0];
  try {
    h.e.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
    h.e.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
    h.e.click();
    return 'CLICKED:' + (h.e.innerText || h.e.className || '').slice(0, 20);
  } catch (err) {
    return 'ERR:' + err;
  }
})()
"""

_JS_KEY = r"""
((key, code, kc) => {
  const el = document.activeElement || document.body || document.documentElement;
  let ev;
  try {
    ev = new KeyboardEvent('keydown', {key: key, code: code, keyCode: kc, which: kc, bubbles: true, cancelable: true});
  } catch (err) {
    ev = new KeyboardEvent('keydown', {key: key, code: code, bubbles: true, cancelable: true});
  }
  el.dispatchEvent(ev);
  (document.body || document.documentElement).dispatchEvent(ev);
  return 'KEY:' + key;
})(%s, %s, %d)
"""

_JS_PAGE_INPUT = r"""
((n) => {
  const ins = [];
  document.querySelectorAll('input').forEach(e => {
    const ph = (e.placeholder || '') + ' ' + (e.className || '') + ' ' + (e.getAttribute('aria-label') || '');
    if (/page|页|jump|跳转/i.test(ph) || e.type === 'number') ins.push(e);
  });
  if (!ins.length) return 'NO_INPUT';
  const el = ins[0];
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(el, String(n));
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return 'SET:' + n;
})(%d)
"""

_JS_FOCUS_CANVAS = r"""
(() => {
  const c = document.querySelector('canvas');
  if (!c) return 'NO_CANVAS';
  c.setAttribute('tabindex', '0');
  try { c.focus(); } catch (err) {}
  const r = c.getBoundingClientRect();
  c.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
  c.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  return 'FOCUS:' + Math.round(r.width) + 'x' + Math.round(r.height);
})()
"""

# 截图前强制合成新帧（v1.0.230 实测：iframe 内 canvas 已翻页，但 Page.captureScreenshot
# 从主 surface 抓图会返回上一帧——5 页里有 2 页截到旧画面，OCR 内容与页码错位）。
# 双 rAF + 100ms：确保渲染器提交新帧后再截图。
_JS_RAF = "new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>setTimeout(r,100))))"


def _force_frame(sess, ctx):
    """截图前强制合成新帧，避免截到旧画面。失败时退化为短 sleep。"""
    if sess is None or ctx is None:
        time.sleep(0.3)
        return
    try:
        sess.eval(_JS_RAF, ctx)
    except Exception:  # noqa: BLE001
        time.sleep(0.3)


# 进入课件：课程任务页点课程卡片（绝不点"已完成"）
_JS_ENTER_COURSE = r"""
((kw) => {
  const bad = /已完成|确认|提交|关闭|退出/;
  const cands = [];
  document.querySelectorAll('*').forEach(e => {
    const tx = (e.innerText || '').trim();
    if (!tx || tx.length > 80) return;
    if (kw && tx.indexOf(kw) < 0) return;
    if (bad.test(tx)) return;
    const cs = getComputedStyle(e);
    if (cs.cursor !== 'pointer') return;
    if (e.children.length > 5) return;
    cands.push(e);
  });
  if (!cands.length) return 'NO_CAND';
  // 取最深的（最贴近文本节点）
  cands.sort((a, b) => b.children.length - a.children.length);
  const el = cands[0];
  const r = el.getBoundingClientRect();
  if (r.width < 20 || r.height < 10) return 'TOO_SMALL';
  el.scrollIntoView({block: 'center'});
  el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, view: window}));
  el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
  el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
  el.click();
  return 'ENTERED:' + (el.innerText || '').slice(0, 24);
})(%s)
"""


def _enter_course_if_needed(port, pick, name, sess):
    """页面停在课程任务页（无 canvas）时，点课程卡片进入课件。返回 (sess, ctx_id, info) 或 (None, None, None)。"""
    from wecom_bridge.frames import open_session
    from core_utils import log_event

    info = sess.pick_content_frame(want_canvas=True)
    if info and (info.get("canvas") or 0) > 0:
        return sess, info.get("ctx"), info

    kw = (name or "").replace("《", "").replace("》", "")
    kw = kw[:8] if kw else ""
    main_ctx = None
    for c in sess.ctxs:
        f = sess.frame_of(c["id"]) or {}
        if (f.get("depth") or 0) == 0:
            main_ctx = c["id"]
            break
    if main_ctx is None and sess.ctxs:
        main_ctx = sess.ctxs[-1]["id"]
    if main_ctx is None:
        return None, None, None

    r = sess.eval(_JS_ENTER_COURSE % (repr(kw) if kw else "''"), main_ctx)
    log_event(f"[learn] 尝试进入课件：{r}")
    if not (isinstance(r, str) and r.startswith("ENTERED")):
        return None, None, None
    time.sleep(3.0)
    try:
        sess.close()
    except Exception:  # noqa: BLE001
        pass
    sess = open_session(port, pick)
    for _ in range(6):
        info = sess.pick_content_frame(want_canvas=True)
        if info and (info.get("canvas") or 0) > 0:
            return sess, info.get("ctx"), info
        time.sleep(1.2)
    # v1.0.364 审查修复（H4）：找不到 canvas 时关闭新开的会话再返回——
    # 原实现直接 return None 导致每次进入课件失败都泄漏一条 CDP 连接，
    # 无人值守跑课反复失败会累积耗尽 CDP/句柄资源。
    try:
        sess.close()
    except Exception:  # noqa: BLE001
        pass
    return None, None, None


def _next_page(sess, ctx, cur_no):
    """在课件 frame 内翻到下一页。按「DOM 点击 → 页码输入 → 合成键盘」顺序尝试。"""
    from core_utils import log_event
    # 1) DOM 点击
    r = sess.eval(_JS_CLICK_NEXT, ctx)
    if isinstance(r, str) and r.startswith("CLICKED"):
        log_event(f"[learn] 翻页：DOM 点击 {r}")
        return True
    # 2) 页码输入框直接跳页
    if cur_no:
        r2 = sess.eval(_JS_PAGE_INPUT % (cur_no + 1), ctx)
        if isinstance(r2, str) and r2.startswith("SET"):
            log_event(f"[learn] 翻页：页码输入 {r2}")
            return True
    # 3) 键盘（先把焦点塞进画布）
    sess.eval(_JS_FOCUS_CANVAS, ctx)
    for key, code, kc in (("PageDown", "PageDown", 34), ("ArrowRight", "ArrowRight", 39),
                          (" ", "Space", 32)):
        r3 = sess.eval(_JS_KEY % (repr(key), repr(code), kc), ctx)
        if isinstance(r3, str) and r3.startswith("KEY"):
            log_event(f"[learn] 翻页：键盘 {key}")
            return True
    return False


def capture_pdf_pages(port, pick, name, max_pages=30, tmp_dir=None):
    """PDF 课件逐页采集（v1.0.230：iframe 内翻页）。

    流程：定位 canvas 所在的 frame（同源 iframe）→ 逐页「截图 → OCR → frame 内翻页」
    → 页码/OCR 签名不再变化即停 → 合并存 产物库/知识库/课件/<课程名>.md

    返回 {"ok": True, "pages": n, "path": 相对路径, "chars": 字符数}。
    """
    import wecom_bridge as wb
    from core_utils import workspace_root, log_event, KB_ROOT
    from wecom_bridge.frames import open_session

    tmp = tmp_dir or os.path.join(workspace_root(), "下载", "_ocr_tmp")
    os.makedirs(tmp, exist_ok=True)
    shot_path = os.path.join(tmp, "_page.png")

    pages_text = []
    seen_sigs = set()

    sess = None
    try:
        sess = open_session(port, pick)
        sess, ctx, info = _enter_course_if_needed(port, pick, name, sess)
        if not ctx:
            return {"ok": False, "error": "未找到课件画布（请先打开课程文档页面，或确认窗口序号 pick）"}
        log_event(f"[learn] 课件 frame 已定位：{(info.get('frame') or {}).get('url', '')[:60]}")

        cur, total = _cur_page(port, pick, sess)
        if total:
            max_pages = min(max_pages, total)
        log_event(f"[learn] 页码 {cur}/{total}，计划采集 {max_pages} 页")

        stall = 0
        for i in range(max_pages):
            # 1. 截图（整窗口，含 iframe 内 canvas）—— 先强制合成新帧，否则可能截到上一页
            _force_frame(sess, ctx)
            raw = _take_shot(sess, port, pick)
            if not raw:
                break
            if not _save_bytes(raw, shot_path):
                break
            # 2. OCR
            txt = _ocr_image(shot_path)
            sig = txt[:120]
            # v1.0.364 审查修复（M14）：空白页也计入 stall——原实现 sig 为空串时
            # 恒不判重，画面卡住/黑屏会连采 30 页空内容直到上限（浪费 1-2 分钟）。
            if (sig and sig in seen_sigs) or (not sig and txt == ""):
                stall += 1
                log_event(f"[learn] 第{i+1}页内容与已有页重复或为空白（stall={stall}）")
                if stall >= 2:
                    break
            else:
                stall = 0
                seen_sigs.add(sig)
                pages_text.append((i + 1, txt))
                log_event(f"[learn] PDF 第{i+1}页 OCR {len(txt)} 字")
            # 3. 翻页（在 canvas frame 内）
            c2, _t2 = _cur_page(port, pick, sess)
            if not _next_page(sess, ctx, c2 or (i + 1)):
                log_event("[learn] 翻页失败，结束采集")
                break
            time.sleep(1.2)
            # 4. 新页 canvas 重绘等待（页码变化即认为成功）
            if total:
                c3, _t3 = _cur_page(port, pick, sess)
                if c3 is not None and c2 is not None and c3 <= c2:
                    time.sleep(1.0)
    finally:
        if sess:
            sess.close()

    try:
        os.remove(shot_path)
    except Exception:  # noqa: BLE001
        pass

    if not pages_text:
        return {"ok": False, "error": "未采集到任何页面（截图/OCR 失败）"}

    md = ["# " + name, "",
          f"- 采集时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
          f"- 来源：在线课程 PDF（截图 OCR，共 {len(pages_text)} 页）",
          ""]
    for pno, txt in pages_text:
        md.append(f"## 第 {pno} 页")
        md.append("")
        md.append(txt)
        md.append("")
    content = "\n".join(md)

    rel = os.path.join(KB_ROOT, "课件", _safe_name(name) + ".md")
    abs_p = os.path.join(workspace_root(), rel)
    os.makedirs(os.path.dirname(abs_p), exist_ok=True)
    with open(abs_p, "w", encoding="utf-8") as f:
        f.write(content)
    log_event(f"[learn] 课程文档已保存：{rel}（{len(content)} 字）")
    return {"ok": True, "pages": len(pages_text), "path": rel, "chars": len(content)}


def capture_video_frames(port, pick, name, interval=10, max_frames=40, tmp_dir=None):
    """视频抽帧采集：按间隔截图 → OCR（画面中的 PPT/字幕文字）。

    需要视频处于播放状态（脚本只负责抽帧 OCR，不控制播放）。
    """
    import wecom_bridge as wb
    from core_utils import workspace_root, log_event, KB_ROOT
    from wecom_bridge.frames import open_session

    tmp = tmp_dir or os.path.join(workspace_root(), "下载", "_ocr_tmp")
    os.makedirs(tmp, exist_ok=True)
    shot_path = os.path.join(tmp, "_frame.png")

    frames = []
    seen = set()
    sess, ctx = None, None
    try:
        sess = open_session(port, pick)
        info = sess.pick_content_frame(want_video=True)
        ctx = (info or {}).get("ctx")
    except Exception:  # noqa: BLE001
        # v1.0.652（审计 P2-C1）：异常路径先 close 再置 None——旧直接丢弃 sess，
        # 每次视频采集失败泄漏一条 CDP 连接，无人值守反复失败会累积耗尽句柄。
        try:
            if sess is not None:
                sess.close()
        except Exception:  # noqa: BLE001
            pass
        sess = None

    try:
        for i in range(max_frames):
            _force_frame(sess, ctx)
            raw = _take_shot(sess, port, pick)
            if not raw:
                break
            if not _save_bytes(raw, shot_path):
                break
            txt = _ocr_image(shot_path)
            sig = txt[:120]
            if sig and sig not in seen:
                seen.add(sig)
                frames.append((i * interval, txt))
                log_event(f"[learn] 视频第{i+1}帧 OCR {len(txt)} 字")
            time.sleep(interval)
    finally:
        if sess:
            sess.close()

    try:
        os.remove(shot_path)
    except Exception:  # noqa: BLE001
        pass

    if not frames:
        return {"ok": False, "error": "未采集到视频帧（视频可能未播放或 OCR 失败）"}

    md = ["# " + name + "（视频）", "",
          f"- 采集时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
          f"- 来源：在线视频抽帧 OCR（每 {interval}s 一帧，共 {len(frames)} 帧）",
          ""]
    for sec, txt in frames:
        md.append(f"## 约 {sec // 60}:{sec % 60:02d}")
        md.append("")
        md.append(txt)
        md.append("")
    content = "\n".join(md)

    rel = os.path.join(KB_ROOT, "课件", _safe_name(name) + "_视频.md")
    abs_p = os.path.join(workspace_root(), rel)
    os.makedirs(os.path.dirname(abs_p), exist_ok=True)
    with open(abs_p, "w", encoding="utf-8") as f:
        f.write(content)
    log_event(f"[learn] 视频文档已保存：{rel}（{len(content)} 字）")
    return {"ok": True, "frames": len(frames), "path": rel, "chars": len(content)}


def _safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", str(name or "课程")).strip()
    return name[:60] or "课程"


# OOPIF + SVG 直连（v1.0.231）
# 形如 https://<学习平台CDN域>/learn/app/filehub/.../...//1.svg
_OOPIF_SVG_RE = re.compile(r"/(\d+)\.svg(\?|$|#)")


def _detect_oopif_svg(port):
    """扫描 list_targets，找 type=iframe 且 URL 匹配 /{N}.svg 的 OOPIF。
    返回 (base_url, start_idx) 或 None。

    注意：跨域 OOPIF 在 /json/list 出现（type=iframe），但 _pages / Page.getFrameTree
    不会列出来，因为不同 origin 不能用主 page WS 操作——只能单独连它自己的 WS，
    或者（更简单）直接 HTTP 拉 SVG 文件。

    v1.0.231 教训：iframe OOPIF 的 URL 会变（reader 加载时是 index.html，而非 /1.svg），
    所以不能直接匹配 ".svg"。改成"任何 iframe target，探测同目录的 /1.svg 是否 200"。
    """
    try:
        import urllib.request
        import wecom_bridge as wb
        tg = wb.list_targets(port)
    except Exception:  # noqa: BLE001
        return None
    for t in tg:
        if t.get("type") != "iframe":
            continue
        u = t.get("url") or ""
        # 先用正则兜底
        m = _OOPIF_SVG_RE.search(u)
        if m:
            n = int(m.group(1))
            base = u[: m.start(1)] + "{}" + u[m.end(1):]
            return base, n
        # iframe URL 可能是 index.html 或带 ?query，取其父目录探测 /1.svg
        # 例如 https://x.com/.../filehash/index.html?x=1 -> base = https://x.com/.../filehash/{}.svg
        if u.count("/") < 3:
            continue
        parent = u.rsplit("/", 1)[0] + "/"
        for probe in (1, 2):
            probe_url = parent + f"{probe}.svg"
            try:
                urllib.request.urlopen(probe_url, timeout=5).read()
                base = parent + "{}.svg"
                return base, probe
            except Exception:
                continue
    return None


def _capture_svg_pages(port, name, max_pages=40, tmp=None):
    """OOPIF SVG 直连采集：枚举 /{N}.svg，提取 <text> 节点。零 OCR。
    返回 {"ok": True, "pages": n, "path": 相对路径, "chars": 字符数, "via": "oopif-svg"}。
    """
    import urllib.request
    import html as _html
    from core_utils import workspace_root, log_event, KB_ROOT

    info = _detect_oopif_svg(port)
    if not info:
        return {"ok": False, "error": "未发现 OOPIF SVG 课件"}
    base, start = info

    pages_text = []
    consecutive_404 = 0
    for n in range(start, start + max_pages):
        url = base.format(n)
        try:
            data = urllib.request.urlopen(url, timeout=10).read()
        except Exception as e:  # noqa: BLE001
            consecutive_404 += 1
            log_event(f"[learn] SVG 第{n}页获取失败：{e.__class__.__name__}")
            if consecutive_404 >= 2:
                break
            continue
        consecutive_404 = 0
        svg = data.decode("utf-8", errors="ignore")
        # 提取 <text> 节点（去掉子标签）
        texts = re.findall(r"<text[^>]*>(.*?)</text>", svg, re.S)
        page_lines = []
        for t in texts:
            t = _html.unescape(re.sub(r"\s+", " ", t.strip()))
            if t:
                page_lines.append(t)
        if not page_lines:
            # 没有 text 节点（可能纯图片页），记录原 SVG 大小便于诊断
            log_event(f"[learn] SVG 第{n}页无 text 节点（svg={len(svg)}B）")
            continue
        pages_text.append((n, page_lines))
        log_event(f"[learn] SVG 第{n}页 {len(page_lines)} 个文本节点")

    if not pages_text:
        return {"ok": False, "error": "OOPIF SVG 全部页面无可用文本"}

    md = ["# " + name, "",
          f"- 采集时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
          f"- 来源：OOPIF 跨域 SVG 直连（{base.split('://',1)[-1].split('/',1)[0]}，共 {len(pages_text)} 页，零 OCR）",
          ""]
    for pno, lines in pages_text:
        md.append(f"## 第 {pno} 页")
        md.append("")
        for ln in lines:
            md.append(f"- {ln}")
        md.append("")
    content = "\n".join(md)

    rel = os.path.join(KB_ROOT, "课件", _safe_name(name) + ".md")
    abs_p = os.path.join(workspace_root(), rel)
    os.makedirs(os.path.dirname(abs_p), exist_ok=True)
    with open(abs_p, "w", encoding="utf-8") as f:
        f.write(content)
    log_event(f"[learn] 课程文档已保存：{rel}（{len(content)} 字 via oopif-svg）")
    return {"ok": True, "pages": len(pages_text), "path": rel, "chars": len(content), "via": "oopif-svg"}


def capture_course(pick=None, port=None, name="", mode="auto"):
    """...（port=None 时从配置读取 cdp_port——v1.0.364 审查修复 H3，原写死 9222）"""
    if port is None:
        from core.config_store import cdp_port   # v1.0.588：端口取值口径统一
        port = cdp_port()
    """课程采集主入口。mode: pdf / video / auto（auto 先尝试 PDF，无内容再试视频）。"""
    try:
        import wecom_bridge as wb
        if pick is None:
            pages = wb._pages(wb.list_targets(port))
            if not pages:
                return {"ok": False, "error": "没有可用窗口"}
            pick = 0
        nm = name or "课程"
        # v1.0.231：先探一下有没有跨域 OOPIF + SVG（学习平台课程都是这种结构，0.3s 搞定）
        if mode in ("auto", "pdf"):
            r = _capture_svg_pages(port, nm)
            if r.get("ok"):
                return r
        if mode in ("auto", "pdf"):
            r = capture_pdf_pages(port, pick, nm)
            if r.get("ok") and r.get("chars", 0) > 200:
                return r
            if mode == "pdf":
                return r
        if mode in ("auto", "video"):
            return capture_video_frames(port, pick, nm)
        return {"ok": False, "error": "未知 mode：" + str(mode)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"采集失败：{e}"}

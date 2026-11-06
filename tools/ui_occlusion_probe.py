# -*- coding: utf-8 -*-
"""tools/ui_occlusion_probe.py — UI 遮挡 / 裁切 / 不可达 系统性探针

背景：企微伴侣的「实时窗口面板一片空白」这类问题，根因是布局状态（折叠类、
overflow:hidden、max-height、z-index、grid 过渡）让内容不可见或不可达，而这类
问题在源码审查里看不出来——必须拿真实渲染树去量。

本工具用 CDP 驱动 headless Chrome 加载**正在运行的 app**（默认 http://127.0.0.1:8787/，
GET / 会自动拿到 __wc_token cookie），在「多档视口 × 全部视图」下做四类检测：

  A occluded   交互元素中心点被**非自身/非祖先/非后代**的元素命中 → 真的被挡住点不到
  B clipped    元素内容超出自身尺寸但 overflow 不可滚动 → 内容被裁掉且无法查看
  C offscreen  元素矩形落在视口外且祖先不可滚动 → 需要横向/纵向拖拽却拖不动
  D blank      可见容器有子元素但自身高度为 0 / 或子元素全不可见 → 看着像坏了

用法：
    python tools/ui_occlusion_probe.py                 # 默认扫全部视图 × 4 档视口
    python tools/ui_occlusion_probe.py --url http://127.0.0.1:8787/
    python tools/ui_occlusion_probe.py --views work,models --sizes 1280x800
    python tools/ui_occlusion_probe.py --json out.json  # 机器可读结果

退出码：0=无发现；1=有发现（供 CI/自查用）；2=环境或连接问题。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

DEFAULT_VIEWS = ["work", "study", "chat", "knowledge", "models", "templates",
                 "skills", "experiences", "connectors", "workspace", "schedules",
                 "components", "settings", "help"]   # v1.0.606：新增「运行组件」独立视图
DEFAULT_SIZES = [(1440, 900), (1280, 800), (1024, 768), (820, 700), (700, 620)]

# 探针主体：在页面里跑，返回发现列表。刻意保守——宁可漏报也不误报。
# 误报来源已逐一排除（第一版跑出 488 条，绝大多数是噪声）：
#   · text-overflow:ellipsis 的标签截断是设计意图，不是遮挡
#   · 元素滚出可滚动祖先的可视区 ≠ 被遮挡（滚一下就能看到）
#   · 覆盖者是 position:fixed/sticky 的（弹层、吸顶头）是有意为之
#   · 覆盖者是 <html>/<body> 说明目标其实在视口外，归 C 类
PROBE_JS = r"""
(() => {
  const V = {w: innerWidth, h: innerHeight};
  const out = [];
  const cs = (e) => getComputedStyle(e);
  const vis = (e) => {
    const c = cs(e);
    if (c.display === 'none' || c.visibility === 'hidden' || +c.opacity === 0) return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const label = (e) => {
    const id = e.id ? '#' + e.id : '';
    const cls = (typeof e.className === 'string' && e.className)
      ? '.' + e.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
    const t = (e.textContent || e.title || e.getAttribute('aria-label') || '')
      .trim().replace(/\s+/g, ' ').slice(0, 36);
    return e.tagName.toLowerCase() + id + cls + (t ? ' [' + t + ']' : '');
  };
  const scrollableAncestor = (e) => {
    let p = e.parentElement;
    while (p) {
      const c = cs(p);
      if (/auto|scroll/.test(c.overflowY) || /auto|scroll/.test(c.overflowX)) return p;
      p = p.parentElement;
    }
    return null;
  };
  // 目标中心点是否落在其各层滚动祖先的可视矩形内。
  // 判中心而不是判整块矩形：跨在滚动边界上的元素（顶边在内、底边在外）中心会落到
  // 下一个兄弟身上，但它本身可见、可点、可滚到——按整块矩形判会大量误报。
  const centerInScrollViews = (e, cx, cy) => {
    let p = e.parentElement;
    while (p && p !== document.body) {
      const c = cs(p);
      if (/auto|scroll|hidden/.test(c.overflowY) || /auto|scroll|hidden/.test(c.overflowX)) {
        const pr = p.getBoundingClientRect();
        if (cx < pr.left - 1 || cx > pr.right + 1 || cy < pr.top - 1 || cy > pr.bottom + 1)
          return false;
        // 中心点**贴着**可继续滚动的祖先下/右内边缘时，同样是"被裁掉但滚一下就到"——
        // 原实现留了 ±1px 容差，正好让这种边界相等被判成"在可视区内"，于是把
        // 侧栏 .nav（700x620 下可视到 319px，模板行 301~337 中心恰好在 319）
        // 误报成被 #taskSec 遮挡。命中过一次就要一次判据修正，别再靠人肉排除。
        if (/auto|scroll/.test(c.overflowY) && p.scrollHeight > p.clientHeight + 1 && cy >= pr.bottom - 1)
          return false;
        if (/auto|scroll/.test(c.overflowX) && p.scrollWidth > p.clientWidth + 1 && cx >= pr.right - 1)
          return false;
      }
      p = p.parentElement;
    }
    return true;
  };
  const INTERACTIVE = 'button,input,select,textarea,a,[onclick],[role="button"],[tabindex]:not([tabindex="-1"])';
  const els = [...document.querySelectorAll('*')];

  // ---- A 遮挡：交互元素中心点被"外来且非吸顶/非浮层"的元素命中 ----
  for (const e of document.querySelectorAll(INTERACTIVE)) {
    if (!vis(e) || !e.isConnected) continue;
    // 折叠 <details> 里的控件不算遮挡：Chrome 用 ::details-content{content-visibility:hidden}
    // 收起内容，子元素**仍能量到矩形**（但看不见也点不着）。实测：组件页「OCR 版本维护」
    // 折叠时四个按钮被判成被 p.comp-foot 挡住——那是折叠，不是叠压。
    if (e.closest && e.closest("details:not([open])")) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) continue;
    const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2);
    if (cx < 0 || cy < 0 || cx >= V.w || cy >= V.h) continue;
    if (!centerInScrollViews(e, cx, cy)) continue;   // 中心已滚出容器：可达，不算遮挡
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || hit === e || e.contains(hit) || hit.contains(e)) continue;
    if (hit.tagName === 'HTML' || hit.tagName === 'BODY') continue;
    const hc = cs(hit);
    if (hc.position === 'fixed' || hc.position === 'sticky') continue;   // 有意覆盖层
    if (hc.pointerEvents === 'none') continue;
    out.push({kind: 'A-occluded', el: label(e), by: label(hit),
              rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
              hitPos: hc.position, hitZ: hc.zIndex});
  }

  // ---- B 裁切：块级容器内容超出且不可滚动，且不是 ellipsis 式有意截断 ----
  for (const e of els) {
    if (!vis(e) || !e.children.length) continue;
    const c = cs(e);
    if (/auto|scroll/.test(c.overflowY + c.overflowX)) continue;
    const cr = e.getBoundingClientRect();
    if (cr.width < 40 || cr.height < 24) continue;
    // 容器自身或其直接子用 ellipsis/nowrap 截断 → 设计意图，跳过
    const ell = [...e.children].some(ch => {
      const cc = cs(ch);
      return cc.textOverflow === 'ellipsis' || cc.whiteSpace === 'nowrap'
          || cc.overflow === 'hidden';
    });
    if (c.textOverflow === 'ellipsis' || ell) continue;
    let bot = -1, rgt = -1;
    for (const ch of e.children) {
      const b = ch.getBoundingClientRect();
      if (b.height === 0 && b.width === 0) continue;
      bot = Math.max(bot, b.bottom); rgt = Math.max(rgt, b.right);
    }
    if (c.overflowY === 'hidden' && bot > cr.bottom + 12)
      out.push({kind: 'B-clipped-y', el: label(e), clippedPx: Math.round(bot - cr.bottom),
                clientH: e.clientHeight, maxH: c.maxHeight});
    if (c.overflowX === 'hidden' && rgt > cr.right + 12)
      out.push({kind: 'B-clipped-x', el: label(e), clippedPx: Math.round(rgt - cr.right),
                clientW: e.clientWidth});
  }

  // ---- C 视口外且不可达 ----
  for (const e of els) {
    if (!vis(e)) continue;
    if (!e.matches(INTERACTIVE) && e.children.length) continue;
    const r = e.getBoundingClientRect();
    if (r.right <= V.w + 2 && r.bottom <= V.h + 2 && r.left >= -2 && r.top >= -2) continue;
    if (scrollableAncestor(e)) continue;
    if (r.left >= V.w + 2 || r.right <= -2)
      out.push({kind: 'C-offscreen-x', el: label(e), rect: [Math.round(r.left), Math.round(r.width)], vw: V.w});
    else if (r.top >= V.h + 2 || r.bottom <= -2)
      out.push({kind: 'C-offscreen-y', el: label(e), rect: [Math.round(r.top), Math.round(r.height)], vh: V.h});
  }

  // ---- D 空白容器：可见、有子、有高度，但内部没有任何可见内容（看着像坏了） ----
  for (const e of els) {
    if (!vis(e) || e.children.length === 0) continue;
    const r = e.getBoundingClientRect();
    if (r.height < 140 || r.width < 140) continue;
    if (e.closest('.model-dd-menu') || e.tagName === 'SELECT') continue;
    const anyVisible = [...e.querySelectorAll('*')].some(c => {
      const cr = c.getBoundingClientRect();
      return cr.width > 2 && cr.height > 2 && vis(c);
    });
    const txt = (e.textContent || '').trim();
    if (!anyVisible && !txt)
      out.push({kind: 'D-blank-box', el: label(e), h: Math.round(r.height), w: Math.round(r.width),
                children: e.children.length});
  }
  return JSON.stringify({viewport: V, view: (document.querySelector('.view.active')||{}).id || '', findings: out});
})()
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
    raise SystemExit("找不到 Chrome，检查 CHROME_CANDIDATES")


class CDP:
    """最小 CDP 客户端（Runtime.evaluate / Page.navigate / Emulation）。"""

    def __init__(self, ws_url):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.id = 0

    def send(self, method, params=None):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def eval_json(self, expr):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("value")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787/")
    ap.add_argument("--views", default=",".join(DEFAULT_VIEWS))
    ap.add_argument("--sizes", default="", help="如 1280x800,900x700；默认 5 档")
    ap.add_argument("--json", default="", help="把结果写入 JSON 文件")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--settle", type=float, default=0.45,
                    help="切视图后等待秒数。侧栏 renderTaskRail/loadConvList 是异步的，"
                         "等待不足会把重建中间态误判成遮挡（默认 0.45，排查假阳性用 1.5）")
    args = ap.parse_args()

    views = [v.strip() for v in args.views.split(",") if v.strip()]
    sizes = DEFAULT_SIZES
    if args.sizes:
        sizes = [tuple(int(x) for x in s.lower().split("x")) for s in args.sizes.split(",")]

    port = free_port()
    chrome = find_chrome()
    prof = os.path.join(os.environ.get("TEMP", "/tmp"), "wc_probe_profile")
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--hide-scrollbars",
         "--remote-debugging-port=%d" % port, "--user-data-dir=" + prof,
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import requests
        ver = None
        for _ in range(60):
            try:
                ver = requests.get("http://127.0.0.1:%d/json/version" % port, timeout=2).json()
                break
            except Exception:
                time.sleep(0.5)
        if not ver:
            print("❌ headless Chrome 调试端口未起来", file=sys.stderr)
            return 2
        # Chrome 111+ 的 /json/new 只接受 PUT；再兜底一层：直接复用启动时的标签页
        tgt = None
        try:
            r = requests.put("http://127.0.0.1:%d/json/new?url=about:blank" % port, timeout=5)
            if r.ok and r.text.strip().startswith("{"):
                tgt = r.json()
        except Exception:
            tgt = None
        if not tgt:
            tabs = [t for t in requests.get("http://127.0.0.1:%d/json" % port, timeout=5).json()
                    if t.get("webSocketDebuggerUrl") and t.get("type") == "page"]
            if not tabs:
                print("❌ headless Chrome 没有可用 page target", file=sys.stderr)
                return 2
            tgt = tabs[0]
        cdp = CDP(tgt["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Page.navigate", {"url": args.url})
        time.sleep(3.0)   # 等 index + js/* + 首轮 refreshWindows

        all_rows = []
        for (w, h) in sizes:
            cdp.send("Emulation.setDeviceMetricsOverride",
                     {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
            for v in views:
                cdp.eval_json("try{openView(%r)}catch(e){}" % v)
                time.sleep(args.settle)
                raw = cdp.eval_json(PROBE_JS)
                if not raw:
                    continue
                d = json.loads(raw)
                for f in d["findings"]:
                    all_rows.append(dict(f, size="%dx%d" % (w, h), view=d.get("view") or v))
                if not args.quiet and d["findings"]:
                    print("[%dx%d] %-11s → %d 项" % (w, h, v, len(d["findings"])))

        print("\n" + "=" * 72)
        if not all_rows:
            print("✅ 未发现遮挡/裁切/越界/空白问题（%d 视口 × %d 视图）"
                  % (len(sizes), len(views)))
        else:
            # 按 (kind, 元素, 遮挡者) 归并：侧栏等共享组件会在 13 个视图里各报一次，
            # 那是同一个问题，不该占 13 个坑位。
            agg = {}
            for r in all_rows:
                k = (r["kind"], r["el"], r.get("by", ""))
                a = agg.setdefault(k, {"n": 0, "sizes": set(), "views": set(), "detail": r})
                a["n"] += 1
                a["sizes"].add(r["size"])
                a["views"].add(r["view"])
            print("❌ 原始 %d 条 → 归并后 %d 个问题：" % (len(all_rows), len(agg)))
            for i, ((kind, el, by), a) in enumerate(
                    sorted(agg.items(), key=lambda x: -len(x[1]["sizes"])), 1):
                d = a["detail"]
                extra = {k: v for k, v in d.items()
                         if k in ("clippedPx", "h", "w", "maxH", "rect", "z", "pos",
                                  "children", "clientH", "scrollH", "clipped")}
                vs = sorted(a["views"])
                vtxt = "全部 %d 视图（共享组件）" % len(vs) if len(vs) > 4 else ", ".join(vs)
                print("  %2d. [%s] 视口 %s | %s"
                      % (i, kind, ",".join(sorted(a["sizes"])), el.encode("utf-8", "replace").decode("utf-8")))
                if by:
                    print("      被挡： %s" % by.encode("utf-8", "replace").decode("utf-8"))
                print("      范围： %s | 命中 %d 次"
                      % (json.dumps(extra, ensure_ascii=True), a["n"]))
                print("      视图： %s" % vtxt)
        if args.json:
            # ensure_ascii=True：CDP 回传的文本里可能出现未配对的 UTF-16 代理对
            # （emoji 跨帧被拆开），直接按 UTF-8 写会 UnicodeEncodeError。
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(all_rows, f, ensure_ascii=True, indent=1)
            print("\n已写出 %s" % args.json)
        return 1 if all_rows else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())

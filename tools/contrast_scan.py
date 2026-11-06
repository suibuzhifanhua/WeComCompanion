# -*- coding: utf-8 -*-
"""tools/contrast_scan.py — 文字对比度扫描器（常驻工具，WCAG 2.1 判据）

为什么要它：`.pill`、`.opt-chip.sel`、`.win .badge` 这类「实色底 + 白字」的芯片
在源码里看是绿的、是白的，数值上 #fff/#07c160 只有 2.38:1——远不到正文最低 4.5:1。
这类问题静态 grep 永远查不出来（同一份色值在不同底色下结论相反），
只能拿真实渲染树逐元素量。本工具用 CDP 驱动 headless Chrome 打开正在跑的 app，
浅/深两套主题 × 全部视图，对每个**自带文字**的可见元素算前景/背景对比度。

前景 = getComputedStyle(color)
背景 = 自身 → 祖先逐层 alpha 合成（渐变取全部色标，按最差色标判）
判据 = 正文 4.5:1；大字（≥24px，或 ≥18.66px 且字重 ≥700）3:1
归并 = 同一 (选择器, 前景, 背景, 主题) 只报一次，附命中次数——
        侧栏/芯片族在 13 个视图里重复出现是同一个问题，不该占 13 个坑。

用法：
    python tools/contrast_scan.py                       # 源码实例（8790），浅+深全视图
    python tools/contrast_scan.py --url http://127.0.0.1:8787/   # 对着打好的 exe 复跑
    python tools/contrast_scan.py --themes light --views work
    python tools/contrast_scan.py --min 4.5 --json out.json
    python tools/contrast_scan.py --gate            # 有未达标即退出码 1（CI 用）

退出码：0=无未达标（或未开 --gate）；1=有未达标；2=环境/连接问题。
"""
import argparse
import json
import os
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_occlusion_probe as P   # 复用 CDP 客户端 / Chrome 发现 / 默认视图表

SCAN_JS = r"""
(() => {
  const parse = (s) => {
    if (!s) return null;
    s = String(s).trim();
    if (s === 'transparent' || s === 'none') return null;
    let m = s.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const p = m[1].split(/[\/,\s]+/).filter(x => x).map(parseFloat);
      if (p.length < 3 || p.some(isNaN)) return null;
      return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    }
    m = s.match(/^#([0-9a-fA-F]{3,8})$/);
    if (m) {
      let h = m[1];
      if (h.length === 3 || h.length === 4) h = h.split('').map(c => c + c).join('');
      const n = parseInt(h.slice(0, 6), 16);
      return [(n >> 16) & 255, (n >> 8) & 255, n & 255, h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1];
    }
    return null;
  };
  const gradStops = (img) => {
    const out = [];
    if (!img || img.indexOf('gradient') < 0) return out;
    const re = /(rgba?\([^)]+\)|#[0-9a-fA-F]{3,8})/g;
    let m;
    while ((m = re.exec(img))) { const c = parse(m[1]); if (c) out.push(c); }
    return out;
  };
  const chan = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * chan(c[0]) + 0.7152 * chan(c[1]) + 0.0722 * chan(c[2]);
  const over = (fg, bg) => { const a = fg[3]; return [fg[0] * a + bg[0] * (1 - a), fg[1] * a + bg[1] * (1 - a), fg[2] * a + bg[2] * (1 - a), 1]; };
  const ratio = (a, b) => { const L1 = lum(a), L2 = lum(b); return (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05); };
  const hex = (c) => '#' + [c[0], c[1], c[2]].map(v => Math.round(v).toString(16).padStart(2, '0')).join('');
  const visible = (e) => {
    const c = getComputedStyle(e);
    if (c.display === 'none' || c.visibility === 'hidden' || +c.opacity < 0.15) return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const sel = (e) => {
    const id = e.id ? '#' + e.id : '';
    const cls = (typeof e.className === 'string' && e.className.trim())
      ? '.' + e.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
    let s = e.tagName.toLowerCase() + id + cls;
    // v1.0.614：裸标签（无 id/class 的 span）报出来没法定位——扫描器的产出要能直接跳到源码。
    // 自身无名时向上找最多 3 层带 id/class 的祖先，拼成 "span ← .msg-ops ← .msg"。
    if (!id && !cls) {
      const up = []; let q = e.parentElement, g = 0;
      while (q && up.length < 3 && g++ < 12) {
        const qi = q.id ? '#' + q.id : '';
        const qc = (typeof q.className === 'string' && q.className.trim())
          ? '.' + q.className.trim().split(/\s+/)[0] : '';
        if (qi || qc) { up.push(q.tagName.toLowerCase() + qi + qc); }
        q = q.parentElement;
      }
      if (up.length) s += ' ← ' + up.join(' ← ');
    }
    return s;
  };
  // 自身+祖先的底色逐层合成（元素自身最内层，最后覆盖）
  function bgStack(e) {
    const chain = []; let p = e, guard = 0;
    while (p && p !== document.body && p !== document.documentElement && guard++ < 30) {
      const c = getComputedStyle(p);
      const op = +c.opacity;
      const stops = gradStops(c.backgroundImage);
      if (stops.length) {
        for (const s of stops) chain.push(op < 1 ? [s[0], s[1], s[2], s[3] * op] : s);
      } else {
        const col = parse(c.backgroundColor);
        if (col && col[3] > 0.01) chain.push(op < 1 ? [col[0], col[1], col[2], col[3] * op] : col);
      }
      p = p.parentElement;
    }
    let base = parse(getComputedStyle(document.body).backgroundColor) || [255, 255, 255, 1];
    let acc = [base[0], base[1], base[2], 1];
    for (const l of chain.reverse()) acc = over(l, acc);
    return acc;
  }
  const agg = {};
  let scanned = 0, skipped = 0;  const all = document.querySelectorAll('body *');
  for (const e of all) {
    if (/^(script|style|noscript|svg|path|link|meta|title|textarea|select|option)$/.test(e.tagName.toLowerCase())) continue;
    if (e.closest('#bootOverlay')) continue;               // 启动遮罩淡出中，不参与
    // v1.0.614：禁用态控件豁免。WCAG 1.4.3 明确把"inactive components"排除在外，
    // 而 `button:disabled{opacity:.45}` 是刻意的灰化——补上前景 opacity 合成之后，
    // 若不豁免就会冒出一批"停止按钮未达标"的假阳性（实测 #setStop 1.96:1）。
    // 只豁免真正 disabled 的元素，不豁免"仅带低 opacity 的正常文字"。
    if (e.disabled === true || e.closest('[disabled]')) { skipped++; continue; }
    // v1.0.614：另一类"非活动组件"——本项目的加载态用的是
    // `button.loading{opacity:.6; pointer-events:none}`（js/01-core.js setBtnLoading 不置
    // disabled 属性），点不动但仍参与扫描，实测 light 主题 #compProbeBtn 4.24:1 就是这么报的。
    // WCAG 1.4.3 的 incidental 同样覆盖 inactive component，故对**控件**按 computed
    // pointer-events:none 豁免；普通文字带 pointer-events 不算失效，照常测（不放宽文字判据）。
    const TAGACT = /^(button|input|a)$/i;
    if (TAGACT.test(e.tagName) && getComputedStyle(e).pointerEvents === 'none') { skipped++; continue; }
    if (!visible(e)) { skipped++; continue; }
    const ownTxt = Array.prototype.filter.call(e.childNodes, n => n.nodeType === 3 && n.textContent.trim());
    if (!ownTxt.length) { skipped++; continue; }
    // 判据精修：纯 emoji / 纯几何符号（▾ ☑ 🤖 💬）不是"文字"——
    // emoji 由系统彩色字形渲染，computed color 根本不参与成像；符号是图标。
    // 把它们算进来会淹掉真问题（首轮 20 条里 6 条是这类）。
    const txt = ownTxt.map(n => n.textContent.trim()).join(' ');
    if (!/[\p{L}\p{N}]/u.test(txt)) { skipped++; continue; }
    const c = getComputedStyle(e);
    let fgRaw = parse(c.color);
    if (!fgRaw) continue;
    // v1.0.614：祖先链上的 opacity 必须也合成进**前景**。
    // 原实现只在 bgStack 里把每层底色乘了自己的 opacity，前景原样使用 →
    // 凡靠 opacity 弱化的文字全部漏检（时间戳 .45、关闭符号 .55、.op-target.min .5）。
    // 这些恰恰是最容易掉到 4.5 以下的一族。
    let op = 1, q = e;
    for (let g = 0; q && q.nodeType === 1 && g < 30; g++, q = q.parentElement) {
      const o = parseFloat(getComputedStyle(q).opacity);
      if (!isNaN(o)) op *= o;
    }
    if (op < 1) fgRaw = [fgRaw[0], fgRaw[1], fgRaw[2], fgRaw[3] * op];
    const bg = bgStack(e);
    const fg = over(fgRaw, bg);
    const fs = parseFloat(c.fontSize) || 12;
    const fw = parseInt(c.fontWeight, 10) || 400;
    const large = fs >= 24 || (fs >= 18.66 && fw >= 700);
    const need = large ? 3.0 : 4.5;
    const r = ratio(fg, bg);
    scanned++;
    if (r >= need) continue;
    const k = sel(e) + '|' + hex(fg) + '|' + hex(bg) + '|' + fs + '|' + (large ? 'L' : 'N');
    if (!agg[k]) agg[k] = { sig: sel(e), fg: hex(fg), bg: hex(bg), fs: fs, fw: fw,
                             large: large ? 1 : 0, need: need, min: r, n: 0,
                             sample: (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30) };
    agg[k].n++;
    if (r < agg[k].min) { agg[k].min = r;
      agg[k].sample = (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30); }
  }
  const rows = Object.values(agg).sort((a, b) => a.min - b.min);
  return JSON.stringify({ scanned: scanned, skipped: skipped, fails: rows.length, rows: rows });
})()
"""


SURFACE_LUM_MAX = 0.30

# v1.0.614：`--states` 把"只在异常态才存在的节点"合成出来测一遍。
# 报错气泡、错误徽标、禁用按钮这类节点在正常扫描时 DOM 里根本没有，扫描器再准也采不到——
# 这就是 `.msg.err` 硬编码浅底 + 亮红字（深色下约 2:1）能一路活到本轮的原因。
# 注入的是**真实类名组合**，走的是同一套 computed style，所以判据与常规采样完全一致。
SYNTH_INJECT = r"""
(() => {
  const host = document.querySelector('.chatpane') || document.querySelector('main') || document.body;
  const old = document.getElementById('__synthStates'); if (old) old.remove();
  const box = document.createElement('div');
  box.id = '__synthStates';
  box.innerHTML =
      '<div class="msg err">【合成】错误气泡：这一步没有做成</div>'
    + '<div class="msg agent"><div class="mc">【合成】正常气泡内容</div><span class="tm">12:34</span></div>'
    + '<div><span class="pill err">超时中止</span> <span class="pill wrn">执行中</span> '
    + '<span class="pill ok">启用</span></div>'
    + '<div class="pp-err">【合成】安装进度失败行</div>'
    + '<div class="comp-ocr-status st-err">【合成】组件状态失败行</div>'
    + '<div><button disabled>【合成】禁用按钮</button></div>'
    + '<div class="op-target min">【合成】弱化目标行</div>'
    // v1.0.615：「↓ 回到最新」平时 display:none（只有上翻才显形），正常扫描永远采不到——
    // 正是本项目反复踩的"扫描器盲区"。合成节点在游离 div 里算不出布局，故直接改组件规则：
    // 贴底时靠 JS 写 style.display=none 隐藏，CSS 保持始终可测量。
    + '<div><button class="jump-latest">↓ 回到最新</button></div>'
    // v1.0.618：`.tag.judge`（判断模型徽章）在 618 之前的两次全视图扫描里都是 0 命中——
    // 它只在特定数据下才渲染（本机那次是组件页出现「可升级」才被抓到 3.70:1）。
    // "靠数据才存在"的组件必须常驻合成名单，否则修没修好全靠运气。整族一起补上。
    + '<div><span class="tag judge">判断模型</span> <span class="tag def">默认</span> '
    + '<span class="tag dis">已禁用</span> <span class="tag wrn">待确认</span></div>';
  host.appendChild(box);
  return 1;
})()
"""
SYNTH_REMOVE = "(() => { const b = document.getElementById('__synthStates'); if (b) b.remove(); return 1; })()"

# 第二块台子：深色主题里"没跟上主题的浅色面"。
# 对比度台子只在浅底上放浅字时才会报警；一个亮白的卡片配深字是"读得清但主题坏了"，
# 它照样要抓——判据：自身底色（逐层合成后）相对亮度 > 0.30 即算浅色面。
# 豁免：截图/图片/视频画布（真实页面内容，本来就不是我们的配色）、滚动条与透明底。
SURFACE_JS = r"""
(() => {
  const parse = (s) => {
    if (!s) return null;
    s = String(s).trim();
    if (s === 'transparent' || s === 'none') return null;
    let m = s.match(/rgba?\(([^)]+)\)/);
    if (m) { const p = m[1].split(/[\/,\s]+/).filter(x => x).map(parseFloat);
             if (p.length < 3 || p.some(isNaN)) return null;
             return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
    m = s.match(/^#([0-9a-fA-F]{3,8})$/);
    if (m) { let h = m[1]; if (h.length === 3 || h.length === 4) h = h.split('').map(c => c + c).join('');
             const n = parseInt(h.slice(0, 6), 16);
             return [(n >> 16) & 255, (n >> 8) & 255, n & 255, h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1]; }
    return null;
  };
  const chan = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * chan(c[0]) + 0.7152 * chan(c[1]) + 0.0722 * chan(c[2]);
  const over = (fg, bg) => { const a = fg[3]; return [fg[0]*a+bg[0]*(1-a), fg[1]*a+bg[1]*(1-a), fg[2]*a+bg[2]*(1-a), 1]; };
  const hex = (c) => '#' + [c[0],c[1],c[2]].map(v => Math.round(v).toString(16).padStart(2,'0')).join('');
  const grad = (img) => { const out=[]; if(!img||img.indexOf('gradient')<0) return out;
    const re=/(rgba?\([^)]+\)|#[0-9a-fA-F]{3,8})/g; let m; while((m=re.exec(img))){const c=parse(m[1]); if(c) out.push(c);} return out; };
  function bgStack(e) {
    const chain = []; let p = e, g = 0;
    while (p && p !== document.body && p !== document.documentElement && g++ < 30) {
      const c = getComputedStyle(p); const op = +c.opacity;
      const st = grad(c.backgroundImage);
      if (st.length) { for (const s of st) chain.push(op<1?[s[0],s[1],s[2],s[3]*op]:s); }
      else { const col = parse(c.backgroundColor); if (col && col[3] > 0.01) chain.push(op<1?[col[0],col[1],col[2],col[3]*op]:col); }
      p = p.parentElement;
    }
    const base = parse(getComputedStyle(document.body).backgroundColor) || [255,255,255,1];
    let acc = [base[0], base[1], base[2], 1];
    for (const l of chain.reverse()) acc = over(l, acc);
    return acc;
  }
  const sel = (e) => { const id = e.id ? '#'+e.id : '';
    const cls = (typeof e.className==='string'&&e.className.trim()) ? '.'+e.className.trim().split(/\s+/).slice(0,2).join('.') : '';
    return e.tagName.toLowerCase()+id+cls; };
  const SKIP = /^(img|video|canvas|svg|iframe|input|select|option|script|style)$/i;
  // 企微实时截图、代码高亮块等是"内容"不是"皮肤"，豁免
  const exempt = (e) => /liveShot|screenshot|attach-|hljs|code-block|md-code|(?:^|\s)dot(?:\s|$)/.test(e.className||'') ||
                        e.closest('#liveShot') || e.closest('pre');
  const agg = {};
  let scanned = 0;
  for (const e of document.querySelectorAll('body *')) {
    if (SKIP.test(e.tagName)) continue;
    if (e.closest('#bootOverlay') || exempt(e)) continue;
    const c = getComputedStyle(e);
    if (c.display === 'none' || c.visibility === 'hidden' || +c.opacity < 0.15) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const paints = grad(c.backgroundImage).length > 0 ||
                   ((parse(c.backgroundColor) || [0,0,0,0])[3] > 0.01);
    if (!paints) continue;                   // 只判"自己画了底色"的面
    const bg = bgStack(e); scanned++;
    const L = lum(bg);
    // 判据：亮 且 灰（彩度低）。品牌绿/警示橙这类高彩度实色在深色里是"应有的强调"，
    // 只有近乎白/灰的面才是没跟上主题的浅色块。
    const chroma = Math.max(bg[0], bg[1], bg[2]) - Math.min(bg[0], bg[1], bg[2]);
    if (L <= %s || chroma > 34) continue;
    const k = sel(e) + '|' + hex(bg);
    if (!agg[k]) agg[k] = { sig: sel(e), bg: hex(bg), lum: L, n: 0,
                            sample: (e.textContent||'').trim().replace(/\s+/g,' ').slice(0,26) };
    agg[k].n++;
  }
  const rows = Object.values(agg).sort((a,b) => b.lum - a.lum);
  return JSON.stringify({ scanned: scanned, fails: rows.length, rows: rows });
})()
""" % SURFACE_LUM_MAX


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8790/")
    ap.add_argument("--views", default=",".join(P.DEFAULT_VIEWS))
    ap.add_argument("--themes", default="light,dark")
    ap.add_argument("--size", default="1440x900")
    ap.add_argument("--json", default="")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--gate", action="store_true", help="有未达标即退出码 1")
    ap.add_argument("--surfaces", action="store_true",
                    help="改查「深色主题里的浅色面」：底亮度 > %.2f 即算没跟上主题" % SURFACE_LUM_MAX)
    ap.add_argument("--settle", type=float, default=0.5)
    ap.add_argument("--states", action="store_true",
                    help="额外合成异常态节点（报错气泡/错误徽标/禁用按钮/弱化文字）一起测——这些节点正常扫描时 DOM 里不存在，是扫描器的盲区")
    args = ap.parse_args()
    payload = SURFACE_JS if args.surfaces else SCAN_JS

    views = [v.strip() for v in args.views.split(",") if v.strip()]
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    if args.surfaces:
        themes = ["dark"]     # 「浅色面」只在深色主题下才是问题
    w, h = (int(x) for x in args.size.lower().split("x"))

    port = P.free_port()
    prof = os.path.join(os.environ.get("TEMP", "/tmp"), "wc_contrast_profile")
    proc = None
    try:
        proc = subprocess.Popen(
            [P.find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--hide-scrollbars",
             "--remote-debugging-port=%d" % port, "--user-data-dir=" + prof,
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                print("❌ 没有可用 page target", file=sys.stderr)
                return 2
            tgt = tabs[0]
        cdp = P.CDP(tgt["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Page.navigate", {"url": args.url})
        time.sleep(3.0)
        cdp.send("Emulation.setDeviceMetricsOverride",
                 {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})

        all_rows = []
        total_scanned = 0
        on = "true"
        for theme in themes:
            on = "true" if theme == "dark" else "false"
            for v in views:
                # 先切视图再压主题：renderSettings() 会用 localStorage 里的 darkMode
                # 重新 applyDarkMode（js/11-workspace-kb-study.js:183），
                # 顺序反过来时 settings/help 会在"深色"标签下渲染出浅色，
                # 首轮基线里 6 条 [dark] 浅色对就是这么来的（判据修正，非应用缺陷）。
                cdp.eval_json("try{openView(%r)}catch(e){}" % v)
                time.sleep(args.settle)
                cdp.eval_json("try{localStorage.setItem('darkMode',%s? '1':'0');applyDarkMode(%s)}catch(e){}"
                              % (on, on))
                time.sleep(0.15)
                if args.states:
                    cdp.eval_json(SYNTH_INJECT)
                    time.sleep(0.12)
                raw = cdp.eval_json(payload)
                if args.states:
                    cdp.eval_json(SYNTH_REMOVE)
                if not raw:
                    continue
                d = json.loads(raw)
                total_scanned += d["scanned"]
                for row in d["rows"]:
                    all_rows.append(dict(row, theme=theme, view=v))
                print("  [%s] %-11s 采样 %-5d %s %d"
                      % (theme, v, d["scanned"],
                         "浅色面" if args.surfaces else "未达标组合", d["fails"]))

        print("\n" + "=" * 76)
        if not all_rows:
            print("✅ %s（%d 次采样 / %d 主题 / %d 视图）"
                  % ("深色主题里没有未跟主题的浅色面" if args.surfaces
                     else "全部文字元素对比度达标",
                     total_scanned, len(themes), len(views)))
            return 0
        if args.surfaces:
            agg = {}
            for r in all_rows:
                k = (r["theme"], r["sig"], r["bg"])
                a = agg.setdefault(k, dict(r, n=0, views=set()))
                a["n"] += r["n"]
                a["views"].add(r["view"])
            rows = sorted(agg.values(), key=lambda x: -x["lum"])
            print("❌ 深色主题里的浅色面 %d 种（采样 %d 次）：" % (len(rows), total_scanned))
            for i, r in enumerate(rows[: args.top], 1):
                print("  %2d. %-22s 底 %-8s 亮度 %.3f  命中%-4d 视图:%s  例：%s"
                      % (i, r["sig"][:22], r["bg"], r["lum"], r["n"],
                         "全部" if len(r["views"]) > 4 else ",".join(sorted(r["views"])),
                         r["sample"].encode("utf-8", "replace").decode("utf-8")))
        else:
            agg = {}
            for r in all_rows:
                k = (r["theme"], r["sig"], r["fg"], r["bg"], r["fs"])
                a = agg.setdefault(k, dict(r, n=0, views=set()))
                a["n"] += r["n"]
                a["views"].add(r["view"])
                if r["min"] < a["min"]:
                    a["min"] = r["min"]
                    a["sample"] = r["sample"]
            rows = sorted(agg.values(), key=lambda x: x["min"])
            print("❌ 归并后 %d 个未达标前景/背景组合（原始 %d 条），列出最差的 %d 项："
                  % (len(rows), len(all_rows), min(len(rows), args.top)))
            for i, r in enumerate(rows[: args.top], 1):
                print("  %2d. [%s] %-18s 前景 %s / 底 %s  %.2f:1 (需 %.1f) %spx%s  命中%d  视图:%s"
                      % (i, r["theme"], r["sig"][:18], r["fg"], r["bg"], r["min"], r["need"], r["fs"],
                         " 大字" if r["large"] else "", r["n"],
                         "全部" if len(r["views"]) > 4 else ",".join(sorted(r["views"]))))
                print("      例：%s" % r["sample"].encode("utf-8", "replace").decode("utf-8"))
        if args.json:
            for r in rows:
                r["views"] = sorted(r["views"])
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=True, indent=1)
            print("\n已写出 %s" % args.json)
        return 1 if args.gate else 0
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""tools/scale_audit.py — 字号/圆角/字重「档位」实测（常驻小工具）

审查报告里说"圆角 15 种、字号 19 种"是在数源码字面量；真正该看的是
**渲染出来**有几种尺寸——源码里 12px 和 12.5px 各写几十处，落到屏幕上
就是两个几乎分不出来的字号，视觉噪声全在这里。

用法：
    python tools/scale_audit.py --url http://127.0.0.1:8787/   # 正在跑的正式包（改前）
    python tools/scale_audit.py --url http://127.0.0.1:8790/   # 源码实例（改后）
输出：全部视图 × 浅/深 下，实际渲染的不同 font-size / border-radius / font-weight 值
及各自出现次数，并给出 Top 明细，方便一轮改动前后直接对数字。
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_occlusion_probe as P

AUDIT_JS = r"""
(() => {
  const fs = {}, rad = {}, fw = {};
  const bump = (o, k) => { if (k) o[k] = (o[k] || 0) + 1; };
  const els = document.querySelectorAll('body *');
  for (const e of els) {
    const c = getComputedStyle(e);
    if (c.display === 'none') continue;
    const r = e.getBoundingClientRect();
    if (r.width < 1 && r.height < 1) continue;
    bump(fs, c.fontSize);
    bump(fw, c.fontWeight);
    const br = c.borderRadius;
    // 圆角四角相同才记一档；不同的（胶囊/半圆按钮）原样记
    if (br && br !== '0px') {
      const uniq = Array.from(new Set(br.split(/[\s,]+/)));
      bump(rad, uniq.length === 1 ? uniq[0] : br);
    }
  }
  return JSON.stringify({fs: fs, rad: rad, fw: fw});
})()
"""


def summarize(name, counter):
    vals = sorted(counter.items(), key=lambda x: -x[1])
    total = sum(counter.values())
    print("  %-13s 档位 %-3d 声明 %-4d" % (name, len(counter), total))
    print("      " + "  ".join("%s×%d" % (k, n) for k, n in vals[:26]))
    return len(counter)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8790/")
    ap.add_argument("--views", default=",".join(P.DEFAULT_VIEWS))
    ap.add_argument("--size", default="1440x900")
    args = ap.parse_args()
    views = [v.strip() for v in args.views.split(",") if v.strip()]
    w, h = (int(x) for x in args.size.lower().split("x"))

    port = P.free_port()
    prof = os.path.join(os.environ.get("TEMP", "/tmp"), "wc_scale_profile")
    proc = subprocess.Popen(
        [P.find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--hide-scrollbars",
         "--remote-debugging-port=%d" % port, "--user-data-dir=" + prof, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import requests
    try:
        ver = None
        for _ in range(60):
            try:
                ver = requests.get("http://127.0.0.1:%d/json/version" % port, timeout=2).json()
                break
            except Exception:
                time.sleep(0.5)
        if not ver:
            print("❌ Chrome 调试端口未起", file=sys.stderr)
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
            tgt = tabs[0]
        cdp = P.CDP(tgt["webSocketDebuggerUrl"])
        cdp.send("Page.enable"); cdp.send("Runtime.enable")
        cdp.send("Page.navigate", {"url": args.url})
        time.sleep(3.0)
        cdp.send("Emulation.setDeviceMetricsOverride",
                 {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
        fs, rad, fw = {}, {}, {}
        for theme in ("light", "dark"):
            for v in views:
                cdp.eval_json("try{openView(%r)}catch(e){}" % v)
                time.sleep(0.4)
                cdp.eval_json("try{localStorage.setItem('darkMode',%s);applyDarkMode(%s)}catch(e){}"
                              % ("'1'" if theme == "dark" else "'0'", "true" if theme == "dark" else "false"))
                time.sleep(0.15)
                d = json.loads(cdp.eval_json(AUDIT_JS) or "{}")
                for k, n in (d.get("fs") or {}).items():
                    fs[k] = fs.get(k, 0) + n
                for k, n in (d.get("rad") or {}).items():
                    rad[k] = rad.get(k, 0) + n
                for k, n in (d.get("fw") or {}).items():
                    fw[k] = fw.get(k, 0) + n
        print("实测 %s（%d 视图 × 2 主题，同一元素在多视图重复计数）" % (args.url, len(views)))
        a = summarize("font-size", fs)
        b = summarize("border-radius", rad)
        c = summarize("font-weight", fw)
        print("\n档位合计：字号 %d 种 / 圆角 %d 种 / 字重 %d 种" % (a, b, c))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())

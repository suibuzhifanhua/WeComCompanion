# -*- coding: utf-8 -*-
"""tools/xform_fields.py — 把 OA（xform）表单里的 fd_xxxx 字段读成「标签 + 值」。

为什么需要：OA 月报详情是 oa.example.com 上的 xform 表单，字段名是
`extendDataFormInfo.value(fd_3d11ce00e2be8a)` 这种**不透明 id**，而批审脚本一直靠
正文正则取「应运维天数 / 在用计算机数量」——遇到"值在表单控件里、正文无值"的页面
就会 miss（现有代码里 page_pcs 的那条注释就是这个坑）。直读表单字段比正则稳。

用法：
    python tools/xform_fields.py                    # 列出当前打开的月报详情页字段
    python tools/xform_fields.py --url-like kmReviewMain --json build/xform_map.json
输出：[{key, label, value, type}]，label 由"同行前序单元格 / 上一行同列单元格"推断。
只读：只做 Runtime.evaluate 读 DOM，不点、不改、不提交。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_occlusion_probe as P   # noqa: E402

JS = r"""
(() => {
  const out = {url: location.href, title: document.title, fields: []};
  const clean = (s) => String(s || '').trim().replace(/\s+/g, ' ');
  // xform 表格常见"表头行在上、值行在下"，而且表头第一格是小节标题（列数比值行多 1）
  // → 按"上一行同列"取标签会整体错位一格（实测把 115 标成小节名「设备管理总体情况」）。
  // 这里改成按**水平投影重叠最大**的表头单元格取标签，与 colspan/错位无关。
  const labelOf = (el) => {
    const td = el.closest('td');
    if (!td) return clean(el.getAttribute('aria-label') || el.name || '');
    const r = td.getBoundingClientRect();
    const isNum = (t) => !t || /^[0-9.,%\s-]+$/.test(t);
    // ① 同行左侧非数字单元格（"标签：值"式布局）
    for (let p = td.previousElementSibling, i = 0; p && i < 3; p = p.previousElementSibling, i++) {
      const t = clean(p.innerText);
      if (!isNum(t) && t.length <= 40) return t;
    }
    // ② 向上最多 6 行，取与本列水平重叠最多的单元格
    const tr = td.closest('tr');
    const table = tr && tr.closest('table');
    if (table) {
      let row = tr.previousElementSibling;
      for (let up = 0; row && up < 6; up++, row = row.previousElementSibling) {
        let best = "", bestW = 0;
        for (const c of Array.from(row.cells || [])) {
          const cr = c.getBoundingClientRect();
          const w = Math.min(r.right, cr.right) - Math.max(r.left, cr.left);
          const t = clean(c.innerText);
          if (w > bestW && !isNum(t) && t.length <= 40) { best = t; bestW = w; }
        }
        if (best) return best;
      }
    }
    return '';
  };
  document.querySelectorAll('input, textarea, select').forEach(e => {
    if (out.fields.length >= 120) return;
    const nm = e.name || e.id || '';
    const ids = nm.match(/fd_[0-9a-f]{8,}/g) || [];
    if (!ids.length) return;
    const tr = e.closest('tr');
    out.fields.push({key: ids[ids.length - 1], type: (e.type || e.tagName).toLowerCase(),
                     value: clean(e.value).slice(0, 60), label: labelOf(e),
                     rowText: clean(tr && tr.innerText).slice(0, 120)});
  });
  return JSON.stringify(out);
})()
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--url-like", default="kmReviewMain", help="按 URL/标题子串选页；空=第一个非空页")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    import requests
    try:
        ts = requests.get("http://127.0.0.1:%d/json" % args.port, timeout=5).json()
    except Exception as e:  # noqa: BLE001
        print("❌ 连不上 CDP %d：%s（企微页面需带调试端口）" % (args.port, e))
        return 2
    pages = [t for t in ts if t.get("type") == "page" and (t.get("url") or "").startswith("http")]
    if args.url_like:
        hit = [t for t in pages if args.url_like in (t.get("url") or "") or args.url_like in (t.get("title") or "")]
    else:
        hit = pages
    if not hit:
        print("❌ 没有匹配的页面。当前：", [(t.get("title") or "")[:26] for t in pages])
        return 2
    rows = []
    for t in hit:
        try:
            cdp = P.CDP(t["webSocketDebuggerUrl"])
            cdp.send("Runtime.enable")
            d = json.loads(cdp.eval_json(JS) or "{}")
            cdp.ws.close()
        except Exception as e:  # noqa: E402
            print("   （跳过 %s：%s）" % ((t.get("title") or "")[:20], str(e)[:60]))
            continue
        fs = d.get("fields") or []
        print("[%s] %s —— 字段 %d 个" % ((d.get("title") or "")[:28], (d.get("url") or "")[:70], len(fs)))
        for f in fs:
            print("   %-24s 值=%-14s 标签=%s"
                  % (f["key"], (f["value"] or "（空）")[:14], f.get("label") or "（未识别）"))
        rows.append({"url": d.get("url"), "title": d.get("title"), "fields": fs})
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print("已写出 %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())

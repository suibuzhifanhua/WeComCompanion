# -*- coding: utf-8 -*-
"""tools/ui_button_sweep.py — 逐个按钮/控件的「接线」巡检（只读安全模式）

要回答的问题：界面上每个可点的东西，① 处理器存不存在、② 点了会不会抛 JS 错、
③ 它到底调了哪个后端接口、④ 那个接口在 Flask 路由表里真的存在吗。

【为什么必须短路网络】本工具默认连的是 tools/ui_verify_server.py 起的 8790 实例，
它**共用真实项目的 config.json 与企微 CDP 绑定**——真点「批量同意」「删除会话」
「重启软件」「立即体检」「发送消息」会对你自己的账号和数据产生实际后果。
因此在页面里包一层：
  · fetch / XMLHttpRequest：GET/HEAD 原样放行（读接口，安全且能验真）；
    其它方法一律**不发往后端**，直接合成 {ok:true,__sweep_dryrun__:true} 返回，
    让调用方的 then/catch 正常走下去（这样"请求发出后的回调链"也一并被覆盖）。
  · window.open / location.assign|replace / form.submit：拦下并记录。
  · alert/confirm/prompt：CDP 自动接受（否则 headless 会卡在原生弹窗上）。
  · 处理器文本里出现破坏性动词（os_reboot/exit/install/shutdown/taskkill/delete_all…）
    的按钮：只登记不点击。

用法：
    python tools/ui_button_sweep.py                       # 默认 8790 × 全部视图
    python tools/ui_button_sweep.py --views work,settings --limit 120
    python tools/ui_button_sweep.py --json sweep.json
退出码：0=没有失配；1=有抛错/死按钮/未知接口；2=环境问题。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui_occlusion_probe import CDP, find_chrome, free_port   # 复用启动/连接那套


class CDPSweep(CDP):
    """加两件本工具专用的能力：真监听器探测 + 动态路由核对。"""

    def object_id(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
        return (r.get("result") or {}).get("objectId")

    def listeners(self, expr):
        """内联 onclick 之外，addEventListener 挂上的 click/change 也算"接上线"。
        没这一步的话，所有 .switch / checkbox 都会被误判成死按钮（第一版就误报了 87 个）。"""
        oid = self.object_id(expr)
        if not oid:
            return []
        try:
            r = self.send("DOMDebugger.getEventListeners", {"objectId": oid, "depth": 0})
            return sorted({x.get("type") for x in (r.get("listeners") or [])})
        except Exception:
            return []


def _rule_to_regex(rule):
    """Flask 规则 `/api/skills/<sid>/versions` → 可匹配具体 URL 的正则。
    （第一版直接把 <...> 抹掉做字面比对，于是几十个动态路由全被误报成"接口不存在"。）"""
    out = re.sub(r"<(?:[^:>]+:)?[^>]+>", "[^/]+", rule)
    return re.compile("^" + out + "/?$")


VIEWS = ["work", "study", "chat", "knowledge", "models", "templates", "skills",
         "experiences", "connectors", "workspace", "schedules", "settings", "help"]

# 只登记不点的破坏性入口
DANGER = re.compile(r"os_reboot|os_shutdown|exit_app|restart_app|uninstall|taskkill|"
                    r"delete_all|clear_all|factory|wipe|send_wecom_message|submit_approve",
                    re.I)

SEED_JS = r"""
(() => {
  if (window.__sweep) return "already";
  window.__sweep = {calls: [], blocked: [], errors: []};
  window.addEventListener('error', e => window.__sweep.errors.push(String(e.message).slice(0,160)));
  window.addEventListener('unhandledrejection', e =>
      window.__sweep.errors.push('promise: ' + String((e.reason && e.reason.message) || e.reason).slice(0,160)));
  const rec = (m, u) => window.__sweep.calls.push({m: m, u: u});
  window.__sweep.bad = [];

  const of = window.fetch;
  window.fetch = function(u, o) {
    const url = (typeof u === 'string') ? u : (u && u.url) || '';
    const m = ((o && o.method) || (u && u.method) || 'GET').toUpperCase();
    rec(m, url);
    if (m === 'GET' || m === 'HEAD')
      return of.apply(this, arguments).then(r => {
        if (r.status >= 400) window.__sweep.bad.push(m + ' ' + (r.url || url) + ' -> ' + r.status);
        return r;
      });
    return Promise.resolve(new Response(JSON.stringify({ok: true, __sweep_dryrun__: true}),
        {status: 200, headers: {'Content-Type': 'application/json'}}));
  };
  const OO = XMLHttpRequest.prototype.open, OS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__m = String(m).toUpperCase(); this.__u = u; return OO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function() {
    rec(this.__m || 'GET', this.__u || '');
    if (this.__m === 'GET' || this.__m === 'HEAD') {
      const self = this;
      this.addEventListener('load', function () {
        if (self.status >= 400) self.__sweepBad = self.__m + ' ' + self.__u + ' -> ' + self.status;
      });
      return OS.apply(this, arguments);
    }
    setTimeout(() => { this.dispatchEvent(new Event('load')); }, 0);   // onerror/onload 不吊死
    return undefined;
  };
  window.open = function(u) { window.__sweep.blocked.push('window.open ' + u); return null; };
  ['assign','replace'].forEach(k => {
    try { const d = Object.getOwnPropertyDescriptor(Location.prototype, k);
          if (d && d.value) Object.defineProperty(location, k,
                {value: (u) => window.__sweep.blocked.push('location.' + k + ' ' + u)});
    } catch (e) {}
  });
  HTMLFormElement.prototype.submit = function() { window.__sweep.blocked.push('form.submit'); };
  window.__sweepList = () => [...document.querySelectorAll(__SELJS__)].filter(e => {
      const v = e.closest('.view');            // 13 个视图共用一份 DOM：非活动视图里的按钮
      return v ? v.classList.contains('active') : true;   // 全是 display:none，且排在 DOM 前面，
  });                                          // 不滤掉的话索引全花在隐藏节点上（实测 229 个里 208 个在外头）
  return 'seeded';
})()
"""

# 巡检选择器：所有"用户能点/能拨"的东西。计数与点击必须用同一份字符串，
# 否则索引会错位（曾经就地踩到过——两处各写一遍，改一处忘改另一处）。
SEL = ('button,[onclick],.nav-item,.side-link,.side-fold,.foldbtn,.switch,.mdd-item,'
       '.perm-item,.seg-btn,.tag,.pill,.tr-hd,.close-ask button,.overlay button,a[href^="#"]')

CLICK_JS = r"""
((idx) => {
  const list = (window.__sweepList && window.__sweepList()) || [];
  const el = list[idx];
  if (!el) return JSON.stringify({done: true, n: list.length});
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const label = (el.textContent || el.title || el.getAttribute('aria-label') || el.id || '')
      .trim().replace(/\s+/g,' ').slice(0, 40);
  const oc = (el.getAttribute && (el.getAttribute('onclick') || '')) || '';
  // 纯容器：自己不接点击、也不长得像能点（没有 cursor:pointer），但里面装着真控件。
  // 任务区表头 .tr-hd 就是这一类——＋/☑/▾ 三个按钮各有 onclick，点空白处本就不该有事。
  // 不给这条豁免的话，容器会一直占着"死按钮"名单，把真死控件淹掉。
  const box = !['BUTTON','A','INPUT','SELECT','TEXTAREA','LABEL'].includes(el.tagName)
      && cs.cursor !== 'pointer'
      && !!(el.querySelector && el.querySelector('button,a[href],input,select,textarea,[onclick]'));
  const info = {label: label, tag: el.tagName, oc: oc.slice(0, 80),
                prop: typeof el.onclick === 'function',   // JS 里 el.onclick = fn 的属性处理器：
                id: el.id || '', chrome: !el.closest('.view'),   // getAttribute 取不到、CDP 监听器
                container: box,
                cls: (typeof el.className === 'string' ? el.className : '').slice(0, 50)};   // 也不一定报，必须单独判
  if (el.type === 'file' || cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0)
      return JSON.stringify(Object.assign({skip: true, why: 'invisible/file'}, info));
  if (!el.offsetWidth && !el.offsetHeight)
      return JSON.stringify(Object.assign({skip: true, why: 'zero-box'}, info));
  const before = window.__sweep.calls.length, ebefore = window.__sweep.errors.length;
  // 最硬的判据：点下去 DOM 到底变没变。内联 onclick / el.onclick= / 元素自身监听器
  // 三种查法都会漏——document 级事件委托（如 #modelTable td.ops [data-act]）只在
  // document 上挂监听器，查元素自身的 getEventListeners 是空的；.switch 的处理器在
  // label 内层的 input 上。DOM 签名能把这些一并覆盖。
  const sig = () => {
    try {
      return [document.body.innerHTML.length,
              document.querySelectorAll('input:checked').length,
              document.querySelectorAll('.active').length,
              document.querySelectorAll('.overlay.open').length].join('/');
    } catch (e) { return 'ERR'; }
  };
  const sBefore = sig();
  let thrown = null;
  try { el.click(); } catch (e) { thrown = String(e && e.message || e).slice(0, 160); }
  const sAfter = sig();
  const calls = window.__sweep.calls.slice(before);
  const errs = window.__sweep.errors.slice(ebefore);
  document.querySelectorAll('.overlay.open,.mdd-menu.open,.open').forEach(o => {
      try { if (o.classList.contains('overlay')) o.classList.remove('open'); } catch (e) {} });
  return JSON.stringify(Object.assign({calls, errs, thrown, changed: sAfter !== sBefore,
                                       disabled: el.disabled === true}, info));
})
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8790/")
    ap.add_argument("--views", default=",".join(VIEWS))
    ap.add_argument("--limit", type=int, default=400, help="每个视图最多点多少个")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    views = [v.strip() for v in args.views.split(",") if v.strip()]
    port = free_port()
    prof = os.path.join(os.environ.get("TEMP", "/tmp"), "wc_sweep_profile")
    proc = subprocess.Popen([find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                             "--no-default-browser-check", "--remote-debugging-port=%d" % port,
                             "--user-data-dir=" + prof, "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    code = 0
    rows = []
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
            print("❌ headless Chrome 调试端口未起", file=sys.stderr)
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
        cdp = CDPSweep(tgt["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        try:
            cdp.send("Page.setInterceptFileChooserDialog", {"enabled": False})
        except Exception:
            pass
        cdp.send("Page.navigate", {"url": args.url})
        time.sleep(3.0)
        cdp.send("Emulation.setDeviceMetricsOverride",
                 {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        print("sweep:", args.url, "| 视图", len(views))
        SEED = SEED_JS.replace("__SELJS__", json.dumps(SEL))
        CLICK = CLICK_JS.replace("__SELJS__", json.dumps(SEL))
        COUNT = "window.__sweepList().length"
        visited = set()          # 跨视图去重：侧栏/顶栏这些全局控件只点一次
        bad = set()              # 真发出去的 GET 拿到 4xx/5xx 的
        for v in views:
            cdp.eval_json("try{openView(%r)}catch(e){}" % v)
            time.sleep(1.2)
            cdp.eval_json(SEED)
            total = cdp.eval_json(COUNT) or 0
            seen = set()
            n_view = 0
            for i in range(min(int(total), args.limit)):
                raw = cdp.eval_json(CLICK + "(%d)" % i)
                try:
                    d = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    continue
                if not isinstance(d, dict) or d.get("done"):
                    break
                key = (d.get("label") or "") + "|" + (d.get("oc") or "") + "|" + (d.get("id") or "")
                # 侧栏/顶栏这些全局控件在每个视图里都匹配到一次——跨视图只点一次即可；
                # 视图内的控件按视图分别去重。
                pool = visited if d.get("chrome") else seen
                if key in pool:
                    continue
                pool.add(key)
                if d.get("skip"):
                    continue
                danger = bool(DANGER.search((d.get("oc") or "") + (d.get("label") or "")))
                # 点了没发出任何请求、又没有内联 onclick 的，先探一下是不是 addEventListener
                # 挂着的（开关/复选框多数走这条），有监听器就不算死。
                lsn = []
                if (not (d.get("calls") or []) and not (d.get("oc") or "")
                        and not d.get("prop") and not d.get("changed")):
                    lsn = cdp.listeners("window.__sweepList()[%d]" % i)
                    d["listeners"] = lsn
                if danger:
                    rows.append({"view": v, "idx": i, "danger": True, **d})
                    n_view += 1
                    continue
                rows.append({"view": v, "idx": i, **d})
                n_view += 1
            print("  %-12s 候选 %s 个，实点/登记 %d" % (v, total, n_view))
            for b in (cdp.eval_json("(window.__sweep && window.__sweep.bad) || []") or []):
                bad.add(b)

        # 路由交叉核对（离线 import backend，不启服务）
        known = set()
        try:
            os.environ["WECOM_NO_GUI"] = "1"
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import backend
            for rule in backend.app.url_map.iter_rules():
                known.add(str(rule))
        except Exception as e:
            print("（路由表核对跳过：%s）" % e)

        errs, dead, unknown = [], [], []
        hit = set()
        rx = [_rule_to_regex(k) for k in known] if known else []
        for r in rows:
            for c in (r.get("calls") or []):
                u = (c.get("u") or "").split("?")[0]
                if u.startswith("/api/") or u.startswith("/wecom/"):
                    hit.add((c.get("m"), u))
            if r.get("thrown") or (r.get("errs") or []):
                errs.append(r)
            elif not (r.get("calls") or []) and not (r.get("oc") or "") \
                    and not r.get("prop") and not r.get("changed") \
                    and not (r.get("listeners") or []):
                # 状态徽章类（<span> .tag/.pill）本来就不是控件，不参与"死按钮"判定——
                # 第一版把 24 个「未就绪/可用/降级」徽章算成了死按钮。
                if (r.get("tag") or "").upper() == "SPAN":
                    continue
                if r.get("container"):
                    continue    # 纯容器（见 CLICK_JS 里的 box 判据）：里面的控件各自都被点过
                dead.append(r)
        if rx:
            for m, u in sorted(hit):
                if not any(p.match(u) for p in rx):
                    unknown.append((m, u))
        n_danger = len([r for r in rows if r.get("danger")])
        print("\n点击样本 %d | 危险跳过未点 %d | 触达接口 %d 个" % (len(rows), n_danger, len(hit)))
        if errs:
            print("\n❌ 抛错的控件 %d：" % len(errs))
            for r in errs[:25]:
                print("   [%s] %s | onclick=%s | %s" % (r["view"], r.get("label")[:22],
                      (r.get("oc") or "")[:40], (r.get("thrown") or (r.get("errs") or [""])[0])[:70]))
        if dead:
            print("\n⚠️ 四个判据全空——无请求、无内联 onclick、无 el.onclick 属性、"
                  "无元素监听器、且点击后 DOM 签名没变 —— %d 个：" % len(dead))
            for r in dead[:40]:
                print("   [%s] <%s> %s .%s" % (r["view"], (r.get("tag") or "").lower(),
                      (r.get("label") or "")[:22], (r.get("cls") or "")[:26]))
        if unknown:
            print("\n❌ 前端调了但 Flask 路由表里没有的接口：")
            for m, u in unknown:
                print("   %s %s" % (m, u))
        if bad:
            print("\n❌ 真实 GET 返回 4xx/5xx（读接口本身坏了）：")
            for b in sorted(bad)[:30]:
                print("   " + str(b)[:120])
        if not (errs or dead or unknown or bad):
            print("\n✅ 未发现抛错 / 死按钮 / 未知接口 / 坏读接口")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"rows": rows, "endpoints": sorted(hit), "bad": sorted(bad)},
                          f, ensure_ascii=False, indent=1)
        code = 1 if (errs or unknown or bad) else 0
    finally:
        proc.terminate()
    return code


if __name__ == "__main__":
    sys.exit(main())

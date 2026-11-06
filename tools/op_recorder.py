# -*- coding: utf-8 -*-
"""tools/op_recorder.py — 人工操作录制器（只读观察，用来把"我点一遍"变成脚本/提示词）

为什么需要它：批量流程（如月报批审）里有些步骤只有用户点一遍才说得清——
点哪个入口、弹了哪个层、哪个字段要填、最后按哪个按钮。让 agent 猜元素容易点错，
而"你操作一遍我来记"最快。

两条观察通道（用户选定"两者都要"）：
  ① 注入只记录监听器：在页面里装一个 capture 阶段的 click / change 监听，
     把每次点击的元素特征（tag/id/class/name/文本/href）与输入框改名记进 window.__opRec。
     **只记录，不拦截、不 preventDefault、不派发任何事件**；停止时把监听器摘掉。
  ② 轮询快照差分：每 --interval 秒读一次页面状态（URL/标题/可见按钮文本/表单字段值/弹层），
     与上一帧做差集 → 抓到"不经点击发生的变化"（自动跳转、异步出数、弹窗出现）。
  另加：监视 产物库/下载 目录，新出现的文件也记一条（附件下载类步骤）。

输出：--out 目录下
  trace.jsonl  逐条事件（机器可读，供我转成脚本/提示词）
  steps.md     人类可读的步骤时间线
  结束打印事件计数与文件路径。

安全边界（写死在这里，不给调用方绕）：
  · 只做 Runtime.evaluate 的读/记录，绝不调用 Input.dispatchMouseEvent、
    绝不 location 跳转、绝不 form.submit、绝不改任何字段值；
  · 只连 CDP 的 page target，不动 OS 窗口（不最小化、不抢焦点）；
  · 注入代码带 __opRecInstalled 幂等标记，重复轮询不会叠加监听器；
  · 页面导航后 __opRec 丢失 → 自动重装（同时记一条 re-inject 事件）。

用法：
    python tools/op_recorder.py                       # 起录，Ctrl+C 结束
    python tools/op_recorder.py --url-like nemp --interval 0.4
    python tools/op_recorder.py --out build/op_trace_run1 --watch-downloads ""   # 不监视下载
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_occlusion_probe as P   # 复用 CDP 客户端与 Chrome 探测

INSTALL_JS = r"""
(() => {
  if (window.__opRecInstalled) return 'already';
  window.__opRec = [];
  const feat = (el) => {
    const attr = (n) => (el.getAttribute && el.getAttribute(n)) || '';
    const txt = (el.innerText || el.value || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    let p = el, path = [];
    for (let i = 0; i < 4 && p && p.tagName; i++) {
      path.unshift(p.tagName.toLowerCase() + (p.id ? '#' + p.id : '') +
        ((typeof p.className === 'string' && p.className.trim()) ? '.' + p.className.trim().split(/\s+/).slice(0, 2).join('.') : ''));
      p = p.parentElement;
    }
    return { tag: el.tagName.toLowerCase(), id: attr('id'), cls: attr('class'),
             name: attr('name'), aria: attr('aria-label'), role: attr('role'),
             href: (attr('href') || '').slice(0, 120), text: txt, path: path.join('>') };
  };
  const push = (kind, el, extra) => {
    const e = { t: new Date().toISOString().slice(11, 23), kind: kind, el: feat(el) };
    if (extra) Object.assign(e, extra);
    window.__opRec.push(e);
    if (window.__opRec.length > 400) window.__opRec.shift();
  };
  const onClick = (ev) => { push('click', ev.target || {}, { x: ev.clientX, y: ev.clientY }); };
  const onChange = (ev) => {
    const el = ev.target || {};
    push('input', el, { valueLen: String(el.value == null ? '' : el.value).length,
                        type: (el.type || '') });
  };
  document.addEventListener('click', onClick, true);
  document.addEventListener('change', onChange, true);
  window.__opRecOff = () => {
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('change', onChange, true);
    window.__opRecInstalled = false;
  };
  window.__opRecInstalled = true;
  return 'installed';
})()
"""

DRAIN_JS = "(() => { const a = window.__opRec || []; window.__opRec = []; return JSON.stringify(a); })()"

SNAPSHOT_JS = r"""
(() => {
  const vis = (e) => {
    const c = getComputedStyle(e);
    if (c.display === 'none' || c.visibility === 'hidden' || +c.opacity < 0.05) return false;
    const r = e.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const txt = (e) => (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const btns = [];
  document.querySelectorAll('button, a[role=button], input[type=button], input[type=submit], .btn, [class*=submit]').forEach(e => {
    if (btns.length >= 60) return;
    if (!vis(e)) return;
    const t = txt(e) || (e.value || '').trim();
    if (t) btns.push(t);
  });
  const fields = {};
  document.querySelectorAll('input, textarea, select').forEach(e => {
    if (Object.keys(fields).length >= 60) return;
    if (!vis(e) || e.type === 'hidden') return;
    const k = e.name || e.id || (e.getAttribute('aria-label') || '') || txt(e) || e.type;
    if (!k) return;
    let v = e.value == null ? '' : String(e.value);
    fields[k] = v.length > 24 ? (v.length + '字符') : v;
  });
  const dialogs = [];
  document.querySelectorAll('[role=dialog], .modal, .layui-layer, .ant-modal, .el-dialog, .dialog').forEach(e => {
    if (dialogs.length >= 8) return;
    if (vis(e)) dialogs.push((e.getAttribute('aria-label') || txt(e) || '弹层').slice(0, 30));
  });
  return JSON.stringify({ url: location.href, title: document.title,
                          buttons: btns, fields: fields, dialogs: dialogs });
})()
"""

UNINSTALL_JS = "(() => { try{ window.__opRecOff && window.__opRecOff(); }catch(e){} return 'off'; })()"


def targets(port, url_like):
    import requests
    ts = requests.get("http://127.0.0.1:%d/json" % port, timeout=5).json()
    out = []
    for t in ts:
        if t.get("type") != "page" or not t.get("webSocketDebuggerUrl"):
            continue
        url = t.get("url") or ""
        if url.startswith(("devtools://", "chrome://", "about:")):
            continue
        if url_like and url_like not in url and url_like not in (t.get("title") or ""):
            continue
        out.append(t)
    return out


class Recorder:
    def __init__(self, args):
        self.args = args
        self.conns = {}          # targetId -> (CDP, url, title)
        self.prev = {}           # targetId -> snapshot dict
        self.events = []
        self.t0 = time.time()
        os.makedirs(args.out, exist_ok=True)
        self.fp = os.path.join(args.out, "trace.jsonl")
        self.fh = io_open(self.fp, "a", encoding="utf-8")
        self.downloads = set()
        if args.watch_downloads and os.path.isdir(args.watch_downloads):
            self.downloads = set(os.listdir(args.watch_downloads))

    def emit(self, kind, **kw):
        e = {"t": "%.1fs" % (time.time() - self.t0), "kind": kind}
        e.update(kw)
        self.events.append(e)
        self.fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        self.fh.flush()
        show = e.get("note") or (e.get("el") or {}).get("text") or e.get("title") or ""
        print("  [%s] %-9s %s" % (e["t"], kind, str(show)[:70]))

    def ensure(self):
        """新 target 挂上来 / 页面导航后重装监听器。"""
        try:
            ts = targets(self.args.port, self.args.url_like)
        except Exception as e:  # noqa: BLE001
            self.emit("error", note="CDP /json 不可达：%s（企微页面需要带调试端口，软件里重新绑定后再录）" % e)
            return False
        live = {t["id"] for t in ts}
        for tid, (cdp, url, title) in list(self.conns.items()):
            if tid not in live:
                try:
                    cdp.ws.close()
                except Exception:
                    pass
                self.conns.pop(tid, None)
                self.prev.pop(tid, None)
        for t in ts:
            if t["id"] not in self.conns:
                try:
                    cdp = P.CDP(t["webSocketDebuggerUrl"])
                    cdp.send("Runtime.enable")
                except Exception as e:  # noqa: BLE001
                    self.emit("error", note="attach 失败 %s：%s" % ((t.get("title") or "")[:24], e))
                    continue
                self.conns[t["id"]] = (cdp, t.get("url") or "", t.get("title") or "")
                self.emit("attach", title=(t.get("title") or "")[:60], url=(t.get("url") or "")[:120])
            cdp = self.conns[t["id"]][0]
            try:
                st = cdp.eval_json(INSTALL_JS)
                if st == "installed":
                    self.emit("re-inject", title=self.conns[t["id"]][2][:60],
                              note="监听器已装载（页面刷新/新开）")
            except Exception:
                pass
        return True

    def poll(self):
        for tid, (cdp, url, title) in list(self.conns.items()):
            try:
                raw = cdp.eval_json(DRAIN_JS)
                for e in (json.loads(raw) if raw else []):
                    self.emit(e.pop("kind", "click"), el=e.get("el"), **{k: v for k, v in e.items()
                                                                        if k not in ("el", "kind", "t")})
                snap = cdp.eval_json(SNAPSHOT_JS)
                snap = json.loads(snap) if snap else None
            except Exception as ex:  # noqa: BLE001
                self.emit("error", note="读取失败（页面可能已关闭）：%s" % str(ex)[:60])
                continue
            if not snap:
                continue
            prev = self.prev.get(tid)
            if prev is None:
                self.emit("baseline", title=snap["title"][:60], url=snap["url"][:120],
                          buttons=len(snap["buttons"]), fields=len(snap["fields"]))
            else:
                if prev["url"] != snap["url"]:
                    self.emit("nav", url=snap["url"][:160], title=snap["title"][:60])
                if set(prev["dialogs"]) != set(snap["dialogs"]):
                    self.emit("dialog", note="弹层变化 %s → %s" % (prev["dialogs"] or "无", snap["dialogs"] or "无"))
                added = [b for b in snap["buttons"] if b not in prev["buttons"]]
                gone = [b for b in prev["buttons"] if b not in snap["buttons"]]
                if added or gone:
                    self.emit("buttons", note="新增%s 消失%s" % (added[:6], gone[:6]))
                chg = {k: v for k, v in snap["fields"].items() if prev["fields"].get(k) != v}
                newf = {k: v for k, v in snap["fields"].items() if k not in prev["fields"]}
                if chg or newf:
                    self.emit("fields", note="变化%s 新出现%s" % (list(chg.items())[:6], list(newf.items())[:6]))
            self.prev[tid] = snap
        d = self.args.watch_downloads
        if d and os.path.isdir(d):
            now = set(os.listdir(d))
            for f in sorted(now - self.downloads):
                self.emit("file", note=f)
            self.downloads = now

    def stop(self):
        for tid, (cdp, url, title) in self.conns.items():
            try:
                cdp.eval_json(UNINSTALL_JS)     # 只摘监听器，不碰页面内容
                cdp.ws.close()
            except Exception:
                pass
        self.conns.clear()
        self.fh.close()
        self.write_steps()

    def write_steps(self):
        lines = ["# 操作录制时间线（tools/op_recorder.py 生成）", "",
                 "- 时长 %.0fs，事件 %d 条" % (time.time() - self.t0, len(self.events)),
                 "- 原始数据：`%s`" % self.fp, ""]
        for e in self.events:
            k = e["kind"]
            if k == "click":
                el = e.get("el") or {}
                lines.append("- **%s 点击** `%s` 文本「%s」 路径 `%s`%s"
                             % (e["t"], el.get("tag"), el.get("text") or el.get("aria") or el.get("name") or "",
                                el.get("path"), "（href=%s）" % el.get("href")[:60] if el.get("href") else ""))
            elif k == "input":
                el = e.get("el") or {}
                lines.append("- %s 输入 `%s`（name=%s，长度 %s）" % (e["t"], el.get("text"), el.get("name"), e.get("valueLen")))
            elif k == "nav":
                lines.append("- **%s 跳转** → %s（%s）" % (e["t"], e.get("url"), e.get("title")))
            elif k in ("dialog", "buttons", "fields", "file", "attach", "baseline", "re-inject", "error"):
                lines.append("- %s %s %s" % (e["t"], k, e.get("note") or e.get("title") or ""))
        with io_open(os.path.join(self.args.out, "steps.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def io_open(*a, **k):
    return open(*a, newline="", **k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="CDP 端口（默认读软件配置，兜底 9222）")
    ap.add_argument("--url-like", default="", help="只录 URL/标题含该子串的页面")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--out", default="")
    ap.add_argument("--max-seconds", type=float, default=0, help="到时自动停（0=一直录到 Ctrl+C）")
    ap.add_argument("--watch-downloads", default="", help="监视该目录新出现的文件（默认 产物库/下载）")
    args = ap.parse_args()

    if not args.port:
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from core.config_store import cdp_port
            args.port = cdp_port()
        except Exception:
            args.port = 9222
    if not args.out:
        args.out = os.path.join("build", "op_trace_" + time.strftime("%Y%m%d_%H%M%S"))
    if not args.watch_downloads:
        cand = os.path.join("dist", "产物库", "下载")
        args.watch_downloads = cand if os.path.isdir(cand) else ""

    print("[recorder] CDP 端口 %s，输出 %s" % (args.port, args.out))
    print("[recorder] 只读观察：不拦截、不派发事件、不动窗口。Ctrl+C 结束；"
          "后台运行时用 `touch %s` 结束（Windows 的 terminate 是硬杀，会漏掉监听器摘除）"
          % os.path.join(args.out, "STOP"))
    stop_file = os.path.join(args.out, "STOP")
    if os.path.exists(stop_file):
        os.remove(stop_file)
    r = Recorder(args)
    try:
        if not r.ensure():
            print("[recorder] 连不上 CDP——先在软件里把企微页面打开/绑定（需要带调试端口），再重跑。")
            return 2
        while True:
            r.ensure()
            r.poll()
            time.sleep(args.interval)
            if os.path.exists(stop_file):
                print("\n[recorder] 收到 STOP 信号，正在摘除监听器…")
                break
            if args.max_seconds and (time.time() - r.t0) > args.max_seconds:
                print("\n[recorder] 到达 --max-seconds 上限，自动收尾。")
                break
    except KeyboardInterrupt:
        print("\n[recorder] 收到 Ctrl+C，正在摘除监听器…")
    finally:
        r.stop()
        if os.path.exists(stop_file):
            try:
                os.remove(stop_file)
            except OSError:
                pass
    print("[recorder] 事件 %d 条 → %s" % (len(r.events), os.path.join(args.out, "steps.md")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

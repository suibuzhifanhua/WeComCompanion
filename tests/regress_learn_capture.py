# -*- coding: utf-8 -*-
"""课件采集（learn_capture）回归测试 —— 一键验证 iframe 翻页三条路径。

为什么需要：课件采集的翻页逻辑全靠 CDP 在**同源 iframe** 内操作，
真实课件（学习平台）依赖登录态，没法随时手测。
本脚本用本地 fixture（父页面 + 同源 iframe + canvas 阅读器，5 页）
驱动无头浏览器跑完整采集流程，断言三种翻页方式都能采满 5 页：

  dom   —— iframe 内点「下一页」按钮
  key   —— 只响应键盘 PageDown（无翻页按钮）
  input —— 只有页码输入框（合成 input 事件 + Enter）

用法（项目根目录）：
  python tests/regress_learn_capture.py

退出码 0=全通过，1=有失败。
"""
import os
import sys
import time
import shutil
import socket
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixture")
HTTP_PORT = 8123
CDP_PORT = 9333
EXPECT_PAGES = 5

sys.path.insert(0, ROOT)

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _find_browser():
    for p in BROWSERS:
        if os.path.exists(p):
            return p
    return None


def _port_free(port):
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return False
    except Exception:  # noqa: BLE001
        return True
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def _wait_cdp(port, timeout=25):
    import requests
    end = time.time() + timeout
    while time.time() < end:
        try:
            requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def _find_fixture_page(port):
    """返回 fixture 页面在 _pages() 里的序号（headless 首个 target 可能是同步引导页）。"""
    from wecom_bridge.windows import list_targets, _pages
    for i, t in enumerate(_pages(list_targets(port))):
        if "fixture" in (t.get("url") or ""):
            return i
    return None


def main():
    browser = _find_browser()
    if not browser:
        print("FAIL 未找到 Edge/Chrome，无法回归")
        return 1
    if not _port_free(HTTP_PORT):
        print(f"FAIL 端口 {HTTP_PORT} 被占用（本地静态服务器）")
        return 1
    if not _port_free(CDP_PORT):
        print(f"FAIL 端口 {CDP_PORT} 被占用（无头浏览器 CDP）")
        return 1

    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(HTTP_PORT), "--bind", "127.0.0.1"],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    profile = tempfile.mkdtemp(prefix="_regress_edge_")
    edge = subprocess.Popen([
        browser, "--headless=new", f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*", f"--user-data-dir={profile}",
        "--window-size=1000,760", "--no-first-run", "--disable-gpu",
        "http://127.0.0.1:%d/tests/fixture/index.html" % HTTP_PORT,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok_all = True
    try:
        if not _wait_cdp(CDP_PORT):
            print("FAIL 无头浏览器 CDP 未就绪")
            return 1
        time.sleep(2.0)

        from wecom_bridge.frames import open_session
        from services import learn_capture as lc

        pick = _find_fixture_page(CDP_PORT)
        if pick is None:
            print("FAIL 找不到 fixture 页面")
            return 1
        print(f"fixture 页面 pick={pick}")

        for mode in ("dom", "key", "input"):
            # 带时间戳 query 强制真重载（只改 hash 不会重新加载页面）
            s = open_session(CDP_PORT, pick)
            s._send("Page.navigate", {"url": "http://127.0.0.1:%d/tests/fixture/index.html?t=%f#%s"
                                            % (HTTP_PORT, time.time(), mode)})
            s.close()
            time.sleep(3.0)
            r = lc.capture_course(pick=pick, port=CDP_PORT, name="_regress_" + mode, mode="pdf")
            got = r.get("pages") if r.get("ok") else -1
            ok = (got == EXPECT_PAGES)
            ok_all = ok_all and ok
            print(f"  mode={mode:<6} pages={got}/{EXPECT_PAGES}  {'PASS' if ok else 'FAIL'}  {r.get('error', '')}")
    finally:
        edge.terminate()
        srv.terminate()
        try:
            edge.wait(timeout=8)
        except Exception:  # noqa: BLE001
            edge.kill()
        try:
            srv.wait(timeout=5)
        except Exception:  # noqa: BLE001
            srv.kill()
        shutil.rmtree(profile, ignore_errors=True)
        # 清理回归产物
        kb = os.path.join(ROOT, "产物库", "知识")
        for m in ("dom", "key", "input"):
            p = os.path.join(kb, f"_regress_{m}.md")
            if os.path.exists(p):
                os.remove(p)

    print("RESULT", "ALL PASS" if ok_all else "FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())

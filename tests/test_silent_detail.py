# -*- coding: utf-8 -*-
"""v1.0.296：月报审核「后台静默开详情」控制流分支测试。

只测控制流（静默成功 / 无 href 回退 / 空页回退 / 空 url），用 monkeypatch 顶替
wecom_bridge 的 CDP 调用，不碰真实 CDP/企微（沙箱无 9222）。
运行：python tests/test_silent_detail.py  或  pytest tests/test_silent_detail.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import batch_flow
import wecom_bridge as wb

# --- 用例隔离（2026-09-11 修）---------------------------------------------------
# 下面各用例用「模块级属性赋值」顶替 wecom_bridge / batch_flow 的 CDP 调用，但从不还原。
# pytest 按定义序执行时，test_silent_empty_fallback 留下的假 open_silent_page
# （返回 {"ok":True,"index":4}）会污染其后运行的 test_open_silent_page_empty_url，
# 使其误判「空 url 未返回 ok:False」——直跑脚本时该用例恰在 __main__ 最前，故此前未暴露。
# 这里在导入时快照两模块属性，每个用例开头还原，保证任意执行顺序都互不干扰。
_SNAPSHOT = [(m, dict(vars(m))) for m in (wb, batch_flow)]


def _isolate():
    """每个用例开头调用：把 wecom_bridge / batch_flow 还原到导入时的模块状态。"""
    for m, snap in _SNAPSHOT:
        vars(m).clear()
        vars(m).update(snap)


def test_silent_success():
    """静默成功：取到 href + open ok + 有内容 → 返回静默页 index，绝不触发前台 click、不关页。"""
    _isolate()
    calls = {"open": 0, "click": 0, "close": 0}
    batch_flow._get_full_href = lambda *a, **k: "https://oa.example.com/detail?id=123456"
    batch_flow._detail_has_content = lambda *a, **k: True

    def _open(url, port=9222, timeout=8.0):
        calls["open"] += 1
        return {"ok": True, "target_id": "T1", "index": 3, "title": "详情", "url": url[:50]}

    def _click(pick, target, port=9222):
        calls["click"] += 1
        return {"ok": True}

    def _close(fp, port=9222):
        calls["close"] += 1
        return {"ok": True}

    wb.open_silent_page = _open
    wb.wait_page_load = lambda *a, **k: {"ok": True}
    wb.click_element = _click
    wb.close_page = _close
    # v1.0.425 标题锚定：fp 取静默页的 title——mock 掉 _pages（原测试隐式依赖
    # 真实 CDP 9222 的 target 列表，离开开发机必挂）
    batch_flow._pages = lambda *a, **k: [{"title": f"页面{i}", "url": "https://x/"} for i in range(5)]

    issues = []
    fp = batch_flow._open_detail(9222, 0, {"text": "月报A", "index": 5}, issues)
    # v1.0.555（P1-6 targetId 寻址）：fp 从"标题锚定"升级为 "#tid:<target_id>"——
    # index 漂移与同名页碰撞（OAuth 中转页标题全同）彻底免疫。
    assert fp == "#tid:T1", f"应返回 targetId pick（v1.0.555 targetId 寻址），实际 {fp}"
    assert calls["open"] == 1 and calls["click"] == 0, f"静默成功不应 click：{calls}"
    assert calls["close"] == 0, f"静默成功不应关页：{calls}"
    print("PASS silent_success：静默后台开页成功（targetId 寻址），未触发前台 click、未关页")


def test_no_href_fallback_click():
    """无完整 href（javascript:/# 链接）→ 不开静默页，回退前台 click。"""
    _isolate()
    calls = {"open": 0, "click": 0}
    batch_flow._get_full_href = lambda *a, **k: None

    def _open(url, port=9222, timeout=8.0):
        calls["open"] += 1
        return {"ok": True, "index": 3}

    def _click(pick, target, port=9222):
        calls["click"] += 1
        return {"ok": True}

    wb.open_silent_page = _open
    batch_flow._pages = lambda *a, **k: []
    wb.read_page = lambda *a, **k: {"links": [], "body": ""}
    wb.wait_page_load = lambda *a, **k: {"ok": True}
    wb.click_element = _click
    wb._set_auto_pick(None)   # v1.0.588：跟随窗按 pane 隔离，走 setter 而非赋值快照

    issues = []
    fp = batch_flow._open_detail(9222, 0, {"text": "月报B", "index": 5}, issues)
    assert calls["open"] == 0, f"无 href 不应开静默页：{calls}"
    assert calls["click"] >= 1, f"应回退前台 click：{calls}"
    print("PASS no_href_fallback：无完整 href → 回退前台 click，未开静默页")


def test_silent_empty_fallback():
    """静默开页成功但后台页无有效内容 → 关静默页 + 回退前台 click。"""
    _isolate()
    calls = {"open": 0, "click": 0, "close": 0}
    batch_flow._get_full_href = lambda *a, **k: "https://oa.example.com/x"
    batch_flow._detail_has_content = lambda *a, **k: False

    def _open(url, port=9222, timeout=8.0):
        calls["open"] += 1
        return {"ok": True, "index": 4}

    def _click(pick, target, port=9222):
        calls["click"] += 1
        return {"ok": True}

    def _close(fp, port=9222):
        calls["close"] += 1
        return {"ok": True}

    wb.open_silent_page = _open
    wb.wait_page_load = lambda *a, **k: {"ok": True}
    wb.close_page = _close
    wb.click_element = _click
    batch_flow._pages = lambda *a, **k: []
    wb.read_page = lambda *a, **k: {"links": [], "body": ""}
    wb._set_auto_pick(None)   # v1.0.588：跟随窗按 pane 隔离，走 setter 而非赋值快照

    issues = []
    fp = batch_flow._open_detail(9222, 0, {"text": "月报C", "index": 5}, issues)
    assert calls["open"] == 1 and calls["close"] == 1, f"空静默页应关页：{calls}"
    assert calls["click"] >= 1, f"应回退前台 click：{calls}"
    assert any("回退" in s for s in issues), f"应记录回退 issue：{issues}"
    print("PASS silent_empty_fallback：静默页无内容 → 关页并回退 click")


def test_open_silent_page_empty_url():
    """open_silent_page 空/非法 url → 直接返回 ok:False，不发请求。"""
    _isolate()
    r = wb.open_silent_page("", port=9222)
    assert isinstance(r, dict) and r.get("ok") is False, f"空 url 应 ok:False：{r}"
    r2 = wb.open_silent_page(None, port=9222)
    assert isinstance(r2, dict) and r2.get("ok") is False, f"None url 应 ok:False：{r2}"
    print("PASS open_silent_page_empty_url：空/非法 url 直接失败，不发请求")


if __name__ == "__main__":
    test_open_silent_page_empty_url()
    test_silent_success()
    test_no_href_fallback_click()
    test_silent_empty_fallback()
    print("\n== 全部通过：静默开详情控制流（成功/无href回退/空页回退/空url）分支正确 ==")

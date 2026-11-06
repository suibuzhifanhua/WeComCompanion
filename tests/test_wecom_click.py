# -*- coding: utf-8 -*-
"""企微点击域回归锁（归并自 tests/test_v592_click.py + tests/test_v623_click_speed.py）。

归并说明（批次2试点，v1.0.645 之后）：两文件同锁 `wecom_bridge.read` 的点击链路
（click_element / _post_click_wait），原按版本各立一户。现按域合并，原版本出处
保留在各 class docstring——这套编年史是本项目的命脉，合并不以丢失出处为代价。

域内三层锁：
  · TestClickOnceSemantics —— v1.0.592：一次调用只能点一下（JS 生效不补真实点击）
  · TestPostClickWait      —— v1.0.623①-④：合并采样、命中即返回、耐心不缩水
  · TestClickStructure     —— v1.0.623 结构侧：串行盲等不得回到点击/读页路径
"""

import inspect
import json
import time

import pytest

import wecom_bridge.read as R
import wecom_bridge.windows as W

_PRE = '["u",100,0,0,0,0,0,0,0]'     # 点击前指纹基线
_POST = '["u",100,1,0,0,1,0,0,0]'    # 点击后指纹（勾选数/highlight 0→1）
_OK_JS = {"ok": True, "method": "selector", "tag": "span", "x": 1005, "y": 259}


def _no_wait(monkeypatch):
    """把点击后合并采样的等待参数调到 0。

    语义锁（点几下）不验耗时；耗时由 TestPostClickWait 的计时锁负责。"""
    monkeypatch.setattr(R, "CLICK_WAIT_BUDGET", 0.0)
    monkeypatch.setattr(R, "CLICK_WAIT_STEP", 0.0)
    monkeypatch.setattr(R, "REAL_CLICK_WAIT_BUDGET", 0.0)
    monkeypatch.setattr(R, "REAL_CLICK_WAIT_STEP", 0.0)


def _click_harness(monkeypatch, sig_after):
    """把 click_element 的 CDP 底座全部换成桩，只记录它发了哪些真实鼠标事件。"""
    fired = []
    state = {"n": 0}

    def fake_eval(ws_url, expr, timeout=6.0, await_promise=False):
        if expr == R._CLICK_SIG_JS:
            state["n"] += 1
            # 第 1 次 = 点击前指纹，第 2 次 = 点击后指纹
            return _PRE if state["n"] == 1 else sig_after
        return None

    def fake_eval_retry(ws_url, expr, retries=3, timeout=6.0):
        return json.dumps(_OK_JS)

    def fake_command(ws_url, method, params=None, timeout=6.0):
        fired.append(method)
        return {}

    monkeypatch.setattr(R, "_page_ws", lambda *a, **k: ("ws://fake", {"id": "T1"}))
    monkeypatch.setattr(R, "_require_page", lambda *a, **k: (None, "ws://fake", {"id": "T1"}))   # v1.0.654 A6 配套
    monkeypatch.setattr(R, "_cdp_eval", fake_eval)
    monkeypatch.setattr(R, "_cdp_eval_retry", fake_eval_retry)
    monkeypatch.setattr(R, "_cdp_command", fake_command)
    monkeypatch.setattr(R, "list_targets",
                        lambda port: [{"id": "T1", "url": "u", "type": "page"}])
    monkeypatch.setattr(R, "_pages", lambda ts: ts)
    monkeypatch.setattr(R, "_scan_new_page", lambda port, ids, urls: (None, None))
    _no_wait(monkeypatch)
    monkeypatch.setattr(R, "_logw", lambda *a, **k: None)
    return fired


class TestClickOnceSemantics:
    """v1.0.592（原 tests/test_v592_click.py）：click_element 一次调用只能点一下。

    背景（真实事故）：财务审核任务里 agent 反复「点待办行 → 点查看单据」却永远进不了详情。
    根因：JS 分支已 target.click()，随后又无条件用 CDP Input.dispatchMouseEvent 在同一坐标
    补点一次。对「点一下选中、再点取消」的切换型元素，两次点击自相抵消，选中态归零，
    工具却回 ok:true + real_click:true，模型只能判定"页面没反应"，连试 3 轮触软预算、0/5。
    判据改为「JS 点击是否已改变页面状态」（_CLICK_SIG_JS 指纹）：变了就不补点，没变才补
    （保留 v1.0.263 MUI 那类"只认真实 input 事件"的降级路径）。"""

    def test_js_click_that_changed_state_is_not_followed_by_real_click(self, monkeypatch):
        """页面状态确实变了（选中行/勾选框）→ 绝不能再补一次真实点击。"""
        fired = _click_harness(monkeypatch, _POST)
        out = R.click_element(0, "BX2610-05379", 9222)
        assert out.get("ok") is True
        assert "Input.dispatchMouseEvent" not in fired, \
            "JS 点击已生效却又补发真实点击 = 一次调用点两下（切换型元素会被自己抵消）"

    def test_js_click_with_no_effect_still_falls_back_to_real_click(self, monkeypatch):
        """JS 点击毫无效果（MUI 那一类）→ 仍须降级为真实鼠标点击，v1.0.263 行为不回退。"""
        fired = _click_harness(monkeypatch, _PRE)
        out = R.click_element(0, "任务列表项", 9222)
        assert out.get("ok") is True
        assert fired.count("Input.dispatchMouseEvent") >= 3, \
            "JS 点击没生效时必须仍走真实鼠标补点，否则 MUI 组件点不动"
        assert out.get("real_click") is True

    def test_real_click_coordinates_refined_to_clickable_container(self, monkeypatch):
        """补点路径的返回契约不变：命中真实点击时仍报 real_click 与坐标，供模型自检。"""
        fired = _click_harness(monkeypatch, _PRE)
        out = R.click_element(0, "任务列表项", 9222)
        assert out.get("real_xy") and isinstance(out["real_xy"], list)
        assert "Input.dispatchMouseEvent" in fired

    def test_flaky_fingerprint_does_not_suppress_the_fallback_click(self, monkeypatch):
        """指纹取失败（_cdp_eval 返回 {"__error__":…} 字典）不能当成「页面已变化」。

        否则一次 CDP 抖动就会把 v1.0.263 的降级补点一起跳过，MUI 组件又点不动。"""
        fired = []

        def fake_eval(ws_url, expr, timeout=6.0, await_promise=False):
            if expr == R._CLICK_SIG_JS:
                return {"__error__": "CDP 评估超时：页面未在限定时间内响应"}
            return None

        monkeypatch.setattr(R, "_page_ws", lambda *a, **k: ("ws://fake", {"id": "T1"}))
        monkeypatch.setattr(R, "_require_page", lambda *a, **k: (None, "ws://fake", {"id": "T1"}))   # v1.0.654 A6 配套
        monkeypatch.setattr(R, "_cdp_eval", fake_eval)
        monkeypatch.setattr(R, "_cdp_eval_retry",
                            lambda ws_url, expr, retries=3, timeout=6.0: json.dumps(_OK_JS))
        monkeypatch.setattr(R, "_cdp_command",
                            lambda ws_url, method, params=None, timeout=6.0: fired.append(method) or {})
        monkeypatch.setattr(R, "list_targets",
                            lambda port: [{"id": "T1", "url": "u", "type": "page"}])
        monkeypatch.setattr(R, "_pages", lambda ts: ts)
        monkeypatch.setattr(R, "_scan_new_page", lambda port, ids, urls: (None, None))
        _no_wait(monkeypatch)
        monkeypatch.setattr(R, "_logw", lambda *a, **k: None)

        out = R.click_element(0, "任务列表项", 9222)
        assert out.get("ok") is True
        assert "Input.dispatchMouseEvent" in fired, "指纹取不到时必须退回原补点行为，不能静默跳过"
        assert not out.get("js_effective")

    def test_parsed_list_fingerprint_still_counts_as_state_change(self, monkeypatch):
        """returnByValue 把 JS 的 JSON 字符串直接解析成 list 时，同样要能判出「状态变了」。"""
        fired = []
        seq = {"n": 0}

        def fake_eval(ws_url, expr, timeout=6.0, await_promise=False):
            if expr == R._CLICK_SIG_JS:
                seq["n"] += 1
                return (["u", 100, 0, 0, 0, 0, 0, 0, 0] if seq["n"] == 1
                        else ["u", 100, 1, 0, 0, 1, 0, 0, 0])
            return None

        monkeypatch.setattr(R, "_page_ws", lambda *a, **k: ("ws://fake", {"id": "T1"}))
        monkeypatch.setattr(R, "_require_page", lambda *a, **k: (None, "ws://fake", {"id": "T1"}))   # v1.0.654 A6 配套
        monkeypatch.setattr(R, "_cdp_eval", fake_eval)
        monkeypatch.setattr(R, "_cdp_eval_retry",
                            lambda ws_url, expr, retries=3, timeout=6.0: json.dumps(_OK_JS))
        monkeypatch.setattr(R, "_cdp_command",
                            lambda ws_url, method, params=None, timeout=6.0: fired.append(method) or {})
        monkeypatch.setattr(R, "list_targets",
                            lambda port: [{"id": "T1", "url": "u", "type": "page"}])
        monkeypatch.setattr(R, "_pages", lambda ts: ts)
        monkeypatch.setattr(R, "_scan_new_page", lambda port, ids, urls: (None, None))
        _no_wait(monkeypatch)
        monkeypatch.setattr(R, "_logw", lambda *a, **k: None)

        out = R.click_element(0, "BX2610-05379", 9222)
        assert "Input.dispatchMouseEvent" not in fired
        assert out.get("js_effective") is True


def _wait_probe(monkeypatch, scan=(None, None), sig_seq=(_POST,)):
    """把合并采样的两个探针换成桩——计时锁绝不能依赖本机真实 CDP 端口。

    sig_seq 按采样轮次取值，取尽后重复最后一个（None 表示指纹取不到）。"""
    state = {"scan": 0, "sig": 0}

    def _scan(port, before_ids, before_urls):
        state["scan"] += 1
        return scan

    def _eval(ws_url, expr, timeout=6.0, await_promise=False):
        i = min(state["sig"], len(sig_seq) - 1)
        state["sig"] += 1
        return sig_seq[i]

    monkeypatch.setattr(R, "_scan_new_page", _scan)
    monkeypatch.setattr(R, "_cdp_eval", _eval)
    return state


class TestPostClickWait:
    """v1.0.623（原 tests/test_v623_click_speed.py ①-④）：
    点击后的等待从「三段串行盲等」改成「合并采样、命中即返回」。

    背景（实测数据）：一天工具台账 click_wecom_element 90 次、中位 1.20s、p90 3.70s、
    累计 154.5s；read_wecom_page 100 次中位 0.40s。点击比读页贵 3 倍，多出来的不是
    CDP 往返，而是三段互不重叠的死等：sleep(0.3) → _find_new_page(3×0.3) → 单独取指纹。
    同页点击（选中行/勾选框/切页签）永远不开新页，于是每次都把 1.2s 等满——中位数正落在这。"""

    def test_state_change_returns_fast_without_waiting_new_page(self, monkeypatch):
        """①同页点击：状态指纹变了就立刻返回，不再等满「新页轮询」的那 0.9s。"""
        _wait_probe(monkeypatch, sig_seq=(_POST,))
        t0 = time.time()
        np, idx, jshad = R._post_click_wait(9222, "ws://x", ["t1"], ["u"], _PRE)
        el = time.time() - t0
        assert (np, idx) == (None, None)
        assert jshad is True
        # 旧形状是 sleep(0.3) + 3×0.3 轮询 + 一次指纹 = 1.2s 起
        assert el < 0.75, f"状态已变化却仍等了 {el:.2f}s（合并采样失效，退回串行盲等）"

    def test_nothing_happens_still_spends_the_full_detection_window(self, monkeypatch):
        """②什么都没发生：仍等满预算——提速不能顺手砍掉「再等等看」的耐心。"""
        _wait_probe(monkeypatch, sig_seq=(_PRE,))
        t0 = time.time()
        np, idx, jshad = R._post_click_wait(9222, "ws://x", ["t1"], ["u"], _PRE,
                                           budget=R.CLICK_WAIT_BUDGET, step=R.CLICK_WAIT_STEP)
        el = time.time() - t0
        assert (np, idx, jshad) == (None, None, False)
        assert el >= R.CLICK_WAIT_BUDGET * 0.9, f"没变化时提前放弃了（{el:.2f}s），新页可能只是开得慢"

    def test_new_page_wins_over_state_fingerprint(self, monkeypatch):
        """③新页命中优先返回：跟随窗必须切到新页，不能被「同页状态也变了」带跑。"""
        page = {"id": "t9", "url": "https://oa/detail", "title": "详情"}
        monkeypatch.setattr(R, "_scan_new_page", lambda port, ids, urls: (page, 3))
        monkeypatch.setattr(R, "_cdp_eval",
                            lambda ws_url, expr, timeout=6.0, await_promise=False: _POST)
        np, idx, _jshad = R._post_click_wait(9222, "ws://x", ["t1"], ["u"], _PRE)
        assert np is page and idx == 3

    def test_state_change_rescans_pages_before_returning(self, monkeypatch):
        """③补扫：指纹变了的那一轮，返回前再扫一次页面列表——同页变化+新开标签同时
        发生时（点列表项弹出详情标签），漏扫会让后续 read 打在老窗口上。"""
        seen = {"n": 0}
        newpage = ({"id": "t9", "url": "https://oa/detail"}, 2)

        def _scan(port, before_ids, before_urls):
            seen["n"] += 1
            # 第 1 次扫描（正常轮询）没有新页；补扫那次才出现（模拟慢开的新标签）
            return newpage if seen["n"] >= 2 else (None, None)

        monkeypatch.setattr(R, "_scan_new_page", _scan)
        monkeypatch.setattr(R, "_cdp_eval",
                            lambda ws_url, expr, timeout=6.0, await_promise=False: _POST)
        np, idx, jshad = R._post_click_wait(9222, "ws://x", ["t1"], ["u"], _PRE,
                                            budget=10.0, step=0.05)
        assert jshad is True, "指纹变化应判为已生效"
        assert np == newpage[0] and idx == 2, f"补扫没做/没采用：{np} {idx}"

    def test_sample_count_is_capped_when_sleep_is_stubbed(self, monkeypatch):
        """④回归锁的回归锁：time.sleep 被打桩成空操作时，采样按 budget/step 封顶，
        否则 deadline 永不触发 → 变成带真 eval 的忙等轮（历史测试惯于这样打桩）。"""
        calls = {"n": 0}

        def _scan(port, before_ids, before_urls):
            calls["n"] += 1
            return (None, None)

        monkeypatch.setattr(R, "_scan_new_page", _scan)
        monkeypatch.setattr(R, "_cdp_eval",
                            lambda ws_url, expr, timeout=6.0, await_promise=False: _PRE)
        monkeypatch.setattr(R.time, "sleep", lambda s: None)
        R._post_click_wait(9222, "ws://x", ["t1"], ["u"], _PRE, budget=2.0, step=0.5)
        assert calls["n"] == 5, f"采样次数没按 budget/step 封顶：{calls['n']}（会忙等）"

    def test_no_snapshot_falls_back_to_the_single_render_pause(self, monkeypatch):
        """拿不到窗口快照也没有指纹基线时，保持旧版 0.3s 单发等待，不做无意义轮询。"""
        slept = []
        monkeypatch.setattr(R.time, "sleep", lambda s: slept.append(s))
        np, idx, jshad = R._post_click_wait(9222, None, [], [], None)
        assert (np, idx, jshad) == (None, None, False)
        assert slept and max(slept) == pytest.approx(0.3), f"应单发 0.3s 渲染窗口，实际 {slept}"


class TestClickStructure:
    """v1.0.623 结构侧（原 tests/test_v623_click_speed.py 后半）：
    串行盲等确实从点击/读页路径里消失了。"""

    def test_click_path_has_no_blind_sleeps(self):
        src = inspect.getsource(R.click_element)
        assert "time.sleep(0.3)" not in src, "click_element 里仍留着固定 0.3s 盲等"
        assert "time.sleep(0.5)" not in src, "真实鼠标补点后仍留着固定 0.5s 盲等"
        assert "_post_click_wait(" in src, "点击后的等待没走合并采样"
        assert "REAL_CLICK_WAIT_BUDGET" in src, "补点路径的检测窗口预算丢了（应仍覆盖 ~2.4s）"

    def test_read_page_retry_waits_on_event_not_clock(self):
        src = inspect.getsource(R._read_page_data)
        assert "time.sleep(1.0)" not in src, "读到空页重读前仍先盲等 1s（等的是 _wait_body_stable，重复）"
        assert "_wait_body_stable(" in src, "删盲等时把事件等待一起删了——空页分诊会退化成读一次就走"

    def test_empty_reread_behavior_waits_on_stability_only(self, monkeypatch):
        """行为锁（grep 之外）：真的驱动一次空读→重读，看它等的是什么。"""
        empty = json.dumps({"text": "", "fields": []})
        full = json.dumps({"text": "正文已经加载完毕" * 20, "fields": []})
        seq = [empty, full]
        state = {"n": 0}
        slept, waited = [], []

        def _eval(ws_url, expr, timeout=6.0, await_promise=False):
            i = min(state["n"], len(seq) - 1)
            state["n"] += 1
            return seq[i]

        monkeypatch.setattr(R, "_cdp_eval", _eval)
        monkeypatch.setattr(R, "_wait_body_stable", lambda *a, **k: waited.append(k) or True)
        monkeypatch.setattr(R, "_logw", lambda *a, **k: None)
        monkeypatch.setattr(R.time, "sleep", lambda s: slept.append(s))

        data = R._read_page_data("ws://x", {"title": "t", "url": "u"})
        assert waited, "空读后没走正文稳定等待，直接返回失真快照"
        assert not slept, f"重读前仍在盲等固定时长 {slept}（应只靠 _wait_body_stable）"
        assert len(data.get("text", "")) > 100, "重读结果没被采用"

    def test_find_new_page_reuses_the_single_scan_helper(self, monkeypatch):
        """_find_new_page 仍可用（多处调用），但判据只剩一份实现，不再两处各写一遍。"""
        wsrc = inspect.getsource(W._find_new_page)
        assert "_scan_new_page(" in wsrc, "轮询版没复用单次扫描，两份判据会漂移"
        monkeypatch.setattr(W, "list_targets", lambda port=None: [])
        assert W._find_new_page(9222, set(), set(), retries=2, interval=0.01) == (None, None)
        # 同一份判据：新 target 出现时必须认出（不只是"轮询壳"）
        monkeypatch.setattr(W, "list_targets",
                            lambda port=None: [{"id": "t2", "url": "https://oa/d", "type": "page",
                                                "webSocketDebuggerUrl": "ws://x/t2"}])
        _p, _i = W._find_new_page(9222, {"t1"}, {"https://oa/list"}, retries=1, interval=0.01)
        assert _p is not None and _p["id"] == "t2", f"新 target 没被认出：{_p} {_i}"

    def test_click_element_end_to_end_uses_merged_wait(self, monkeypatch):
        """整链冒烟：桩掉 CDP 底座后，一次「同页选中」点击不应触发真实鼠标补点，
        且返回 js_effective（v1.0.592 契约）——合并采样换掉判据载体后这条仍成立。"""
        ok = {"ok": True, "method": "selector", "tag": "span", "x": 100, "y": 200}
        fired = []
        sigcalls = {"n": 0}

        def _eval(ws_url, expr, timeout=6.0, await_promise=False):
            if expr != R._CLICK_SIG_JS:
                return None
            sigcalls["n"] += 1
            # 第 1 次 = 点击前基线，之后 = 点击后（选中态 0→1）
            return _PRE if sigcalls["n"] == 1 else _POST

        monkeypatch.setattr(R, "_page_ws", lambda *a, **k: ("ws://fake", {"id": "T1"}))
        monkeypatch.setattr(R, "_require_page", lambda *a, **k: (None, "ws://fake", {"id": "T1"}))   # v1.0.654 A6 配套
        monkeypatch.setattr(R, "_cdp_eval", _eval)
        monkeypatch.setattr(R, "_cdp_eval_retry", lambda *a, **k: json.dumps(ok))
        monkeypatch.setattr(R, "_cdp_command", lambda *a, **k: fired.append(a[1]) or {})
        monkeypatch.setattr(R, "list_targets",
                            lambda port=None: [{"id": "T1", "url": "u", "type": "page"}])
        monkeypatch.setattr(R, "_pages", lambda ts: ts)
        monkeypatch.setattr(R, "_scan_new_page", lambda port, ids, urls: (None, None))
        monkeypatch.setattr(R, "_logw", lambda *a, **k: None)
        for name in ("CLICK_WAIT_BUDGET", "CLICK_WAIT_STEP",
                     "REAL_CLICK_WAIT_BUDGET", "REAL_CLICK_WAIT_STEP"):
            monkeypatch.setattr(R, name, 0.0)

        out = R.click_element(0, "BX2610-05379", 9222)
        assert out.get("ok") is True
        assert out.get("js_effective") is True, "状态已变应报 js_effective，否则模型会按「没点着」重复点"
        assert not fired, f"一次调用又补发了真实鼠标事件：{fired}"

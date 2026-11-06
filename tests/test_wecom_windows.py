# -*- coding: utf-8 -*-
"""企微窗口域回归锁（归并自 tests/test_v594_tucker.py + tests/test_v629_tuck_owner.py）。

按域合并说明：两文件同锁「window-tucker 静默收纳」这一条红线——
**只收本软件自己开的窗口**。原按版本各立一户，判据是逐版加严的连续过程，
放在一个文件里才能看清"三道闸"的全貌；版本出处保留在各 class docstring。

三道闸（缺一即复现用户现场：自己点的页面一打开就被缩回任务栏）：
  · 归属基线差集 —— 只有"开页之后新出现"的 hwnd 可收
  · 真正绑定     —— /json 可读但 WS 被 403 拒时算未绑定，零动作
  · 点击/静默开页登记 —— 登记即消耗、有 TTL，攒着等于把红线往后无限延
"""

import inspect
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wecom_bridge.windows as W      # noqa: E402
import wecom_bridge.read as RD        # noqa: E402


@pytest.fixture
def spy(monkeypatch):
    """最小桩：只盯"到底最小化了谁"（v594 那组用）。"""
    calls = []
    monkeypatch.setattr(W, "_minimize_hwnds", lambda hs: (calls.append(list(hs)), len(hs))[1])
    monkeypatch.setattr(W, "_logw", lambda *a, **k: None)
    return calls


@pytest.fixture
def rig(monkeypatch):
    """完整桩：把 tucker 的叶子调用全换成可控桩，并清空开页登记队列（v629 那组用）。"""
    calls = {"min_hwnds": [], "min_title": [], "hwnds": ["U1", "U2"]}
    monkeypatch.setattr(W, "_wxwork_visible_hwnds", lambda: list(calls["hwnds"]))
    monkeypatch.setattr(W, "_minimize_hwnds",
                        lambda hs: (calls["min_hwnds"].append(list(hs)), len(hs))[1])
    monkeypatch.setattr(W, "minimize_windows_by_title",
                        lambda t, only=None: (calls["min_title"].append(t), [t])[1])
    monkeypatch.setattr(W, "list_targets", lambda port=None, **kw: [])
    monkeypatch.setattr(W, "_logw", lambda *a, **k: None)
    monkeypatch.setattr(W, "_silent_mode", lambda: True)
    # 默认"已绑定"：把探测缓存填成新鲜值（ts 非 0 且在 TTL 内），
    # 否则 _tuck_binding_ok 会真去打 WebSocket——本机是 403，测试会被环境带倒。
    monkeypatch.setattr(W, "_bind_probe", {"ts": W.time.monotonic(), "ok": True})
    W._open_marks.clear()
    W._unbound_warned["flag"] = False
    calls["state"] = {"known_hwnds": {"U1", "U2"}}
    calls["monkeypatch"] = monkeypatch
    return calls


class TestTuckerGate:
    """v1.0.594（原 tests/test_v594_tucker.py）：收纳只能在 agent 任务执行中发生。

    真实故障（用户实报）：开着软件时企微独立窗口一打开就一秒左右缩回任务栏，
    "不管有没有绑定企微"，日志每 1.2s 一条「静默收纳 1 个新企微窗口（CDP停摆兜底）」。
    v1.0.570 定过「只有 agent 任务执行中才收纳」的门禁，但只写在 CDP 路径的判断链上；
    CDP 停摆时另有一条纯 Win32 PID 差分兜底分支直接调收纳，绕过门禁——而没绑定时
    CDP 必然不可用，恰好只有这条会跑，于是用户自己点的每个页面都被收。
    门禁做成收纳入口的 busy 入参后，两条路径都必须传，测试直接打在入口上。"""

    def test_idle_must_not_minimize_anything(self, spy):
        known, n = W._tuck_fresh_hwnds([11, 12, 13], {11}, "CDP停摆兜底", busy=False,
                                       owner_base=set())
        assert spy == [], "空闲状态下仍然最小化了窗口 —— 就是用户报的那个现象"
        assert n == 0
        assert known == {11, 12, 13}, "空闲时也要把基线刷成当前窗口集，否则任务一开始就追溯收掉已有窗口"

    def test_busy_minimizes_only_new_windows(self, spy):
        known, n = W._tuck_fresh_hwnds([11, 12, 13], {11}, "CDP停摆兜底", busy=True,
                                       owner_base=set())
        assert spy == [[12, 13]], "只应收纳「新出现」的窗口"
        assert n == 2
        assert known == {11, 12, 13}

    def test_busy_but_nothing_new_is_noop(self, spy):
        known, n = W._tuck_fresh_hwnds([11], {11}, "CDP停摆兜底", busy=True, owner_base=set())
        assert spy == []
        assert n == 0
        assert known == {11}

    def test_default_owner_base_is_not_silently_busy(self, spy):
        """v1.0.629 契约反转（照实改断言，不是把测试放宽）：本函数的默认值现在是**不动手**。
        旧默认"传了 busy 就收"要求调用点永远记得带对参数；现在归属基线是必填式入参，
        缺省即不收——`_tucker_tick` 不带基线的调用会静默零动作，而不是误收用户窗口。"""
        W._tuck_fresh_hwnds([21], set(), "PID关联《x》")
        assert spy == [], "没给归属基线却动了窗口"
        W._tuck_fresh_hwnds([21], set(), "开页登记《x》", True, set())
        assert spy == [[21]]


class TestTuckerOwner:
    """v1.0.629（原 tests/test_v629_tuck_owner.py）：静默收纳只能收本软件自己开的窗口。

    现场（2026-10-27 用户报）：**没绑定企微**，但他在企微里点开图片/网页，窗口一出现就被
    缩进任务栏。日志实锤：13:11:58「静默收纳 3 个新企微窗口（PID关联《短标题页》）」，
    而 12:08:10 bind 结果是 bound:false（WS 403）。两个缺口：
      ① 判据是「agent 忙 + 企微 PID 名下出现新窗口」——busy 覆盖整个 LLM 流，且"谁开的"
         这一维从未进判据，于是用户自己点的窗口一起被收；
      ② "绑定"只看 /json 通不通，WS 被 403 拒时照样动手。
    现在收纳要同时过三道闸，本组逐闸打点，外加"点击开页才登记"的结构锁。"""

    # ---------------------------------------------------------------- ① 归属
    def test_no_owner_base_never_touches(self, rig):
        """没有"本软件自己开的"依据（owner_base 缺省 None）→ 一个窗口都不许动。"""
        known, n = W._tuck_fresh_hwnds(["U1", "X9"], {"U1"}, "无登记", busy=True)
        assert rig["min_hwnds"] == [], "无归属登记却最小化了窗口"
        assert n == 0
        assert known == {"U1", "X9"}, "不动手也要把基线刷成当前窗口集"

    def test_owner_base_shields_user_windows(self, rig):
        """开页前基线里的窗口（用户早就自己开着的）永远进不了差集。"""
        known, n = W._tuck_fresh_hwnds(["U1", "U2", "A1"], {"U1"}, "开页登记",
                                       busy=True, owner_base={"U1", "U2"})
        assert rig["min_hwnds"] == [["A1"]], rig["min_hwnds"]
        assert n == 1
        assert known == {"U1", "U2", "A1"}

    def test_tick_without_mark_is_noop(self, rig):
        """v1.0.632（B2）契约：没有开页登记时**一轮什么都不做**——不枚举窗口、不起线程。
        629 在这里每 1.2s 起一个线程刷基线（实测 3,000 线程/小时空转），而归属判据用的
        是登记自带的开页前快照，跨轮基线对"该不该收"没有作用。"""
        rig["hwnds"] = ["U1", "U2", "用户刚点的图片查看器"]
        guards = []
        _orig = W._stage_guard

        def spy_fn(fn, timeout=5.0, label="stage"):
            guards.append(label)
            return _orig(fn, timeout, label)

        rig["monkeypatch"].setattr(W, "_stage_guard", spy_fn)
        assert W._tucker_tick(9222, rig["state"]) == 0
        assert rig["min_hwnds"] == [] and rig["min_title"] == []
        assert guards == [], f"空闲轮不该再打任何 Win32 枚举：{guards}"

    def test_tick_updates_baseline_only_when_it_acts(self, rig):
        """有登记才枚举；枚举后基线要推进，避免同一条登记被后续轮次重复解释。"""
        W.note_agent_open({"U1", "U2"}, target_id="T1", url="http://x", why="点击开页")
        rig["hwnds"] = ["U1", "U2", "A1"]
        assert W._tucker_tick(9222, rig["state"]) == 1
        assert "A1" in rig["state"]["known_hwnds"]
        rig["hwnds"] = ["U1", "U2", "A1", "用户后来点的网页"]
        assert W._tucker_tick(9222, rig["state"]) == 0
        assert rig["min_hwnds"] == [["A1"]]

    def test_title_path_cannot_touch_windows_outside_the_diff(self, rig):
        """v1.0.632（B4）：标题路径只允许命中"登记之后新出现"的窗口——
        用户自己开着的同名窗口（同一张单据开两个窗口）在基线里，不能被标题匹配带走。"""
        seen = {}

        def _by_title(title, only=None):
            seen["only"] = only
            return [title] if only and "A1" in only else []

        rig["monkeypatch"].setattr(W, "minimize_windows_by_title", _by_title)
        rig["monkeypatch"].setattr(W, "list_targets",
                                   lambda port=None, **kw: [{"id": "T1", "title": "付款单详情页面",
                                                       "url": "http://x"}])
        W.note_agent_open({"U1", "U2"}, target_id="T1", url="http://x", why="点击开页")
        rig["hwnds"] = ["U1", "U2", "A1"]
        assert W._tucker_tick(9222, rig["state"]) == 1
        assert seen.get("only") == {"A1"}, f"标题匹配没有限定在差集内：{seen}"

    def test_none_baseline_fails_closed_even_with_title(self, rig):
        """拍不到开页前基线（None）时**一律不收**——标题不足以证明归属，差集才是。"""
        W.note_agent_open(None, target_id="T", url="http://x", why="点击开页")
        rig["hwnds"] = ["U1", "U2", "A1"]
        rig["monkeypatch"].setattr(W, "list_targets",
                                   lambda port=None, **kw: [{"id": "T", "title": "课程详情：第一章",
                                                       "url": "http://x"}])
        assert W._tucker_tick(9222, rig["state"]) == 0
        assert rig["min_hwnds"] == [] and rig["min_title"] == []

    def test_tick_tucks_only_the_agent_opened_window(self, rig):
        """完整一轮：登记时基线含用户两个窗口 → 之后新出现的 A1 被收，U1/U2 不动。"""
        W.note_agent_open({"U1", "U2"}, target_id="T1", url="http://x/detail", why="点击开页")
        rig["hwnds"] = ["U1", "U2", "A1"]
        assert W._tucker_tick(9222, rig["state"]) == 1
        assert rig["min_hwnds"] == [["A1"]], rig["min_hwnds"]

    def test_marks_are_consumed_once(self, rig):
        """登记取出即消耗——同一页不该反复收，也不该留下让后续用户窗口背锅。"""
        W.note_agent_open({"U1"}, target_id="T1", url="u", why="x")
        assert len(W._peek_open_marks()) == 1
        rig["hwnds"] = ["U1", "A1"]
        assert W._tucker_tick(9222, rig["state"]) == 1
        assert W._peek_open_marks() == []
        rig["hwnds"] = ["U1", "A1", "用户后来点的网页"]
        assert W._tucker_tick(9222, rig["state"]) == 0
        assert rig["min_hwnds"] == [["A1"]], "第二条不该再收任何东西"

    def test_mark_expires(self, monkeypatch):
        """过期登记作废（不能攒着，攒着就等于把红线往后无限延）。"""
        monkeypatch.setattr(W, "_OPEN_MARK_TTL", -1.0)
        W.note_agent_open({"U1"}, target_id="T", url="u", why="x")
        assert W._peek_open_marks() == []
        monkeypatch.setattr(W, "_OPEN_MARK_TTL", 60.0)
        W.note_agent_open({"U1"}, target_id="T", url="u", why="x")
        assert len(W._peek_open_marks()) == 1

    # ---------------------------------------------------------------- ② 绑定
    def test_tick_refuses_when_not_bound(self, rig, monkeypatch):
        """未真正绑定（WS 不可用）→ 零动作，且登记作废、只提示一次。"""
        monkeypatch.setattr(W, "_tuck_binding_ok", lambda port: False)
        W.note_agent_open({"U1"}, target_id="T", url="http://x", why="点击开页")
        rig["hwnds"] = ["U1", "A1"]
        assert W._tucker_tick(9222, rig["state"]) == 0
        assert rig["min_hwnds"] == [] and rig["min_title"] == []
        assert W._peek_open_marks() == []

    def test_binding_probe_is_fail_closed(self, monkeypatch):
        """绑定探测本身异常 → 按"未绑定"处理（宁可不收，也不许乱动用户窗口）。"""
        import wecom_bridge.cdp_client as CC

        def _boom(port=None):
            raise RuntimeError("探测炸了")

        monkeypatch.setattr(CC, "_cdp_ws_ok", _boom)
        monkeypatch.setattr(W, "_bind_probe", {"ts": 0.0, "ok": True})   # 不吃缓存
        assert W._tuck_binding_ok(9222) is False

    def test_binding_probe_uses_ws_not_json(self, monkeypatch):
        """判据必须是 WebSocket 级探测：/json 可读而 WS 被 403 拒时算未绑定。"""
        seen = {}

        def _ws(port=None):
            seen["called"] = True
            return False

        import wecom_bridge.cdp_client as CC
        monkeypatch.setattr(CC, "_cdp_ws_ok", _ws)
        monkeypatch.setattr(W, "_bind_probe", {"ts": 0.0, "ok": True})
        assert W._tuck_binding_ok(9222) is False and seen.get("called")

    # ---------------------------------------------------------- ③ 点击才算开页
    def test_open_silent_page_registers(self, monkeypatch):
        """静默开页通道也必须登记（批审/月报走的就是这条）：拍基线 → 建 target → 登记。"""
        class _R:
            status_code = 200
            text = "{}"

            @staticmethod
            def json():
                return {"id": "TGT-9", "url": "http://oa/detail", "title": ""}

        class _Req:
            def put(url, timeout=None):
                return _R()

            def get(url, timeout=None):
                return _R()

        monkeypatch.setattr(W, "requests", _Req)
        monkeypatch.setattr(W, "_pages", lambda t: [{"id": "TGT-9", "url": "http://oa/detail"}])
        monkeypatch.setattr(W, "list_targets", lambda port=None, **kw: [])
        monkeypatch.setattr(W, "new_window_baseline", lambda: {"U1", "U2"})
        monkeypatch.setattr(W.time, "sleep", lambda *_: None)
        W._open_marks.clear()
        r = W.open_silent_page("http://oa/detail")
        assert r.get("ok") is True, r
        marks = W._peek_open_marks()
        assert len(marks) == 1 and marks[0]["baseline"] == {"U1", "U2"} \
            and marks[0]["target_id"] == "TGT-9", marks

    def test_click_registers_only_after_new_page(self):
        """登记必须发生在「点击确实产出了新页」之后，基线必须在点击派发之前拍。
        写成"每次点击都登记"就等于把红线又退回 busy 期间全收——这里按结构锁死。"""
        src = inspect.getsource(RD.click_element)
        assert "new_window_baseline()" in src, "点击前没拍窗口基线"
        assert "note_agent_open(" in src
        _gate = src.index("if _before_ids and _np:")
        _note = src.index("note_agent_open(")
        assert _note > _gate, "note_agent_open 不在「确实开出新页」分支里 → 无差别登记"
        assert src.index("_win_base = new_window_baseline()") < _gate, "基线必须在点击之前拍"

    def test_tucker_has_no_busy_only_path_left(self):
        """源码级：windows.py 里每个 _tuck_fresh_hwnds 调用都必须带归属基线实参，
        且旧的「CDP停摆兜底 / PID关联」两条无归属路径不得复活。"""
        from wecom_bridge import tucker   # v1.0.656 拆分：tucker 段已迁 wecom_bridge/tucker.py
        body = inspect.getsource(tucker)
        assert "CDP停摆兜底" not in body
        assert "PID关联《" not in body
        i = body.index("def _tucker_tick")
        tick = body[i:body.index("def start_window_tucker")]
        assert "_tuck_fresh_hwnds(" in tick
        assert 'm.get("baseline")' in tick, "tick 里的收纳没带归属基线"
        assert "_tuck_binding_ok(port)" in tick

    def test_agent_busy_no_longer_gates_tucking(self):
        """v1.0.629 起 `_agent_busy` 不再是收纳判据（它只说明"软件在忙"，不说明"谁开的"）。"""
        body = inspect.getsource(W._tucker_tick)
        assert "_agent_busy" not in body, "又拿 busy 当收纳依据——正是要修掉的口径"


# ============================================================ 来自 test_v614_ws_size.py（v1.0.614）


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctypes
import io
import re
import threading

import core.paths as cp          # noqa: E402
import core.workspace_io as wio  # noqa: E402


@pytest.fixture()
def wsroot(tmp_path, monkeypatch):
    root = tmp_path / "产物库"
    for sub in cp._WORKSPACE_SUBS + cp._RO_SUBS + ("组件",):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cp, "workspace_root", lambda: str(root))
    wio.invalidate_size_cache()
    yield root
    wio.invalidate_size_cache()


def _w(path, mb):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * int(mb * 1048576))


def test_write_list_not_widened():
    """只加展示/读取口径，agent 可写目录名单不能跟着放宽。
    v1.0.616：展示口径改成动态（tree_subs()），可写名单仍然是那 5 个 + 知识库嵌套段。"""
    assert cp._TREE_SUBS == cp._WORKSPACE_SUBS + cp._RO_SUBS
    for ro in cp._RO_SUBS:
        assert ro not in cp._ALLOWED_SUBS, f"{ro} 变成了可写目录"
        assert ro not in cp._WORKSPACE_SUBS
    assert "备份" not in cp._ALLOWED_SUBS and "日报" not in cp._ALLOWED_SUBS


def test_size_covers_everything_including_runtime(wsroot):
    _w(wsroot / "产出" / "报告.md", 11)
    _w(wsroot / "组件" / "node" / "node.exe", 12)     # 运行时大头
    _w(wsroot / "日志" / "companion.log", 0.5)
    info = wio.workspace_size_info(refresh=True)
    assert info["runtime_mb"] == 12.0, info
    assert info["total_mb"] == 23.5, info
    assert set(info["dirs"]) >= {"产出", "组件", "日志"}
    # 组件里的 12MB 属运行环境，不进"该清理的大文件"清单（那清单会引导用户去删运行时）
    assert [f["sub"] for f in info["large_files"]] == ["产出"], info["large_files"]


def test_size_cache_and_invalidate(wsroot):
    """v1.0.616 契约：缓存只留给 组件/。用户目录必须**即时**反映增删（旧实现整份结果
    缓存 600s，手动删完大文件后最长 10 分钟仍报旧体积）。"""
    _w(wsroot / "下载" / "a.zip", 1)
    assert wio.workspace_size_info(refresh=True)["cached"] is False
    assert wio.workspace_size_info()["cached"] is True          # 组件段取自缓存
    _w(wsroot / "下载" / "b.zip", 3)
    assert wio.workspace_size_info()["total_mb"] == 4.0         # 用户目录即时可见
    _w(wsroot / "组件" / "node" / "x.exe", 5)
    assert wio.workspace_size_info()["runtime_mb"] == 0.0        # 组件仍取缓存值
    wio.invalidate_size_cache()
    assert wio.workspace_size_info()["runtime_mb"] == 5.0


def test_tree_lists_readonly_subs(wsroot):
    _w(wsroot / "台账" / "批审台账.md", 0.01)
    import backend as backend_mod
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        d = c.get("/api/workspace").get_json()
    groups = {g["sub"]: g for g in d["tree"]}
    assert set(groups) == set(cp.tree_subs()), sorted(set(groups) ^ set(cp.tree_subs()))
    assert groups["台账"]["readonly"] is True and groups["产出"]["readonly"] is False
    assert [f["name"] for f in groups["台账"]["files"]] == ["批审台账.md"]


def test_read_resolves_the_sub_not_fallback(wsroot):
    """ws_sub("台账") 会静默回退成 产出/ ——读取必须按段自己解析，否则读到别的目录。"""
    from urllib.parse import quote
    (wsroot / "产出" / "同日.md").write_text("产出内容", encoding="utf-8")
    (wsroot / "台账" / "同日.md").write_text("台账内容", encoding="utf-8")
    import backend as backend_mod
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        d = c.get("/api/workspace/file?sub=%s&name=%s" % (quote("台账"), quote("同日.md"))).get_json()
        assert d["ok"] is True and d["content"] == "台账内容", d
        assert c.get("/api/workspace/file?sub=%s&name=x.md" % quote("产出")).status_code == 404
        bad = c.get("/api/workspace/file?sub=%s&name=x.md" % quote("组件"))
        assert bad.status_code == 400, "组件不在展示名单里（7 万文件不进树）"


def test_size_route_over_limit_uses_cleanable(wsroot):
    _w(wsroot / "组件" / "git" / "git.exe", 11)      # 运行时超 500MB 也不该催用户清理
    for i in range(6):
        _w(wsroot / "下载" / ("f%d.bin" % i), 1)
    import backend as backend_mod
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        d = c.get("/api/workspace/size").get_json()
        assert d["runtime_mb"] == 11.0 and d["cleanable_mb"] == 6.0
        assert d["over_limit"] is False
        d2 = c.get("/api/workspace/size?refresh=1").get_json()
        assert d2["cached"] is False and d2["total_mb"] == 17.0


def test_frontend_wiring_present():
    """新设置卡与体积/树渲染的 DOM id 必须两边齐备（JS 引用不存在的 id 是静默失效）。"""
    root = cp.BASE
    html = open(os.path.join(root, "index.html"), encoding="utf-8").read()
    js = open(os.path.join(root, "js", "11-workspace-kb-study.js"), encoding="utf-8").read()
    nav = open(os.path.join(root, "js", "02-nav-quicklinks.js"), encoding="utf-8").read()
    for i in ("pii_name", "pii_account", "pii_bank", "pii_mask", "dsh_base_url", "dsh_api_key",
              "dshClearKey", "personalState", "bg_digests_daily", "bg_digests_time",
              "bg_kb_organize", "bg_kb_digest", "bg_todo_watch",
              "dsh_cwd", "dsh_extra_args", "bg_todo_interval"):
        assert 'id="%s"' % i in html, i
        assert '"%s"' % i in js, "JS 引用了 HTML 里不存在的 id: " + i
    for fn in ("loadPersonal", "savePersonal", "saveBackground", "clearDshKey", "postPersonal"):
        assert "function " + fn in js, fn
    assert "loadPersonal()" in nav, "设置页打开时没人调用 loadPersonal"
    assert "onclick=\"savePersonal(this)\"" in html and "onclick=\"saveBackground(this)\"" in html
    assert "g.readonly" in js and "runtime_mb" in js


def test_dsh_credential_hint_points_at_real_ui(monkeypatch):
    """体检/工具提示的修复路径必须指向一个真存在的界面（v1.0.614 批 1 同口径）。"""
    from agent_tools.dsh_bridge import dsh_check
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    r = dsh_check({"dsh": {"base_url": "", "api_key": ""}})
    cred = [h for h in r.get("hints") or [] if "凭据" in h]
    assert cred and "个人信息与凭据" in cred[0], r["hints"]
    assert "config.json" not in "".join(cred), "仍让用户手编加密落盘的 config.json"


# ============================================================ 来自 test_v617_ctypes.py（v1.0.617）


from wecom_bridge import wecom_proc as wp   # noqa: E402


from conftest import rd  # noqa: E402


# ---------- 1. 进程枚举：私有实例 + 导入期绑一次 ----------

def test_pid_enum_never_touches_the_shared_kernel32():
    """共享对象上的签名必须一直是 None——被写过就说明又回到"全局互踩"的旧形状。"""
    shared = ctypes.windll.kernel32
    assert shared.Process32First.argtypes is None, "导入期就有人往共享 windll 写签名"
    out = wp.find_wecom_all_pids()
    lst = wp.find_wecom_pids()
    win = wp._find_wecom_pids_winapi()
    assert isinstance(out, set) and isinstance(lst, list) and isinstance(win, list)
    assert shared.Process32First.argtypes is None, "调用后共享 windll 的签名被改写（竞态根源）"
    assert shared.CreateToolhelp32Snapshot.argtypes is None
    # 私有实例必须确实绑好了（否则上面"没写全局"只是因为压根没绑）
    assert wp._K32.Process32First.argtypes is not None


def test_one_struct_class_and_one_enumerator():
    src = rd("wecom_bridge/wecom_proc.py")
    # 判代码形状前先把注释行去掉：本项目的注释习惯是"引用被替换掉的旧写法原文"，
    # 上面那条修前说明里就写着 `class PROCESSENTRY32`，不剥会把自己判成第二份定义。
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    n_class = len(re.findall(r"class\s+\w*PROCESSENTRY32\b", code))
    assert n_class == 1, "结构体又变成多份定义（%d 份）——POINTER(类A) 与 byref(实例B) 不兼容" % n_class
    assert not re.search(r"=\s*ctypes\.windll\.\w+", code), "进程枚举不要再拿共享 windll 对象"
    assert "ctypes.windll" not in code
    for fn in ("find_wecom_all_pids", "_find_wecom_pids_winapi"):
        i = src.find("def %s(" % fn)
        seg = src[i:i + 900]
        assert "_enum_pids_named(" in seg, "%s 没走唯一的枚举通路" % fn


def test_concurrent_pid_enumeration_raises_nothing_and_says_nothing():
    """并发跑两条入口：既不许抛，也不许出现"枚举异常/回退 tasklist"的痕——
    改前这里一旦竞态命中，find_wecom_all_pids 会静默返回空集合、一条日志都不留。"""
    notes = []
    orig_logw = wp._logw
    wp._logw = lambda m: notes.append(m)
    errs = []
    try:
        def _work(i):
            try:
                for _ in range(12):
                    (wp.find_wecom_all_pids() if i % 2 else wp.find_wecom_pids())
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))
        ts = [threading.Thread(target=_work, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
    finally:
        wp._logw = orig_logw
    assert not errs, errs[:3]
    quiet = [n for n in notes if "枚举异常" in n or "回退 tasklist" in n]
    assert not quiet, quiet[:2]


# ---------- 2. 窗口枚举回调：异常不许逃进 ctypes ----------

def test_guard_cb_records_first_error_and_keeps_enumerating():
    from desktop import save_dialog_auto as sd
    sd._CB_ERRORS[:] = []
    calls = []

    def boom(hwnd, l):
        if hwnd == 1:
            raise RuntimeError("窗口没了")
        calls.append(hwnd)
        return True

    guarded = sd._guard_cb(boom)
    # WINFUNCTYPE(BOOL,...) 把返回值按 C BOOL 编组回 Python 时是 1/0，不是 True/False
    assert guarded(1, 0) == 1, "回调抛错后必须继续枚举（返回真值），不能中断整轮"
    assert guarded(2, 0) == 1
    assert calls == [2], calls
    assert len(sd._CB_ERRORS) == 1, sd._CB_ERRORS
    seen = []
    orig = sd.log_event
    sd.log_event = lambda m: seen.append(m)
    try:
        sd._drain_cb_errors()
        sd._drain_cb_errors()
    finally:
        sd.log_event = orig
    assert len(seen) == 1 and "RuntimeError" in seen[0], seen
    assert sd._CB_ERRORS == []


def test_no_unguarded_enum_callbacks_left_in_save_dialog():
    src = rd("desktop/save_dialog_auto.py")
    bad = re.findall(r"@_WNDENUMPROC\s*\n\s*def _cb", src)
    assert not bad, "还有未包护栏的枚举回调（异常会被吞成 Unraisable）"
    # 记了错还得有人排空。只断言 `_drain_cb_errors()` 这个串在文件里＝空锁：
    # 函数定义那一行本身就含这个串（删掉调用点它照样绿，变异自检当场抓到）。
    assert re.search(r"^\s*_drain_cb_errors\(\)\s*(?:#.*)?$", src, re.M), \
        "watcher 里没有 _drain_cb_errors() 的调用行，护栏记的异常永远出不来"


def test_win_send_reports_enumeration_failure_instead_of_silence(monkeypatch):
    from wecom_bridge import win_send as ws

    class _Bad(object):
        def IsWindowVisible(self, *a):
            raise OSError("假坏")

        def EnumWindows(self, *a):
            cb = a[0]
            cb(1234, 0)        # 真的走一次回调，让异常在回调里发生
            return 1

    logs = []
    monkeypatch.setattr(ws, "_user32", _Bad())
    monkeypatch.setattr(ws, "_wecom_pids", lambda: {999})
    monkeypatch.setattr(ws, "log_event", lambda m: logs.append(m))
    assert ws._find_main_window() == 0
    assert logs and "枚举企微主窗口异常" in logs[0], logs
    assert "OSError" in logs[0], "日志里要看得出是什么异常"


def test_os_ops_reports_enumeration_failure_instead_of_silence(monkeypatch):
    from agent_tools import os_ops as oo

    u = ctypes.windll.user32
    seen = []

    def _boom(*a):
        raise OSError("假的可见性查询坏了")

    monkeypatch.setattr(u, "IsWindowVisible", _boom)
    monkeypatch.setattr(oo, "log_event", lambda m: seen.append(m))
    bbox, title = oo._find_window_rect("随便什么标题")
    assert (bbox, title) == (None, None)
    assert seen and "枚举窗口异常" in seen[0], seen
    assert "OSError" in seen[0]


# ---------- 3. 棘轮：共享 windll 的签名改写点只准减 ----------

CAP_SHARED_PROTOTYPE_WRITES = 92   # v1.0.617 修完 wecom_proc 后的实测基数（含 close_guard 54 / windows 23 / os_ops 12 / b_admin 3）
SKIP_DIRS = ("build", "dist", "tests", "docs", "__pycache__", ".git", "产物库", "js", "archive", "node_modules")


def _count_shared_prototype_writes():
    per = {}
    for dp, dn, fns in os.walk(ROOT):
        dn[:] = [x for x in dn if x not in SKIP_DIRS]
        for f in fns:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            s = io.open(p, encoding="utf-8").read()
            names = set(re.findall(r"(\w+)\s*=\s*ctypes\.windll\.(\w+)", s))
            if not names:
                continue
            n = 0
            for line in s.split("\n"):
                if line.strip().startswith("#"):
                    continue
                for nm, _dll in names:
                    if re.search(r"\b%s\.\w+\.(?:argtypes|restype)\s*=" % re.escape(nm), line):
                        n += 1
            if n:
                per[rel] = n
    return per


def test_shared_windll_prototype_writes_ratchet():
    per = _count_shared_prototype_writes()
    tot = sum(per.values())
    assert tot <= CAP_SHARED_PROTOTYPE_WRITES, \
        "又有 %d 处在函数体内改共享 windll 签名（基线 %d）：%s——这种写法并发下会互踩，见本文件开头" % (
            tot, CAP_SHARED_PROTOTYPE_WRITES, per)
    assert "wecom_bridge/wecom_proc.py" not in per, "进程枚举回到共享对象上写签名了"

# ============================================================ 来自 test_v619_defer_ws.py（v1.0.619）


from core import paths as P   # noqa: E402
from core import logging as CL   # noqa: E402


@pytest.fixture()
def appdir(tmp_path, monkeypatch):
    """把 _app_dir 指到空目录，并把闸门关回去；退出时恢复"开闸"状态（其他测试依赖旧语义）。

    为什么连 workspace_root 也要自己接管：tests/ 里有 6 个模块在 **import 期**就裸赋值
    `core.paths.workspace_root = lambda: TMP`（test_components_compat / test_self_repair /
    test_skill_evolve / test_skill_versions / test_v327 / test_wecom_bind），会话收集完它们，
    全局 workspace_root 就永久是它们的假根——只 patch _app_dir 的话，本文件断言的是别人的
    目录（v619 首次全量运行 4 条红，全是这个造成的）。"""
    P._reset_workspace_gate_for_tests()
    monkeypatch.setattr(P, "_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(P, "workspace_root", lambda: os.path.join(str(tmp_path), "产物库"))
    yield tmp_path
    P.allow_workspace_creation("teardown-restore")   # 让后续测试仍看得到"目录会自动存在"


def _root(tmp):
    return os.path.join(str(tmp), "产物库")


# ---------- 闸门未开：不建目录、日志攒着 ----------

def test_gate_closed_creates_nothing(appdir):
    assert not P.workspace_ready()
    assert not os.path.exists(_root(appdir)), "workspace_root() 仍在顺手建目录"
    assert not os.path.exists(os.path.join(_root(appdir), "日志"))
    CL.log_event("[测试] 启动期一行日志")
    assert not os.path.exists(_root(appdir)), "log_event 绕过了闸门（它就是那条早建目录的路径）"
    assert P.creation_reason() in ("", "test")
    assert CL.early_log_count() == 1


# ---------- 开闸：建根+5 个可写子目录，并把攒的日志补写 ----------

def test_gate_open_creates_dirs_and_flushes_logs(appdir):
    CL.log_event("[测试] 第一行")
    CL.log_event("[测试] 第二行")
    assert P.allow_workspace_creation("unit-test") is True
    assert P.allow_workspace_creation("unit-test-again") is False, "幂等性破了"
    assert os.path.isdir(_root(appdir))
    for s in P._WORKSPACE_SUBS:
        assert os.path.isdir(os.path.join(_root(appdir), s)), "缺可写子目录 " + s
    logf = os.path.join(_root(appdir), "日志", "companion.log")
    body = io.open(logf, encoding="utf-8").read()
    assert "[测试] 第一行" in body and "[测试] 第二行" in body, "启动期日志被丢了，不是延后"
    assert P.creation_reason() == "unit-test", "开闸原因没记下来（排障时要靠它）"


# ---------- 写路径自解锁：功能绝不因"还没建目录"而失败 ----------

def test_first_write_opens_the_gate(appdir):
    from core.workspace_io import save_workspace_file
    CL.log_event("[测试] 写操作之前的启动日志")
    r = save_workspace_file("产出", "探针.txt", "内容")
    assert r.get("ok") is True, r
    assert os.path.isfile(r["path"]) and os.path.getsize(r["path"]) == len("内容".encode())
    assert P.workspace_ready(), "写操作没把闸门带上——前 12 秒的保存会莫名失败"
    assert P.creation_reason().startswith("write:产出"), P.creation_reason()
    logf = os.path.join(_root(appdir), "日志", "companion.log")
    # 之后不再调用 log_event：补写必须由"开闸"本身触发。等下一条日志顺便带出＝这一行永远丢掉。
    assert os.path.isfile(logf), "开闸后没补写日志（补写挂在下一条日志上，用户不再操作就整段丢）"
    assert "写操作之前的启动日志" in io.open(logf, encoding="utf-8").read()
    assert CL.early_log_count() == 0


# ---------- 读路径：不建目录，且容忍目录不存在 ----------

def test_readers_do_not_create_and_tolerate_absence(appdir):
    subs = P.tree_subs()
    assert list(P._WORKSPACE_SUBS) == subs[:5], subs
    assert not os.path.exists(_root(appdir)), "读文件树把产物库建出来了"
    from core.workspace_io import workspace_size_info
    info = workspace_size_info()
    assert isinstance(info["total_mb"], float)
    assert not os.path.exists(_root(appdir)), "体积统计把产物库建出来了"


# ---------- 启动序列：不许再出现"开窗前就建目录" ----------

def test_backend_boot_sequence_is_gated():
    src = io.open(os.path.join(ROOT, "backend.py"), encoding="utf-8").read()
    assert "启动时即建立" not in src, "旧的无条件建目录还在"
    i = src.find("def _workspace_ready_later")
    j = src.find("def _open_ws_gate")
    assert 0 < j < i, "闸门函数/延时线程不见了"
    assert "paths.allow_workspace_creation" in src
    assert "paths.WORKSPACE_READY_DELAY_S" in src
    assert "preexisting" in src, "产物库已存在时应立刻开闸（老用户不该白等 12 秒）"
    # 迁移自己会 makedirs，必须排在闸门之后
    assert src.find("_startup_migrate()", src.find("def _open_ws_gate")) > 0
    assert "_startup_migrate()          # 旧布局/知识库迁移" in src or "必须在闸门之后" in src
    k = src.find("_attach_pywebview_log")
    assert k > 0 and "paths.workspace_ready()" in src[k:k + 900], "pywebview FileHandler 未等闸门"


def test_delay_is_sane():
    assert 5 <= P.WORKSPACE_READY_DELAY_S <= 30, P.WORKSPACE_READY_DELAY_S


# ---------- 归位搬家：日志跟着 exe 走，旧位置一个目录都不留 ----------

def test_relocate_flush_lands_in_new_place_not_old(appdir):
    """搬家那条路：本进程在旧位置的闸门永远不会再开（马上 os._exit），
    补写必须显式落到新位置——否则整段启动日志（含 [relocate] 证据行）随进程蒸发。"""
    CL.log_event("[relocate] 检测到桌面运行，已自动归位到 X")
    assert CL.early_log_count() == 1
    new_dir = os.path.join(str(appdir), "新家", "产物库", "日志")
    assert CL.flush_early_logs(log_dir=new_dir) == 1
    assert not os.path.exists(_root(appdir)), "往新家补写却把旧位置的产物库又捏出来了"
    body = io.open(os.path.join(new_dir, "companion.log"), encoding="utf-8").read()
    assert "[relocate] 检测到桌面运行" in body
    assert CL.early_log_count() == 0


def test_startup_banner_marks_the_deferred_block(appdir):
    """补写的行落盘时间晚于真实时间，日志里要有一条"本次启动"分隔线，
    否则后来的人会把这段当成上一次运行末尾丢了日志。每次进程只打一条。"""
    CL._START_MARK_DONE = False
    CL.log_event("[测试] 延后的一行")
    P.allow_workspace_creation("unit-test")
    assert P.allow_workspace_creation("again") is False
    CL.log_event("[测试] 即时的一行")
    body = io.open(os.path.join(_root(appdir), "日志", "companion.log"),
                   encoding="utf-8").read()
    lines = body.splitlines()
    assert lines[0].startswith("# ---- 本次启动"), lines[:3]
    assert sum(1 for x in lines if x.startswith("# ---- 本次启动")) == 1, "分隔线重复了"
    assert "[测试] 延后的一行" in body and "[测试] 即时的一行" in body
    assert body.index("延后的一行") < body.index("即时的一行"), "补写插到了即时行后面"

# ============================================================ 来自 test_v632_ws_pool.py（v1.0.632）


import wecom_bridge.cdp_client as C   # noqa: E402


class FakeWS:
    def __init__(self, url):
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("WECOM_CDP_WS_POOL", "1")
    C._WS_POOL.clear()
    made = []

    def _fake_open(ws_url, timeout):
        w = FakeWS(ws_url)
        made.append(w)
        return w

    monkeypatch.setattr(C, "_open_ws", _fake_open)
    monkeypatch.setattr(C, "_logw", lambda *a, **k: None)
    yield made
    C._WS_POOL.clear()


def _url(tid):
    return f"ws://127.0.0.1:9222/devtools/page/{tid}"


def _hold(tid):
    """取一条连接且**不归还**（模拟正在执行 25s evaluate 的操作）。"""
    ws, entry, err = C._pool_acquire(_url(tid), 1.0)
    assert err is None, err
    return ws, entry


# ---------------------------------------------------------------- ③ 容量
def test_pool_never_grows_past_capacity(pool):
    """40 个页面各连一次：池子必须封顶，多出来的要真被关掉。"""
    live = []
    for i in range(40):
        ws, entry, err = C._pool_acquire(_url("T%02d" % i), 1.0)
        assert err is None, err
        live.append((ws, entry))
        C._pool_release(entry, ws, False)      # 归还，否则锁会挡住淘汰
    assert len(C._WS_POOL) <= C._WS_POOL_MAX, len(C._WS_POOL)
    closed = [w for w in pool if w.closed]
    assert len(closed) >= 40 - C._WS_POOL_MAX, (len(pool), len(closed))


def test_capacity_evicts_least_recently_used(pool, monkeypatch):
    monkeypatch.setattr(C, "_WS_POOL_MAX", 3)
    monkeypatch.setattr(C, "_WS_POOL_IDLE_S", 1e9)     # 只考察容量这一条
    handles = [_hold(i) for i in ("A", "B", "C")]
    for ws, e in handles:
        C._pool_release(e, ws, False)
    ws, entry, err = C._pool_acquire(_url("D"), 1.0)   # 第 4 个挤进来
    assert err is None
    C._pool_release(entry, ws, False)
    assert _url("A") not in C._WS_POOL, "该淘汰最久未用的，而不是随机一个"
    assert _url("D") in C._WS_POOL


# ---------------------------------------------------------------- ① 页面消失
def test_prune_drops_gone_targets(pool):
    for t in ("AAAA", "BBBB"):
        ws, entry, err = C._pool_acquire(_url(t), 1.0)
        C._pool_release(entry, ws, False)
    assert len(C._WS_POOL) == 2
    n = C.pool_prune_dead([{"id": "aaaa"}])             # 只剩 A（大小写不敏感）
    assert n == 1
    assert _url("AAAA") in C._WS_POOL and _url("BBBB") not in C._WS_POOL
    assert any(w.closed and w.url == _url("BBBB") for w in pool), "摘掉槽位却没关 socket = 泄漏"


def test_empty_alive_list_is_not_read_as_everything_gone(pool):
    """枚举异常/空响应不得等于"所有页面都没了"——那会把整个池连坐拆掉。"""
    ws, entry, _ = C._pool_acquire(_url("AAAA"), 1.0)
    C._pool_release(entry, ws, False)
    assert C.pool_prune_dead([]) == 0
    assert len(C._WS_POOL) == 1


def test_busy_slot_is_never_touched(pool):
    """正在被占用的连接一律不回收：否则一条 evaluate 用到一半 socket 被关。"""
    ws, entry = _hold("AAAA")
    assert C.pool_prune_dead([{"id": "ZZZZ"}]) == 0     # A 已不在列表里，但它在忙
    assert _url("AAAA") in C._WS_POOL
    assert ws.closed is False
    C._pool_release(entry, ws, False)
    assert C.pool_prune_dead([{"id": "ZZZZ"}]) == 1     # 空闲后下一次回收才摘走


def test_browser_level_url_is_not_pruned_by_target_ids(pool):
    """非 /devtools/page/ 的 url 认不出 targetId，就不该按存活集合判死。"""
    u = "ws://127.0.0.1:9222/devtools/browser/7c1d-uuid"
    ws, entry, _ = C._pool_acquire(u, 1.0)
    C._pool_release(entry, ws, False)
    assert C.pool_prune_dead([{"id": "OTHER"}]) == 0
    assert u in C._WS_POOL


# ---------------------------------------------------------------- ② 闲置
def test_idle_slot_is_reclaimed(pool, monkeypatch):
    monkeypatch.setattr(C, "_WS_POOL_IDLE_S", -1.0)
    ws, entry, _ = C._pool_acquire(_url("AAAA"), 1.0)
    C._pool_release(entry, ws, False)
    ws2, entry2, err = C._pool_acquire(_url("BBBB"), 1.0)   # 新连接入场时触发 sweep
    assert err is None
    assert ws.closed is True, "闲置超时的连接没被关"
    assert _url("AAAA") not in C._WS_POOL
    C._pool_release(entry2, ws2, False)


def test_acquire_reuses_live_connection(pool):
    """复用本身不能被回收逻辑弄坏：同 url 两次 acquire 拿到同一个 ws，只握手一次。"""
    a, ea, _ = C._pool_acquire(_url("AAAA"), 1.0)
    C._pool_release(ea, a, False)
    b, eb, _ = C._pool_acquire(_url("AAAA"), 1.0)
    assert b is a and len(pool) == 1
    C._pool_release(eb, b, False)


def test_released_connection_survives_a_non_idle_sweep(pool, monkeypatch):
    """刚用过的连接不该在闲置扫描里被当旧连接拆掉（ts 由 acquire/release 盖戳）。"""
    ws, entry, _ = C._pool_acquire(_url("AAAA"), 1.0)
    C._pool_release(entry, ws, False)
    monkeypatch.setattr(C, "_WS_POOL_IDLE_S", 3600.0)
    ws2, e2, _ = C._pool_acquire(_url("BBBB"), 1.0)
    C._pool_release(e2, ws2, False)
    assert ws.closed is False and _url("AAAA") in C._WS_POOL


# ---------------------------------------------------------------- dead 槽位不复活
def test_dead_slot_is_not_reinserted(pool, monkeypatch):
    """排队等锁期间槽位被回收 ⇒ acquire 重来，不把脱离池的 entry 交出去。

    交出去的后果：调用方 _pool_release 后这条连接再没人引用，既不在池里也没被关，
    成一条永久占着 devtools 会话数的孤儿 socket。"""
    ws, entry, _ = C._pool_acquire(_url("AAAA"), 1.0)      # entry 锁此刻被我们持有
    # 模拟另一个线程在这期间回收了该槽位
    with C._WS_POOL_LOCK:
        C._pop_slot(_url("AAAA"), entry)
    assert entry.get("dead") is True
    C._pool_release(entry, ws, False)                      # dead 槽位归还：不会被插回池
    ws2, entry2, err = C._pool_acquire(_url("AAAA"), 1.0)
    assert err is None and entry2 is not entry
    assert _url("AAAA") in C._WS_POOL
    C._pool_release(entry2, ws2, False)


def test_broken_release_clears_slot_but_keeps_it_for_rebuild(pool):
    """连接被判不可信：关掉、槽位清空但**保留**，下次 acquire 原地重建（旧行为不能丢）。"""
    ws, entry, _ = C._pool_acquire(_url("AAAA"), 1.0)
    C._pool_release(entry, ws, True)
    assert ws.closed is True and entry["ws"] is None
    assert _url("AAAA") in C._WS_POOL
    ws2, entry2, err = C._pool_acquire(_url("AAAA"), 1.0)
    assert err is None and entry2 is entry and ws2 is not ws
    C._pool_release(entry2, ws2, False)


def test_release_stamps_the_slot_as_recently_used(pool):
    """归还时要盖"最近使用"戳，否则 LRU 会先拆掉正在被反复使用的热连接。"""
    ws, entry, _ = C._pool_acquire(_url("AAAA"), 1.0)
    entry["ts"] = 0.0                       # 假装这连接已经冷掉了
    C._pool_release(entry, ws, False)
    assert entry["ts"] > 0.0, entry
    assert ws.closed is False


def test_acquire_falls_back_when_repeatedly_reclaimed(pool, monkeypatch):
    """三次都被抢先前回收 → 退回"每次新建"的旧行为，绝不让调用失败。

    用一个"登记即作废"的池来演这种极端时序：每次新建槽位都在交给调用方之前
    被回收器摘走。真跑不出这个窗口，但退路必须是可验证的——否则回收逻辑本身
    就成了新的失败模式（页面操作报"取不到连接"）。"""
    class _Evil(dict):
        def __setitem__(self, k, v):
            dict.__setitem__(self, k, v)
            v["dead"] = True              # 刚登记就被回收器抢先标死

    monkeypatch.setattr(C, "_WS_POOL", _Evil())
    ws, entry, err = C._pool_acquire(_url("AAAA"), 1.0)
    assert err is None, err
    assert entry is None, "回退路径应当交回「不入池」的裸连接，由调用方直接关闭"
    assert len(pool) == 1, pool
    C._pool_release(entry, ws, False)
    assert ws.closed is True


# ---------------------------------------------------------------- 接线
def test_list_targets_wires_the_prune():
    """回收必须挂在"每次成功枚举"上；只在单测里活着的函数等于没修。"""
    src = io.open(os.path.join(ROOT, "wecom_bridge", "targets.py"), encoding="utf-8").read()
    i = src.find("def list_targets")
    body = src[i:src.find("def _pages", i)]
    assert "pool_prune_dead(" in body, body[:400]
    assert "pool_prune_dead" in src.split("def list_targets")[0], "没 import"


def test_prune_failures_cannot_break_enumeration():
    """CDP 枚举是全站热点路径：回收逻辑再错也不能把 list_targets 拖成失败。"""
    src = io.open(os.path.join(ROOT, "wecom_bridge", "targets.py"), encoding="utf-8").read()
    i = src.find("def list_targets")
    body = src[i:src.find("def _pages", i)]
    assert "except" in body.split("pool_prune_dead(")[1][:200], body

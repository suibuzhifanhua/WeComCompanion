# -*- coding: utf-8 -*-
"""tests/test_wecom_bind.py — 企微绑定强化（v1.0.336）
①cdp_is_wecom：企微 UA 识别 / Chrome 占用拒绝 / 连接失败
②ensure_binding 端口被非企微占用 → 自动换端口并持久化
③ensure_binding reuse / 全家杀（WXWorkWeb 一并处理）
全部 mock 网络与进程探测，无真实企微依赖。pytest 与直跑双兼容（手动补丁）。
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.result import is_err, err_msg  # noqa: E402
import core  # noqa: E402

import core_utils  # noqa: E402
from core import config_store  # noqa: E402

WS = tempfile.mkdtemp(prefix="wec_bind_")
core.paths.workspace_root = lambda: WS
config_store.config_path = lambda: os.path.join(WS, "config.json")

import wecom_bridge.wecom_proc as wp  # noqa: E402
import wecom_bridge.cdp_client as cc  # noqa: E402


class _Resp:
    """HTTP 响应的假桩。v1.0.651：`cdp_up` 现在要**状态码 200 + 能解析出 JSON** 才算
    端口上是 CDP（本机实测有个对 /json/version 一律回 404 的占用者被旧口径当成"活"），
    所以假桩必须带上这两样，否则测的是空气（"假桩猜签名"那类反复占用回归轮次的坑）。"""
    def __init__(self, text, status_code=200, is_json=True):
        self.text = text
        self.status_code = status_code
        self._is_json = is_json

    def json(self):
        if not self._is_json:
            raise ValueError("not json")
        return json.loads(self.text) if self.text.strip().startswith(("{", "[")) else {"browser": self.text}


class _Patches:
    """手动补丁栈（pytest monkeypatch 之外的直跑兼容）。"""
    def __init__(self):
        self._stack = []

    def set(self, obj, name, fn):
        self._stack.append((obj, name, getattr(obj, name)))
        setattr(obj, name, fn)

    def undo(self):
        for obj, name, old in reversed(self._stack):
            setattr(obj, name, old)
        self._stack.clear()


# v1.0.649：`_relaunch_with_cdp` 现在会先确认 exe 真在（找不到就**不动进程**直接返回原因），
# 所以假路径要换成一个真存在的空文件，否则测不到"清干净→启动新实例"那一支。
FAKE_EXE = os.path.join(WS, "WXWork.exe")
io.open(FAKE_EXE, "w", encoding="utf-8").close()


def _reset_cfg(port=9222, exe=None, **extra):
    cfg = {"schema_version": 2, "models": [],
           "wecom": {"cdp_port": port, "exe_path": exe or FAKE_EXE,
                     "auto_launch": True, "auto_cdp": True, **extra}}
    io.open(config_store.config_path(), "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False))
    return cfg


def test_cdp_is_wecom():
    p = _Patches()
    try:
        def fake_get(url, timeout):
            port = url.split(":")[-1].split("/")[0]
            ua = {"9222": '{"Browser":"Chrome/131.0.0.0","User-Agent":"Mozilla/5.0 Chrome/131"}',
                  "9223": '{"Browser":"Chrome/131.0.0.0","User-Agent":"Mozilla/5.0 ... wxwork/4.1.0 ..."}',
                  "9224": '{"Browser":"Chrome/131.0.0.0","User-Agent":"Mozilla/5.0 (WeCom)"}'}[port]
            return _Resp(ua)
        # 注意：cc.requests 是 _LazyRequests 实例——p.set 的 getattr(cc.requests,"get")
        # 会触发 __getattr__ 把 cdp_client globals 替换成真 requests 模块，随后的
        # setattr 落在被丢弃的实例上（fake 永不生效）。必须整体替换模块属性 cc.requests。
        p.set(cc, "requests", type("NS", (), {"get": staticmethod(fake_get)}))
        assert cc.cdp_is_wecom(9222) is False, "Chrome 占用不应判为企微"
        assert cc.cdp_is_wecom(9223) is True, "wxwork UA 应判为企微"
        assert cc.cdp_is_wecom(9224) is True, "WeCom UA 应判为企微"

        def broken(url, timeout):
            raise ConnectionError("refused")
        p.set(cc, "requests", type("NS", (), {"get": staticmethod(broken)}))
        # v1.0.555（P1-12 三态化）：连接失败从 False 改 None——"探测失败"与
        # "明确非企微"必须可区分，否则 ensure_binding 会把瞬时抖动当端口被占
        # → 换端口 + 强杀用户企微。契约变更同步更新本断言。
        assert cc.cdp_is_wecom(9222) is None
    finally:
        p.undo()
    print("PASS cdp_is_wecom：企微 UA 识别/Chrome 拒绝/失败 None（三态）")


def test_port_hijack_auto_switch():
    _reset_cfg(9222)
    p = _Patches()
    launched = {}
    state = {"launched": False}
    try:
        # 9222 恒被非企微程序占用；9223 在企微启动后才就绪（两阶段语义）
        p.set(wp, "cdp_up", lambda port: port == 9222 or (port == 9223 and state["launched"]))
        p.set(wp, "cdp_is_wecom", lambda port: port == 9223 and state["launched"])
        p.set(wp, "_cdp_ws_ok", lambda port: port == 9223 and state["launched"])
        p.set(wp, "find_wecom_pids", lambda: [4321] if state["launched"] else [])
        p.set(wp, "find_wecom_all_pids", lambda: {4321})
        p.set(wp, "list_targets", lambda port: [])
        p.set(wp, "window_list_summary", lambda port: [])
        p.set(wp, "_logw", lambda msg: None)
        def fake_popen(argv, creationflags=0):
            state["launched"] = True
            launched.setdefault("args", argv)
        p.set(wp.subprocess, "Popen", fake_popen)
        cfg_obj = _reset_cfg(9222)
        st = wp.ensure_binding(cfg_obj, force=True, allow_kill=True)
        assert st["bound"] is True, st
        assert st["port"] == 9223, f"应自动换到 9223：{st}"
        assert cfg_obj["wecom"]["cdp_port"] == 9223, "传入 cfg 的内存端口应已更新"
        # 持久化由调用方（routes 层 _save_config_safe(cfg)）负责——保存【改过的 cfg_obj】
        from core.config_store import _save_config_safe
        assert _save_config_safe(cfg_obj)[0]
        cfg = json.load(io.open(config_store.config_path(), encoding="utf-8"))
        assert cfg["wecom"]["cdp_port"] == 9223, "新端口应持久化"
        assert launched.get("args") and "--remote-debugging-port=9223" in " ".join(launched["args"])
    finally:
        p.undo()
    print("PASS 端口被占 → 自动换 9223 + 持久化 + 按新端口启动")


def test_reuse_and_ws_positive():
    _reset_cfg(9222, bound_pid=111, bound_sig="x")
    p = _Patches()
    try:
        p.set(wp, "cdp_up", lambda port: True)
        p.set(wp, "cdp_is_wecom", lambda port: True)
        p.set(wp, "_cdp_ws_ok", lambda port: True)
        p.set(wp, "find_wecom_pids", lambda: [111])
        p.set(wp, "find_wecom_all_pids", lambda: {111})
        p.set(wp, "list_targets", lambda port: [])
        p.set(wp, "window_list_summary", lambda port: ["w1"])
        p.set(wp, "_logw", lambda msg: None)
        st = wp.ensure_binding(config_store.load_config(), force=False, allow_kill=True)
        assert st["bound"] is True and st["reason"] in ("reuse", "windows changed"), st
    finally:
        p.undo()
    print("PASS reuse：企微在位+WS 通 → 不重启")


def test_relay_kill_all():
    """v1.0.649（用户："软件修复的时候不要影响企业微信"）：这条原来是"5s 没退干净就
    `taskkill /f /im WXWork.exe` 按镜像名全杀"的行为锁——那个升级分支正是报障的成因：
    bound_pid 陈旧 → 精准杀落空 → 无条件升级 → 用户自己开着的企微窗口一并被带走。

    现在锁的是**新契约**：杀的对象只能是"当场枚举到的企微 PID"（各自带 /t 连子进程树，
    所以 WXWorkWeb 一样被清掉，v1.0.194 那个"只杀主进程会闪退"的坑仍然堵着），
    并且**任何情况下都不许出现按镜像名杀**。"""
    _reset_cfg(9222, bound_pid=111)
    p = _Patches()
    killed, launched = {"cmds": []}, {"n": 0}
    try:
        p.set(wp, "cdp_up", lambda port: False)
        p.set(wp, "cdp_is_wecom", lambda port: False)
        p.set(wp, "_cdp_ws_ok", lambda port: True)
        p.set(wp, "_run_hidden", lambda args, **kw: killed["cmds"].append(" ".join(args)) or None)
        p.set(wp.subprocess, "Popen",
              lambda argv, creationflags=0: launched.__setitem__("n", launched["n"] + 1))
        p.set(wp, "list_targets", lambda port: [])
        p.set(wp, "window_list_summary", lambda port: [])
        p.set(wp, "_logw", lambda msg: None)
        # 进程序列：主 111+web 222 → web 残留 → **全清** → 启动后新实例 4321
        # （v1.0.649 必须给一个"真退干净"的状态：残留不退干净现在会放弃重启，而不是升级成 /im）
        seq = [{"wxwork.exe": [111], "wxworkweb.exe": [222]},
               {"wxwork.exe": [], "wxworkweb.exe": [222]},
               {"wxwork.exe": [], "wxworkweb.exe": []},
               {"wxwork.exe": [4321], "wxworkweb.exe": []}]
        state = {"n": 0}
        p.set(wp, "find_wecom_all_pids",
              lambda: {pid for fam in seq[min(state["n"], len(seq) - 1)].values() for pid in fam})
        p.set(wp, "find_wecom_pids", lambda: seq[min(state["n"], len(seq) - 1)]["wxwork.exe"])
        p.set(wp.time, "sleep", lambda s: state.__setitem__("n", min(state["n"] + 1, len(seq) - 1)))
        st = wp.ensure_binding(config_store.load_config(), force=True, allow_kill=True)
        cmds = " | ".join(killed["cmds"])
        assert "/pid 111" in cmds and "/pid 222" in cmds, f"两个企微 PID 都该被清：{cmds}"
        assert "/t" in cmds, f"要连子进程树一起收（v1.0.194 的闪退坑）：{cmds}"
        assert "/im" not in cmds, f"又按镜像名全杀了：{cmds}"
        assert launched["n"] == 1, f"清干净后应启动一个新实例，实得 {launched['n']}"
    finally:
        p.undo()
    print("PASS 只按当场枚举的 PID 杀（含子树），绝不按镜像名全杀")


def test_relaunch_gives_up_instead_of_shotgunning():
    """残留不退干净就**放弃重启**：不 Popen 新实例、不升级成 /im，并把"该你手动退企微"说清。"""
    _reset_cfg(9222, bound_pid=111)
    p = _Patches()
    killed, launched = {"cmds": []}, {"n": 0}
    try:
        p.set(wp, "cdp_up", lambda port: False)
        p.set(wp, "cdp_is_wecom", lambda port: False)
        p.set(wp, "_cdp_ws_ok", lambda port: False)
        p.set(wp, "_run_hidden", lambda args, **kw: killed["cmds"].append(" ".join(args)) or None)
        p.set(wp.subprocess, "Popen", lambda argv, creationflags=0: launched.__setitem__("n", launched["n"] + 1))
        p.set(wp, "list_targets", lambda port: [])
        p.set(wp, "window_list_summary", lambda port: [])
        p.set(wp, "_logw", lambda msg: None)
        p.set(wp, "find_wecom_all_pids", lambda: {222})     # 永远有个 web 进程赖着不走
        p.set(wp, "find_wecom_pids", lambda: [222])
        st = wp.ensure_binding(config_store.load_config(), force=True, allow_kill=True)
        cmds = " | ".join(killed["cmds"])
        assert "/im" not in cmds, cmds
        assert launched["n"] == 0, "企微还没退干净就 Popen 新实例，等于给用户开两个"
        assert st["bound"] is False and "没有" in st["reason"] and "强杀" in st["reason"], st
    finally:
        p.undo()
    print("PASS 退不干净 → 放弃重启并说明，不杀全家也不开第二个实例")


def test_no_image_name_kill_of_wecom_in_production_code():
    """全仓生产代码里不许再出现 `taskkill ... /im WXWork`——这条是红线的静态兜底。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("dist", "build", "tests", "__pycache__",
                                                "产物库", "_dist_backup", "_rollback", "node_modules")]
        for f in files:
            if not f.endswith(".py"):
                continue
            pth = os.path.join(base, f)
            try:
                txt = io.open(pth, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for ln, line in enumerate(txt.splitlines(), 1):
                # 只认 argv 写法（`"/im", "WXWork.exe"`）——文档字符串里那句
                # "原来会 taskkill /f /im WXWork.exe" 是在**说明为什么删掉它**，
                # 按散文匹配会把这条锁自己判红（本项目注释风格的老坑）。
                low = line.lower().replace(" ", "")
                if '"/im"' in low and "wxwork" in low:
                    hits.append("%s:%s" % (f, ln))
    assert not hits, "还有按镜像名杀企微的代码：%s" % hits


def test_pc_apps_refuses_killing_wecom():
    """模型那条手也要按住：pc_apps(op="kill") 对企微进程名/企微 PID 一律拒绝。"""
    from agent_tools.os_ops import pc_apps
    for nm in ("WXWork.exe", "wxworkweb", "WXWork"):
        r = pc_apps("kill", name=nm)
        assert "拒绝" in err_msg(r), (nm, r)
    p = _Patches()
    try:
        p.set(wp, "find_wecom_all_pids", lambda: {4321})
        r = pc_apps("kill", name="4321")
        assert "拒绝" in err_msg(r), r
    finally:
        p.undo()


def test_check_command_blocks_killing_wecom():
    """`pc_run` 那条手也要按住：taskkill / Stop-Process 打企微都命中危险模式。"""
    from security_ops import check_command
    cfg = {"security": {}}
    for cmd in ('taskkill /f /im WXWork.exe',
                'taskkill /IM wxworkweb.exe /F',
                'powershell -c "Stop-Process -Name WXWork -Force"'):
        verdict, reason = check_command(cmd, cfg)
        assert verdict == "block", (cmd, verdict, reason)
    assert check_command("taskkill /f /pid 1234", cfg)[0] == "pass", "误伤正常 PID 结束"


def test_bind_route_refuses_kill_while_agent_busy():
    """confirm_kill 只该是"人坐在屏幕前点的那一下"。v1.0.425 记录过模型在任务中途
    自己 curl 这个接口重启企微，打断整条流水线——所以任何 agent 任务在跑都拒。"""
    import backend
    import wecom_bridge.windows as win
    client = backend.app.test_client()
    p = _Patches()
    called = {"bind": 0}
    try:
        import wecom_bridge as wb
        p.set(win, "_agent_busy", lambda: True)
        p.set(wb, "ensure_binding", lambda *a, **k: called.__setitem__("bind", called["bind"] + 1))
        r = client.post("/api/wecom/bind", json={"confirm_kill": True})
        assert r.status_code == 409, r.status_code
        assert called["bind"] == 0, "守卫之后还在跑绑定，等于没拦"
        # 判定取不到时按"在跑"处理（红线方向唯一：宁可不杀）
        p.set(win, "_agent_busy", lambda: (_ for _ in ()).throw(RuntimeError("探测失败")))
        assert client.post("/api/wecom/bind", json={"confirm_kill": True}).status_code == 409
        # 空闲 + 未确认 → 照常走 needs_kill 人工确认路径（不该被这条守卫误伤）
        p.set(win, "_agent_busy", lambda: False)
        p.set(wb, "ensure_binding", lambda cfg, force=False, allow_kill=False:
              {"bound": False, "changed": True, "needs_kill": True, "reason": "x", "windows": 0})
        r2 = client.post("/api/wecom/bind", json={})
        assert r2.status_code == 200 and r2.get_json().get("needs_kill"), r2.get_json()
    finally:
        p.undo()


# ---------- v1.0.646：CDP origin 收紧（安全，行为级锁） ----------
# 背景：--remote-allow-origins=* 等于把企微会话控制权开给本机任意进程/网页。
# v1.0.510 曾收紧失败，根因是 Python websocket 不带 Origin 头被内核当外来连接 403，
# 不是收紧本身不可行。现为「客户端带 Origin + 服务端白名单」配套，这里锁住两侧。

def test_cdp_flags_are_not_wildcard_by_default(monkeypatch):
    """默认启动 flag 绝不能再是 *：本机恶意网页带自有 Origin 必须被拒绝。"""
    monkeypatch.delenv("WECOM_CDP_ORIGINS", raising=False)
    flags = cc._cdp_allowed_origins()
    assert flags != "*", "默认又退回 --remote-allow-origins=*（本机任意网页可接管企微会话）"
    assert "127.0.0.1:8787" in flags, f"白名单要包含本软件自身 origin：{flags}"


def test_wildcard_escape_hatch_still_available(monkeypatch):
    """逃生门：WECOM_CDP_ORIGINS=* 可回退（企微内核版本差异导致误判 403 时用）。"""
    monkeypatch.setenv("WECOM_CDP_ORIGINS", "*")
    assert cc._cdp_allowed_origins() == "*"
    monkeypatch.setenv("WECOM_CDP_ORIGINS", "http://x.tld,http://y.tld")
    assert cc._cdp_allowed_origins() == "http://x.tld,http://y.tld"


def test_handshake_carries_matching_origin(monkeypatch):
    """行为锁：握手真的带了白名单内那个 Origin——只改 flag 不带头会 403 绑不上。"""
    seen = {}

    def fake_conn(ws_url, timeout=None, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(cc.websocket, "create_connection", fake_conn)
    cc._open_ws("ws://127.0.0.1:9222/devtools/page/T1", 5)
    assert seen.get("origin") == cc.CDP_CLIENT_ORIGIN, \
        f"握手没带 Origin 头（{seen}）：白名单收紧后企微内核会 403"
    assert cc.CDP_CLIENT_ORIGIN in cc._cdp_allowed_origins(), \
        "客户端 Origin 不在服务端白名单里 = 必然 403"


if __name__ == "__main__":
    test_cdp_is_wecom()
    test_port_hijack_auto_switch()
    test_reuse_and_ws_positive()
    test_relay_kill_all()
    # 注：origin 三条走 monkeypatch fixture，只能 pytest 跑（直跑请用 pytest tests/test_wecom_bind.py）
    print("\n== 全部通过：企微绑定强化 ==")


# ========== v1.0.651：CDP 端口自动识别空闲 + 避让占用者（用户："能改成自动识别优先使用空端口么"） ==========
# 起因是本机实测：9222 上有个对 /json/version 一律回 404（Content-Length:0、无 Server 头）
# 的东西占着。旧 cdp_up「requests 不抛异常就算活」（404 也不抛），旧 cdp_is_wecom 又因
# 响应体没特征返回 None（按"企微繁忙"留原端口）——两条一叠加，v1.0.336 那段自动换端口
# 永远不触发，界面就永久卡在「窗口列表读取失败：接口返回结构异常」。

import re as _re651
import socket as _socket651


def _port_of(url):
    return int(url.split("//", 1)[1].split("/")[0].split(":")[1])


def _http_stub(p, live=None, foreign=None, squatter=None):
    """按端口造 CDP 响应。live=企微自己的口；foreign=别的浏览器的 CDP；
    squatter=应答但不是 CDP（404 空响应，本机真实形态）；其余端口一律"连不上"。"""
    live = live or {}
    foreign = foreign or {}
    squatter = squatter or set()

    def fake_get(url, timeout=None):
        port = _port_of(url)
        ua = live.get(port)
        if ua:
            return _Resp(ua)
        if port in foreign:
            return _Resp('{"Browser":"Chrome/120.0.0.0"}')
        if port in squatter:
            return _Resp("", status_code=404, is_json=False)
        raise OSError("connection refused")

    p.set(cc.requests, "get", fake_get)
    # v1.0.652：port_bindable 必须与 HTTP 桩一致——squatter/live/foreign 都是
    # "有东西在听"（bind 失败），其余端口真空闲。旧版不桩 port_bindable，用例
    # 靠本机 9222 恰好被真实进程占用才碰巧通过（环境依赖型红/绿）。
    _busy = set(live) | set(foreign) | set(squatter)
    p.set(wp, "port_bindable", lambda port: port not in _busy)


def test_cdp_up_requires_200_and_json():
    """404 占用者与"企微繁忙"必须分得开：前者立刻判死，后者才值得重试。"""
    p = _Patches()
    try:
        _http_stub(p, live={9222: "WXWork/4.1"}, squatter={9333})
        assert cc.cdp_up(9222) is True
        assert cc.cdp_up(9333) is False, "404 空响应仍被当成活的 CDP——这次的根因"
        assert cc.cdp_up(9999) is False
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = io.open(os.path.join(_root, "wecom_bridge", "cdp_client.py"), encoding="utf-8").read()
        assert 'getattr(r, "status_code", 0) != 200' in src, "cdp_up 又回到『有响应就算活』"
    finally:
        p.undo()


def test_cdp_up_still_retries_real_transport_failures():
    """超时/拒绝连接=可能只是抖动，重试 3 次再判死（v1.0.425 那条不能顺手删）。"""
    calls = {"n": 0}
    p = _Patches()
    try:
        def boom(url, timeout=None):
            calls["n"] += 1
            raise OSError("timeout")
        p.set(cc.requests, "get", boom)
        p.set(cc.time, "sleep", lambda s: None)
        assert cc.cdp_up(9222) is False
        assert calls["n"] == 3, calls
    finally:
        p.undo()


def test_port_bindable_uses_a_real_socket():
    """"空闲"只能真 bind 一次来判：拿 cdp_up 的反面当空闲，会把企微启动到一个
    已被监听却不成 CDP 的端口上，比原报错更难查。"""
    held = _socket651.socket(_socket651.AF_INET, _socket651.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    busy = held.getsockname()[1]
    try:
        assert cc.port_bindable(busy) is False, "被监听的端口判成了空闲"
        assert cc.port_bindable(45555) is True
    finally:
        held.close()
    assert cc.port_bindable(busy) is True, "释放后仍判占用——测试自身得可靠"


def test_pick_cdp_port_prefers_a_live_wecom_port():
    p = _Patches()
    try:
        _http_stub(p, live={9222: "WXWork/4.1"})
        port, why = wp.pick_cdp_port(9222)
        assert port == 9222, (port, why)
        assert "复用" in why
    finally:
        p.undo()


def test_pick_cdp_port_avoids_squatter_and_takes_next_free():
    p = _Patches()
    try:
        _http_stub(p, squatter={9222}, live={9223: "WXWork/4.1"})
        port, _why = wp.pick_cdp_port(9222)
        assert port == 9223, port
    finally:
        p.undo()
    # 纯空闲顺延（9222 被监听、9223 真空着）
    p2 = _Patches()
    try:
        def fake_get(url, timeout=None):
            raise OSError("connection refused")
        p2.set(cc.requests, "get", fake_get)
        p2.set(wp, "port_bindable", lambda port: port != 9222)
        port, _why = wp.pick_cdp_port(9222)
        assert port == 9223, port
    finally:
        p2.undo()


def test_pick_cdp_port_does_not_move_off_an_unclear_port():
    """v1.0.555（P1-12）的教训不能因为这次收紧就丢回去：响应了但认不出是谁
    （代理/网关劫持、企微繁忙回空 200）不能断言"被非企微占用"——一次瞬时抖动
    就换端口 + 重启企微，丢的是用户未保存的聊天草稿。"""
    p = _Patches()
    try:
        def fake_get(url, timeout=None):
            return _Resp('{\"browser\":\"MysteryProxy/1.0\"}', status_code=200)
        p.set(cc.requests, "get", fake_get)
        assert cc.cdp_is_wecom(9222) is None
        port, why = wp.pick_cdp_port(9222)
        assert port == 9222, (port, why)
        assert "不断言" in why or "特征不明" in why
    finally:
        p.undo()


def test_pick_cdp_port_gives_up_with_a_clear_reason():
    p = _Patches()
    try:
        p.set(cc.requests, "get", lambda url, timeout=None: (_ for _ in ()).throw(OSError("refused")))
        p.set(wp, "port_bindable", lambda port: False)
        port, why = wp.pick_cdp_port(9222, span=3)
        assert port is None
        assert "占用" in why, why
    finally:
        p.undo()


def test_ensure_binding_avoids_port_without_touching_wecom():
    """端到端：9222 被 404 占用者挡住 → 端口避让并写回配置，但**一个进程都不动**。
    杀不杀企微仍归 allow_kill/确认闸管（v1.0.649 那条红线不能被这次的"自动"顺带绕开）。"""
    p = _Patches()
    killed = {"cmds": [], "spawn": 0}
    try:
        _http_stub(p, squatter={9222})
        p.set(wp, "find_wecom_pids", lambda: [111])
        p.set(wp, "find_wecom_all_pids", lambda: {111})
        p.set(wp, "detect_wecom_exe", lambda: FAKE_EXE)
        p.set(wp, "_cdp_ws_ok", lambda port=9222: False)
        p.set(wp, "_run_hidden", lambda cmd, **kw: killed["cmds"].append(cmd))
        p.set(wp.subprocess, "Popen",
              lambda *a, **k: killed.__setitem__("spawn", killed["spawn"] + 1))
        cfg = _reset_cfg()
        st = wp.ensure_binding(cfg, force=True, allow_kill=False)
        assert cfg["wecom"]["cdp_port"] == 9223, cfg["wecom"]["cdp_port"]
        assert st["port"] == 9223, st
        assert st.get("bound") is not True
        assert not killed["cmds"], killed["cmds"]
        assert killed["spawn"] == 0, "端口避让顺手把企微重启了——这是越权"
    finally:
        p.undo()


def test_port_setting_is_visible_and_range_guarded():
    """避让之后用户看得见"现在用的是哪个口"，也能手填一个（否则自动挪走就成了黑箱）。"""
    html = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "index.html"), encoding="utf-8").read()
    assert 'id="opt_cdp_port"' in html and "自动往后找一个空闲端口" in html
    js = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "js", "05-connectors-status.js"), encoding="utf-8").read()
    assert "_cpv >= 1024 && _cpv <= 65535" in js, "端口不做范围守卫会把绑定整体拖到不可用口"
    assert "w.cdp_port" in js
    # v1.0.652（审计 P1-7）：字段契约锁——js 读的键必须在 bind_status 响应里真实存在。
    # 旧版前端读 w.cdp_port、后端只回 port：设置页恒显 9222，且 refreshStatus 每 8s
    # 把输入框刷回 9222 后任何一次保存都反向覆盖自动避让端口；本测试当时只断言 js
    # 源码含字符串，把错误字段名锁死（假安全感）。
    wecom_src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "routes", "wecom.py"), encoding="utf-8").read()
    assert '"cdp_port": port' in wecom_src, "bind_status 必须回传 cdp_port（前端契约）"
    assert "_cdpPortDirty" in js, "端口提交需脏标记，防止轮询回填值覆盖自动避让端口"

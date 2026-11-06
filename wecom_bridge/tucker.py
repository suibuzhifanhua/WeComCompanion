"""新窗口自动后台化（window-tucker）：只收纳**本软件自己开的**企微新窗口
（v1.0.656 自 windows.py 拆出）。

兼容口径：对 windows 命名空间的运行期引用统一走 `_w.<name>` 动态属性
（`from . import windows as _w`），保证 monkeypatch.setattr(wecom_bridge.windows, "X")
与拆分前「windows.py 模块内直调」语义完全一致（含本模块内互相调用）。
"""

import sys as _sys

from .cdp_client import DEFAULT_PORT
from . import windows as _w

# ---------- 新窗口自动后台化（v1.0.274） ----------
# 用户在做自己的事时，agent 点开的学习平台独立窗口（课程/视频/考试/地图）默认弹到
# 最前面盖住工作。agent 的操作全走 CDP/DOM 级（read/click/type 均不依赖 OS 前台），
# 因此新学习窗口一出现就按标题找到其 OS 窗口并最小化（ShowWindow SW_MINIMIZE，
# 不激活、不抢焦点）；用户需要时从任务栏点一下即恢复。
# 约束：
#   - 只处理 URL 命中学习平台域的窗口（真实域名由 config.study.learn_url_marks 提供），用户自己的
#     OA/文档/微盘窗口一律不碰；
#   - 只处理「新出现」的 target（启动时快照存量，存量不动）；用户手动恢复过
#     的窗口不会再次被最小化（其 target 已入 known）；
#   - 企微 CDP 未暴露 Browser 窗口级 API（getWindowForTarget 报
#     "Browser window not found"），故走 OS 级 user32，仅 Windows 生效。
#   - 窗口最小化后像素截图会拿不到新帧 → 走既有「DOM 绘制截图」降级，
#     agent 的 DOM 读/点/输入完全不受影响。
_WIN32 = (_sys.platform == "win32")
_LEARN_URL_MARKS = ("learn.example.com",)   # v1.0.657：中性默认；真实课程域名经 _effective_learn_marks 从 config 读取
# v1.0.523 静默模式保护域：这些域的「新窗口」即使是任务打开的也不收纳——
# 批审/表单流程自身依赖的列表页/门户/企微文档页被最小化不影响 CDP，但保险起见要保下来。
# v1.0.616（审查 P2）：这份名单此前在 services/batch_flow.py::_KEEP_URL_PAT 又抄了一遍
# （注释还写着"两处修改时同步改"——靠人记忆的同步迟早漏）。现在这里是唯一事实源，
# batch_flow 从这里 import；名单改动只需改这一处，且有源码锁盯着不许再出现第二份字面量。
KEEP_URL_MARKS = ("tpp-web", "portal.example.com", "doc.weixin.qq.com", "file://")   # v1.0.657：portal 位为中性占位，真实门户域名经 _effective_keep_marks 从 config 追加
_SILENT_KEEP_URL_MARKS = KEEP_URL_MARKS      # 本文件内旧名沿用


def _effective_keep_marks():
    """v1.0.657 公开发布匿名化：真实门户域名不写死在源码——从 config.study.portal_mark
    追加进 keep 名单（未配置时名单即中性默认）。"""
    try:
        from core.config_store import load_config
        s = (load_config() or {}).get("study") or {}
        pm = str(s.get("portal_mark") or "").strip()
    except Exception:  # noqa: BLE001
        pm = ""
    return KEEP_URL_MARKS + (pm,) if pm and pm not in KEEP_URL_MARKS else KEEP_URL_MARKS


def _effective_learn_marks():
    """同上：真实学习平台课程域名由 config.study.learn_url_marks 提供。"""
    try:
        from core.config_store import load_config
        s = (load_config() or {}).get("study") or {}
        lm = s.get("learn_url_marks")
    except Exception:  # noqa: BLE001
        lm = None
    if isinstance(lm, (list, tuple)) and lm and all(str(x).strip() for x in lm):
        return tuple(str(x).strip() for x in lm)
    return _LEARN_URL_MARKS

# ---------- v1.0.570（用户需求）：收纳只在 agent 任务执行时发生 ----------
# 用户反馈：打开软件后企微所有新开页面都被自动缩小——空闲时自己开的页面不想被收。
# busy 信号 = LLM 在途 / 后台批审运行中 / 距上次任务活动 <30s（工具执行间隙容忍）。
_BUSY_GRACE_S = 30.0
_last_agent_active = 0.0


def _agent_busy():
    """agent 任务执行中判定：tucker 收纳与启动收纳的统一闸门。
    空闲时（False）新开页面保持原样；tucker 循环仍会更新快照（防任务开始后误缩）。"""
    import time as _t
    try:
        from llm_client import llm_active_count
        if llm_active_count() > 0:
            setattr(_w, "_last_agent_active", _t.monotonic())
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from services import batch_flow as _bf
        if _bf.background_progress().get("running"):
            setattr(_w, "_last_agent_active", _t.monotonic())
            return True
    except Exception:  # noqa: BLE001
        pass
    return (_t.monotonic() - _w._last_agent_active) < _w._BUSY_GRACE_S


_TUCKER_STARTED = False


def _silent_mode():
    """v1.0.523：静默模式总开关（wecom.silent_mode，默认 True）。
    函数内惰性 import 防止 core↔bridge 潜在循环；读不到配置按静默处理。"""
    try:
        from core.config_store import silent_mode as _sm
        return _sm()
    except Exception:  # noqa: BLE001
        return True


def window_enum_probe():
    """v1.0.284：「枚举到 0 个可见窗口」分层探针（只读，不做任何最小化/修改）。
    背景：2026-09-04 exe（session 1 交互桌面，用户双击启动）内 window_diag 实测
    EnumWindows 仍返回 0 个可见窗口——已排除「非交互会话/隐藏窗口站」假设，
    锁定「安全软件钩子拦截枚举链路」，但不知道拦在哪一层。本探针分层计数：
      raw_count    = 回调实际收到的顶层窗口数（=0 → EnumWindows/回调层被拦）
      visible_count= 其中 IsWindowVisible 为真的数（=0 → IsWindowVisible 被拦）
      titled_count = 其中 GetWindowTextW 返回非空标题的数（=0 → GetWindowTextW 被拦）
      shell/foreground = 已知句柄（任务栏/前台窗）单独验证（正常应 visible=true 且
      任务栏标题为 "Taskbar"）——若全链路计数 0 但 shell.visible=true，
      说明钩子只针对枚举回调路径。"""
    if not _w._WIN32:
        return {"error": "非 Windows 平台"}
    out = {}
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        u32.EnumWindows.restype = wintypes.BOOL
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsWindowVisible.restype = wintypes.BOOL
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowTextW.restype = ctypes.c_int
        cnt = {"raw": 0, "vis": 0, "titled": 0}
        samples = []

        def _cb(h, l):
            cnt["raw"] += 1
            try:
                if u32.IsWindowVisible(h):
                    cnt["vis"] += 1
                    n = u32.GetWindowTextW(h, None, 0)
                    if n > 0:
                        cnt["titled"] += 1
                        if len(samples) < 5:
                            buf = ctypes.create_unicode_buffer(n + 1)
                            u32.GetWindowTextW(h, buf, n + 1)
                            samples.append(buf.value[:50])
            except Exception as e:  # noqa: BLE001
                out.setdefault("cb_err", []).append(f"{type(e).__name__}")
            return True

        _ref = WNDENUMPROC(_cb)
        out["enum_returned"] = bool(u32.EnumWindows(_ref, 0))
        out["raw_count"] = cnt["raw"]
        out["visible_count"] = cnt["vis"]
        out["titled_count"] = cnt["titled"]
        out["samples"] = samples
        # 已知句柄单独验证（GetShellWindow=任务栏，正常可见且标题 Taskbar）
        for label in ("shell", "foreground"):
            fn = u32.GetShellWindow if label == "shell" else u32.GetForegroundWindow
            fn.argtypes = []
            fn.restype = wintypes.HWND
            h = fn()
            if h:
                out[label] = {"hwnd": str(h),
                              "visible": bool(u32.IsWindowVisible(h)),
                              "title_len": u32.GetWindowTextW(h, None, 0)}
            else:
                out[label] = None
        # v1.0.285：验证「PID 关联最小化」方案的关键 API（GetWindowThreadProcessId）
        # 是否也被钩子拦截——foreground_pid 为 0/None 即被拦，方案作废需另想。
        # 同时统计企微 PID 名下可见窗口数（该路径的前置条件，正常应 ≥1）。
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        fg = u32.GetForegroundWindow()
        if fg:
            pid = wintypes.DWORD(0)
            u32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            out["foreground_pid"] = pid.value or None
        else:
            out["foreground_pid"] = None
        try:
            from .wecom_proc import find_wecom_all_pids
            wx_pids = find_wecom_all_pids()
        except Exception:  # noqa: BLE001
            wx_pids = set()
        out["wxwork_pids"] = sorted(wx_pids)
        cnt2 = {"wx_win": 0}

        def _cb2(h, l):
            try:
                if u32.IsWindowVisible(h):
                    pid = wintypes.DWORD(0)
                    u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    if pid.value in wx_pids:
                        cnt2["wx_win"] += 1
            except Exception:  # noqa: BLE001
                pass
            return True

        _ref2 = WNDENUMPROC(_cb2)
        u32.EnumWindows(_ref2, 0)
        out["wxwork_visible_windows"] = cnt2["wx_win"]
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _tuck_binding_ok(port):
    """本软件是否真的**能驱动**企微（不是只看 /json 通不通）。

    v1.0.629（用户实报：没绑定企微，软件照样缩他的窗口）：`/json` 可读但
    WebSocket 被 origin 限制拒（403）时，软件实际处于「未绑定」——bind 日志
    明写 bound=False，而 tucker 只看 list_targets 返回是不是 list，于是
    "未绑定的软件"照样在管企微窗口。判据改为与 ensure_binding 同源的 WS 探测，
    探测异常一律按未绑定处理（fail-closed：不动任何窗口）。TTL 缓存 20s，
    只在真的有待收纳登记时才打。"""
    now = _w.time.monotonic()
    if _w._bind_probe["ts"] and now - _w._bind_probe["ts"] < 20.0:
        return _w._bind_probe["ok"]
    try:
        from .cdp_client import _cdp_ws_ok
        ok = bool(_cdp_ws_ok(port))
    except Exception as e:  # noqa: BLE001
        _w._logw(f"[window-tucker] 绑定探测异常（按未绑定处理，不动窗口）：{e}")
        ok = False
    _w._bind_probe["ts"] = now
    _w._bind_probe["ok"] = ok
    return ok


def _target_title_for(targets, target_id, url):
    """按登记的 target id（退化到 URL 前缀）在 CDP 列表里找当前标题。找不到返回 ""。"""
    if not isinstance(targets, list):
        return ""
    for t in targets:
        if not isinstance(t, dict):
            continue
        if target_id and t.get("id") == target_id:
            return (t.get("title") or "").strip()
    if url:
        key = url.split("#")[0]
        for t in targets:
            if isinstance(t, dict) and (t.get("url") or "").split("#")[0] == key:
                return (t.get("title") or "").strip()
    return ""


def _fresh_hwnds(cur_hwnds, known_hwnds, owner_base):
    """归属差集（单点判据）：既不在"开页动作前"的基线里、也不在上一轮快照里的窗口，
    才算本软件这次开页开出来的。owner_base 为 None（没拍到基线）⇒ 一律不算，
    宁可不收也不许把用户已有窗口算进来。"""
    if owner_base is None:
        return set()
    _base = set(owner_base or ())
    _known = set(known_hwnds or ())
    return {h for h in (cur_hwnds or []) if h not in _base and h not in _known}


def _tuck_fresh_hwnds(cur_hwnds, known_hwnds, why, busy=True, owner_base=None):
    """静默收纳的唯一入口：最小化 hwnds 中「新出现」的窗口，返回 (新的 known 集, 收纳数)。

    v1.0.594：从 start_window_tucker 的闭包里提到模块级，并把 v1.0.570 的
    「只有 agent 任务执行中才收纳」门禁做成**本函数自己的 busy 参数**——
    原来门禁只写在 CDP 路径的判断链上，CDP 停摆那条兜底分支另起炉灶直接调它，
    于是没绑定/企微没带调试端口时（CDP 必然不可用）用户自己开的每个独立页
    都会在 1.2s 内被最小化（用户实报）。现在空闲状态下无论从哪条路径进来，
    都只会把基线刷成当前窗口集，不会动手。

    v1.0.629：再加一道**归属**门禁 owner_base——它是本软件开页动作**之前**的窗口快照。
    只有既不在 known（上次轮询）也不在 owner_base（开页前）里的窗口才算"软件自己开的"。
    owner_base=None（没有任何开页登记）时**一律不动**：busy 只代表"软件在忙"，
    不代表"这个窗口是软件开的"，而后者才是红线。
    """
    cur = list(cur_hwnds or [])
    _known = set(known_hwnds or ())
    if not busy or owner_base is None:
        # 不动手，但把基线刷成当前窗口集（否则下一次登记会把用户已有窗口算成新窗口）
        return set(cur), 0
    fresh = sorted(_w._fresh_hwnds(cur, _known, owner_base))   # 差集判据单点在 _fresh_hwnds
    if not fresh:
        return _known | set(cur), 0
    _st3, _n = _w._stage_guard(lambda: _w._minimize_hwnds(fresh), 5.0, why)
    if _st3 == "ok" and _n:
        _w._logw(f"[window-tucker] 收纳本软件开页 {_n} 个窗口（{why}）")
        return (_known | set(cur)), _n
    return _known | set(cur), 0


def _tucker_tick(port, state):
    """一轮收纳判定（v1.0.629 从 start_window_tucker 的闭包里提出来——红线必须可单测）。

    state: {"known_hwnds": set|None} 跨轮窗口基线。返回本轮实际最小化的窗口数。
    三道闸，缺一即不动手：
      ① 有本软件自己的开页登记（_pop_open_marks，取出即消耗、过期作废）；
      ② 本软件真能驱动企微（_tuck_binding_ok：WS 级探测，探测异常按未绑定处理）；
      ③ 目标窗口既不在"开页动作前"的基线里、也不在上一轮快照里（owner_base 差分）。
    旧口径是「agent 任务执行中出现的新 target/新窗口就收」：busy 覆盖整个 LLM 流
    （实测一次生成 PPT 的思考死循环里，用户自己点的 5 个企微窗口在 15 分钟内被连着收掉），
    而且它压根不看窗口是谁开的——归属这一维从来没进过判据。
    """
    marks = _w._pop_open_marks()
    if not marks:
        # v1.0.632（B2）：空闲轮**什么都不做**。629 在这里每 1.2s 起一个线程做 EnumWindows
        # 刷基线——实测 200 次空闲 tick = 200 线程 + 200 次枚举（3,000 线程/小时、72,000/天
        # 纯空转，与 v1.0.355 降空闲开销的取向相反）。归属判据用的是**登记时拍的开页前快照**
        # （note_agent_open 自带 baseline），跨轮基线对"该不该收"已经没有作用，故整段删除。
        return 0
    if not _w._tuck_binding_ok(port):
        if not _w._unbound_warned["flag"]:
            _w._unbound_warned["flag"] = True
            _w._logw(f"[window-tucker] 未真正绑定企微（CDP WebSocket 不可用）——不收纳任何窗口，"
                     f"{len(marks)} 条开页登记作废。要恢复静默请在企微页点「重启并绑定」。")
        return 0
    if _w._unbound_warned["flag"]:
        _w._unbound_warned["flag"] = False
    silent = _w._silent_mode()
    tg = _w.list_targets(port)
    tucked = 0
    for m in marks:
        url = m.get("url") or ""
        if any(k in url for k in _w._effective_keep_marks()):
            # 保护域（批审列表/门户/企微文档页）：任务流程自身依赖它在前台可点，
            # 后台最小化会把「回退前台点击」那一支打死——v1.0.523 的口径原样保留。
            continue
        if not silent and not any(k in url for k in _w._effective_learn_marks()):
            continue      # 非静默模式维持 v1.0.274 原口径：只管学习平台域
        title = _w._target_title_for(tg, m.get("target_id"), url)
        # v1.0.632（B4）：先算差集，标题路径也只在这批"登记之后新出现的窗口"里匹配。
        # 原来 minimize_windows_by_title 是全局按标题模糊匹配——用户自己开着的同名窗口
        # （同一张 OA 单据开了两个窗口）会被一起最小化，红线同源。
        _st2, cur = _w._stage_guard(_w._wxwork_visible_hwnds, 5.0, "wxhwnds-mark")
        if _st2 != "ok":
            _w._logw(f"[window-tucker] 企微窗口枚举失败（{_st2}）——跳过《{title[:36] or '短标题页'}》")
            continue
        _prev_known = set(state.get("known_hwnds") or ())
        fresh = _w._fresh_hwnds(cur, _prev_known, m.get("baseline"))
        if not fresh:
            # 登记了但没开出新窗口（复用既有标签页 / 秒开秒关）——不动任何窗口，只推进基线
            state["known_hwnds"] = _prev_known | set(cur)
            continue
        if title and len(title) >= 4:
            # v1.0.282：OS 最小化走超时守卫——EnumWindows/GetWindowTextW 若跨线程死等
            # 挂起窗口属主线程，旧代码会静默杀死整个 tucker 循环（守护线程一死全停且无日志）。
            _st, _res = _w._stage_guard(lambda: _w.minimize_windows_by_title(title, only=fresh),
                                        5.0, f"min:{title[:20]}")
            if _st == "ok" and _res:
                _w._logw(f"[window-tucker] 本软件开页已收纳：《{title[:40]}》")
                tucked += len(_res)
                state["known_hwnds"] = _prev_known | set(cur)
                continue
            if _st == "ok":
                # v1.0.652（审计 P1-15）：标题可读但没匹配上任何 fresh 窗口——这批窗口
                # 对不上登记页（很可能是用户自己新开的）。红线"宁可不收也不误收"：
                # 不收，只推进基线。旧实现落到下方盲收差集，用户窗口在窄时窗内被最小化。
                state["known_hwnds"] = _prev_known | set(cur)
                continue
            if _st != "ok":
                _w._logw(f"[window-tucker] 标题路径失败（{_st}）——改用开页差分《{title[:24]}》")
        # 标题不可读/过短（OA 表单初始就是 about:blank）→ 差分兜底。
        # v1.0.652（审计 P1-15）：仅当登记后 ≤6s 内才盲收——软件开页的窗口创建是即时的，
        # 超过此时窗 fresh 差集已混入"用户在登记后自己点开的窗口"（基线管不到），
        # 盲收即触碰产品红线。宁可本软件的窗口少收几个，也不许动用户自开窗口。
        _mark_age = (_w.time.monotonic() - m["ts"]) if m.get("ts") else 1e9
        if _mark_age > 6.0:
            _w._logw(f"[window-tucker] 标题不可读且登记已 {_mark_age:.0f}s——fresh 差集已不可信，"
                     f"按红线宁可不收（保护用户自开窗口）：《{(title or url or m.get('why') or '未命名')[:32]}》")
            state["known_hwnds"] = _prev_known | set(cur)
            continue
        # 复用上面已经取好的 cur（v1.0.632：不再二次枚举），差集判据仍单点在 _tuck_fresh_hwnds。
        state["known_hwnds"], _n = _w._tuck_fresh_hwnds(
            cur, _prev_known,
            f"开页登记《{(title or url or m.get('why') or '未命名')[:32]}》",
            True, m.get("baseline"))
        tucked += _n
    return tucked


def start_window_tucker(port=DEFAULT_PORT, interval=2.5):
    """后台线程（v1.0.274）：把**本软件自己刚打开的**企微窗口最小化，新开的课程/视频/
    考试/OA 详情不再弹到用户工作面上方。非 Windows 或重复调用返回 None。

    v1.0.629：判据从「任务期间出现的新窗口」换成「本软件开页动作之后出现的窗口」，
    并加绑定闸（WS 可用才算绑定）。一轮逻辑全在 _tucker_tick 里（可单测），本函数只
    负责起线程与拍启动基线。轮询间隔仍 1.2s（静默，v1.0.523）/2.5s（v1.0.355）。"""
    if getattr(_w, "_TUCKER_STARTED", False) or not _w._WIN32:
        return None
    setattr(_w, "_TUCKER_STARTED", True)

    import threading as _th
    import time as _tm

    def _loop():
        state = {"known_hwnds": None}
        try:
            # v1.0.361 审查修复（P8）：启动快照走超时守卫——挂起会让线程在首次迭代前死掉
            _st0, _h0 = _w._stage_guard(lambda: set(_w._wxwork_visible_hwnds()), 5.0, "snapshot-hwnds")
            if _st0 == "ok":
                state["known_hwnds"] = _h0
            _w._logw(f"[window-tucker] 已启动（只收纳**本软件自己开**的窗口；存量企微窗口 "
                     f"{len(state['known_hwnds'] or [])} 个一律不动；"
                     f"标题路径={'可用' if _w._title_path_ok() else '不可用（安全软件拦 GetWindowTextW），改用开页登记的窗口差分'}；"
                     f"静默模式={'开' if _w._silent_mode() else '关（仅学习平台域）'}）")
        except Exception as e:  # noqa: BLE001
            _w._logw(f"[window-tucker] 初始快照失败（稍后重试）：{e}")
        while True:
            _tm.sleep(1.2 if _w._silent_mode() else interval)
            try:
                _w._tucker_tick(port, state)
            except Exception as e:  # noqa: BLE001
                _w._logw(f"[window-tucker] 异常（忽略继续）：{e}")

    th = _th.Thread(target=_loop, name="window-tucker", daemon=True)
    th.start()
    return th


def tuck_new_windows(baseline, why="自动化新开页", restore_to=None):
    """即时收纳 baseline 之后新出现的企微可见窗口，并把前台还给 restore_to。

    为什么批审要自己做一份而不用后台 window-tucker：tucker 的收纳门禁是
    _agent_busy()（v1.0.594 红线修复——空闲时绝不动用户窗口），任务执行中它
    按 1.2~2.5s 轮询才收一次，于是自动化自己开的详情页会先抢走用户焦点、
    再被收掉（2026-10-22 实测一轮月报 23 张 = 抢 23 次焦点）。本函数在
    "内容已确认、马上要读数据"的那一刻立刻收，并把焦点当场还回去。
    返回 (新基线集合, 收纳个数)。
    """
    cur = _w._wxwork_visible_hwnds()
    known = set(baseline or set())
    fresh = [h for h in (cur or []) if h not in known]
    n = 0
    if fresh:
        _st, _n = _w._stage_guard(lambda: _w._minimize_hwnds(fresh), 5.0, "tuck:" + (why or "")[:20])
        if _st == "ok" and _n:
            n = _n
            _w._logw(f"[win-tuck] 即时收纳自动化新开窗口 {_n} 个（{why}）")
            known = known | set(fresh)
    if restore_to:
        _w.restore_foreground(restore_to)
    return known, n

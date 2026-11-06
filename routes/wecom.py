"""
routes/wecom — 企微绑定/窗口/抓取/互动元素/截图/发消息 + bind_status
====================================================================
从 backend.py 逐字迁入（T05）。R7 修复：补 save_workspace_file import（原缺失致截图落盘静默失效）。
"""
import os
import time
import wecom_bridge as wb
from flask import Blueprint, request, jsonify

from core_utils import log_event
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）
from core.workspace_io import save_workspace_file  # v1.0.562 修复：拆分后函数在 workspace_io，原 paths. 断链
from core.config_store import load_config, update_config, _save_config_safe  # noqa: F401
from agent_tools import _cdp_port

# 绑定状态缓存：v1.0.510 起定义在 core/wecom_state.py（backend 与本蓝图共用同一
# dict 对象；原"backend 反向 import routes.wecom._BIND_CACHE"的入口层私有符号依赖已消除）
from core.wecom_state import _BIND_CACHE  # noqa: F401（re-export 供存量 patch/引用兼容）

bp = Blueprint("wecom", __name__, url_prefix="/api")

@bp.route("/wecom/bind", methods=["POST"])
def api_wecom_bind():
    # 强杀/重启企微属破坏性动作：后端强制要求 confirm_kill=true 才放行（P0-2）
    data = request.get_json(force=True, silent=True) or {}
    confirm_kill = bool(data.get("confirm_kill", False))
    # v1.0.413（观测点）：绑定流程全程留痕——2026-09-30 17:07 实测点击绑定后软件
    # 进程消失且零日志（无崩溃记录、无 taskkill 证据），入口无日志导致完全无法回溯。
    log_event(f"[bind] 收到绑定请求：confirm_kill={confirm_kill} pid={os.getpid()}")
    # v1.0.425（2026-10-01 23:34 实测）：审计流水线运行期间拒绝杀企微型绑定——
    # 模型曾在附件下载卡住时自行 curl 本 API 重启企微，打断整条流水线。
    if confirm_kill:
        try:
            from services import batch_flow as _bf
            if _bf._AUDIT_RUNNING.is_set():
                log_event("[bind] 拒绝：审计流水线运行中，禁止重启企微")
                return jsonify({"ok": False,
                                "error": "月报审计流水线正在运行，禁止重启企业微信。请等待流水线结束（或先停止审计任务）再绑定。"}), 409
        except Exception:  # noqa: BLE001
            pass
    # v1.0.646（用户："软件修复的时候不要影响企业微信"）：把上面那条"审计中拒杀"
    # 扩成**任何 agent 任务在跑都拒**——历史事故就是模型在任务中途自己 curl 这个接口
    # 重启企微（v1.0.425 记录）。confirm_kill 只该是"人坐在屏幕前点的那一下"，
    # 任务跑着的时候它一定不是人点的（人在看着界面操作时不会有并发 agent 任务）。
    if confirm_kill:
        try:
            from wecom_bridge.windows import _agent_busy
            if _agent_busy():
                log_event("[bind] 拒绝：agent 任务正在运行，禁止重启企业微信")
                return jsonify({"ok": False,
                                "error": "有任务正在运行，期间不会重启企业微信（你的企微窗口可能正被任务使用）。"
                                         "请等任务结束或先点「停止」，再到 设置 → 企业微信 里绑定。"}), 409
        except Exception as e:  # noqa: BLE001
            # fail-closed：判据取不到就当"在跑"，宁可不杀也不能误杀（这条是红线，方向唯一）
            log_event(f"[bind] agent 忙判定异常，按运行中处理（拒绝杀企微）：{e}")
            return jsonify({"ok": False,
                            "error": "暂时无法确认是否有任务在运行，为保护企业微信已拒绝本次重启。请稍后再试。"}), 409
    # v1.0.555（P0-10）：ensure_binding 就地改 cfg['wecom']（exe_path/bound_pid/
    # bound_sig/bound_started_ts/cdp_port），原先 load→ensure_binding→save——绑定
    # 流程内含拉进程+CDP 等待可达数十秒，期间用户的其它配置保存会被这个陈旧快照
    # 整体回滚。改为：事务内 fresh cfg 上重跑 ensure_binding 的落盘 delta。
    # （ensure_binding 本身不写盘，只在 cfg 上记 delta，故可安全重入。）
    cfg = load_config()
    st = wb.ensure_binding(cfg, force=True, allow_kill=confirm_kill)
    log_event(f"[bind] 绑定流程返回：bound={st.get('bound')} reason={str(st.get('reason'))[:60]}")
    # v1.0.652（P1-17）+ v1.0.654 A9：按键合并进 fresh cfg（绝不整段覆盖 wecom 段），
    # 合并器单源在 core.config_store.make_bind_merger（与 backend 启动绑定同源）。
    from core.config_store import make_bind_merger
    ok = update_config(make_bind_merger(cfg))
    if not ok:
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    _BIND_CACHE.clear()
    _bs_cache_invalidate()   # v1.0.364（H6）：绑定状态变化立即失效缓存
    return jsonify(st)


@bp.route("/wecom/windows")
def api_windows():
    return jsonify(wb.window_list_summary(_cdp_port(load_config())))


@bp.route("/wecom/close", methods=["POST"])
def api_wecom_close():
    # v1.0.272：工作台窗口列表手动关闭单个窗口。复用 agent 工具验证过的
    # wb.close_page（CDP /json/close/<targetId>）。pick 必填——缺省 close_page
    # 会关 agent 的「当前跟随窗口」，手动入口绝不能误伤 agent 会话。
    data = request.get_json(force=True, silent=True) or {}
    pick = data.get("pick")
    if pick is None:
        return jsonify({"ok": False, "error": "未指定窗口序号（pick）"}), 400
    try:
        pick = int(pick)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "pick 必须是窗口序号（整数）"}), 400
    result = wb.close_page(pick, _cdp_port(load_config()))
    if result.get("ok"):
        log_event(f"[wecom] 工作台手动关闭窗口 {result.get('closed_index')}"
                  f"（{(result.get('title') or '')[:40]}）")
    return jsonify(result)


@bp.route("/wecom/capture")
def api_capture():
    pick = request.args.get("pick", type=int)
    return jsonify(wb.capture(pick, _cdp_port(load_config())))


@bp.route("/wecom/interactive")
def api_interactive():
    pick = request.args.get("pick", type=int)
    return jsonify(wb.get_interactive_elements(pick, _cdp_port(load_config())))


@bp.route("/wecom/screenshot")
def api_screenshot():
    pick = request.args.get("pick", type=int)
    # v1.0.225：预览截图默认【不再落盘】——前端实时预览每 5s 轮询一次，
    # 旧版每次调用都存 PNG，实测堆积 11176 张 / 2.3GB（agent 操作是 DOM/CDP 级，
    # 不依赖截图；截图只是给人看的预览）。需要保存时显式传 save=1。
    # v1.0.274：默认【不再前置窗口】——5s 轮询每轮都 Page.bringToFront 是
    # 「企微窗口反复弹到最前盖住用户工作」的主因；仅在 save=1（手动存图）
    # 或 front=1（显式要求）时前置强制新帧。
    front = request.args.get("front") == "1" or request.args.get("save") == "1"
    result = wb.capture_screenshot(pick, _cdp_port(load_config()), front=front)
    if request.args.get("save") == "1":
        data = result.get("data")
        if data:
            import time as _tm, base64 as _b64
            fname = "screenshot_" + _tm.strftime("%Y%m%d_%H%M%S") + ".png"
            try: save_workspace_file("下载", fname, _b64.b64decode(data))
            except Exception:  # noqa: BLE001
                pass
    return jsonify(result)


@bp.route("/wecom/window_diag")
def api_window_diag():
    """v1.0.274 诊断（只读）：CDP 页面列表 vs OS 可见窗口标题列表。
    用于验证「新学习窗口自动最小化」是否生效；若 companion.log 出现
    「未找到 OS 窗口」，用本端点比对两侧标题修正 _title_match 规则。
    v1.0.282：三个阶段（cdp/os/proc）各自 5s 超时守卫——v1.0.279 的
    GetUserObjectInformationW 在 exe 内崩 libffi 拖死全进程的教训：任何单阶段
    挂起都不允许拖死端点（超时返回部分数据 + stage_status 标注）。"""
    port = _cdp_port(load_config())

    def _cdp_stage():
        tg = wb.list_targets(port)
        return ([{"title": (t.get("title") or "")[:60], "url": (t.get("url") or "")[:90]}
                 for t in tg if isinstance(t, dict) and t.get("type") == "page"]
                if isinstance(tg, list) else [])

    st_cdp, cdp_pages = wb._stage_guard(_cdp_stage, 5.0, "cdp")
    st_os, os_windows = wb._stage_guard(wb.list_os_window_titles, 5.0, "os")
    st_proc, process = wb._stage_guard(wb.process_session_info, 5.0, "proc")
    resp = {"cdp_pages": cdp_pages, "os_windows": os_windows, "process": process}
    if "ok" not in (st_cdp, st_os, st_proc):
        resp["stage_status"] = {"cdp": st_cdp, "os": st_os, "proc": st_proc}
    return jsonify(resp)


@bp.route("/wecom/window_probe")
def api_window_probe():
    """v1.0.284 诊断（只读）：「枚举到 0 个可见窗口」分层探针。
    逐层计数 EnumWindows 回调 / IsWindowVisible / GetWindowTextW，
    定位安全软件钩子拦截的具体层级（见 window_enum_probe 文档）。"""
    st, res = wb._stage_guard(wb.window_enum_probe, 10.0, "probe")
    if st != "ok":
        return jsonify({"stage": st, "detail": res})
    return jsonify(res)


@bp.route("/wecom/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    hook = (cfg.get("wecom", {}) or {}).get("webhook", "")
    return jsonify(wb.send_webhook(hook, data.get("content", ""), data.get("msgtype", "text")))

# ---------- 绑定状态（不重启） ----------
# v1.0.364 审查修复（H6）：前端每 8s 轮询 /api/status → bind_status 每次做进程快照
# + 3 次 CDP HTTP + 1 次 WS 握手（~1.08 万次/天）。结果缓存 3s——状态感知延迟可忽略，
# CDP churn 大幅下降；绑定/解绑等写操作直接失效缓存。
_BS_CACHE = {"ts": 0.0, "data": None}
_BS_TTL = 3.0
_BS_STALE_MAX = 30.0        # v1.0.540：陈旧值最长可用期（超此才同步阻塞刷新）
_BS_LOCK = __import__("threading").Lock()
_BS_REFRESHING = False


def _bs_cache_invalidate():
    with _BS_LOCK:
        _BS_CACHE["ts"] = 0.0


def _bs_refresh_async(cfg):
    """后台刷新绑定状态（v1.0.540 stale-while-revalidate）——企微 CDP 探测最坏 4s，
    /api/status 高频调用绝不同步等它（实测 status 响应 4.2s→瞬时）。"""
    global _BS_REFRESHING
    with _BS_LOCK:
        if _BS_REFRESHING:
            return
        _BS_REFRESHING = True

    def _job():
        global _BS_REFRESHING
        try:
            bind_status(cfg, force_sync=True)
        except Exception:  # noqa: BLE001
            pass
        finally:
            with _BS_LOCK:
                _BS_REFRESHING = False
    __import__("threading").Thread(target=_job, name="bs-refresh", daemon=True).start()


def bind_status(cfg, force_sync=False):
    now = time.time()
    with _BS_LOCK:
        cached = (_BS_CACHE["data"] is not None and now - _BS_CACHE["ts"] < _BS_STALE_MAX)
        if cached:
            d = dict(_BS_CACHE["data"])
            age = now - _BS_CACHE["ts"]
    if cached:
        # 3s~30s：返回陈旧值 + 后台刷新（force_sync=True 的调用方【绑定页】仍同步等待新鲜值）
        if age > _BS_TTL and force_sync:
            pass   # 落到下方同步探测
        else:
            if age > _BS_TTL:
                _bs_refresh_async(cfg)
            d["cached"] = True
            return d
    w = cfg.get("wecom", {}) or {}
    port = _cdp_port(cfg)   # v1.0.588：统一口径（原 w.get("cdp_port", 9222) 在值为空串时会透出空端口）
    exe = w.get("exe_path") or wb.detect_wecom_exe()
    if exe:
        w["exe_path"] = exe
    pids = wb.find_wecom_pids()
    up = wb.cdp_up(port)
    prev = w.get("bound_pid")
    alive = prev in pids if prev else False
    sig = wb.windows_signature(wb.list_targets(port)) if up else None
    changed = not (up and alive and sig == w.get("bound_sig"))
    d = {
        "bound": up, "changed": changed, "exe_path": exe, "port": port,
        # v1.0.652（审计 P1-7）：补 cdp_port 别名——前端设置页按 config 段字段名
        # w.cdp_port 读取，而本响应旧只有 port，设置页端口恒显 9222 回退值，
        # 且 refreshStatus 每 8s 把输入框刷回 9222 后任何一次保存都会反向覆盖
        # pick_cdp_port 自动避让写回配置的端口。双写过渡，前端可逐步收敛到单名。
        "cdp_port": port,
        "pid": prev if alive else (pids[0] if pids else None),
        "auto_launch": w.get("auto_launch", True),
        "auto_cdp": w.get("auto_cdp", True),
        "silent_mode": bool(w.get("silent_mode", True)),   # v1.0.523：静默模式开关回传前端
        "needs_kill": bool(pids) and not up,  # 有企微进程但 CDP 未起：需重启才能绑定
        "windows": len(wb.window_list_summary(port)) if up else 0,
    }
    # v1.0.652（审计 P2-7）：缓存写入进锁——原读侧持锁写侧裸写，与 _bs_refresh_async
    # 并发时可能产生 ts/data 不配对的缓存（陈旧 data 配新鲜 ts，被当新鲜值用满 30s）。
    with _BS_LOCK:
        _BS_CACHE["ts"] = now
        _BS_CACHE["data"] = d
    return d
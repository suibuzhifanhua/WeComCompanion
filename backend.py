"""
backend.py — 企微伴侣 · Agent 后端 (Flask, 单文件可冻结骨架)
====================================================================
v1.0.162 重构（T05）：业务路由全部迁入 routes/ 包（6 蓝图，统一 /api 前缀）。
本文件只保留 app 骨架（单实例/全局异常/首页）与启动器逻辑（日志注入/启动绑定/托盘/webview 入口）。

路由清单（见 routes/ 包，均挂 /api）：
  management — status/config/quick_links(含AI)/models/connectors/experiences/rules
  skills     — 技能列表/详情/覆盖/优化
  templates  — 模板 CRUD / 模板 AI 生成 / 模板运行(SSE)
  chat       — /chat /chat/abort /chat/continue (SSE)
  wecom      — 企微 bind/windows/capture/interactive/screenshot/send
  workspace  — 产物库树/上传/读取/打开/大小
"""
import os
import sys
import ctypes
import json
import time
import threading
import urllib.request
import traceback
from flask import Flask, request, send_file, jsonify
from werkzeug.exceptions import HTTPException

import wecom_bridge as wb
# 密钥保护模块（v1.0.63 拆分）：此处仅留日志注入（其余符号随 config_store.py 迁移，T03）
# v1.0.647：security.py 归位 core/secrets.py（原模块名撤销）
from core.secrets import set_logger as _security_set_logger

from version import get as _version_get  # v1.0.510（P2-13）：版本兜底块收敛到 version.get
VERSION = _version_get()

from core_utils import (resource_path, config_path, log_event, migrate_legacy_layout, migrate_knowledge_base)
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）
from core.config_store import load_config, save_config, APP_PORT, cdp_port

import routes
# 绑定状态缓存对象与 wecom 蓝图共用（v1.0.510：从 routes/wecom.py 下沉到 core，
# 消除入口层→路由层私有符号依赖；routes/wecom.py 仍 re-export 同一对象保持兼容）
from core.wecom_state import _BIND_CACHE
# 主窗口 X 关闭拦截（最小化到托盘 / 退出选择）：v1.0.648 守卫函数整体迁往
# desktop/shell.py，close_guard 的模块级导入也一并挪走（backend 里只剩函数内局部导入）。

# v1.0.358 版本管理：完成上次排队的版本切换（运行中 exe 被锁时改在启动时替换）
try:
    from services import version_store
    version_store.apply_pending_switch()
except Exception as _e:
    log_event(f"[warn] backend.py:49 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志

# v1.0.533（管理员重启诊断）：提权实例最早的存活证据——写在此处（模块加载完成），
# 早于单实例锁/任何服务。若日志既无本行也无 "[system] 本次以管理员模式启动"，
# 则说明提权 exe 根本未执行（安全软件拦截 runas），归因直接明确。
if "--admin" in sys.argv or "--admin-respawn" in sys.argv:
    try:
        log_event("[system] 提权进程 bootstrap：backend 模块加载完成（最早存活证据）")
    except Exception:  # noqa: BLE001
        pass

# v1.0.542（管理员重启·计划任务方案）：--admin-respawn = schtasks 预授权任务拉起的
# 实例（最高权限、不弹 UAC）。启动时核销标志文件：存在 → 等价一次管理员重启（继续
# 启动流程，wait_grace 在 ensure_single_instance 调用处生效）；不存在 → 任务被误触发
# （/sc once 不该自动跑，但保险），静默退出。
if "--admin-respawn" in sys.argv:
    try:
        _flag = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                             "产物库", "系统", "admin_respawn.flag")
        if os.path.isfile(_flag):
            os.remove(_flag)
            log_event("[system] 计划任务 respawn：标志已核销，按管理员重启继续")
        else:
            log_event("[system] 计划任务 respawn：无标志文件（误触发），退出")
            os._exit(0)
    except Exception as _e:  # noqa: BLE001
        log_event(f"[system] respawn 标志核销异常（继续启动）：{_e}")



# 单实例互斥句柄（全局）
_SINGLE_MUTEX = None


def _admin_watchdog_gate():
    """看门狗闸门（v1.0.533）：必须在 ensure_single_instance() **之前**调用——
    看门狗是普通权限分离进程，若经过单实例锁会撞上提权实例的 mutex → 弹
    「软件已在运行」→ os._exit → 20s 核实从未执行（v1.0.532 用户实测截图实锤）。
    命中 --admin-watchdog 时执行看门狗后立即退出，绝不触碰单实例锁。"""
    if "--admin-watchdog" not in sys.argv:
        return False
    try:
        _admin_watchdog()
    except Exception as _e:  # noqa: BLE001
        try:
            log_event(f"[admin] 看门狗异常：{_e}")
        except Exception:  # noqa: BLE001
            pass
    os._exit(0)
    return True   # 不可达（os._exit），类型安抚


def ensure_single_instance():
    """只允许一个实例。已存在则弹窗提示并退出。
    v1.0.361 审查修复（P5）：①补 global（原函数内赋值，模块级 _SINGLE_MUTEX 恒 None，
    句柄只活在局部——改用 PyHANDLE 类 restype 时 GC 关句柄会让单实例失效）；
    ②改 use_last_error=True + ctypes.get_last_error()（GetLastError() 会被中间
    系统调用清掉，语义不可靠）。
    v1.0.373 修复（用户实测：管理员重启后软件直接消失）：--admin 重启场景下，
    UAC 拉起的新实例启动时旧实例还在 1.2s 退出倒计时里、mutex 仍被持有——
    原逻辑立即 os._exit → 新旧实例全部消失。现 --admin 启动时轮询等待 mutex
    释放（最多 15s）再继续（core_utils.acquire_single_instance_mutex），竞态消除；
    普通启动行为不变（立即退出）。
    v1.0.373 补（用户实测任务栏双图标）：普通二次启动检测到已有实例时，先把
    已在运行的窗口激活到前台再退出——用户『以为没开再点一次』时窗口会弹出，
    不再静默退出造成困惑。
    v1.0.533（管理员重启看门狗误撞锁）：--admin-watchdog 分支**必须在本函数之前**
    （见 _admin_watchdog_gate）——看门狗是普通权限分离进程，若先经过本函数会撞上
    提权实例/旧实例的 mutex，弹「软件已在运行」后自杀，20s 核实从未执行（用户实测
    截图：PowerShell 弹「软件已在运行」而任务栏无实例）。"""
    global _SINGLE_MUTEX
    try:
        import ctypes as _ct
        # v1.0.647：直连定义处（core_utils 的转发保留给存量测试）
        from core.instance_lock import acquire_single_instance_mutex
        ok, handle = acquire_single_instance_mutex(
            "WeComCompanion_SingleInstance",
            wait_grace=("--admin" in sys.argv or "--admin-respawn" in sys.argv))
        if not ok:
            activated = False
            try:
                from desktop import close_guard
                activated = close_guard.activate_existing("企业微信伴侣")
            except Exception as _e:# noqa: BLE001
                log_event(f"[warn] backend.py:83 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志
            try:
                from core_utils import native_toast
                native_toast("软件已在运行", "已为你切换到正在运行的窗口" if activated
                             else "检测到软件已在运行，请从任务栏或托盘打开")
            except Exception as _e:# noqa: BLE001
                log_event(f"[warn] backend.py:89 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志
            os._exit(0)
        _SINGLE_MUTEX = handle   # 全局持有（进程生命周期内不释放）
    except Exception as _e:
        log_event(f"[warn] backend.py:93 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志


def _admin_watchdog():
    """管理员重启看门狗（v1.0.532/v1.0.533 增强）：旧实例提权重启退出后，由其拉起的
    分离小进程。**1s 粒度轮询 30 次**，每次记录 {WeComCompanion 进程数, mutex 持有者,
    8787 端口} 三元组——提权实例「从未出现」「出现后崩溃（存活 Xs）」「正常存活」
    三种结局都能从时间线直接归因（此前单点检查 20s 处只有一句结论，死因黑盒）。"""
    import time as _t
    import urllib.request as _ur
    timeline = []
    proc_seen_s = None
    port_seen_s = None
    for i in range(30):
        _t.sleep(1)
        t = i + 1
        try:
            import ctypes as _ct
            k32 = _ct.WinDLL("kernel32", use_last_error=True)
            k32.OpenMutexW.restype = _ct.c_void_p
            h = k32.OpenMutexW(0x00100000, False, "WeComCompanion_SingleInstance")  # SYNCHRONIZE
            mutex = bool(h)
            if h:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            mutex = None
        try:
            import subprocess as _sp
            out = _sp.check_output(["tasklist", "/fi", "IMAGENAME eq WeComCompanion.exe"],
                                   creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            n_proc = out.decode("gbk", "ignore").count("WeComCompanion.exe")
        except Exception:  # noqa: BLE001
            n_proc = None
        port = None
        try:
            _ur.urlopen("http://127.0.0.1:8787/api/health", timeout=1)
            port = True
        except Exception:  # noqa: BLE001
            port = False
        timeline.append(f"t={t}s 进程={n_proc} mutex={mutex} port={port}")
        if n_proc and proc_seen_s is None:
            proc_seen_s = t
        if port and port_seen_s is None:
            port_seen_s = t
        if port_seen_s is not None and t - port_seen_s >= 3:
            break   # 服务已稳定 3s → 判定成功，提前收工
    try:
        if port_seen_s:
            log_event(f"[admin] 看门狗核实：提权实例已存活 ✓（端口 {port_seen_s}s 出现，"
                      f"进程 {proc_seen_s}s 出现）")
        elif proc_seen_s:
            log_event("[admin] 看门狗核实：提权进程出现过（%ss 起）但服务未起、进程已消失——"
                      "启动早期崩溃。时间线：%s；请手动右键 exe「以管理员身份运行」对照，"
                      "并检查安全软件拦截记录" % (proc_seen_s, "；".join(timeline[:30])))
            try:
                from core_utils import native_toast
                native_toast("管理员重启失败", "提权实例启动后崩溃（存活至约 %ss）。"
                             "请右键软件图标「以管理员身份运行」对照，并查看日志。" % proc_seen_s)
            except Exception:  # noqa: BLE001
                pass
        else:
            log_event("[admin] 看门狗核实：30s 内提权 exe 从未执行——runas 请求被安全软件"
                      "静默拦截。请手动右键 exe「以管理员身份运行」，或将 exe 加入安全软件白名单。"
                      "时间线：%s" % "；".join(timeline[::6]))
            try:
                from core_utils import native_toast
                native_toast("管理员重启失败", "提权 exe 30 秒内从未执行（可能被安全软件拦截）。"
                             "请右键软件图标「以管理员身份运行」，或将 exe 加入白名单。")
            except Exception as e:  # noqa: BLE001
                # v1.0.614：补日志。toast 本身失败不该把"提权没成"这件事也一起吞掉。
                log_event(f"[restart] 失败提示 toast 未发出（不影响判定）：{e}")
    except Exception as e:  # noqa: BLE001
        # v1.0.614：整块守护逻辑原先被一个 pass 包住，提权失败根因完全不可追。
        log_event(f"[restart] 管理员重启看门狗异常：{e}\n{traceback.format_exc().rstrip()}")


def _exit_app():
    # v1.0.522：正常退出补日志——此前点 X 选「退出程序」/托盘退出走 os._exit 不留痕，
    # 进程消失后无法与崩溃/异常退出区分（v1.0.521 后端静默消失排查时的盲区）。
    try:
        log_event("[system] 进程正常退出（窗口关闭/托盘退出）")
    except Exception:  # noqa: BLE001
        pass
    try:
        from desktop import close_guard
        close_guard.remove_tray_icon()   # 退出前删托盘图标（防幽灵图标）
    except Exception as _e:# noqa: BLE001
        log_event(f"[warn] backend.py:101 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志
    os._exit(0)


app = Flask(__name__)


@app.after_request
def _security_headers(resp):
    """全局安全响应头（v1.0.555，借鉴 TencentCloud/Octop workspace.py 的静态响应加固）：
    X-Content-Type-Options: nosniff —— 禁止浏览器 MIME 嗅探把 JSON/文本响应当
    HTML/SVG 执行（ Octop 对 user-controlled 内联响应统一加 nosniff + CSP sandbox；
    本应用 /workspace/file 以 JSON 回传产物库内容、index.html 同源，嗅探是残余
    攻击面，一行头封死且对现有前端零影响）。"""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


# ---------- 本机 API 鉴权（v1.0.405，审查 H1 修复） ----------
# 原状态：127.0.0.1 绑定挡住外网，但本机任何进程/恶意网页（DNS rebinding）都可直调
# /api/*——agent 有 pc_run 等高危工具，等于整台电脑的遥控器裸奔。
# 方案：①随机 token 持久化（产物库/系统/api_token.txt），首次访问 / 时 Set-Cookie
# （HttpOnly + SameSite=Strict），后续同源 fetch 自动携带，跨站请求不带 → 401；
# ②Host 白名单（127.0.0.1/localhost/[::1]:port）→ 防 DNS rebinding（恶意域名解析到
# 127.0.0.1 时 Host 头是攻击者域名，直接 403）。静态资源（/ /css /js）不涉敏不放行外。
import secrets as _secrets_mod

_API_TOKEN = None
_TOKEN_PERSISTED = False   # v1.0.619：token 是否已落到 产物库/系统（首启闸门未开时先只存内存）
_TOKEN_LOCK = threading.Lock()   # v1.0.652（审计 P2-4）：并发首次访问 / 各自生成 token，后写覆盖先写→先到响应的 cookie 失效
_AUTH_DENIED_LOGGED = False  # fail-closed 拒绝只记一次，避免每个请求刷一行
_AUTH_DISABLED = False   # 仅 tests/conftest.py 置 True（test_client 不带 cookie）；生产恒 False

_ALLOWED_HOSTS = {f"127.0.0.1:{APP_PORT}", f"localhost:{APP_PORT}", f"[::1]:{APP_PORT}",
                  "127.0.0.1", "localhost", "[::1]"}   # 无端口形式：Flask test_client 默认 Host


def _harden_token_acl(p):
    """v1.0.510 优化（P1-5）：token 文件 ACL 收紧——默认继承链允许本机其他账户/
    内置组读取，任意本机进程可拿 token 直调 /api/*。现去掉继承、仅授予当前用户
    完全控制。失败只记日志（非 NTFS/受限环境降级为原状，不影响使用）。"""
    try:
        import subprocess as _sp
        me = os.environ.get("USERNAME", "")
        dom = os.environ.get("USERDOMAIN", "")
        who = f"{dom}\\{me}" if dom and me else me
        if not who:
            return
        flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        _sp.run(["icacls", p, "/inheritance:r", "/grant:r", f"{who}:F"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10, creationflags=flags)
    except Exception as e:  # noqa: BLE001
        log_event(f"[auth] token 文件 ACL 收紧失败（保持默认权限，不影响使用）：{e}")


def _persist_token(p, t):
    """把 token 写进 产物库/系统/api_token.txt 并收紧 ACL。返回是否成功。
    失败只记日志——内存里的 token 照样能保护本机 API，持久化只是为了跨进程
    （巡检脚本）和跨重启能读到同一个值。"""
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # v1.0.646：先建空文件 → 收紧 ACL → 再写内容。旧实现是「写完明文再收紧」，
        # 中间存在一个明文 token 落在默认继承权限下的窗口（本机其他账户/内置组可读）。
        # 空文件阶段收紧后，token 字节从未出现在宽权限下。
        fresh = not os.path.exists(p)
        if fresh:
            with open(p, "w", encoding="utf-8"):
                pass
            _harden_token_acl(p)
        with open(p, "w", encoding="utf-8") as f:
            f.write(t)
        _harden_token_acl(p)   # 幂等兜底：文件可能是旧版留下的、或刚被外部重建
        return True
    except Exception as e:  # noqa: BLE001
        log_event(f"[auth] token 持久化失败（内存 token 仍可用，不影响本机界面）：{e}")
        return False


def _api_token():
    """本机 API 的随机 token（64 hex）。

    v1.0.619 两处收口：
      ① 生成不再依赖产物库可写。原实现读写一失败就把 `_API_TOKEN` 缓存成空串，
         而 `_api_gate` 判的是 `if want and ...` ——空串=整进程**跳过 cookie 校验**，
         产物库被占用/只读的机器上鉴权是静默全关的（fail-open）。
      ② 落盘等产物库闸门：闸门未开时不 makedirs，否则"为了写 token"会在归位前的
         旧位置捏出一个 产物库/，正好破掉 v1.0.619 的首启延后建目录。开闸后第一次
         访问 /api 自动补持久化。
    """
    global _API_TOKEN, _TOKEN_PERSISTED
    with _TOKEN_LOCK:   # v1.0.652（审计 P2-4）：生成/读取/持久化全程进锁，防并发首访双写
        p = os.path.join(paths.workspace_root(), "系统", "api_token.txt")
        if _API_TOKEN:
            if not _TOKEN_PERSISTED and paths.workspace_ready():
                _TOKEN_PERSISTED = _persist_token(p, _API_TOKEN)
            return _API_TOKEN
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    t = f.read().strip()
                if len(t) >= 32:
                    _API_TOKEN = t
                    _TOKEN_PERSISTED = True
                    return _API_TOKEN
            _API_TOKEN = _secrets_mod.token_hex(32)
            if paths.workspace_ready():
                _TOKEN_PERSISTED = _persist_token(p, _API_TOKEN)
            return _API_TOKEN
        except Exception as e:  # noqa: BLE001
            # 连 secrets 都取不到属于极端环境：留空并让 _api_gate 走 fail-closed（拒绝服务），
            # 不缓存空串——下一次调用还会重试。
            log_event(f"[auth] token 生成失败（/api 拒绝服务直到恢复）：{e}")
            _API_TOKEN = ""
            return ""


@app.before_request
def _api_gate():
    """统一准入：Host 白名单（全部路径）+ /api/* 的 cookie token 校验。"""
    # v1.0.614：先记一次"界面活动"，开机后台任务（OCR 自检 / 更新预热）据此让位。
    # 放在鉴权短路之前——_AUTH_DISABLED 或 401 的请求同样说明用户在用界面。
    try:
        from core import idle
        idle.touch()
    except Exception:  # noqa: BLE001
        pass
    if _AUTH_DISABLED:
        return None
    host = (request.host or "").lower()
    if host not in _ALLOWED_HOSTS:
        log_event(f"[auth] 拒绝非本机 Host：{host} {request.path}")
        return jsonify({"error": "forbidden host"}), 403
    if request.path.startswith("/api/"):
        want = _api_token()
        if not want:
            # v1.0.619（审查①）fail-closed：原写法 `if want and not compare(...)` 在
            # token 取不到时**整进程跳过校验**——鉴权失效是静默的，比拒绝服务严重得多。
            global _AUTH_DENIED_LOGGED
            if not _AUTH_DENIED_LOGGED:
                _AUTH_DENIED_LOGGED = True
                log_event(f"[auth] token 不可用，/api 一律 503（fail-closed）：{request.path}")
            return jsonify({"error": "auth unavailable"}), 503
        if not _secrets_mod.compare_digest(request.cookies.get("__wc_token", ""), want):
            return jsonify({"error": "unauthorized"}), 401
    return None


# 注入日志到 security 模块（密钥保护告警走统一日志）
_security_set_logger(log_event)
# 注入日志到 wecom_bridge 模块（CDP/绑定等异常走统一日志，P3-6）
wb.set_logger(log_event)


routes.register_all(app)   # 业务蓝图统一挂 /api（T05）


# ---------- 全局异常捕获（把 500 细节写进日志，API 返回 JSON） ----------
@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    log_event("[error] " + traceback.format_exc().replace("\n", " "))
    if request.path.startswith("/api/"):
        return jsonify({"error": "服务器内部错误", "detail": str(e)}), 500
    return "Internal Server Error", 500

# ---------- 路由 ----------

# v1.0.580：关闭确认层的 HTTP 桥——点 X 时 close_guard 钩子（GUI 线程）只设
# pending 标志，前端既有轮询发现后在网页内弹确认层，选择 POST 回传执行。
# 不走 evaluate_js（钩子线程调它会自我死锁，v1.0.580 实锤）。
@app.route("/api/close_ask", methods=["GET"])
def api_close_ask_status():
    from desktop import close_guard
    return jsonify({"pending": close_guard.close_ask_pending()})


@app.route("/api/close_ask/choice", methods=["POST"])
def api_close_ask_choice():
    from desktop import close_guard
    d = request.get_json(force=True, silent=True) or {}
    try:
        close_guard.apply_web_choice(bool(d.get("minimize")), bool(d.get("remember")))
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:120]}), 500


@app.route("/")
def index():
    resp = send_file(resource_path("index.html"))
    # v1.0.405（H1）：首次访问种下鉴权 cookie——同源 fetch/SSE 自动携带，
    # 跨站（恶意网页）不带 → /api/* 401。HttpOnly 防 XSS 读取，Strict 防跨站发送。
    tok = _api_token()
    if tok:
        resp.set_cookie("__wc_token", tok, httponly=True, samesite="Strict", path="/")
    return resp

@app.route("/css/<path:fname>")
def css_assets(fname):
    """前端外置样式（v1.0.401 自 index.html 外置到 css/style.css）。"""
    if os.path.basename(fname) != fname or "/" in fname or "\\" in fname:
        from flask import abort
        abort(404)
    p = resource_path(os.path.join("css", fname))
    if not os.path.isfile(p):
        from flask import abort
        abort(404)
    resp = send_file(p, mimetype="text/css")
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/js/<path:fname>")
def js_assets(fname):
    """前端拆分后的 js/ 静态文件（v1.0.309）。
    index.html 经 / 打出后，<script src="js/xx.js"> 的相对路径会打到本路由；
    冻结环境文件在 _MEIPASS/js/。只允许单段文件名（basename 相等），防路径穿越。"""
    if os.path.basename(fname) != fname or "/" in fname or "\\" in fname:
        from flask import abort
        abort(404)
    p = resource_path(os.path.join("js", fname))
    if not os.path.isfile(p):
        from flask import abort
        abort(404)
    resp = send_file(p, mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-store"   # 与 index.html 同生命周期，避免升级后旧缓存
    return resp

# ---------- 启动器（冻结后即为 exe 入口） ----------
def _serve():
    """v1.0.646：优先 waitress（有线程池上限、长连接更稳），缺失则回退 Werkzeug。

    SSE 是本软件的生命线（/api/chat 一路长连接），Werkzeug 开发服务器没有线程池
    上限、也没有连接治理：多 pane 并行 + 后台 daemon 全部靠 GIL 和手工锁硬扛。
    waitress 是纯 Python、可进 PyInstaller，行为与 WSGI 一致，失败即回退——
    宁可跑回旧服务器，也不能因为少一个依赖起不来。"""
    try:
        from waitress import serve  # noqa: PLC0415
        log_event(f"[serve] waitress 承载 127.0.0.1:{APP_PORT}（threads=16）")
        serve(app, host="127.0.0.1", port=APP_PORT, threads=16,
              ident="WeComCompanion", channel_timeout=1800)
        return
    except Exception as e:  # noqa: BLE001
        log_event(f"[serve] waitress 不可用（{e}），回退 Werkzeug 开发服务器")
    app.run(host="127.0.0.1", port=APP_PORT, threaded=True, use_reloader=False)


def ensure_binding_on_start():
    """启动时尝试自动绑定本机企业微信（开箱即用）。

    探测/拉起/带 CDP 重启等逻辑均在 wecom_bridge（detect_wecom_exe / ensure_binding）
    内完成，本函数只负责：先把探测到的 exe 路径持久化、再调用绑定、并捕获任何异常
    降级为「未连接」状态——绝不能因绑定失败而阻塞 UI / webview 打开。
    """
    cfg = load_config()
    w = cfg.setdefault("wecom", {})
    # 先持久化探测到的 exe 路径，便于下次启动跳过重探测
    try:
        exe = wb.detect_wecom_exe()
        if exe:
            w["exe_path"] = exe
    except Exception as e:  # noqa: BLE001
        log_event(f"[bind] 探测企微路径失败：{e}")
        exe = None
    # 自动绑定（幂等；已绑定且未变更则不重启）
    # P0-2/P1-6：启动自动绑定只做「探测+记录」，绝不强杀/重启企微。
    # 需要重启时返回 needs_kill，由用户在手动静绑定时确认后触发。
    try:
        st = wb.ensure_binding(cfg, force=False, allow_kill=False)
        if st.get("needs_kill"):
            log_event("[bind] 企微在运行但未带 CDP flag，已跳过自动重启（需用户确认）。")
            # v1.0.573（用户反馈"实时窗口抓不到"）：启动绑定被 confirm_kill 保护拦下时用户无感知
            # → 留一条通知。v1.0.646（用户："软件修复的时候不要影响企业微信"）：文案**不再引导
            # 去重启企业微信**——重启本软件时企微常常正在被用户用着，把"点一下确认 → 关掉你的企微"
            # 做成一条自动弹出的捷径，正是那条报障的触发口。现在只说明少了什么能力，
            # 要不要重启企微由用户自己在「设置 → 企业微信」里决定。
            try:
                from core.notify import native_toast
                native_toast("企业微信未绑定（不影响其它功能）",
                             "问答、本机文件、联网检索、跑任务都照常可用；只是"
                             "「实时窗口 / 页面操作」暂时用不了。需要用时到 设置 → 企业微信 里绑定。")
            except Exception:  # noqa: BLE001
                pass
            st = {
                "bound": False, "changed": True,
                "reason": "企微正在运行但未带 CDP flag；为避免未保存内容丢失，未自动重启。"
                          "请在「企微」页点击「重启并绑定」手动确认。",
                "exe_path": exe, "port": cdp_port(cfg),
                "pid": st.get("pid"), "auto_launch": w.get("auto_launch", True),
                "auto_cdp": w.get("auto_cdp", True), "needs_kill": True, "windows": 0,
            }
    except Exception as e:  # noqa: BLE001
        log_event(f"[bind] 自动绑定异常（降级为未连接）：{e}")
        st = {
            "bound": False, "changed": True,
            "reason": f"自动绑定失败：{e}；可在「企微」页手动重绑。",
            "exe_path": exe, "port": cdp_port(cfg),
            "pid": None, "auto_launch": w.get("auto_launch", True),
            "auto_cdp": w.get("auto_cdp", True), "windows": 0,
        }
    try:
        # v1.0.555（P0-10）+ v1.0.652（P1-17）：绑定落盘只做**按键合并**进 fresh cfg
        # （update_config 锁内读-改-写），绝不整段覆盖 wecom 段。
        # v1.0.654 A9：合并器收敛到 core.config_store.make_bind_merger（与 routes/wecom 同源）。
        from core.config_store import update_config as _update_config, make_bind_merger
        _update_config(make_bind_merger(cfg))
    except Exception as e:  # noqa: BLE001
        log_event(f"[bind] 绑定后配置保存失败：{e}")
    _BIND_CACHE.update(st)
    return st


def _startup_bind():
    """后台线程执行启动自动绑定，不阻塞主线程（启动慢根因之一：ensure_binding 内
    含企微进程拉起 + CDP 探测等待，最坏可长达数十秒，原实现同步阻塞在创建窗口前）。
    绑定结果怎么上界面：界面走的是看板 `/api/status`（其 wecom 段由 bind_status(cfg) 现算），
    后台绑定完成后下一次刷新自然变绿。
    （v1.0.619 纠正：这两处注释此前都把它说成 `/api/wecom/status` 的定时拉取——js 全库对该
    端点零调用，照着注释排"绑定状态不同步"会被带偏到不存在的代码路径上。）"""
    try:
        st = ensure_binding_on_start()
        log_event("[bind] " + json.dumps(st, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        log_event(f"[bind] 启动自动绑定后台执行异常：{e}")


# ---- 桌面壳层（v1.0.648 迁至 desktop/shell.py，此处 re-export 保调用方不感知）----
from desktop.shell import (   # noqa: F401
    _TRAY_SHOW, _bind_native_drop, _hotkey_stop_all, _install_close_guard,
    _on_tray_minimize, _saved_close_action, _save_close_action, _tray_show_fn,
)


def _tray_loop():
    """托盘线程：托盘"退出"调用 _exit_app 结束整个进程；"打开伴侣"还原主窗口。"""
    try:
        from desktop import tray
        tray.start_tray(VERSION, _exit_app, get_show_fn=_tray_show_fn)
    except Exception:  # noqa: BLE001
        log_event("[warn] 托盘图标启动失败，应用仍正常运行（关闭窗口即可退出）")


if __name__ == "__main__":
    # v1.0.555（P0-11）：打包冒烟入口（v1.0.648 迁至 core/smoke.py）——必须位于单实例锁/
    # 看门狗闸门之前（冒烟进程不该撞 mutex）。build.py 打包后自动调用。
    if "--smoke" in sys.argv:
        from core.smoke import run as _smoke_run
        raise SystemExit(_smoke_run())
    # v1.0.559（用户需求）：exe 放在桌面/下载/磁盘根目录会产生大量配置文件——
    # 启动早期自动归位：创建「企微伴侣」文件夹、搬入 exe 与运行时文件、重启。
    # 必须在单实例锁之前（老进程不拿锁即退出，新实例拿锁零竞争）；
    # 需要迁移时函数内部拉起新实例并 os._exit(0) 闭环，无需调用方处理。
    try:
        from core.relocate import maybe_relocate_and_relaunch
        maybe_relocate_and_relaunch()
    except Exception as _reloc_e:  # noqa: BLE001
        try:
            log_event(f"[relocate] 迁移入口异常（原地继续启动）：{_reloc_e}")
        except Exception:  # noqa: BLE001
            pass
    try:
        _admin_watchdog_gate()                   # v1.0.533：看门狗闸门——必须在单实例锁之前
        _respawn = "--admin-respawn" in sys.argv
        ensure_single_instance()                 # 单实例：已在运行则激活其窗口后退出
        if "--admin" in sys.argv:
            log_event("[system] 本次以管理员模式启动（--admin）✓")   # v1.0.373 前移：提权实例存活的第一证据
        if _respawn:
            log_event("[system] 本次以管理员模式启动（计划任务 respawn）✓")   # v1.0.542
        def _startup_migrate():
            """v1.0.306：两项布局迁移后台化（幂等、只移不删、失败不致命）。
            原先在服务启动前同步执行——扫描/迁移产物库拖慢开窗；冷启动关键路径上只留必须的事。"""
            try:
                _m = migrate_legacy_layout()
                if _m:
                    log_event("[migrate] 旧布局迁移 " + str(len(_m)) + " 项：" + "，".join(_m[:8]))
            except Exception as e:  # noqa: BLE001
                log_event(f"[migrate] 旧布局迁移失败（不影响主流程）：{e}")
            try:
                _mk = migrate_knowledge_base()
                if _mk:
                    log_event("[migrate] 知识库迁移 " + str(len(_mk)) + " 项：" + "，".join(_mk[:8]))
            except Exception as e:  # noqa: BLE001
                log_event(f"[migrate] 知识库迁移失败（不影响主流程）：{e}")

        # 启动自动绑定移入后台线程：原 ensure_binding_on_start() 同步阻塞会拖慢窗口出现
        #（启动慢根因），后台完成后由看板 /api/status 的 wecom 段反映绑定态。
        threading.Thread(target=_startup_bind, daemon=True).start()
        # v1.0.619（用户需求）：产物库**不在开窗前建**。core/relocate.py 的「exe 放桌面/下载/
        # 根目录 → 自建『企微伴侣』文件夹、搬进去并重启」归位功能，与"启动即建目录"是在抢时序：
        # 只要有任何一次早期落盘（日志或产物库），归位前的旧位置就会留下一个 产物库/，
        # 症状是桌面被塞文件夹、或搬家后出现两处产物库。现在：
        #   ① 产物库本就存在（老用户升级，没有可归位的问题）→ 立刻开闸，日志即时落盘；
        #   ② 首启（目录不存在）→ 开窗 WORKSPACE_READY_DELAY_S 秒后再建；期间任何写产物库的
        #      操作会立即提前开闸（功能不会因为目录没建而失败），启动期日志先在内存攒着、开闸补写。
        def _open_ws_gate(reason):
            # allow_workspace_creation 只在"首次打开"时返回 True，迁移/日志补写都以它为准，
            # 因此延时线程、写操作、preexisting 快速路径三条路同时到达也只跑一次。
            first = paths.allow_workspace_creation(reason)
            log_event(("[workspace] 产物库就绪（%s）" % reason) + paths.workspace_root())
            if first:
                _startup_migrate()          # 旧布局/知识库迁移自己会 makedirs，必须在闸门之后

        def _workspace_ready_later():
            try:
                time.sleep(paths.WORKSPACE_READY_DELAY_S)
            except Exception:  # noqa: BLE001
                return
            if paths.workspace_ready():
                return                     # 已被写操作提前打开，迁移也一起做过了
            try:
                _open_ws_gate("startup-timer")
            except Exception as e:  # noqa: BLE001
                log_event(f"[workspace] 延时建立产物库异常（写操作仍会即时建）：{e}")

        if os.path.isdir(paths.workspace_root()):
            _open_ws_gate("preexisting")
        else:
            threading.Thread(target=_workspace_ready_later, daemon=True).start()
        # v1.0.313：定时自动化调度线程（30s tick；到点任务直接走 agent_loop，结果落产物库）
        try:
            from services import scheduler as _sched
            _sched.start_daemon()
        except Exception as e:  # noqa: BLE001
            log_event(f"[sched] 调度线程启动失败（不影响其他功能）：{e}")
        # v1.0.348 自我修复：每日体检线程（开关 feature_flags.self_repair_daily；
        # 距上次 ≥24h 且新增错误达阈值才真正调用 LLM，平时静默空转）
        try:
            from services import self_repair as _sr
            threading.Thread(target=_sr.daily_loop, daemon=True).start()
            log_event("[repair] 自我修复线程已启动（每日自动体检，开关在设置页）")
        except Exception as e:  # noqa: BLE001
            log_event(f"[repair] 自我修复线程启动失败（不影响其他功能）：{e}")
        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        url = f"http://127.0.0.1:{APP_PORT}/"
        # 细粒度探测（0.2s/次，共 60 次≈12s 上限）：服务一就绪就立刻开窗，
        # 避免旧实现 sleep(1) 造成最多 1s 的开窗延迟（启动慢体感之一）。
        for _ in range(60):
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)  # 预期：服务尚未就绪，重试探测
        log_event(f"[ok] 伴侣已启动：{url}")

        # 托盘（后台线程，可选；不影响主窗口）
        threading.Thread(target=_tray_loop, daemon=True).start()

        # v1.0.274：新学习窗口自动最小化（后台线程）——agent 点开的课程/视频/考试
        # 独立窗口不再弹到用户工作面上方。仅处理学习平台新 target，存量窗口与
        # 用户 OA 窗口不动；日志走 companion.log [window-tucker]。
        try:
            from agent_tools import _cdp_port as _cdp_port_tucker
            wb.start_window_tucker(_cdp_port_tucker(load_config()))
        except Exception as e:  # noqa: BLE001
            log_event(f"[window-tucker] 启动失败（不影响主流程）：{e}")

        # v1.0.413：另存为对话框自动应答兜底；v1.0.604 **默认关闭**——弹窗的源头
        # （下载「点击下载」回退）已整体停用：附件只走页内 fetch 静默直传，我们不再
        # 制造「另存为」框。留着的 Watcher 会去替用户点他自己打开的企微保存框
        # （静默改目录+点保存，风险大于收益），故改为按需开启：
        # config.feature_flags.auto_save_dialogs = true。
        try:
            from desktop import save_dialog_auto
            _flags = (load_config().get("feature_flags") or {})
            save_dialog_auto.start_daemon(enabled=bool(_flags.get("auto_save_dialogs", False)))
        except Exception as e:  # noqa: BLE001
            log_event(f"[save-dialog] 启动失败（不影响其他功能）：{e}")

        # v1.0.608：开机后台自检 OCR「真能不能识别」。目录齐不代表可用（DLL 挂不上、
        # 模型缺文件都会一跑就废），所以造一张已知文本图走生产 OCR 入口判相似度。
        # 延迟 20 秒避开启动高峰，daemon 线程不拖慢启动；组件未装时自检自身判掉不外抛。
        try:
            from core.ocr import ocr_selfcheck_async
            ocr_selfcheck_async(delay=20.0)
        except Exception as e:  # noqa: BLE001
            log_event(f"[ocr] 开机自检未能启动（不影响其他功能）：{e}")

        # v1.0.611：顺带预热「检查更新」结果（实测全量扫描 34.6 秒，是镜像往返，不是本机慢）。
        # 让它进后台，进「运行组件」才不用为一句"可升级"白等半分钟；用户点「检查更新」仍当场重算。
        try:
            from core.ocr import ocr_update_check_async
            ocr_update_check_async(delay=35.0)
        except Exception as e:  # noqa: BLE001
            log_event(f"[ocr] 更新缓存预热未能启动（不影响其他功能）：{e}")

        # v1.0.301：知识库后台维护（daemon 线程 60s 轮询）——空闲时自动整理
        # （INDEX 索引+查重，不动文件）+ 每日 AI 消化（24h 增量 → 知识库/消化/）。
        try:
            from services import knowledge_ops
            knowledge_ops.keeper.start()
        except Exception as e:  # noqa: BLE001
            log_event(f"[kb-ops] 启动失败（不影响主流程）：{e}")

        # v1.0.523-3：每日工作小结 daemon（默认 18:30，静默生成 产物库/日报/）。
        try:
            from services import daily_digest
            daily_digest.start_daemon()
        except Exception as e:  # noqa: BLE001
            log_event(f"[digest] 启动失败（不影响主流程）：{e}")

        # v1.0.523-4：待办变更监视线程（默认关，config todo_watch.enabled 开启）。
        try:
            from services import todo_watch
            todo_watch.start_daemon()
        except Exception as e:  # noqa: BLE001
            log_event(f"[todo-watch] 启动失败（不影响主流程）：{e}")

        # v1.0.477：企微聊天文件静默分析（明文附件/企业网盘 → 知识库/企微文件/，
        # 增量指纹台账；消息文本加密存储不做解密——合规边界见 services/wecom_archive.py）
        try:
            from services import wecom_archive
            wecom_archive.start_archive_daemon()
        except Exception as e:  # noqa: BLE001
            log_event(f"[wecom-archive] 启动失败（不影响主流程）：{e}")

        # v1.0.280：无头模式（WECOM_NO_GUI=1）——工具/沙箱拉起的进程弹不出 pywebview
        # 窗口（隐藏窗口站：页面加载事件永不触发，进程 20~100s 内干净自退，实测
        # 01:50 exe / 01:54 源码后端两次复现）。设此环境变量时完全跳过 pywebview
        # 主窗口，主线程 Event().wait() 永久阻塞保活（Flask/托盘/tucker 均为 daemon
        # 线程，主线程不退出进程就活着）；功能入口 = 浏览器 http://127.0.0.1:8787/
        # （webview 窗口加载的就是同一个 URL，UI 完全一致，仅少了原生壳）。
        if os.environ.get("WECOM_NO_GUI") == "1":
            log_event("[no-gui] 无头模式：跳过 pywebview 主窗口，主线程常驻（浏览器访问 " + url + "）")
            threading.Event().wait()
        # 用 pywebview 包一个真正的原生桌面窗口（不再是浏览器标签页）
        # 关键：通过 WebView2 官方支持的环境变量解决 GPU 兼容导致的黑屏。
        # 部分机器 GPU 驱动被 WebView2 误判/触发 bug workaround，导致渲染器崩溃、窗口黑屏
        # （页面 JS 正常执行但画面全黑/冻结）。用 ignore-gpu-blocklist 强制启用 GPU、
        # disable-gpu-driver-bug-workarounds 关闭有问题的驱动 workaround。
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--ignore-gpu-blocklist --disable-gpu-driver-bug-workarounds"
        import webview
        # 把 pywebview 内部日志写进 companion.log，便于排查 WebView2 初始化失败/黑屏
        # v1.0.277：路径切到 产物库/日志/（v1.0.275 整合漏改此处，导致每次运行
        # 都在 dist 顶层重新生成 companion.log，破坏「顶层只剩 exe+config+产物库」布局）
        # v1.0.619：FileHandler 会立刻打开文件，而首启的产物库要等闸门才建（目录不在就是
        # FileNotFoundError）→ 改成等闸门开了再挂；60s 内没开就不挂（诊断日志，不是功能）。
        def _attach_pywebview_log():
            import time as _t
            for _ in range(200):
                if paths.workspace_ready():
                    break
                _t.sleep(0.3)
            if not paths.workspace_ready():
                return
            try:
                import logging as _logging
                _lg = _logging.getLogger("pywebview")
                if not any(isinstance(h, _logging.FileHandler) for h in _lg.handlers):
                    _fh = _logging.FileHandler(os.path.join(paths.companion_log_dir(), "companion.log"),
                                               encoding="utf-8")
                    _fh.setFormatter(_logging.Formatter("%(asctime)s [pywebview:%(levelname)s] %(message)s"))
                    _lg.addHandler(_fh)
                    _lg.setLevel(_logging.DEBUG)
            except Exception as _e:  # noqa: BLE001
                log_event(f"[warn] pywebview 日志挂接失败（不影响功能）：{_e}")

        threading.Thread(target=_attach_pywebview_log, daemon=True).start()
        icon_path = resource_path("app.ico")
        if not os.path.exists(icon_path):
            icon_path = None
        window = webview.create_window(
            "企业微信伴侣",
            url,
            width=1180,
            height=760,
            min_size=(920, 600),
            text_select=True,
        )
        try:
            window.events.loaded += lambda: log_event("[webview] 页面加载完成")
            # v1.0.373：绑定挪到 webview.start(func) —— start 前调用 DOM 桥 API 会
            # 等 loaded 超时抛 'Main window failed to start'（v1.0.372 拖拽桥失效根因）
        except Exception as _e:# noqa: BLE001
            log_event(f"[warn] backend.py:506 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志
        # v1.0.373：开机自启「启动后隐藏主窗口」——注册表值带 --minimized 参数时，
        # 窗口显示后立即 hide()（从任务栏消失，托盘图标常驻，点托盘「打开伴侣」恢复）
        _AUTOSTART_HIDE_ARG = "--minimized"   # 与 routes/management.py 的注册表参数保持一致
        if _AUTOSTART_HIDE_ARG in sys.argv:
            def _hide_after_shown():
                try:
                    window.hide()
                    try:
                        from desktop import close_guard
                        hwnd = _TRAY_SHOW.get("hwnd")
                        if hwnd:
                            close_guard.hide_from_taskbar(hwnd)   # 需求7：任务栏残留清理
                    except Exception as _e:# noqa: BLE001
                        log_event(f"[warn] backend.py:520 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志
                    log_event("[webview] --minimized：主窗口已隐藏到托盘")
                except Exception as e:  # noqa: BLE001
                    log_event(f"[webview] 隐藏主窗口失败（正常显示）：{e}")
            try:
                window.events.shown += _hide_after_shown
            except Exception as _e:# noqa: BLE001
                log_event(f"[warn] backend.py:527 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志

        # 关键修复：关闭 private_mode、使用持久化 storage_path。
        # WebView2 在 private mode 下用内存 user data folder，某些 runtime 版本会初始化失败，
        # 导致窗口黑屏（on_webview_ready 里 args.IsSuccess=False 直接 return，不加载 URL）。
        _storage = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.dirname(config_path()),
                                "WeComCompanion", "webview_data")
        try:
            os.makedirs(_storage, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            log_event(f"[webview] 创建持久化存储目录失败，退回临时模式：{e}")
            _storage = None
        # 安装 X 关闭拦截（后台线程等窗口句柄出现后子类化 WNDPROC，不阻塞开窗）
        _guard_icon = resource_path("app.ico")
        if not os.path.exists(_guard_icon):
            _guard_icon = None
        threading.Thread(
            target=_install_close_guard,
            args=("企业微信伴侣", _guard_icon),
            daemon=True,
        ).start()

        # 注意：pywebview 的 icon 是 webview.start() 的参数，create_window() 不接受；
        # webview.start() 会阻塞主线程直到所有窗口被关闭，关闭后才返回，再退出整个进程。
        # 第一个参数 func 在窗口创建后由 pywebview 调用（官方 DOM 事件桥绑定姿势）。
        webview.start(_bind_native_drop, window, icon=icon_path, private_mode=False, storage_path=_storage)
        _exit_app()   # 窗口关闭 → 退出整个进程
    except KeyboardInterrupt:
        log_event("[*] 退出")
    except Exception as e:  # noqa: BLE001
        log_event("[!] 启动异常：" + str(e))
        try:
            log_event(traceback.format_exc())
        except Exception as _e:# noqa: BLE001
            log_event(f"[warn] backend.py:562 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志

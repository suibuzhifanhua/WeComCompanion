"""
wecom_bridge.cdp_client — CDP 通信内核
========================================
原 wecom_bridge.py L26-35 / L37-78 / L202-221 / L401-543 / L1787-1810 逐字搬移（T01）。
职责：CDP 会话常量、日志回调注入、隐藏子进程执行、CDP 连通性检查、
WebSocket 会话开关、Runtime.evaluate 与通用 CDP 命令发送、页面就绪等待。
"""
# v1.0.324 启动提速 + v1.0.364 M8 收敛：requests 懒加载代理实现收敛到 core_utils
from core_utils import _lazy_requests as requests
import json
import os
import re
import time
import threading
import subprocess
import websocket
from core.result import is_err

DEFAULT_PORT = 9222
# ---- CDP 会话常量（P3-3：统一集中管理，避免散落魔法数字）----
CDP_CONNECT_TIMEOUT = 8     # WebSocket 连接握手超时（秒）
CDP_EVAL_TIMEOUT = 25       # Runtime.evaluate 总超时（秒）——v1.0.652 起 _recv_until 真正按总时限执行
CDP_UPLOAD_TIMEOUT = 15     # 文件上传会话总超时（秒）
_POOL_SOCK_TIMEOUT = 10.0   # v1.0.652：池化连接归还时恢复的默认 socket 超时（防长调用拉高后毒化短调用）
CDP_SCREENSHOT_TRY_TIMEOUT = 4  # Page.captureScreenshot 单次尝试超时（秒），合成帧挂起时超时即降级 DOM 绘制，避免预览卡死 12s
DEVNULL = subprocess.DEVNULL
# Chromium 新版要求显式声明允许 CDP WebSocket 的 origin，否则 websocket 握手 403。
# v1.0.521 回退（v1.0.510 收紧实测翻车）：白名单值下企微内嵌 Chromium 连【不带
# Origin 头的 Python websocket】也返回 403——根因是客户端没带头，不是收紧本身
# 不可行。现改为「客户端永远带 Origin 头 + 服务端白名单收紧」配套：
#   · _open_ws/_cdp_ws_ok 握手一律带 Origin: http://127.0.0.1:8787（服务端是 * 也兼容）；
#   · CDP_FLAGS 默认只放行本机软件自身的 origin，本机恶意网页（带自有 Origin）连不上。
# 逃生门：WECOM_CDP_ORIGINS=* 恢复旧行为（若某企微内核版本对白名单仍误判 403，
# 设上它重启企微即可回退，无需改码）。逗号分隔可追加自定义 origin。
def _cdp_allowed_origins():
    raw = os.environ.get("WECOM_CDP_ORIGINS", "").strip()
    if raw:
        return raw
    return "http://127.0.0.1:8787,http://localhost:8787"


# WS 握手固定携带的 Origin（必须在白名单内；取 8787 软件自身地址，语义上就是
#「本软件的前端来源」，企微内核只校验它在不在 --remote-allow-origins 列表里）。
CDP_CLIENT_ORIGIN = "http://127.0.0.1:8787"
CDP_FLAGS = ["--remote-allow-origins=" + _cdp_allowed_origins()]

# ---------- v1.0.345 CDP 常驻连接池 ----------
# 旧实现每个工具调用 _open_ws 新建 WebSocket、用完即弃——百轮任务 300+ 次握手，
# 每次本地 10-50ms。池化后同目标页面复用常驻连接（entry 锁串行化并发调用，
# 并行读工具天然按页面排队）。连接故障（send/recv 异常、评估超时）标记 broken
# 丢弃重建；页面被关闭 → 下次 acquire 重建，配合 _cdp_eval_retry 自愈。
# 环境变量 WECOM_CDP_WS_POOL=0 可整体停用（回退旧的每调用新建行为）。
_WS_POOL = {}                     # ws_url -> {"lock": Lock, "ws": ws_or_None, "ts": float}
_WS_POOL_LOCK = threading.Lock()
# v1.0.632（B6）：池要有回收。旧实现只有 get/set，全项目没有任何 pop 路径——
# 而 ws_url 是按 CDP targetId 分的：跑课/批审一轮开几百个详情页，每个都留一个
# {"lock": Lock, "ws": 死 socket}，页面早就关了、槽位却永远挂在字典里。
# 三重回收：① 目标消失（每次 /json 枚举成功后按活着的 id 摘除）② 闲置超时
# ③ 容量上限 LRU。三条都**不碰正在被占用的槽位**（entry.lock.locked() 即跳过），
# 所以不会出现"连接用到一半被关掉"。
_WS_POOL_MAX = 24                 # 同时常驻的 CDP 连接数上限
_WS_POOL_IDLE_S = 300.0           # 闲置 5 分钟的连接先断（重建成本 10-50ms，比占着稳妥）
_PAGE_TARGET_RE = re.compile(r"/devtools/page/([0-9A-Fa-f\-]+)")


def _pool_enabled():
    return os.environ.get("WECOM_CDP_WS_POOL", "1") != "0"


def _close_ws_quiet(ws):
    try:
        if ws is not None:
            ws.close()
    except Exception:  # noqa: BLE001
        pass


def _pool_trim_locked(alive_ids=None, room=0):
    """池锁内执行回收，返回需要关闭的 ws 列表（**关闭动作必须放到锁外做**：
    websocket.close() 会等对端回帧，攥着全局池锁去等网络等于卡住所有页面操作）。

    alive_ids 给定时（小写 targetId 集合）额外摘除"页面已经不存在"的槽位；
    room=1 表示调用方紧接着要**新插一个槽位**，容量按插入后封顶来算。
    认不出 targetId 的 url（如浏览器级端点）只受闲置/容量约束，不按存活摘除。"""
    now = time.time()
    victims = []
    for url, e in list(_WS_POOL.items()):
        if e["lock"].locked():
            continue
        if now - float(e.get("ts") or 0.0) > _WS_POOL_IDLE_S:
            victims.append(_pop_slot(url, e))
    if alive_ids:
        for url, e in list(_WS_POOL.items()):
            if e["lock"].locked():
                continue
            m = _PAGE_TARGET_RE.search(url or "")
            tid = m.group(1).lower() if m else ""
            if tid and tid not in alive_ids:
                w = _pop_slot(url, e)
                if w is not None:
                    victims.append(w)
    over = len(_WS_POOL) + room - _WS_POOL_MAX
    if over > 0:
        idle = sorted((float(e.get("ts") or 0.0), u)
                      for u, e in _WS_POOL.items() if not e["lock"].locked())
        for _ts, u in idle[:over]:
            e = _WS_POOL.get(u)
            if e is not None and not e["lock"].locked():
                w = _pop_slot(u, e)
                if w is not None:
                    victims.append(w)
    return [v for v in victims if v is not None]


def _pop_slot(url, entry):
    """摘除一个槽位（调用方持池锁）。打上 dead 标记：正在排队等这把锁的
    acquire 会看到标记并重来，不会把一个脱离池的槽位重新插回去。"""
    _WS_POOL.pop(url, None)
    entry["dead"] = True
    return entry.get("ws")


def pool_prune_dead(alive_ids):
    """CDP 枚举成功后的回收入口（v1.0.632 B6）：页面关了连接必然失效，
    留在池里只是占内存 + 占住企微内核的 devtools 会话数。返回摘除的槽位数。"""
    if not _pool_enabled():
        return 0
    ids = set()
    try:
        for t in (alive_ids or ()):
            tid = t.get("id") if isinstance(t, dict) else t
            if tid:
                ids.add(str(tid).lower())
    except TypeError:
        return 0
    if not ids:
        return 0                      # 空列表多半是枚举异常，不该据此判定"所有页面都没了"
    with _WS_POOL_LOCK:
        victims = _pool_trim_locked(ids)
    for ws in victims:
        _close_ws_quiet(ws)
    if victims:
        _logw(f"[cdp] 连接池回收 {len(victims)} 个失效槽位（池内剩 {len(_WS_POOL)}）")
    return len(victims)


def _pool_acquire(ws_url, timeout):
    """取可用的 CDP 连接：池命中复用（持 entry 锁），未命中新建并登记。
    返回 (ws, entry, err)——entry 非 None 表示走池（调用方 finally 必须配对
    _pool_release 并保持持锁状态）；err 非 None 表示新建失败（结构化错误）。"""
    if not _pool_enabled():
        ws = _open_ws(ws_url, timeout)
        if isinstance(ws, dict):
            return None, None, ws
        return ws, None, None
    for _attempt in range(3):
        pending = []
        with _WS_POOL_LOCK:
            entry = _WS_POOL.get(ws_url)
            if entry is None:
                # 先腾地方再插：反过来（先插再裁）容量判定会把**刚插的这条**算进
                # LRU 候选，于是本请求拿到一个已经被标 dead 的槽位，白跑一趟。
                pending = _pool_trim_locked(room=1)
                entry = {"lock": threading.Lock(), "ws": None, "ts": time.time()}
                _WS_POOL[ws_url] = entry
        for ws_dead in pending:
            _close_ws_quiet(ws_dead)
        # v1.0.361 审查修复（P11）：取锁加超时——原实现无上限，前一个请求满 25s 超时
        # + broken 重建时，后一个请求实际可等 50s+，自身 timeout 完全失效。
        if not entry["lock"].acquire(timeout=timeout + 5.0):
            return None, None, {"ok": False, "error": f"页面正忙（另一 CDP 操作超 {timeout + 5.0:.0f}s 未完成），请稍后重试"}
        if entry.get("dead"):
            # 排队等锁期间槽位被回收了（页面刚关 / 容量淘汰）：放开重来，
            # 绝不把一个已脱离池的 entry 再交出去——那等于泄漏一条常驻连接。
            entry["lock"].release()
            continue
        try:
            entry["ts"] = time.time()
            if entry["ws"] is None:
                ws = _open_ws(ws_url, timeout)
                if isinstance(ws, dict):
                    entry["lock"].release()
                    return None, None, ws
                entry["ws"] = ws
        except Exception:  # noqa: BLE001
            entry["lock"].release()
            raise
        return entry["ws"], entry, None
    # 三次都被回收器抢先（极端运气）：退回"每次新建"的旧行为，不因此让调用失败
    ws = _open_ws(ws_url, timeout)
    if isinstance(ws, dict):
        return None, None, ws
    return ws, None, None


def _pool_release(entry, ws, broken):
    """归还连接（调用方持锁状态调用）。broken=True 表示连接状态不可信：
    关闭底层 socket 并把槽位清空，下次 acquire 自动重建。
    v1.0.652（审计 P1-5 放大器）：归还时把 socket 超时**恢复池默认**——旧实现
    「只升不降」：一次 page_fetch_to_disk(90s)/下载(300s) 抬高超时后，后续 1.5s
    短探测在这条连接上 recv 可阻塞数百秒才超时，短调用的快失败语义失效。"""
    if entry is None:
        _close_ws_quiet(ws)
        return
    try:
        if broken and ws is not None:
            _close_ws_quiet(ws)
            entry["ws"] = None
        elif not entry.get("dead"):
            if ws is not None:
                try:
                    ws.settimeout(_POOL_SOCK_TIMEOUT)
                except Exception:  # noqa: BLE001
                    pass
            entry["ts"] = time.time()
    finally:
        try:
            entry["lock"].release()
        except Exception:  # noqa: BLE001
            pass

# ---------- 日志回调（由 backend 注入 log_event，P3-6：裸异常不再完全静默） ----------
_log = None  # backend 注入的日志函数；未注入时所有日志静默，不阻塞本模块独立使用


def set_logger(fn):
    """由 backend 注入统一日志回调（log_event）。"""
    global _log
    _log = fn


def _logw(msg):
    """安全写日志：回调缺失或自身异常都不影响主流程。"""
    if _log:
        try:
            _log(msg)
        except Exception:  # noqa: BLE001
            pass

# ---------- 隐藏子进程窗口（避免 --noconsole 下频繁弹黑框） ----------
try:
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
except Exception:  # noqa: BLE001
    CREATE_NO_WINDOW = 0x08000000  # 预期：旧 Python 无该常量时手动回退


def _hide_startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _run_hidden(args, **kw):
    """以完全隐藏方式运行命令行程序，绝不弹黑框。"""
    kw.setdefault("stdout", DEVNULL)
    kw.setdefault("stderr", DEVNULL)
    kw.setdefault("creationflags", CREATE_NO_WINDOW)
    kw.setdefault("startupinfo", _hide_startupinfo())
    try:
        return subprocess.run(args, **kw)
    except Exception:  # noqa: BLE001
        return None


def cdp_up(port=DEFAULT_PORT):
    """CDP HTTP 端点探活。v1.0.425：企微 /json 端点在繁忙时会短暂无响应
    （实测 3 分钟级挂死与秒级抖动都有），单发 1s 超时误报率高——重试 3 次、
    读超时 2s。供 dispatch 绑定闸与绑定状态展示使用。

    v1.0.651（用户需求"自动识别优先使用空端口"）：**只有真的拿到 CDP 面才算活**。
    原实现是"requests.get 不抛异常就 True"，而 HTTP 404/403/5xx 都不抛——
    实测本机 9222 上有个对 /json/version 一律回 404（Content-Length:0、无 Server 头）
    的东西占着，`cdp_up` 恒真而 `cdp_is_wecom` 恒 None（特征不明→按"企微繁忙"处理），
    于是 ensure_binding 的自动换端口分支永远不触发，界面永久卡在
    「窗口列表读取失败：接口返回结构异常」——用户视角就是"软件坏了"。
    现在：状态非 200、或响应体不是 JSON → False（这不是"繁忙"，繁忙是超时/拒绝连接，
    仍按原口径重试）。拿到明确非 CDP 响应时立刻返回，不再白等 3 轮。"""
    for _ in range(3):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=(1, 2))
            if getattr(r, "status_code", 0) != 200:
                return False
            try:
                r.json()          # 网关/代理劫持会回 200 + HTML，JSON 解析失败即非 CDP
            except Exception:  # noqa: BLE001
                return False
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)       # 超时/拒绝连接=可能只是抖动，留给重试
    return False


def port_bindable(port):
    """这个端口**真的能拿来监听**吗？（v1.0.651）

    为什么不能拿 `cdp_up` 的反面当"空闲"：一个不会提供 CDP 的服务（本机实测的 404
    空响应）在 cdp_up 眼里先是"活"、后来是"不死活"，两种判法都可能被当成可用，
    于是把企微往一个**已被监听**的端口上启动——它的调试端口起不来，绑定永远失败，
    而这比原来的报错更难查。这里用真实 bind 判定：能 bind 上就是真空闲。"""
    import socket
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def cdp_is_wecom(port=DEFAULT_PORT):
    """校验端口上的 CDP 是否真的属于企微（v1.0.336）。
    9222 是 CDP 通用端口——Chrome/Edge 带 --remote-debugging-port=9222 残留时
    /json/version 照样有响应，直接绑定会绑到别人的浏览器上（页面全是 chrome://，
    企微操作全部失败）。企微 Electron 的 /json/version UA 含 WXWork 特征。
    v1.0.555（P1-12）三态返回：
      True  = 明确是企微；False = 明确非企微（有响应但无 WXWork 特征）；
      None  = 探测失败（超时/连接错误/5xx）——不可断言"被非企微占用"。
    原两态版把探测失败也归 False，调用方 `up and not wecom_on_port` 的解释是
    "端口被非企微占用"→ 换端口 + 强杀用户企微重启（实测企微 /json 端点繁忙时会
    短暂无响应，秒级抖动都有）——一次瞬时抖动即误杀，未保存内容丢失。"""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        blob = (r.text or "").lower()
        if ("wxwork" in blob) or ("wecom" in blob):
            return True
        # 有响应但无企微特征：仅当响应体明确是 Chromium 系浏览器才判非企微
        if ("chrome" in blob) or ("edg" in blob) or ("headless" in blob):
            return False
        # 响应了但特征不明（代理/网关劫持/空 200）——按探测失败处理，不断言占用
        return None
    except Exception:  # noqa: BLE001
        return None


def _cdp_ws_ok(port=DEFAULT_PORT):
    """验证 CDP 的 WebSocket 连接可用（Chromium 可能因 origin 限制返回 403）。"""
    from .windows import list_targets  # 函数内延迟导入，避免包级循环依赖
    try:
        targets = list_targets(port)
        page = next((t for t in targets if t.get("webSocketDebuggerUrl")), None)
        if not page:
            return False
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=2,
                                         origin=CDP_CLIENT_ORIGIN)
        ws.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _open_ws(ws_url, timeout, connect_timeout=CDP_CONNECT_TIMEOUT):
    """打开 CDP WebSocket 会话；返回 ws 或 {"ok": False, "error": ...}（绝不抛未捕获异常）。
    统一处理 403 origin 限制、连接超时，连接层瞬时失败重试一次；_cdp_eval / upload_file 等共用。"""
    last_err = None
    for _ in range(2):
        try:
            ws = websocket.create_connection(ws_url, timeout=min(timeout, connect_timeout),
                                             origin=CDP_CLIENT_ORIGIN)
            ws.settimeout(timeout)  # 关键：recv 必须有超时上限，否则页面卡住会永久阻塞
            return ws
        except websocket.WebSocketBadStatusException as e:
            if "403" in str(e):
                return {"ok": False, "error": ("企微 CDP WebSocket 被 origin 限制拒绝（403）。请点「重启并绑定」"
                                      "让企微带白名单 origin 重新启动；若该内核版本仍误判，设环境变量 "
                                      "WECOM_CDP_ORIGINS=* 后重启企微回退旧行为。")}
            return {"ok": False, "error": f"CDP WebSocket 连接失败：{e}"}
        except websocket.WebSocketTimeoutException as e:
            _logw(f"[wecom] CDP WebSocket 连接超时：{e}")
            last_err = f"CDP 连接超时：{e}"
        except Exception as e:  # noqa: BLE001
            _logw(f"[wecom] CDP WebSocket 连接失败：{e}")
            last_err = f"CDP WebSocket 连接失败：{e}"
    return {"ok": False, "error": last_err}


def _json_result(raw, fail=None, empty=None):
    """CDP eval 结果的 str→JSON 解析单源（v1.0.654 A5）——原五件套
    （isinstance str→json.loads→except 兜底→raw or 默认）在 act/read/download/windows
    12 处手抄。fail=解析失败返回值（callable 则 fail(raw, exc)）；None=记录原文并
    返回 {"raw": raw}。empty=raw 为空时的默认值。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001
            if fail is not None:
                return fail(raw, e) if callable(fail) else fail
            _logw(f"[wecom] 页面数据 JSON 解析失败，按原文返回：{e}")
            return {"raw": raw}
    return raw or (empty if empty is not None else {})


def _json_ok_result(raw):
    """工具层契约 {ok,error} 形态的解析封装（4 处同款样板收敛）。"""
    return _json_result(raw, fail=lambda r, e: {"ok": False, "error": r},
                        empty={"ok": False, "error": "no response"})


def _recv_until(ws, wanted_id, deadline=None):
    """阻塞 recv 直到拿到 id == wanted_id 的消息；超时/异常返回 None（上层据此给出结构化提示）。
    相比手写 N 次轮询循环：不空转烧 CPU，且语义统一为「等目标响应」。
    v1.0.652（审计 P1-10）：补**总时限** deadline（time.monotonic 时刻）——ws.settimeout
    是单次 recv 的空闲超时，对端持续推事件（console 风暴/executionContextCreated 风暴）
    时每次 recv 都在超时前返回，while True 永不退出：单个工具调用挂死任意久，
    连接池 entry 锁被长期持有 → 同页面后续 CDP 调用集体超时（页面级雪崩）。
    deadline=None 时退化旧行为（仅依赖 socket 空闲超时）。"""
    while True:
        if deadline is not None:
            _left = deadline - time.monotonic()
            if _left <= 0:
                return None
            try:
                _cur = ws.gettimeout()
                ws.settimeout(min(float(_cur) if _cur else _left, _left))
            except Exception:  # noqa: BLE001
                pass
        try:
            msg = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            return None
        except Exception:  # noqa: BLE001
            return None
        if msg.get("id") == wanted_id:
            return msg


def _cdp_eval(ws_url, expr, timeout=CDP_EVAL_TIMEOUT, await_promise=False):
    """经 CDP 在目标页面执行 JS 并返回解析结果。
    返回：Python 值 / None(无返回) / {"ok": False, "error": ...}(结构化错误，绝不抛未捕获异常)。
    健壮性：连接超时 + 每次 recv 超时(防止页面无响应永久卡死) + 连接层瞬时失败重试一次。
    await_promise=True 时等待 Promise 结果（用于 fetch 等异步操作）。
    v1.0.345：连接经常驻池复用（评估超时/收发异常标记 broken 丢弃重建）。"""
    ws, entry, err = _pool_acquire(ws_url, timeout)
    if err:
        return err
    # v1.0.425（2026-10-03 实证）：池连接的 socket 超时是**建连那次调用**的 timeout
    # （read_page 类短评估 8s 建连后常驻）——后续重 eval（如 1.7MB 附件
    # fetch+arrayBuffer 需 >8s）沿用旧短超时必误报"CDP 评估超时"→ broken → 重连
    # 再超时 → 上层回退点击 → 弹框 → 模型看到"CDP 断开"。按本次调用的 timeout
    # 放宽本连接 recv 上限（只在变大时设置，避免缩短他人依赖的长超时）。
    try:
        if float(timeout) > ws.gettimeout():
            ws.settimeout(float(timeout))
    except Exception:  # noqa: BLE001
        pass
    broken = False
    _deadline = time.monotonic() + float(timeout)   # v1.0.652（审计 P1-10）：总时限
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable", "params": {}}))
        try:
            ws.recv()
        except websocket.WebSocketTimeoutException:
            pass  # 预期：Runtime.enable 无响应仅代表该通道无需 enable
        except Exception as e:  # noqa: BLE001
            broken = True
            _logw(f"[wecom] CDP Runtime.enable 失败：{e}")
        if not broken:
            ws.send(json.dumps({
                "id": 2, "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
            }))
            msg = _recv_until(ws, 2, _deadline)
            if msg is None:
                # 超时后连接状态不可信（可能半死）：标记 broken 丢弃重建
                broken = True
                return {"ok": False, "error": "CDP 评估超时：页面未在限定时间内响应（可能弹窗阻塞或页面已崩溃）"}
            if "error" in msg:
                return {"ok": False, "error": str(msg["error"])}
            # v1.0.361 审查修复（P10）：JS 抛错不走 msg["error"]（协议级错误），
            # 而是 result.exceptionDetails + result.result.type=error——原实现返回
            # None 被上层当"偶发空响应"空转重试，真实堆栈（exception.description）永久丢失。
            _res = msg.get("result", {}) or {}
            if _res.get("exceptionDetails"):
                try:
                    desc = (_res["exceptionDetails"].get("exception") or {}).get("description") \
                           or _res["exceptionDetails"].get("text") or "JS 异常"
                except Exception:  # noqa: BLE001
                    desc = "JS 异常"
                return {"ok": False, "error": f"页面 JS 异常：{str(desc)[:500]}"}
            res = _res.get("result", {}) or {}
            if res.get("type") == "undefined":
                return None
            if res.get("type") == "object" and res.get("subtype") == "error":
                return {"ok": False, "error": f"页面 JS 异常：{res.get('description', '')[:500]}"}
            return res.get("value")
        return {"ok": False, "error": "CDP 连接不可用（Runtime.enable 失败，已丢弃池化连接）"}
    except Exception as e:  # noqa: BLE001
        # send/recv 层异常：连接已不可信
        broken = True
        return {"ok": False, "error": f"CDP 通信异常：{e}"}
    finally:
        _pool_release(entry, ws, broken)


def _cdp_eval_retry(ws_url, expr, retries=3, timeout=CDP_EVAL_TIMEOUT,
                    await_promise=False, retry_delay=0.4):
    """重试封装：对瞬时抖动（页面繁忙/React 重渲染导致 evaluate 返回空）自动重试，
    拿到 str/dict 即返回。全部重试耗尽后返回最后一次 raw（可能为 None 或 {"ok": False, "error": ...}）。
    v1.0.361 审查修复（O9）：①{"ok": False, "error":...} 不再当成功提前返回（那恰是最该重试的
    瞬时故障——连接失败/评估超时）；②合法 None/数字/布尔不再被空转重试。"""
    raw = None
    for _attempt in range(retries):
        raw = _cdp_eval(ws_url, expr, timeout=timeout, await_promise=await_promise)
        if is_err(raw):
            time.sleep(retry_delay)     # 结构化错误：可重试（连接/超时类）
            continue
        if raw is None:
            time.sleep(retry_delay)     # None：偶发空响应，重试
            continue
        break                            # str / dict（有效数据）/ 其它原生值：成功
    return raw


def _wait_dom_ready(ws_url, timeout=2.0):
    """读取前等待页面 DOM 就绪（document.readyState === 'complete'），防空快照（P4-2）。
    详情页/表单常异步渲染，过早 evaluate 可能拿到空白或未渲染的 body，导致读字段全未命中
    或快照无内容。等待耗时可控（默认 ≤2s），超时不阻塞，直接返回继续读（页面已稳定则不额外等）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = _cdp_eval(ws_url, "document.readyState", timeout=1.5)
        except Exception:  # noqa: BLE001
            return False
        if r == "complete":
            return True
        time.sleep(0.15)
    return False


# 正文长度探测：零拷贝（只取 innerText.length），比完整读取快得多
_BODY_LEN_JS = "(() => { try { return ((document.body ? document.body.innerText : '') || '').length; } catch(e) { return -1; } })()"


def _wait_body_stable(ws_url, budget=4.0, interval=0.4, stable_checks=3, min_len=0):
    """等待页面「正文长度」连续多次采样不变，作为 SPA 异步渲染落定的判据。

    背景：财务 OA / 企业微信内嵌页均为 SPA，导航后 document.readyState 很快就是
    'complete'，但表格行、表单字段是之后由 JS 异步渲染进来的——只看 readyState
    会读到半加载快照（表现为字段大量 count=0）。正文长度在渲染期间通常会持续增长，
    连续 stable_checks 次不变才认为内容暂时稳定。

    min_len（v1.0.191）：稳定值还必须 ≥ min_len 才判定稳定——页面切换瞬间
    （旧内容清空、新内容未渲染）body 会短暂稳定在 0/极小值，仅"稳定"会被误判为
    "已就绪"导致读到空页/半加载快照。要求稳定在非空长度上可避开该盲区。

    返回 True=已稳定（且长度 ≥ min_len）；False=预算耗尽仍未稳定（页面可能有
    定时器/动画持续变动，或长度始终不足 min_len），调用方应继续执行而非阻塞。"""
    deadline = time.time() + max(0.5, float(budget))
    interval = max(0.15, float(interval))
    need_runs = max(1, int(stable_checks) - 1)
    min_len = max(0, int(min_len))
    last = None
    runs = 0
    while time.time() < deadline:
        try:
            r = _cdp_eval(ws_url, _BODY_LEN_JS, timeout=1.5)
            n = int(r) if r is not None else -1
        except Exception:  # noqa: BLE001
            return False
        if n < 0:
            time.sleep(interval)
            continue
        if n == last and n >= min_len:
            runs += 1
            if runs >= need_runs:
                return True
        else:
            runs = 0
        last = n
        time.sleep(interval)
    return False


def _cdp_command(ws_url, method, params=None, timeout=CDP_EVAL_TIMEOUT):
    """发送任意 CDP 命令（非 Runtime.evaluate），返回 result dict 或 {"ok": False, "error": ...}。
    用于 Browser.setDownloadBehavior 等浏览器级命令。连接/超时语义与 _cdp_eval 一致。
    v1.0.345：连接经常驻池复用（超时/收发异常标记 broken 丢弃重建）。"""
    ws, entry, err = _pool_acquire(ws_url, timeout)
    if err:
        return err
    broken = False
    _deadline = time.monotonic() + float(timeout)   # v1.0.652（审计 P1-10）：总时限
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        msg = _recv_until(ws, 1, _deadline)
        if msg is None:
            broken = True
            return {"ok": False, "error": "CDP 命令超时"}
        if "error" in msg:
            return {"ok": False, "error": str(msg["error"])}
        return msg.get("result", {})
    except Exception as e:  # noqa: BLE001
        broken = True
        return {"ok": False, "error": f"CDP 通信异常：{e}"}
    finally:
        _pool_release(entry, ws, broken)

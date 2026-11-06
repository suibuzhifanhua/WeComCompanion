"""企微进程检测与 CDP 绑定（自动检测 exe / 进程枚举 / 签名 / 幂等绑定重启）。"""

import os
import time
import subprocess
import hashlib

from .cdp_client import (
    DEFAULT_PORT, CDP_FLAGS, CREATE_NO_WINDOW, _logw, _run_hidden, cdp_up, _cdp_ws_ok,
    cdp_is_wecom, port_bindable,
)
from .windows import (list_targets, window_list_summary,   # v1.0.523：+静默收纳三件套
                      _stage_guard, _wxwork_visible_hwnds, _minimize_hwnds,
                      _agent_busy)   # v1.0.570：启动收纳的忙闲闸门

# ---------- 进程枚举的唯一绑定点（v1.0.617）----------
# 修前的形状：两处进程检测各自在**函数体内** `class PROCESSENTRY32`（每次调用都新建一个
# 类对象），再把 `POINTER(自己的类)` 写进**全进程共享**的 `ctypes.windll.kernel32`。
# 单线程下看不出问题（谁调用前谁覆写，紧接着就用），但静默收纳守护跑在后台线程、
# 界面轮询与重启流程跑在请求线程：A 设完签名还没调用，B 已把它改成 B 的类，
# A 再带着 A 的实例调用 → ctypes 按**类身份**判等，直接 ArgumentError。
# 而 find_wecom_all_pids 外层是 `except Exception: return set()`——企微进程检测**静默变空**，
# 重启流程据此认为"进程都死干净了"直接 Popen 新实例，Electron 单实例锁让新实例闪退
# （就是历史上那批"偶发绑定失败"）。
# 现在：一份结构体 + 一个模块私有的 kernel32 实例（WinDLL 的函数指针缓存在实例上，
# 不与 windll 共享），签名只在导入期绑一次，调用点不再改任何全局对象。
try:
    import ctypes
    from ctypes import wintypes as _wt

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", _wt.DWORD),
            ("cntUsage", _wt.DWORD),
            ("th32ProcessID", _wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", _wt.DWORD),
            ("cntThreads", _wt.DWORD),
            ("th32ParentProcessID", _wt.DWORD),
            ("pcPriClassBase", _wt.LONG),
            ("dwFlags", _wt.DWORD),
            ("szExeFile", _wt.CHAR * 260),
        ]

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE = _wt.HANDLE(-1).value
    _K32.CreateToolhelp32Snapshot.restype = _wt.HANDLE
    _K32.CreateToolhelp32Snapshot.argtypes = [_wt.DWORD, _wt.DWORD]
    _K32.Process32First.restype = _wt.BOOL
    _K32.Process32First.argtypes = [_wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    _K32.Process32Next.restype = _wt.BOOL
    _K32.Process32Next.argtypes = [_wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    _K32.CloseHandle.restype = _wt.BOOL
    _K32.CloseHandle.argtypes = [_wt.HANDLE]
except (OSError, AttributeError, ImportError):   # 非 Windows：本项目不会遇到，留导入不炸
    ctypes = None
    _K32 = None
    _PROCESSENTRY32 = None
    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE = -1

# ---------- 1. 自动检测企微 exe ----------
def detect_wecom_exe():
    """返回本机企微 exe 路径，找不到返回 None。优先常见路径，回退注册表。"""
    candidates = [
        r"C:\Program Files (x86)\WXWork\WXWork.exe",
        r"C:\Program Files\WXWork\WXWork.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\WXWork\WXWork.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    # 回退：注册表卸载项里找 DisplayName 含「企业微信/WeCom/WXWork」的 InstallLocation
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub = winreg.EnumKey(key, i)
                    sk = winreg.OpenKey(key, sub)
                    name = winreg.QueryValueEx(sk, "DisplayName")[0]
                    if any(k in name for k in ("企业微信", "WeCom", "WXWork")):
                        loc = winreg.QueryValueEx(sk, "InstallLocation")[0]
                        if loc:
                            for cand in (os.path.join(loc, "WXWork.exe"),
                                         os.path.join(loc, "WXWork", "WXWork.exe")):
                                if os.path.exists(cand):
                                    return cand
                except OSError:
                    continue
    except Exception as e:  # noqa: BLE001
        _logw(f"[wecom] 检测企微安装路径异常，将尝试用默认路径：{e}")
    return None


# ---------- 2. 进程检测 ----------
def find_wecom_pids():
    """返回正在运行的 WXWork.exe 进程 PID 列表。
    优先用 Win32 API 枚举（CreateToolhelp32Snapshot），不创建任何控制台窗口；
    极端情况下才回退到隐藏式 tasklist。
    """
    try:
        return _find_wecom_pids_winapi()
    except Exception as e:  # noqa: BLE001
        # 回退本身是好事（tasklist 也能拿到 PID），但**必须留痕**：不然"为什么这次走了子进程
        # 慢路径"事后完全看不出来，而这条路上的异常正是上面那类签名竞态的出口。
        _logw(f"[wecom] Win32 进程枚举失败，回退 tasklist：{type(e).__name__}: {e}")
        return _find_wecom_pids_tasklist()


def _enum_pids_named(names):
    """唯一的进程枚举通路：返回进程名在 names（小写元组）里的 PID 列表。

    异常照实抛给调用方（要么回退 tasklist，要么记日志后按"没有企微进程"处理），
    但不再存在"撞上别的调用点刚写进去的 POINTER(类)"这种自伤。
    """
    if _K32 is None:
        raise OSError("非 Windows，无法用 Win32 枚举进程")
    snap = _K32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        raise OSError("CreateToolhelp32Snapshot failed")
    pids = []
    try:
        e = _PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if _K32.Process32First(snap, ctypes.byref(e)):
            while True:
                if e.szExeFile.decode("ascii", "ignore").lower() in names:
                    pids.append(int(e.th32ProcessID))
                if not _K32.Process32Next(snap, ctypes.byref(e)):
                    break
    finally:
        _K32.CloseHandle(snap)
    return pids


def find_wecom_all_pids():
    """v1.0.285：返回 WXWork.exe + WXWorkWeb.exe 全部进程 PID 集合（set[int]）。
    给 window-tucker 的「无标题 PID 关联最小化」用：学习平台的 OS 窗口属主
    可能是主进程也可能是 webview 子进程，两族都算企微窗口。

    v1.0.617：枚举失败仍返回空集合（契约不变——调用方按"没有企微窗口"处理是安全方向，
    宁可不最小化），但**必须留痕**：以前 `except Exception: return set()` 一个字都不写，
    竞态把 PID 检测打成空之后，重启流程据此认为"进程已经死干净"直接 Popen 新实例 → 闪退。"""
    try:
        return set(_enum_pids_named(("wxwork.exe", "wxworkweb.exe")))
    except Exception as e:  # noqa: BLE001
        _logw(f"[wecom] find_wecom_all_pids 枚举异常（本轮按无企微进程处理）：{e}")
        return set()


def _find_wecom_pids_winapi():
    """纯 Win32 API 枚举 WXWork.exe，零控制台窗口、零依赖。"""
    return _enum_pids_named(("wxwork.exe",))


def _find_wecom_pids_tasklist():
    try:
        res = _run_hidden(
            ["tasklist", "/fi", "imagename eq WXWork.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10,
        )
        if not res:
            return []
        out = res.stdout
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.strip().splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == "wxwork.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass  # 预期：非数字 PID 行直接跳过
    return pids


def windows_signature(targets):
    """用窗口标题+URL 算一个短签名，用于「变更检测」。"""
    if not isinstance(targets, list):
        return "none"
    s = "|".join(f"{t.get('title')}|{t.get('url')}" for t in targets if t.get("type") == "page")
    return hashlib.md5(s.encode("utf-8", "ignore")).hexdigest()[:12]


# ---------- 3. 幂等绑定 ----------
def _relaunch_with_cdp(exe, port, pid=None):
    """带 CDP flag 重启企微。返回 `(True, "")` 或 `(False, 为什么没重启)`。

    v1.0.649（用户："软件修复的时候不要影响企业微信（之前修复软件打包等过程中会关闭企业微信）"）：
    **删掉按镜像名全杀那一步**。原流程是"精准杀 bound_pid → 5s 没清干净就
    `taskkill /f /im WXWork.exe`"，而 bound_pid 是上次绑定时记的 PID，重启过软件或企微自升级后
    必然陈旧（本机实测：dist 配置里 69560、源码配置里 7484，都对不上当前进程）——
    精准杀几乎每次落空，于是**无条件**升级成"杀全家"，把用户自己开着的企微窗口一并带走。
    这就是那条报障的成因，不是打包脚本干的（build.py 只杀 WeComCompanion.exe）。

    现在：只杀**当场枚举到的**企微进程（各自连子进程树 `/t`——v1.0.194 那个"只杀主进程
    新实例会闪退"的坑仍然堵着）；5s 内没退干净就放弃重启、返回原因，由界面提示
    "请手动退出企业微信后再绑定"。宁可这次绑不上，也不替用户决定关掉他的工作现场。
    """
    alive = find_wecom_all_pids()
    targets = sorted(alive | ({pid} if pid and pid in alive else set()))
    for t in targets:
        _run_hidden(["taskkill", "/f", "/t", "/pid", str(t)])
    for _ in range(10):  # 最多 5s
        if not find_wecom_all_pids():
            break
        time.sleep(0.5)
    else:
        _logw("[wecom] 企微进程 5s 内未全部退出，已放弃带 CDP 重启（不再按镜像名全杀）")
        return False, ("企业微信没能完全退出（可能有窗口或托盘进程仍在），本次**没有**强杀它。"
                       "请手动完全退出企业微信（含系统托盘右键「退出」）后再点「重新绑定」。")
    if not (exe and os.path.exists(exe)):
        return False, "没找到企业微信程序（exe 路径无效），未做任何进程操作。请重新检测或手动指定路径。"
    subprocess.Popen([exe, f"--remote-debugging-port={port}"] + CDP_FLAGS,
                     creationflags=CREATE_NO_WINDOW)
    _logw(f"[wecom] 已带 CDP flag 重启企微（{exe} port={port}）")
    _minimize_wecom_when_ready(port)   # v1.0.523：静默模式下 CDP 就绪后自动收纳窗口
    return True, ""


def _minimize_wecom_when_ready(port, max_wait_s=45):
    """v1.0.523（静默模式）：企微重启后主窗口默认前置抢焦点——等 CDP 就绪
    （说明已完成登录进入主界面，此时收走不影响用户操作 CDP）后最小化全部
    企微窗口。CDP 始终未就绪（等扫码登录）则不动——用户需要看登录界面。"""
    import threading as _th

    def _job():
        import time as _t
        deadline = _t.time() + max_wait_s
        ready = False
        while _t.time() < deadline:
            try:
                tg = list_targets(port)
                if isinstance(tg, list) and tg:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            _t.sleep(2.0)
        if not ready:
            _logw("[wecom] 企微重启后 CDP 未就绪（可能等待登录），保持窗口可见")
            return
        _t.sleep(1.5)   # 等窗口渲染稳定再收，避免收掉半初始化的窗口
        from core.config_store import silent_mode as _sm
        if not _sm():
            return
        # v1.0.570（用户需求）：启动收纳同样只在 agent 任务执行时发生——
        # 空闲启动（无在途任务）企微窗口保持原样，不打扰用户。
        if not _agent_busy():
            _logw("[wecom] 启动收纳跳过：当前无 agent 任务在跑（窗口保持可见）")
            return
        _st, hwnds = _stage_guard(_wxwork_visible_hwnds, 5.0, "relaunch-min")
        if _st == "ok" and hwnds:
            _st2, n = _stage_guard(lambda: _minimize_hwnds(hwnds), 5.0, "relaunch-min2")
            if _st2 == "ok" and n:
                _logw(f"[wecom] 静默模式：已收纳重启后的 {n} 个企微窗口（不影响 CDP 操作）")

    _th.Thread(target=_job, name="wecom-relaunch-min", daemon=True).start()


CDP_PORT_SCAN_SPAN = 8      # 避让时最多往后试几个口（沿用 v1.0.336 的 8 个）


def _cdp_port_state(port):
    """一个端口相对"能不能当企微 CDP 用"的五种状态（v1.0.651）。

    策略放在本模块而不是 cdp_client：它调的是本模块的 `cdp_up` / `cdp_is_wecom`
    这两个**模块级名字**——测试正是 patch 这两个来造场景。放进传输层会绕过补丁
    （第一版就这么栽的：假场景没生效，端口择优拿本机真端口探活，把真实企微 PID
    录进了 kill 列表，只是被记录型假桩接住才没真杀）。

      wecom    端口上就是企业微信自己的调试端口——**必须原样复用**，
               为了"更保险"挪走等于要重启企微。
      free     真的没人监听，企微带 --remote-debugging-port 起得来。
      foreign  有 CDP 面但不是企微（Chrome/Edge 残留的调试端口，v1.0.336 就为这个换过口）。
      occupied 有东西在监听但不成 CDP 面（本机实测：对 /json/version 一律 404、
               Content-Length:0、无 Server 头）——**这种最阴**：旧口径里它既算"活"
               又"特征不明"，于是自动避让永远不触发。
      unclear  响应了但认不出是谁（代理劫持、企微繁忙回空 200）。沿用 v1.0.555
               （P1-12）：探测失败不许断言占用——一次瞬时抖动就换端口 + 重启企微，
               丢的是用户未保存的聊天草稿。
    """
    if cdp_up(port):
        _w = cdp_is_wecom(port)
        if _w is True:
            return "wecom"
        if _w is False:
            return "foreign"
        return "unclear"
    return "free" if port_bindable(port) else "occupied"


def pick_cdp_port(start, span=CDP_PORT_SCAN_SPAN):
    """给 CDP 挑一个能用的端口，返回 (port, reason)；port=None 表示整段都不行。
    优先：已经是企微调试端口的 > 真空闲的（都含配置端口本身）。
    本函数只读不改：不写配置、不动进程——落盘与重启由调用方按各自的闸门决定。"""
    try:
        start = int(start)
    except (TypeError, ValueError):
        start = DEFAULT_PORT
    for cand in [start] + list(range(start + 1, start + 1 + int(span))):
        st = _cdp_port_state(cand)
        if st == "wecom":
            return cand, "端口上已是企业微信的调试端口，原样复用"
        if st == "unclear":
            return cand, "端口响应特征不明，按原端口继续（不断言被占用）"
        if st == "free":
            return cand, ("配置端口可用" if cand == start else f"{start} 不可用，改用空闲端口")
    return None, f"{start} 及其后 {span} 个候选端口都被其他程序占用"


def ensure_binding(cfg, force=False, allow_kill=True):
    """
    幂等绑定企微 CDP。直接修改传入 cfg['wecom'] 的 exe_path / bound_pid / bound_sig。
    返回 status dict：{bound, changed, reason, exe_path, port, pid, auto_launch, windows}
    allow_kill=False 时绝不强杀/重启企业微信：仅在需要重启时返回 needs_kill=True，
    由调用方（UI 手动按钮）向用户确认后再以 allow_kill=True 重试。
    """
    w = cfg.setdefault("wecom", {})
    # v1.0.588：端口取值统一走 core.config_store.cdp_port。原 `w.get("cdp_port",
    # DEFAULT_PORT)` 只在键缺失时兜底，配置里 cdp_port 为空串/None 时会把空值传下去，
    # 绑定与后续 CDP 调用全部落到无效端口。
    from core.config_store import cdp_port as _cdp_port
    port = _cdp_port(cfg)
    exe = w.get("exe_path") or detect_wecom_exe()
    if exe:
        w["exe_path"] = exe
    auto_launch = w.get("auto_launch", True)
    auto_cdp = w.get("auto_cdp", True)
    prev_pid = w.get("bound_pid")

    pids = find_wecom_pids()
    # 端口择优（v1.0.651，用户需求"自动识别优先使用空端口"）。三条历史约束都保住：
    #  · v1.0.336：9222 是 CDP 通用口，Chrome/Edge 残留时 /json/version 照样有响应，
    #    直接复用会绑到别人的浏览器上（页面全是 chrome://，企微操作全挂）→ 认不出企微就避让；
    #  · v1.0.555（P1-12）：探测失败（超时/5xx/特征不明）不能断言"被非企微占用"——
    #    企微 /json 繁忙时秒级抖动都有，误判会换来"改端口 + 强杀重启用户企微"，
    #    未保存的聊天/文档丢一次就再没人敢让软件自动动进程 → unclear 一律留原端口；
    #  · 避让只改端口，**不**顺带杀进程；杀不杀仍由下方 allow_kill/确认闸决定。
    # 本机实测的失效形态由这条修掉：9222 上有个对 /json/version 一律回 404 的东西，
    # 旧 cdp_up"有响应即活"、旧 cdp_is_wecom"无特征即 None"，两条一叠加
    # 自动换端口永远不触发，界面就永久卡在「窗口列表读取失败：接口返回结构异常」。
    _chosen, _why = pick_cdp_port(port)
    if _chosen is None:
        return {
            "bound": False, "changed": True,
            "reason": (f"CDP 端口 {port} 及其后 {CDP_PORT_SCAN_SPAN} 个候选都被其他程序占用，"
                       "且都不是企业微信的调试端口。本次**没有**关闭或重启企业微信——"
                       "请关掉占用端口的程序，或在「设置 → 企业微信」里手填一个空闲端口。"),
            "exe_path": exe, "port": port, "pid": (pids[0] if pids else None),
            "auto_launch": auto_launch, "windows": 0,
        }
    if _chosen != port:
        _logw(f"[wecom] CDP 端口自动避让：{port} → {_chosen}（{_why}），已写回配置")
        w["cdp_port"] = _chosen
        port = _chosen
    up = cdp_up(port)
    wecom_on_port = cdp_is_wecom(port) if up else False
    ws_ok = _cdp_ws_ok(port) if up else False

    def _save(pid, refresh_started=True):
        w["bound_pid"] = pid
        # v1.0.470（P1-18）：记录绑定确认时刻——启动宽限窗口的起点（见下方宽限检查）
        # v1.0.555（P0-8）：refresh_started=False 供宽限路径使用——原宽限每次调用都
        # _save() 刷新起点时间戳，age 永远 <90s → 永远宽限、每次同步阻塞 75s、
        # 永不进入 relaunch 注入 CDP（用户永久卡"启动中"，只能 force 或手动杀进程）。
        if refresh_started:
            w["bound_started_ts"] = time.time()
        try:
            w["bound_sig"] = windows_signature(list_targets(port))
        except Exception as e:  # noqa: BLE001
            _logw(f"[wecom] 计算绑定进程签名失败：{e}")
            w["bound_sig"] = None

    # 已绑定且未变更：直接复用，不做任何操作
    if not force and up and ws_ok and wecom_on_port and prev_pid and prev_pid in pids:
        sig = windows_signature(list_targets(port))
        changed = (sig != w.get("bound_sig"))
        if changed:
            _save(prev_pid)
        return {
            "bound": True, "changed": changed, "reason": "reuse" if not changed else "windows changed",
            "exe_path": exe, "port": port, "pid": prev_pid,
            "auto_launch": auto_launch, "windows": len(window_list_summary(port)),
        }

    # v1.0.470 修复（P1-18）启动宽限窗口：冷启动+登录校验超 15s 时上一轮调用返回
    # "未绑定"，前端重试再次进入会走到下方 relaunch 路径——把刚启动/正在登录的企微
    # 强杀重启（杀-启-杀循环，用户草稿丢失）。绑定 PID 仍存活且距最近一次启动确认
    # < 90s → 只等待 CDP 就绪，绝不重启；宽限内仍不就绪则返回"启动中"，
    # 把重启决定留给窗口过期后的下一次调用。
    if pids and prev_pid and prev_pid in pids and auto_cdp and not force:
        try:
            _age = time.time() - float(w.get("bound_started_ts") or 0)
        except (TypeError, ValueError):
            _age = 1e9
        if 0 <= _age < 90:
            _logw(f"[wecom] 企微启动宽限中（{_age:.0f}s < 90s），等待 CDP 就绪而不重启")
            for _ in range(150):   # 最多再等 75s
                if cdp_up(port) and _cdp_ws_ok(port) and cdp_is_wecom(port):
                    break
                if not find_wecom_pids():
                    break   # 进程消失（启动失败）→ 走正常处理
                time.sleep(0.5)
            _pids2 = find_wecom_pids()
            new_pid2 = _pids2[0] if _pids2 else None
            # v1.0.555（P0-8）：宽限路径不刷新起点时间戳——age 从"最初发现未就绪"
            # 起算，90s 后自然落到下方 relaunch 自愈（原每次 _save 都刷新 → 无限宽限）。
            _save(new_pid2, refresh_started=False)
            if cdp_up(port) and _cdp_ws_ok(port):
                return {
                    "bound": True, "changed": True, "reason": "grace-wait cdp ready",
                    "exe_path": exe, "port": port, "pid": new_pid2,
                    "auto_launch": auto_launch, "windows": len(window_list_summary(port)),
                }
            if _pids2:
                return {
                    "bound": False, "changed": False,
                    "reason": "企微启动中（宽限窗口内不重启）——CDP 尚未就绪，请稍候重试",
                    "exe_path": exe, "port": port, "pid": new_pid2,
                    "auto_launch": auto_launch, "windows": 0,
                }

    # CDP HTTP 在线但 WebSocket 被 403 拒绝：需要带 --remote-allow-origins=* 重启
    if up and not ws_ok:
        if pids and not auto_cdp:
            return {
                "bound": False, "changed": True,
                "reason": ("企微 CDP 已开启但 WebSocket 被 origin 限制拒绝（403）。"
                           "请手动带 --remote-debugging-port=9222 与白名单 origin 启动；"
                           "或设环境变量 WECOM_CDP_ORIGINS=* 后重启企微回退旧行为。"),
                "exe_path": exe, "port": port, "pid": pids[0],
                "auto_launch": auto_launch, "windows": 0,
            }
        # 下面走 relaunch/launch 路径（已自动加上 CDP_FLAGS）

    # CDP 已在线且 websocket 通（可能是上次启动遗留）：记录即可
    if up and ws_ok:
        new_pid = pids[0] if pids else None
        _save(new_pid)
        return {
            "bound": True, "changed": True, "reason": "cdp already up, recorded",
            "exe_path": exe, "port": port, "pid": new_pid,
            "auto_launch": auto_launch, "windows": len(window_list_summary(port)),
        }

    # CDP 没起来
    if pids and not auto_cdp:
        return {
            "bound": False, "changed": True,
            "reason": "企微正在运行但未带 CDP flag，且已关闭自动注入（auto_cdp=false）。请手动带 --remote-debugging-port 启动。",
            "exe_path": exe, "port": port, "pid": pids[0],
            "auto_launch": auto_launch, "windows": 0,
        }
    if pids:
        if not allow_kill:
            return {
                "bound": False, "changed": True,
                "reason": "企微正在运行但未带 CDP flag，需重启企业微信才能注入 CDP。"
                          "重启将关闭全部企业微信窗口，未保存内容可能丢失；请在界面确认后手动触发「重启并绑定」。",
                "exe_path": exe, "port": port, "pid": pids[0],
                "auto_launch": auto_launch, "windows": 0, "needs_kill": True,
            }
        _ok, _why = _relaunch_with_cdp(exe, port, prev_pid or (pids[0] if pids else None))
        if not _ok:
            # v1.0.649：退不干净 / 找不到 exe 都**停在这里**，不换成"按镜像名全杀"再试一次
            return {
                "bound": False, "changed": True, "reason": _why,
                "exe_path": exe, "port": port, "pid": pids[0],
                "auto_launch": auto_launch, "windows": 0,
            }
    elif auto_launch and exe:
        subprocess.Popen([exe, f"--remote-debugging-port={port}"] + CDP_FLAGS,
                         creationflags=CREATE_NO_WINDOW)  # 没跑 → 启动
    else:
        return {
            "bound": False, "changed": True,
            "reason": "未检测到企微运行进程" + ("；auto_launch 已关闭" if not auto_launch else "")
                      + ("" if exe else "；且未找到企微 exe"),
            "exe_path": exe, "port": port, "pid": None,
            "auto_launch": auto_launch, "windows": 0,
        }
    # 等待 CDP 起来（v1.0.194：预算 6s→15s，企微冷启动/登录校验可能超过 6s；
    # 杀干净后启动成功率大幅提升，15s 足够覆盖正常启动）
    # v1.0.336：HTTP+WebSocket 双确认才算就绪——只查 HTTP 会把「端口通但 WS 403」
    # 误判为绑定成功（假阳性：界面显示已连接，实际页面操作全部失败）
    for _ in range(30):
        if cdp_up(port) and _cdp_ws_ok(port):
            break
        time.sleep(0.5)
    _pids = find_wecom_pids()
    new_pid = _pids[0] if _pids else None
    _save(new_pid)
    if cdp_up(port) and _cdp_ws_ok(port):
        return {
            "bound": True, "changed": True, "reason": "launched/relaunched with CDP",
            "exe_path": exe, "port": port, "pid": new_pid,
            "auto_launch": auto_launch, "windows": len(window_list_summary(port)),
        }
    # 启动失败诊断（v1.0.194）：区分"企微没起来"与"企微起来了但没开 CDP"
    if _pids and cdp_up(port):
        return {
            "bound": False, "changed": True,
            "reason": ("企微已启动、CDP 端口已开但 WebSocket 不可用（多为 403 origin 限制）。"
                       "请在软件里点「重启并绑定」让企微带白名单 origin 重新启动；"
                       "若该内核版本仍误判，设环境变量 WECOM_CDP_ORIGINS=* 后重试。"),
            "exe_path": exe, "port": port, "pid": new_pid,
            "auto_launch": auto_launch, "windows": 0,
        }
    if _pids:
        return {
            "bound": False, "changed": True,
            "reason": ("企微进程已启动但 CDP 调试端口不可用（可能版本禁用 remote debugging，"
                       "或启动时弹出了单实例提示）。请手动完全退出企业微信后，在软件里点「重启并绑定」重试。"),
            "exe_path": exe, "port": port, "pid": new_pid,
            "auto_launch": auto_launch, "windows": 0,
        }
    return {
        "bound": False, "changed": True,
        "reason": "企微重启后进程未出现（启动失败）。请手动打开企业微信，或检查安装路径后重试。",
        "exe_path": exe, "port": port, "pid": None,
        "auto_launch": auto_launch, "windows": 0,
    }

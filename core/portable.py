# -*- coding: utf-8 -*-
"""core.portable — 便携运行时的统一定位 + 软件内自动安装（v1.0.612）

为什么要有这个模块：「一键安装」的语义原先在 Node / Git 上断掉了——体检行只丢两个
"去官网下载"的链接，用户得自己装完再回来点第二下。而 Node 是 dsh（PC 接管）的**硬前置**，
缺它整条 pc_takeover 不可用，实测报错就是「Node 环境不满足，无法安装：node --version
未返回有效版本」。这里把前置条件收回软件自己承担：下载 zip 版运行时解到 `组件/<name>/`，
**进程内解析**（不写系统 PATH、不需要管理员、不动用户已有的安装）。

同时收掉一处重复：`which("git") + ~/.workbuddy/binaries/PortableGit/...` 这套找法原先在
`routes/b_ocr_admin.py`、`routes/b_repair.py`、`services/self_repair.py`、`services/version_store.py`
各抄了一遍——加第三种来源就要改四处，是本项目反复踩过的"改一处漏一处"。现在只有一个 `find_git()`。
"""
import json
import os
import re
import shutil
import threading
import time
import zipfile

from core.logging import log_event
from core.paths import workspace_root

_IS_WIN = os.name == "nt"
# 每个运行时的安装状态各自一份（OCR/ASR 那侧也是这个形状，组件页 3 秒轮询靠它出百分比）
_STATE_LOCK = threading.Lock()
_STATE = {}

_NODE_MIN = (22, 19)

# 索引页与挑选规则。npmmirror 的 `/-/binary/<产品>/` 直接返回 JSON 目录列表，
# 不用解析 HTML；官方源只是兜底（npmmirror 偶发 5xx，或某版本没同步）。
_TARGETS = {
    "node": {
        "name": "Node.js",
        "index": "https://registry.npmmirror.com/-/binary/node/latest-v22.x/",
        "official": lambda url: url.replace("/-/binary/node/", "/dist/").replace(
            "https://registry.npmmirror.com", "https://nodejs.org"),
        "file_re": re.compile(r"^node-v(\d+)\.(\d+)\.(\d+)-win-x64\.zip$", re.I),
        "exe": "node.exe",
        "size_mb": 34,
        # 落盘量按 2026-10-23 本机实测：node.exe 81.6MB + npm/corepack ≈ 95MB 合计。
        "disk_mb": 95,
    },
    "git": {
        "name": "Git",
        # 钉一个稳定版目录（v2.56.0.windows.1 是 rc 之后的正式版）；MinGit 是官方
        # "最小可执行"便携包，解压即用，没有 GUI 组件，正合软件内调用。
        "index": "https://registry.npmmirror.com/-/binary/git-for-windows/v2.56.0.windows.1/",
        "official": lambda url: (
            "https://github.com/git-for-windows/git/releases/download/"
            + url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1]),
        "file_re": re.compile(r"^MinGit-(\d+)\.(\d+)\.([\d.]+)-64-bit\.zip$", re.I),
        "exe": os.path.join("cmd", "git.exe"),
        "size_mb": 38,
        "disk_mb": 120,      # 本机系统已装 Git，便携版没实测过，按官方包结构估
    },
}


def portable_root(name):
    """`<运行目录>/组件/<name>`（frozen 下就是 dist/组件/，与 OCR、ASR 同一层）。"""
    d = os.path.join(workspace_root(), "组件", name)
    os.makedirs(d, exist_ok=True)
    return d


def _marker(name):
    return os.path.join(portable_root(name), "_portable.json")


def installed_meta(name):
    try:
        with open(_marker(name), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _exe_candidates(name, rel, base=None):
    """便携目录里的可执行文件。zip 可能自带一层顶层目录（node 的 zip 是
    `node-v22.20.0-win-x64/node.exe`，解到 current/ 下就是两层），所以往下找两层为止。
    先信标记里记下的确切路径，再回落到搜索——包结构哪天变了也不会静默找不到。

    base 显式指定搜索根（v1.0.632 B5：原子提交**前**要先验刚解压的那批文件），
    此时不采信标记里的旧路径——旧 runtime 的文件真实存在，会让"新包有没有 exe"
    这个判定凭空通过。"""
    out = []
    root = portable_root(name) if base is None else base
    seen = set()
    if base is None:
        rec = installed_meta(name).get("exe")
        if rec and os.path.isfile(rec):
            out.append(rec)
            seen.add(rec)

    def _walk(base, depth):
        if depth > 2:
            return
        p = os.path.join(base, rel)
        if os.path.isfile(p) and p not in seen:
            seen.add(p)
            out.append(p)
        try:
            subs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        except OSError:
            return
        for d in subs:
            _walk(os.path.join(base, d), depth + 1)

    _walk(root, 0)
    return out


def portable_exe(name):
    t = _TARGETS.get(name)
    if not t:
        return None
    cands = _exe_candidates(name, t["exe"])
    return cands[0] if cands else None


# ---------------------------------------------------------------- 定位（PATH → 便携）
def find_node():
    """node 可执行路径：系统 PATH 优先（尊重用户已有安装），其次软件自装的便携版。"""
    p = shutil.which("node")
    if p:
        return p
    return portable_exe("node")


def find_npm():
    """npm 与 node 同目录（便携版是 npm.cmd；系统装的也可能是 npm.cmd/npm）。"""
    node = find_node()
    if not node:
        return None
    d = os.path.dirname(node)
    for c in ("npm.cmd", "npm", "npm.exe"):
        p = os.path.join(d, c)
        if os.path.isfile(p):
            return p
    return shutil.which("npm")


def find_git():
    """git 可执行：PATH → Qoder 自带的 PortableGit → 软件自装的 MinGit。"""
    p = shutil.which("git")
    if p:
        return p
    cand = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "PortableGit",
                        "versions", "1.2.0", "cmd", "git.exe")
    if os.path.isfile(cand):
        return cand
    return portable_exe("git")


def node_env(extra_path=None):
    """给子进程的 env：把 node 所在目录顶到 PATH 前面。

    npm.cmd 内部要再起 node，而便携版从没进过系统 PATH —— 不补这一步，
    `npm install -g` 会在"node 找不到"上失败，用户看到的还是那句 WinError 2。"""
    env = dict(os.environ)
    pre = [p for p in (extra_path,) if p]
    node = find_node()
    if node:
        pre.append(os.path.dirname(node))
    if pre:
        env["PATH"] = os.pathsep.join(pre + [env.get("PATH", "")])
    return env


def _find_exe_under(root, rel, depth=2):
    """root 下（含至多 depth 层子目录）找相对路径 rel 的文件；纯查询，不建任何目录。"""
    if depth < 0 or not os.path.isdir(root):
        return None
    direct = os.path.join(root, rel)
    if os.path.isfile(direct):
        return direct
    try:
        subs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return None
    for d in subs:
        hit = _find_exe_under(os.path.join(root, d), rel, depth - 1)
        if hit:
            return hit
    return None


def component_bin_dirs():
    """软件自装的便携运行时该进 PATH 的目录（node 的目录里同时有 npm.cmd）。

    为什么不复用 `portable_root()` / `_exe_candidates()`：那两条都会 `os.makedirs`。
    启动早期（v1.0.619 的产物库闸门还没开）调一次就凭空造出
    `产物库/组件/node`，正是首启延时要避免的事——这里只 stat。
    """
    ws = workspace_root()
    out = []
    for name, t in _TARGETS.items():
        root = os.path.join(ws, "组件", name)
        exe = None
        mk = os.path.join(root, "_portable.json")
        if os.path.isfile(mk):
            try:
                with open(mk, encoding="utf-8") as f:
                    exe = (json.load(f) or {}).get("exe")
            except Exception:  # noqa: BLE001
                exe = None
        if not (exe and os.path.isfile(exe)):
            exe = _find_exe_under(root, t["exe"])
        if exe:
            d = os.path.dirname(exe)
            if d and d not in out:
                out.append(d)
    return out


def apply_component_path():
    """把便携运行时目录顶进**本进程**的 PATH，返回本次新增的目录（可能为空）。

    组件页一直写着「不写系统 PATH、只在本软件内可用」，但 v1.0.623 之前那句对 agent
    的命令行工具其实是假的：只有 dsh 的 npm 子进程走 `node_env()`，`pc_run` 起的
    cmd/powershell 用的还是继承来的环境——用户让 agent 跑个 node 脚本，它 `where node`
    找不到，只能满盘搜目录（就是这么绕到「找了一圈才发现是软件装的组件」）。
    这里补齐最后一环：**不动注册表、不改用户/系统环境变量**，只在本进程加，
    子进程自然继承，退出即还原。
    """
    cur = os.environ.get("PATH", "")
    parts = [p for p in cur.split(os.pathsep) if p]
    norm = {p.rstrip("\\").lower() for p in parts}
    added = [d for d in component_bin_dirs() if d.rstrip("\\").lower() not in norm]
    if added:
        os.environ["PATH"] = os.pathsep.join(added + parts)
        log_event("[portable] 便携运行时目录已注入本进程 PATH（不写系统环境变量）：%s"
                  % "；".join(added))
    return added


def find_system_tool(cmd):
    """PATH 上找 `cmd`，但**排除软件自己装的便携目录**——用来回答"用户机器上本来有没有装"。

    为什么不能直接用 `shutil.which("node")`：v1.0.623 起便携目录被注入本进程 PATH，
    which 会命中我们自己解压的那份，组件页那句「来源：软件内便携版 / 系统 PATH」就恒报
    "系统 PATH"，"缺不缺组件"的判断也会跟着失真。这里把便携目录从搜索路径里摘掉再找。
    """
    dirs = {d.rstrip("\\").lower() for d in component_bin_dirs()}
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep)
             if p and p.rstrip("\\").lower() not in dirs]
    if not parts:
        parts = [os.defpath]
    return shutil.which(cmd, path=os.pathsep.join(parts))


_RT_TTL = 30.0
_RT_CACHE = {"ts": 0.0, "ws": "", "data": {}}


def runtime_locations(refresh=False):
    """已就绪的运行时 → {名字: 可执行路径}，供提示词直接报名字和路径。

    为什么要专门告诉模型：组件是便携版，从来不在系统 PATH 上，模型不知道它存在，
    接到"跑个 node 脚本"就 `where node` 落空、再满盘 `dir` 搜（用户原话「找了
    一圈才找到软件安装的组件」）。v1.0.623 已把目录注进本进程 PATH（命令用名字就
    能命中），这里再把位置本身报出来，连搜都不用搜——**不需要往系统 PATH 里加东西**。

    带 30s 记忆：每条消息都 stat 一遍不值得，而安装完成时会主动 `refresh=True`
    作废（`_install_worker` 末尾），所以"刚装完立刻能用"不会被缓存拖住。
    """
    ws = workspace_root()
    now = time.time()
    if (not refresh and _RT_CACHE["ws"] == ws and now - _RT_CACHE["ts"] < _RT_TTL
            and _RT_CACHE["data"] is not None):
        return dict(_RT_CACHE["data"])
    data = {}
    node = find_node()
    if node:
        data["node"] = node
    npm = find_npm()
    if npm:
        data["npm"] = npm
    git = find_git()
    if git:
        data["git"] = git
    _RT_CACHE.update({"ts": now, "ws": ws, "data": dict(data)})
    return data


def runtime_hint_text():
    """给模型的一行运行时播报；一个都没定位到时返回空串（不塞废话进上下文）。"""
    locs = runtime_locations()
    if not locs:
        return ""
    return ("[本机运行时] " + "；".join("%s = %s" % (k, v) for k, v in locs.items())
            + "。这些是本机已就绪的 node/npm/git（含软件「运行组件」自装的便携版，"
              "目录已注入本进程 PATH）：命令行里直接用 `node x.js` / `npm i` / `git status` 即可，"
              "**不必再去搜文件位置**，也不要因为 `where node` 曾经落空就判断「没装 node」。")


# ---------------------------------------------------------------- 终端共享（写用户 PATH，默认关）
# 用户问的是「装完组件软件能第一时间找到吗？有办法就不用加 PATH」。软件内部确实不用加
# （v1.0.623 注进本进程 PATH，v1.0.624 把路径播报进对话上下文）。但「软件外的终端」要用
# node 只有一个办法：写用户环境变量。那是用户机器上的**持久副作用**，所以做成显式开关，
# 并守四条硬规矩：
#   ① 用 winreg，**不用 setx**——setx 在 1024 字符处截断，本机用户 PATH 实测已 1002 字符，
#      用 setx 等于把尾部几条直接抹掉；
#   ② 原样保持注册表值类型——本机是 REG_SZ，里面还有一条字面量
#      `%USERPROFILE%\AppData\...\WindowsApps`，改成 REG_EXPAND_SZ 会悄悄改变它的展开语义；
#   ③ 只增不排：不动用户已有条目的内容与顺序；撤销只按台账摘我们自己加的那几条；
#   ④ 拼出来超过上限就拒绝写入并说原因，绝不静默截断。
_ENV_KEY = "Environment"
_PATH_VALUE = "Path"
_PATH_LIMIT = 20000


def _user_path_raw():
    """读 HKCU\\Environment\\Path → (条目 list, 注册表类型)；没设过返回 ([], None)。"""
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY, 0, winreg.KEY_READ)
    except OSError:
        return [], None
    try:
        try:
            raw, typ = winreg.QueryValueEx(k, _PATH_VALUE)
        except FileNotFoundError:
            return [], None
    finally:
        winreg.CloseKey(k)
    return ([p for p in str(raw).split(";") if p], typ) if raw else ([], typ)


def _broadcast_env_change():
    """广播「环境变量变了」：只影响之后新开的终端，已经开着的不会自动更新（如实说明）。"""
    try:
        import ctypes
        # 传 c_wchar_p 实例而不是裸 str，也**不给共享函数指针设 argtypes**：
        # ctypes.windll 是进程级缓存，改它的 argtypes 会污染别处（本机吃过这类坑）。
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x1A, 0, ctypes.c_wchar_p("Environment"), 0x0002, 5000, None)
    except Exception as e:  # noqa: BLE001
        log_event("[portable] WM_SETTINGCHANGE 广播失败（PATH 已写入，不影响结果）：%s" % e)


def _write_user_path(parts, typ):
    """整值写回（保留原类型）。超过 _PATH_LIMIT 直接拒绝——截断是不可逆的数据丢失。"""
    import winreg
    val = ";".join(parts)
    if len(val) > _PATH_LIMIT:
        raise RuntimeError("用户 PATH 会超过 %d 字符，已拒绝写入（不做静默截断；"
                           "也不改用 setx——它在 1024 字符就截断，本机现在已 1002）" % _PATH_LIMIT)
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(k, _PATH_VALUE, 0, typ or winreg.REG_SZ, val)
    finally:
        winreg.CloseKey(k)
    _broadcast_env_change()
    return val


def _share_ledger_path():
    return os.path.join(workspace_root(), "系统", "path_share.json")


def _ledger_read():
    try:
        with open(_share_ledger_path(), encoding="utf-8") as f:
            d = json.load(f)
        return [str(x) for x in (d.get("added") or []) if x]
    except Exception:  # noqa: BLE001
        return []


def _ledger_write(items):
    """台账写失败不该让开关"看起来失败"——PATH 才是真相，台账只帮撤销。"""
    p = _share_ledger_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"added": list(items),
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
        os.replace(tmp, p)
        return True
    except Exception as e:  # noqa: BLE001
        log_event("[portable] 终端共享台账写入失败：%s" % e)
        return False


_UP_TTL = 10.0
_UP_CACHE = {"ts": 0.0, "val": None}


def user_path_shared():
    """当前是否已共享（**以注册表为准**，不信配置布尔）：我们的目录都在用户 PATH 里才算开。

    用户手工删掉那条，界面就该如实回到"未开启"，而不是跟着配置继续显示已开启。
    组件页每 3 秒轮询，每次都开注册表不值——加 10s 记忆，而 `share_user_path()`
    写完主动作废（否则点完撤销，按钮还会"已开启"十几秒，用户以为没生效）。
    """
    now = time.time()
    if _UP_CACHE["val"] is not None and now - _UP_CACHE["ts"] < _UP_TTL:
        return _UP_CACHE["val"]
    ours = component_bin_dirs()
    val = False
    if ours:
        try:
            parts, _typ = _user_path_raw()
        except Exception as e:  # noqa: BLE001
            log_event("[portable] 读用户 PATH 失败（按未共享处理）：%s" % e)
            parts = []
        low = {p.rstrip("\\").lower() for p in parts}
        val = all(d.rstrip("\\").lower() in low for d in ours)
    _UP_CACHE.update({"ts": now, "val": val})
    return val


def share_user_path(on=True):
    """把本软件自装的便携运行时目录写进 / 摘出**用户** PATH（HKCU，不需要管理员）。

    只在用户显式点「运行组件 → 终端共享」时调用；软件内部使用从来不依赖它。
    """
    ours = component_bin_dirs()
    try:
        parts, typ = _user_path_raw()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "读不到用户 PATH（注册表访问失败）：%s" % e}
    low = {p.rstrip("\\").lower() for p in parts}
    if on:
        if not ours:
            return {"ok": False,
                    "error": "没检测到本软件自装的可执行目录——先在「运行组件」里一键安装 node/git"}
        add = [d for d in ours if d.rstrip("\\").lower() not in low]
        if not add:
            return {"ok": True, "changed": False, "added": [],
                    "msg": "已经在用户 PATH 里了，没做任何改动"}
        try:
            _write_user_path(parts + add, typ)      # 只追加在尾部，原有条目一字不动
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        _UP_CACHE["val"] = None                      # 状态刚变，别让 10s 记忆把按钮卡在旧文案
        _ledger_write(sorted(set(_ledger_read()) | {a.rstrip("\\") for a in add}))
        return {"ok": True, "changed": True, "added": add,
                "msg": "已加入用户 PATH：%s。新开一个终端就能用 node/npm/git"
                       "（已经开着的终端不会自动更新，重开一个）" % "；".join(add)}
    led = _ledger_read()
    drop = {p.rstrip("\\").lower() for p in (led + ours)}
    removed = [p for p in parts if p.rstrip("\\").lower() in drop]
    if not removed:
        return {"ok": True, "changed": False, "removed": [], "msg": "本来就没加过，无需撤销"}
    try:
        _write_user_path([p for p in parts if p.rstrip("\\").lower() not in drop], typ)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    _UP_CACHE["val"] = None
    _ledger_write([])
    return {"ok": True, "changed": True, "removed": removed,
            "msg": "已从用户 PATH 撤掉 %d 条：%s（你原有的条目一条没动）"
                   % (len(removed), "；".join(removed))}


# ---------------------------------------------------------------- 安装
def install_status(name):
    with _STATE_LOCK:
        return dict(_STATE.get(name) or {"running": False, "phase": "", "pct": 0,
                                         "ok": None, "error": "", "version": ""})


def _set(name, **kw):
    with _STATE_LOCK:
        st = _STATE.setdefault(name, {"running": False, "phase": "", "pct": 0,
                                      "ok": None, "error": "", "version": ""})
        st.update(kw)


def _pick_url(name):
    """从 npmmirror 的 JSON 目录里挑一个 zip（node 取该 LTS 线最新版；git 取 MinGit）。"""
    t = _TARGETS[name]
    from core_utils import _lazy_requests
    r = _lazy_requests.get(t["index"], timeout=(8, 30), headers={"User-Agent": "wecom-companion"})
    if r.status_code != 200:
        raise RuntimeError(f"索引 {t['index']} -> HTTP {r.status_code}")
    rows = r.json() if hasattr(r, "json") else json.loads(r.text)
    hits = []
    for x in rows if isinstance(rows, list) else []:
        fn = (x or {}).get("name") or ""
        url = (x or {}).get("url") or ""
        if url and t["file_re"].match(fn):
            hits.append((fn, url))
    if not hits:
        raise RuntimeError(f"{t['index']} 里没有匹配的 zip（{t['file_re'].pattern}）")
    hits.sort(key=lambda h: _verkey(h[0]), reverse=True)
    return hits[0][1], hits[0][0]


def _verkey(fn):
    """文件名里的版本号排序键（node-v22.20.0 > node-v22.9.0；补齐 4 段）。"""
    nums = [int(x) for x in re.findall(r"\d+", str(fn))]
    return tuple((nums + [0, 0, 0, 0])[:4])


def _download(url, dest, name):
    """流式下载（镜像 → 官方 → 镜像 → 官方交替），进度写进安装状态。"""
    from core_utils import _lazy_requests
    t = _TARGETS[name]
    mirror = url
    official = t["official"](url)
    for attempt, u in enumerate([mirror, official, mirror, official], start=1):
        try:
            r = _lazy_requests.get(u, stream=True, timeout=(8, 120))
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        _set(name, phase=f"下载 {os.path.basename(u)}", pct=int(done * 70 / total))
            return u
        except Exception as e:  # noqa: BLE001
            log_event(f"[portable] {name} 第 {attempt} 次下载失败（{u}）：{type(e).__name__}: {e}")
            if attempt == 4:
                raise RuntimeError(f"{name} 下载失败（镜像与官方各试两次）：{e}")
            time.sleep(2)
    raise RuntimeError("unreachable")


def _safe_extract(zf, dest):
    """解压并挡 zip-slip：MinGit/node 的包本身可信，但解到用户目录的任何东西都要先归一路径。"""
    base = os.path.realpath(dest)
    for member in zf.infolist():
        target = os.path.realpath(os.path.join(base, member.filename))
        if target != base and not target.startswith(base + os.sep):
            raise RuntimeError(f"压缩包里有越界路径，已中止解压：{member.filename}")
    zf.extractall(dest)


def _install_worker(name):
    t = _TARGETS.get(name)
    if not t:
        _set(name, running=False, ok=False, error=f"未知运行时：{name}")
        return
    try:
        url, fn = _pick_url(name)
        _set(name, phase=f"解析到 {fn}", pct=2)
        root = portable_root(name)
        tmp = os.path.join(root, fn)
        used = _download(url, tmp, name)
        _set(name, phase="解压", pct=72)
        stage = os.path.join(root, "_staging")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage, exist_ok=True)
        with zipfile.ZipFile(tmp) as zf:
            _safe_extract(zf, stage)
        os.remove(tmp)
        # v1.0.632（B5）：**先验后换**。旧顺序是 rmtree(current) → rename → 再找 exe，
        # 于是"镜像给的包结构变了/解压被杀软打断"这类失败会把用户原本能用的
        # node/MinGit 一起带走：旧的已删、新的又没有可执行文件，组件从"可降级"变成"彻底没了"。
        # 现在校验只作用于 staging，验不过就直接失败，current/ 保持上一个可用版本。
        final = os.path.join(root, "current")
        cands = _exe_candidates(name, t["exe"], base=stage)
        if not cands:
            raise RuntimeError(f"解压后没找到 {t['exe']}（包结构变了？）：{stage}")
        # 原子提交：staging → 正式目录（半解压的 node.exe 比没有更糟）
        shutil.rmtree(final, ignore_errors=True)
        os.rename(stage, final)
        # 提交后的真实路径由 staging 相对位置推出：不能再搜一遍——搜索会把
        # 标记里记着的**上一个版本**的路径排在前面，装完写进标记的就是旧位置。
        exe = os.path.join(final, os.path.relpath(cands[0], stage))
        m = re.search(r"\d+\.\d+(?:\.\d+)?", fn)       # node-v22.20.0-… / MinGit-2.56.0-…
        ver = m.group(0) if m else fn
        with open(_marker(name), "w", encoding="utf-8") as f:
            json.dump({"name": name, "version": ver, "file": fn, "url": used, "exe": exe,
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
        _set(name, running=False, phase="完成", pct=100, ok=True, version=ver, error="")
        # 装完就注入本进程 PATH：否则"已安装"和"agent 能直接用"之间还隔一次重启
        apply_component_path()
        # 30s 的运行时缓存也要作废，不然刚装好的 node 在提示词里还要"隐身"半分钟
        runtime_locations(refresh=True)
        from core.workspace_io import invalidate_size_cache
        invalidate_size_cache()          # 一次多出一两个 GB，别让产物库页 10 分钟还报旧体积
        log_event(f"[portable] {t['name']} 便携版安装完成：{exe}")
    except Exception as e:  # noqa: BLE001
        _set(name, running=False, phase="失败", ok=False, error=str(e)[:200])
        # 失败也要把 staging 收走：半解压的运行时既不能用的、又会一直占着磁盘
        # （语音组件就留过 258.7MB 的 `asr_staging`，只在"下次安装开头"才清）。
        try:
            shutil.rmtree(os.path.join(portable_root(name), "_staging"), ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        import traceback
        log_event(f"[portable] {name} 安装失败：{e}\n{traceback.format_exc().rstrip()}")


def install_async(name):
    """软件内安装便携运行时（后台线程，进度走 install_status；重复调用不重开）。"""
    t = _TARGETS.get(name)
    if not t:
        return {"ok": False, "error": f"未知运行时：{name}"}
    if not _IS_WIN:
        return {"ok": False, "error": "便携运行时自动安装目前只支持 Windows"}
    st = install_status(name)
    if st.get("running"):
        return {"ok": True, "already_running": True, "msg": f"{t['name']} 安装已在进行中"}
    if portable_exe(name):
        return {"ok": True, "already_installed": True,
                "msg": f"{t['name']} 已就绪（{installed_meta(name).get('version') or '便携版'}）"}
    _set(name, running=True, phase="准备", pct=0, ok=None, error="", version="")
    threading.Thread(target=_install_worker, args=(name,), daemon=True,
                     name="portable-" + name).start()
    return {"ok": True, "msg": f"开始安装 {t['name']} 便携版（约 {t['size_mb']}MB，国内镜像优先）"}

"""agent_tools.dsh_bridge — deepseek-harness（dsh）电脑接管通道（v1.0.298）。

定位：内置 pc_* 直连工具搞不定的任务（多步骤/复杂/需要看结果再调整的电脑操作），
委托给本机 deepseek-harness headless agent 自主完成。dsh 自带终端/文件/工作区
工具与自己的 sandbox+审批安全策略，跑完打印最终答案并退出（不开端口、不留进程）。

三个入口（均为 agent 工具后端）：
  dsh_check(cfg)     就绪探测：node 版本(需>=22.19 或 24+) / dsh 是否安装 / 模型凭据，
                     不满足时给可执行的修复提示
  dsh_prepare(cfg)   npm i -g @deepseek-ai/dsh（官方源失败自动回退 npmmirror 镜像）
  dsh_run(cfg,task)  一次性委托：dsh --profile headless "<task>"，超时强杀进程树，
                     全程转写存档 产物库/产出/dsh/

实现要点：
  - Windows npm 全局 bin 是 .cmd shim：直接 Popen 会 WinError 193，
    需经 cmd /c 启动且对含空格参数加引号（_build_cmd 处理）
  - 长任务用 Popen+进程组（CREATE_NEW_PROCESS_GROUP），超时 taskkill /F /T 杀整树
    （dsh 内部会再派生 shell 子进程，只 kill 主进程会留孤儿）
  - stdout=最终答案 / stderr=推理流；模型端点走 dsh 官方机制：
    环境变量 DEEPSEEK_BASE_URL + DEEPSEEK_API_KEY（config.json 的 dsh 段映射注入）
  - 非 win32 全部返回结构化错误（源码托管跨平台跑测试不崩）
"""

import os
import re
import sys
import time
import shutil
import subprocess

from core_utils import log_event
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）

# ---------- 常量 ----------
IS_WIN = (sys.platform == "win32")

DSH_PKG = "@deepseek-ai/dsh"
NODE_MIN = (22, 19, 0)                    # dsh engines: ^22.19.0 || >=24.0.0
MIRROR = "https://registry.npmmirror.com"

RUN_TIMEOUT_DEFAULT = 900                 # pc_takeover 默认超时（秒）
RUN_TIMEOUT_MIN = 60
RUN_TIMEOUT_MAX = 1800
PREPARE_TIMEOUT = 900                     # npm 全局安装上限（首次 1-10 分钟）

ANSWER_LIMIT = 20000                      # 最终答案截断
REASONING_LIMIT = 8000                    # 推理流尾部截断
DASH_DIR = os.path.join("产出", "dsh")


def _not_win():
    return {"ok": False, "error": "dsh 接管通道仅支持 Windows（当前 " + sys.platform + "）"}


def _ts():
    return time.strftime("%Y%m%d_%H%M%S")


def _dec(b):
    """node 输出默认 utf-8；兜底 gbk（Windows 控制台编码）再兜底 replace。"""
    if not b:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return b.decode("utf-8", errors="replace")


def _run_captured(args, timeout, env=None):
    """同步跑一条命令，返回 {exit_code, stdout, stderr}；不抛异常。
    v1.0.299：CREATE_NO_WINDOW——npm i -g / node --version 不再闪可见控制台黑框。
    v1.0.612：可选 env —— 便携版 node 不在系统 PATH 里，npm.cmd 内部还要再起 node，
    必须把 node 目录顶到子进程的 PATH 前面，否则 npm 自己就 WinError 2。"""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, env=env,
                           creationflags=0x08000000 if IS_WIN else 0)
        return {"exit_code": r.returncode,
                "stdout": _dec(r.stdout), "stderr": _dec(r.stderr)}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": f"命令超时（{timeout}s）"}
    except Exception as e:  # noqa: BLE001
        return {"exit_code": -1, "stdout": "", "stderr": f"启动失败：{e}"}


# ============================================================
# 环境探测
# ============================================================
def _node_info():
    """探测 node 版本是否满足 dsh 要求（^22.19 或 >=24）。

    v1.0.612：不再只认 PATH 里的 node —— 软件自装的便携版（`组件/node/…/node.exe`）
    同样是合法运行时。原先 `subprocess.run(["node", ...])` 在 PATH 无 node 时抛
    `[WinError 2] 系统找不到指定的文件`，一键安装就在 dsh 这一步死掉并把锅甩回用户。"""
    try:
        from core import portable as _pb
        exe = _pb.find_node()
    except Exception:  # noqa: BLE001
        exe = shutil.which("node")
    if not exe:
        return {"ok": False, "version": None, "detail": "未检测到 node（系统 PATH 与软件便携版都没有）"}
    try:
        r = _run_captured([exe, "--version"], timeout=15)
        m = re.search(r"v(\d+)\.(\d+)\.(\d+)", r["stdout"])
        if m:
            v = tuple(int(x) for x in m.groups())
            vs = "v" + ".".join(map(str, v))
            ok = v >= NODE_MIN or v[0] >= 24
            return {"ok": ok, "version": vs, "path": exe,
                    "detail": "" if ok else f"{vs} 过低，需 Node 22.19+ 或 24+"}
        return {"ok": False, "version": None, "path": exe,
                "detail": "node --version 未返回有效版本：" + r["stderr"][:100]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "version": None, "path": exe, "detail": f"无法执行 node：{e}"}


def _node_dir_on_path():
    """node 所在目录（便携版或系统版），给子进程 PATH 前置用；没有 node 返回 None。"""
    try:
        from core import portable
        n = portable.find_node()
        return os.path.dirname(n) if n else None
    except Exception:  # noqa: BLE001
        return None


def _which_dsh():
    """解析 dsh 可执行路径。Windows npm 全局 bin 是 dsh.cmd shim（PATH 里同时有
    dsh.cmd/dsh/dsh.ps1 三份），shutil.which 按 PATHEXT 顺序解析；逐个尝试更稳。

    v1.0.612：还要找**软件自装便携 node 的目录**——npm -g 装进的就是那个前缀，
    而它从不写进系统 PATH，所以只查 PATH 会「装成功却找不到 dsh」的假失败。"""
    candidates = (["dsh.cmd", "dsh", "dsh.ps1"] if IS_WIN else ["dsh"])
    for c in candidates:
        try:
            p = shutil.which(c)
        except Exception:  # noqa: BLE001
            p = None
        if p:
            return p
    try:
        from core import portable as _pb
        node = _pb.find_node()
    except Exception:  # noqa: BLE001
        node = None
    if node:
        d = os.path.dirname(node)
        for c in candidates:
            p = os.path.join(d, c)
            if os.path.isfile(p):
                return p
    return None


def _dsh_cfg(cfg):
    d = (cfg or {}).get("dsh")
    return d if isinstance(d, dict) else {}


def dsh_check(cfg=None):
    """就绪探测：node / dsh 安装 / 模型凭据，返回结构化状态 + 修复提示。"""
    if not IS_WIN:
        return _not_win()
    node = _node_info()
    bin_path = _which_dsh()
    d = _dsh_cfg(cfg)
    base_url = str(d.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL") or "").strip()
    api_key = str(d.get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()

    hints = []
    if not node["ok"]:
        hints.append(f"Node 环境不满足：{node['detail'] or '未检测到 node'}。"
                     "需安装 Node.js 22.19+（或 24+）并在 PATH 中可用。")
    if not bin_path:
        hints.append("dsh 未安装：调用 dsh_prepare 安装（等价 npm i -g "
                     f"{DSH_PKG}，首次 1-10 分钟），或让用户手动执行。")
    if not base_url and not api_key:
        hints.append("未检测到模型凭据：在「设置 → 个人信息与凭据」填 dsh 端点与访问凭据"
                     "（如本地 LM Studio http://localhost:1234/v1 + 任意 key），"
                     "或设置系统环境变量 DEEPSEEK_API_KEY（DeepSeek 官方）。"
                     "缺凭据时 dsh 会用官方默认模型，必须有效的官方 key。")
    ready = node["ok"] and bool(bin_path) and bool(base_url or api_key)
    return {
        "ok": ready,
        "ready": ready,
        "node": node,
        "dsh_bin": bin_path,
        "base_url": base_url or None,
        "has_api_key": bool(api_key),
        "package": DSH_PKG,
        "hints": hints,
    }


# ============================================================
# 安装
# ============================================================
def dsh_prepare(cfg=None, timeout=None):
    """npm i -g @deepseek-ai/dsh；**国内镜像（npmmirror）优先**，官方源只作兜底。

    v1.0.608 顺序调整：原来先撞 registry.npmjs.org、失败才回退 npmmirror——本机实测
    官方源常常是"连得上但极慢"，要等它把 timeout 预算耗掉才轮到国内镜像，用户看到的就是
    一键安装卡住。镜像放前面后，官方源只在镜像真失败时才试一次。"""
    if not IS_WIN:
        return _not_win()
    node = _node_info()
    if not node["ok"]:
        return {"ok": False, "error": f"Node 环境不满足，无法安装：{node['detail']}",
                "node": node}
    existing = _which_dsh()
    if existing:
        return {"ok": True, "already_installed": True, "dsh_bin": existing,
                "version_note": "dsh 已在 PATH 中，无需安装"}

    timeout = int(timeout or PREPARE_TIMEOUT)
    last = None
    # v1.0.612：npm 用**绝对路径**跑（便携版不在系统 PATH 上），并给子进程补 PATH，
    # 否则 npm.cmd 自己找不到 node —— 那正是截图里那句「WinError 2 系统找不到指定的文件」。
    npm = None
    try:
        from core import portable as _pb
        npm = _pb.find_npm()
    except Exception:  # noqa: BLE001
        npm = None
    if not npm:
        npm = "npm"
    npm_env = None
    try:
        from core import portable as _pb
        npm_env = _pb.node_env()
    except Exception:  # noqa: BLE001
        pass
    # 第 1 次走 npmmirror 国内镜像，第 2 次才回落官方源（v1.0.608）
    for attempt, extra in enumerate((["--registry", MIRROR], []), start=1):
        args = [npm, "install", "-g", DSH_PKG] + extra
        log_event(f"[dsh] 安装尝试 #{attempt}（{'npmmirror 国内镜像' if extra else 'npm 官方源'}）: {' '.join(args)}")
        r = _run_captured(args, timeout=timeout, env=npm_env)
        last = r
        if r["exit_code"] == 0:
            bin_path = _which_dsh()
            return {"ok": bool(bin_path), "installed": True, "attempt": attempt,
                    "used_mirror": attempt == 1, "dsh_bin": bin_path,
                    "stderr_tail": r["stderr"][-500:],
                    "note": "安装完成，pc_takeover 可用" if bin_path
                    else "npm 返回成功但未在 PATH 找到 dsh，可能 PATH 未刷新，重启软件再试"}
        log_event(f"[dsh] 安装尝试 #{attempt} 失败 exit={r['exit_code']}：{r['stderr'][-300:]}")
    return {"ok": False,
            "error": "npm 全局安装失败（npmmirror 国内镜像 + 官方源都试过）",
            "stderr_tail": (last or {}).get("stderr", "")[-1500:],
            "hint": f"可让用户手动执行：npm i -g {DSH_PKG} --registry={MIRROR}"}


# ============================================================
# 一次性委托（headless）
# ============================================================
def _resolve_workdir(cwd_param, cfg_dsh):
    """归一化工作目录：参数 > config dsh.cwd > 产物库根。返回 (path, err)。"""
    want = str(cwd_param or cfg_dsh.get("cwd") or "").strip()
    if not want:
        return paths.workspace_root(), None
    p = want.replace("/", "\\").strip('"').strip("'")
    if p.startswith("~"):
        p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser("~"), p)
    p = os.path.normpath(p)
    if not os.path.isdir(p):
        return None, f"工作目录不存在：{p}"
    return p, None


def _build_cmd(bin_path, rest):
    """拼最终 argv，返回 (cmd, via)。

    npm 全局 bin 是 .cmd shim。cmd 的引号语义（/c 首尾引号剥离、4 引号规则）会把
    「含空格路径 + 含空格任务」拆碎——实证两次翻车（""双重引号"" 与路径空格截断）。
    所以首选：解析 shim 拿到真正的 JS 入口，直接 [node, entry.js, ...args]，
    不经过 cmd，引号由 list2cmdline 一次处理，任务文本原样进 node argv。
    解析失败（shim 格式变化/无 node）才回退 cmd /c（仅无空格场景可用）。
    """
    if IS_WIN and bin_path.lower().endswith((".cmd", ".bat")):
        entry = _resolve_shim_entry(bin_path)
        if entry:
            node = shutil.which("node") or "node"
            return [node, entry] + rest, "node-direct"
    return [bin_path] + rest, "direct"


def _resolve_shim_entry(bin_path):
    """从 npm .cmd shim 解析真实 JS 入口绝对路径。

    npm shim 内嵌一行 `FOR %%F IN ("<绝对路径>.js") DO ...`（npm 安装时把绝对路径
    写死进 shim）。取最后一个存在的绝对 .js 路径；找不到返回 None。"""
    if not IS_WIN:
        return None
    try:
        with open(bin_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:  # noqa: BLE001
        return None
    # 引号包裹的 .js 路径，排除含 % 的批处理变量行
    for m in reversed(re.findall(r'"([A-Za-z]:[^"\n%]+?\.js)"', text)):
        if os.path.exists(m):
            return m
    return None


def _kill_tree(p):
    """杀 dsh 进程整棵树（它会派生 shell 子进程，只杀主进程留孤儿）。"""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=15,
                           creationflags=0x08000000)
        else:
            import signal
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    try:
        p.kill()
    except Exception:  # noqa: BLE001
        pass


def _save_transcript(task, answer, reasoning, exit_code, cwd, elapsed, timed_out=False):
    """全程转写落盘 产物库/产出/dsh/，用户可复查 dsh 到底干了什么。
    v1.0.361（O6）：支持 timed_out 标记（超时被杀路径也落转写）。"""
    try:
        # 不走 ws_sub（白名单只认 5 个扁平子目录，"产出/dsh" 会被回退到 产出），
        # 同 pc_screenshot 模式：直接拼 workspace_root + makedirs
        d = os.path.join(paths.workspace_root(), DASH_DIR)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{_ts()}_dsh.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# dsh 接管记录\n\n"
                    f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"- 工作目录：{cwd}\n"
                    f"- 耗时：{elapsed}s\n"
                    f"- 退出码：{exit_code if exit_code is not None else '（超时被杀）'}\n"
                    + ("- 状态：⚠ 超时被强杀（可能已执行部分操作）\n" if timed_out else "")
                    + "\n"
                    f"## 任务\n{task}\n\n"
                    f"## 最终答案\n{answer or '（空）'}\n\n"
                    f"## 过程推理（stderr 全文）\n{reasoning or '（空）'}\n")
        return {"path": path}
    except Exception as e:  # noqa: BLE001
        log_event(f"[dsh] 转写存档失败：{e}")
        return {}


def dsh_run(cfg, task, cwd=None, timeout=None):
    """委托 dsh headless agent 跑一个任务，返回最终答案 + 过程记录路径。"""
    if not IS_WIN:
        return _not_win()
    task = str(task or "").strip()
    if not task:
        return {"ok": False, "error": "pc_takeover 需要非空 task：把要完成的具体任务描述写清楚"
                             "（含路径、期望结果、约束）"}
    d = _dsh_cfg(cfg)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = RUN_TIMEOUT_DEFAULT
    timeout = max(RUN_TIMEOUT_MIN, min(timeout, RUN_TIMEOUT_MAX))

    bin_path = _which_dsh()
    if not bin_path:
        return {"ok": False, "error": "dsh 未安装，先调用 dsh_prepare（npm i -g "
                             f"{DSH_PKG}）装好再委托任务",
                "hint": "安装首次需 1-10 分钟（视网络）"}

    workdir, err = _resolve_workdir(cwd, d)
    if err:
        return {"ok": False, "error": err}

    env = dict(os.environ)
    # v1.0.612：便携版 node 不在系统 PATH 里，而 npm 装的 dsh.cmd 内部要靠 node 起进程
    _np = _node_dir_on_path()
    if _np:
        env["PATH"] = os.pathsep.join([_np, env.get("PATH", "")])
    base_url = str(d.get("base_url") or "").strip()
    api_key = str(d.get("api_key") or "").strip()
    if base_url:
        env["DEEPSEEK_BASE_URL"] = base_url
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key

    rest = ["--profile", "headless", task]
    extra = str(d.get("extra_args") or "").strip()
    if extra:
        rest += extra.split()
    cmd, via = _build_cmd(bin_path, rest)
    if via == "direct" and IS_WIN and str(bin_path).lower().endswith((".cmd", ".bat")):
        log_event("[dsh] 警告：shim 入口解析失败，回退 cmd /c——含空格的任务可能被 cmd 拆碎")

    log_event(f"[dsh] 接管任务开始 via={via} cwd={workdir} timeout={timeout}s task={task[:120]!r}")
    t0 = time.time()
    # v1.0.299：CREATE_NEW_PROCESS_GROUP（超时杀整树）| CREATE_NO_WINDOW（不弹黑框）
    creationflags = (0x00000200 | 0x08000000) if IS_WIN else 0
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=workdir, env=env, creationflags=creationflags)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"启动 dsh 失败：{e}"}

    try:
        out_b, err_b = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            out_b, err_b = p.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out_b, err_b = b"", b""
        elapsed = round(time.time() - t0, 1)
        log_event(f"[dsh] 接管任务超时被杀 elapsed={elapsed}s")
        # v1.0.361 审查修复（O6）：超时路径也落转写——dsh 可能已执行过操作（含文件
        # 改动），无留痕违背"全程转写存档可复查"的设计承诺。
        _save_transcript(task, _dec(out_b), _dec(err_b), None, workdir, elapsed, timed_out=True)
        return {"ok": False, "timed_out": True,
                "error": f"dsh 运行超时（{timeout}s），进程组已强杀。"
                         "任务可能没完成：拆小任务重试，或加大 timeout（上限 1800s）",
                "elapsed_sec": elapsed}

    elapsed = round(time.time() - t0, 1)
    answer = _dec(out_b)
    reasoning = _dec(err_b)
    rec = _save_transcript(task, answer, reasoning, p.returncode, workdir, elapsed)
    log_event(f"[dsh] 接管任务结束 exit={p.returncode} elapsed={elapsed}s "
              f"answer={len(answer)}字 reasoning={len(reasoning)}字")

    if p.returncode == 0:
        return {
            "ok": True,
            "answer": answer[:ANSWER_LIMIT] + (f"\n...[已截断：共 {len(answer)} 字符]"
                                               if len(answer) > ANSWER_LIMIT else ""),
            "reasoning_tail": reasoning[-REASONING_LIMIT:],
            "elapsed_sec": elapsed,
            "cwd": workdir,
            "transcript": rec.get("path"),
            "note": "以上 answer 是 dsh agent 的最终结论；完整过程见 transcript 文件",
        }
    return {
        "ok": False,
        "exit_code": p.returncode,
        "error": f"dsh 退出码 {p.returncode}：" + (reasoning[-800:] or "（无错误输出）"),
        "answer_tail": answer[-1000:],
        "elapsed_sec": elapsed,
        "cwd": workdir,
        "transcript": rec.get("path"),
        "hint": "常见原因：模型端点/key 未配置或不可达（先 dsh_status 查就绪）、"
                "任务需要本机缺少的环境（依赖/工具）。详见 transcript 过程记录",
    }

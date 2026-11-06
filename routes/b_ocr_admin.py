# -*- coding: utf-8 -*-
"""routes.b_ocr_admin — OCR 组件管理 API（状态/安装/更新）
"""
# -*- coding: utf-8 -*-
import json
import os
import sys
import time

from flask import Blueprint, request, jsonify

from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）
from core.config_store import load_config
from core_utils import log_event   # v1.0.625：终端共享开关要留痕（其它 routes 同一惯例）

from version import get as _version_get  # v1.0.510（P2-13）：版本兜底块收敛到 version.get
VERSION = _version_get()


bp = Blueprint("mgmt_ocr_admin", __name__)

# ============================================================
# OCR 组件在线安装（v1.0.307）：设置页状态/手动安装；
# 自动触发在 core_utils.ocr_ready_or_install（碰到 OCR 事务时）。
# ============================================================

# ---------------- v1.0.473 组件体检中心：外部运行组件统一状态汇总 ----------------
def _which_version(cmd_args, exe=None):
    """探测可执行文件并取版本输出（超时 4s）；返回 (path_or_None, version_or_None)。"""
    import shutil as _sh
    import subprocess as _sp
    p = _sh.which(exe or cmd_args[0])
    if not p:
        return None, None
    ver = None
    try:
        r = _sp.run([p] + cmd_args[1:], capture_output=True, timeout=4,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
        ver = out.splitlines()[0][:60] if out else "（无版本输出）"
    except Exception:  # noqa: BLE001
        ver = "（版本获取失败）"
    return p, ver


def _locate_build_python():
    """构建用解释器 + PyInstaller 定位（v1.0.473 统一口径：组件体检/一键升级/一键打包）。
    探测链：环境变量 WECOM_BUILD_PYTHON → config.build.python → 源码跑当前解释器 →
    WorkBuddy 管理环境 glob（取最新）→ PATH python。
    v1.0.473 修复（用户实测截图 16:40）：frozen exe 里 sys.executable 是软件自己
    （不是 python），原探测把 WeComCompanion.exe 当解释器传给 -m pip 必然失败。
    返回 (py_exe or "", pyinstaller_path or "")。"""
    import shutil as _sh
    import glob as _glob
    py_exe = os.environ.get("WECOM_BUILD_PYTHON") or ""
    if not py_exe or not os.path.isfile(py_exe):
        try:
            from core.config_store import load_config
            py_exe = str(((load_config() or {}).get("build") or {}).get("python") or "")
        except Exception:  # noqa: BLE001
            py_exe = ""
    if not py_exe or not os.path.isfile(py_exe):
        if not getattr(sys, "frozen", False):
            # 源码跑：当前解释器即构建环境（envs/wecom 的 python.exe）
            _d = os.path.dirname(sys.executable)
            for _c in (os.path.join(_d, "python.exe"),
                       os.path.join(os.path.dirname(_d), "Scripts", "python.exe"),
                       sys.executable):
                if os.path.isfile(_c):
                    py_exe = _c
                    break
        else:
            # frozen：WorkBuddy 管理环境取最新 mtime 的 venv（本机打包一直用它）
            _envs = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "python", "envs")
            cands = _glob.glob(os.path.join(_envs, "*", "Scripts", "python.exe"))
            if cands:
                py_exe = max(cands, key=os.path.getmtime)
    if not py_exe or not os.path.isfile(py_exe):
        py_exe = _sh.which("python") or ""
    if not py_exe or not os.path.isfile(py_exe):
        return "", ""
    # PyInstaller：与解释器同目录（Scripts 布局）或 venv 根的 Scripts/ 下；都没有则空（用 -m 验证）
    _pd = os.path.dirname(py_exe)
    for _c in (os.path.join(_pd, "pyinstaller.exe"),
               os.path.join(os.path.dirname(_pd), "Scripts", "pyinstaller.exe")):
        if os.path.isfile(_c):
            return py_exe, _c
    return py_exe, ""


def _source_root_dir():
    """源码根目录：frozen=exe 的**上级**（v1.0.473 口径：开发机上 exe 在 <项目根>/dist/，
    上级才是源码根；用户那边归位后是 Desktop\\企微伴侣\\*.exe → 上级 = Desktop）；
    源码跑=routes/ 的上级。单独拆出来是为了能被测试改道（整块写死在函数里时，
    锁只能 patch 上层函数，就测不到"到底有没有 requirements-dev.txt"这一步）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_source_root():
    """「打包构建环境」所要求的源码根目录，以及那里有没有 requirements-dev.txt。

    返回 (src_dir, has_manifest)。

    v1.0.619（用户在另一台机器实测报障）：那里没有依赖清单时，以前被当成"组件坏了"——
    体检报 missing、一键安装把它列进待办、点下去回一句带用户家目录路径的
    「未找到依赖清单：…requirements-dev.txt（软件需在源码目录结构内运行）」。
    正式包本来就不带源码，软件内「一键打包」在这类机器上是**不适用**，不是失败。
    """
    src = _source_root_dir()
    return src, os.path.isfile(os.path.join(src, "requirements-dev.txt"))


# v1.0.648：_probe_component 原本是 230 行的 if/elif 链，按 key 拆成下面七个
# _probe_*。契约：入参是要填的组件字典 c（已含 key/status/version/hint/actions），
# 就地填充，异常向上抛（由 _probe_component 统一转成 status=unknown）。
# 逐段搬而不是重写，是为了让每个组件的判据（版本来源/降级口径/安装动作）原样保留。


def _probe_ocr(c):
    from core_utils import ocr_install_status, _OCR_WHEELS, ocr_check_updates
    st = ocr_install_status()
    from core_utils import ocr_size_hint
    _sz = ocr_size_hint()
    c.update(name="OCR 组件", desc="课件采集 / 扫描件识别（%s，国内镜像按需下载）" % _sz)
    # 体积口径只留 core/ocr.py 一处：前端「一键安装」确认文案直接取这个字段。
    # （此前同一组数字在 js 里又抄了一遍，改一处漏一处，才会长期挂着过期的旧数。）
    c["size_hint"] = _sz
    if st.get("state") == "running":
        c["status"] = "installing"; c["hint"] = st.get("msg") or "正在下载安装…"
    elif st.get("installed"):
        # 版本徽章直接摆**磁盘真值**（v1.0.609）：这六个包版本必须成套匹配，
        # 用户排查兼容问题时最先要看的就是这个，而不是"推荐组合"应该是什么。
        from core_utils import ocr_versions
        _v = ocr_versions()
        # 标签短化：前端 .comp-ver 只留 60 字，写全名会被截成半截版本号。
        _shown = " · ".join(f"{lbl} {_v[pkg]}" for pkg, lbl in
                            (("rapidocr-onnxruntime", "rapidocr"),
                             ("onnxruntime", "onnxruntime"), ("numpy", "numpy"))
                            if _v.get(pkg))
        if _shown:
            c["version"] = _shown
        c["hint"] = "推荐组合：" + "、".join(f"{k} {v}" for k, v in _OCR_WHEELS.items())
        # 已装但落后于镜像最新版 → 给一键升级动作（走已验证的钉定组合还是 latest 由用户选）
        try:
            # 界面路径只读缓存（v1.0.611）：全量扫描实测 34.6 秒，挂在这里会让
            # 「运行组件」进页面白等半分钟。开机后台预热；没预热好就暂不报可升级，
            # 用户要准信点「检查更新」（那条走 force，立刻重算）。
            upd = ocr_check_updates(cache_only=True)
            _out = [p for p in upd.get("packages", []) if p.get("update_available")]
            if upd.get("pending"):
                # 没查过就说实话"待探测"，不能靠"没报可升级"暗示用户已是最新。
                c["hint"] += "（更新状态待后台探测，点「检查更新」立即确认）"
            if _out:
                c["status"] = "outdated"
                c["hint"] = ("可升级：" + "、".join(f"{p['pkg']} {p['installed']}→{p['latest']}" for p in _out)
                             + "。最新组合未经验证，兼容问题用「恢复推荐版本」回退")
                c["actions"] = [
                    {"type": "api", "label": "⬆ 更新到最新版", "api": "/api/ocr/update",
                     "kind": "update",
                     "body": {"mode": "latest"}, "confirm": "最新组合未经本软件回归验证，可能出现不兼容。\n出问题可用「恢复推荐版本」一键回退。确定更新？"},
                    {"type": "api", "label": "↩ 恢复推荐版本", "api": "/api/ocr/update",
                     "kind": "restore", "body": {"mode": "recommended"}},
                ]
        except Exception:  # noqa: BLE001
            pass
        # v1.0.608：状态以**真实识别自检**为准——目录齐 ≠ 能识别（DLL 挂不上、
        # 模型缺文件都会让"已安装"一跑就废）。自检过不了就降级，并把自检入口给出来。
        sc = st.get("selfcheck") or {}
        if sc.get("state") == "done":
            _ok = bool(sc.get("ok"))
            c["hint"] = (("✅ 真实识别自检通过（%.1f 秒）" % ((sc.get("ms") or 0) / 1000.0))
                         if _ok else "❌ 真实识别自检未通过：" + str(sc.get("error") or "")[:70]) + "；" + c.get("hint", "")
            if not _ok:
                c["status"] = "degraded"
        c.setdefault("actions", []).append(
            {"type": "api", "label": "🔁 立即自检", "api": "/api/ocr/selfcheck", "kind": "check"})
    else:
        c["status"] = "missing"; c["hint"] = "未安装——首次使用课件采集/扫描件识别时自动下载，或现在一键安装（%s，阿里云/清华镜像）" % ocr_size_hint()
        c["actions"] = [{"type": "api", "label": "⬇ 一键安装（国内源）", "api": "/api/ocr/install", "kind": "install"}]


def _probe_asr(c):
    # v1.0.611：语音组件此前只在设置页有个孤零零的「语音转写组件」按钮，没进体检——
    # 而它是唯一会"装好了却仍然不可用"的组件（依赖树残缺 / 模型下载中断都在这一路）。
    # 判据一律用文件级的 component_complete()，绝不 import（v1.0.539 死锁教训）。
    from services import asr as _asr
    c.update(name="语音转写组件", desc="语音输入转文字（本地 whisper，录音不外发）")
    prog = _asr.install_progress()
    if prog.get("running"):
        c["status"] = "installing"
        c["hint"] = "正在安装：%s（%d%%）" % (prog.get("phase") or "下载依赖", prog.get("pct") or 0)
    elif _asr.component_complete():
        _pk = _asr.installed_meta().get("pkgs") or []
        if _pk:
            c["version"] = "、".join(_pk)
        c["hint"] = ("引擎文件已就位（首次说话约 3~8 秒加载模型，之后即时）。"
                     "界面上「转写失败」多有两因：输入设备选错（会明说「没收到声音」）"
                     "或模型下载中断——下面这条是当次真实原因")
        _err = _asr.last_error()
        if _err:
            c["status"] = "degraded"
            c["hint"] = "❌ 上次模型加载失败：" + _err[:120]
    else:
        c["status"] = "missing"
        c["hint"] = "未安装或依赖残缺——语音输入不可用。软件内安装（约 75MB，清华 PyPI 源，仅一次）"
        c["actions"] = [{"type": "api", "label": "⬇ 一键安装（国内源）",
                         "api": "/api/asr/install", "kind": "install"}]


def _probe_git(c):
    from core import portable as _pb
    c.update(name="Git", desc="自我修复自动提交 / 版本登记 / 源码比对")
    gst = _pb.install_status("git")
    p = _pb.find_git()
    if gst.get("running"):
        c["status"] = "installing"
        c["hint"] = "正在安装便携版：%s（%d%%）" % (gst.get("phase") or "下载", gst.get("pct") or 0)
    elif p:
        _, ver = _which_version([p, "--version"], exe=p)
        c["version"] = ver
        c["hint"] = ("来源：%s｜缺失时影响：自动修复可应用但不提交、版本登记不含源码哈希"
                     % ("软件内便携版（不写系统 PATH）" if not _pb.find_system_tool("git") else "系统 PATH"))
        _act, _shared = _share_path_action("git", "git")
        if _act:
            c["actions"] = (c.get("actions") or []) + [_act]
            c["hint"] += "｜终端共享：%s" % ("已开启" if _shared else "未开启（软件内不需要它）")
    else:
        c["status"] = "degraded"
        c["hint"] = "未安装——自我修复将跳过 git 提交，版本管理仅切换 exe（主功能不受影响）。点「一键安装」由软件自装便携版（约 38MB，npmmirror 国内镜像，不写系统 PATH）"
        c["actions"] = [
            {"type": "api", "label": "⬇ 一键安装便携版（国内镜像）",
             "api": "/api/components/fix", "kind": "install", "body": {"key": "git"}},
            {"type": "link", "label": "🌐 官网下载", "url": "https://git-scm.com/download/win"},
            {"type": "link", "label": "🇨🇳 国内镜像（npmmirror）",
             "url": "https://registry.npmmirror.com/-/binary/git-for-windows/"},
        ]


def _probe_node(c):
    from core import portable as _pb
    c.update(name="Node.js", desc="前端 JS 语法校验（自我修复 .js 补丁）/ PC 接管 dsh 依赖")
    nst = _pb.install_status("node")
    p = _pb.find_node()
    if nst.get("running"):
        c["status"] = "installing"
        c["hint"] = "正在安装便携版：%s（%d%%）" % (nst.get("phase") or "下载", nst.get("pct") or 0)
    elif p:
        _, ver = _which_version([p, "--version"], exe=p)
        c["version"] = ver
        c["hint"] = ("来源：%s｜dsh（PC 接管）需 Node ≥22.19 或 24+，@deepseek-ai/dsh 由软件内 npm 安装"
                     "（npmmirror 优先）｜便携版已注入本软件运行环境，命令行任务可直接用 node/npm"
                     % ("软件内便携版（不写系统 PATH）" if not _pb.find_system_tool("node") else "系统 PATH"))
        _act, _shared = _share_path_action("node", "node/npm")
        if _act:
            c["actions"] = (c.get("actions") or []) + [_act]
            c["hint"] += "｜终端共享：%s" % ("已开启" if _shared else "未开启（软件内不需要它）")
    else:
        # v1.0.612：不再把"你先去装 Node"丢回给用户——Node 是 dsh 的硬前置，
        # 一键安装原先正是最后一步卡死在这（实测「Node 环境不满足…WinError 2」）。
        c["status"] = "missing"
        c["hint"] = "未安装——自我修复对 .js 补丁只做基础校验、dsh 装不上。点「一键安装」由软件自装便携版（约 34MB，npmmirror；只在本软件内可用，不动系统 PATH）"
        c["actions"] = [
            {"type": "api", "label": "⬇ 一键安装便携版（国内镜像）",
             "api": "/api/components/fix", "kind": "install", "body": {"key": "node"}},
            {"type": "link", "label": "🇨🇳 官网安装包（npmmirror）",
             "url": "https://npmmirror.com/mirrors/node/"},
        ]


def _probe_build_env(c):
    # v1.0.473：探测统一走 _locate_build_python（frozen exe 不再把自身当解释器）
    py_exe, pi = _locate_build_python()
    c.update(name="打包构建环境", desc="软件内「一键打包并重启」依赖（PyInstaller + 源码）")
    _src, _has_req = _build_source_root()
    if not _has_req:
        # v1.0.619（用户在另一台机器的实测报障）：exe 上一级没有 requirements-dev.txt
        # 说明这台机器只有正式包、没有源码树。软件内一键打包在这类机器上**做不到、
        # 也不需要**，以前却报成 missing + 挂一个「一键升级」按钮，点下去拿到
        # 「未找到依赖清单：C:\Users\...\Desktop\requirements-dev.txt」——
        # 用户据此以为组件坏了。现在判 na：不进「待处理」、不给安装动作。
        c["status"] = "na"
        c["hint"] = ("本机只有正式包（exe 上一级没有源码目录），软件内「一键打包」不适用——"
                     "需要自己改源码发版时，把项目源码放到 exe 的上一级目录再重新体检")
    else:
        ok = py_exe and os.path.isfile(py_exe) and pi and os.path.isfile(pi)
        if ok:
            _, ver = _which_version([py_exe, "--version"], exe=py_exe)
            c["version"] = ver
            c["hint"] = "PyInstaller 就绪：" + pi
        else:
            c["status"] = "missing"
            _cmd = "pip install -r requirements-dev.txt -i https://mirrors.aliyun.com/pypi/simple/"
            c["hint"] = "不完整——软件内一键打包不可用（更新需手动重新打包）。点「一键升级」软件内自动安装（阿里云源，约 1-3 分钟）"
            c["actions"] = [
                {"type": "api", "label": "⬆ 一键升级（阿里云源）", "api": "/api/components/fix",
                 "kind": "install",
                 "body": {"key": "build_env"},
                 "confirm": "将自动执行 pip 安装构建依赖（PyInstaller 等，阿里云源，约 1-3 分钟）。期间请勿关闭软件。继续？"},
                {"type": "cmd", "label": "📋 复制命令（手动执行）", "cmd": _cmd},
            ]


def _probe_dsh(c):
    # v1.0.612：判据必须和 dsh_bridge 用同一个 `_which_dsh`。原先这里自己抄了一遍
    # shutil.which 循环，而 npm -g 装进**便携 node 目录**的 shim 不在系统 PATH 上 ——
    # 结果 dsh 明明装好了（109 秒那次实测），体检还说"未安装"，又是一处"装了却不可用"。
    from agent_tools.dsh_bridge import _which_dsh, dsh_check
    dsh = _which_dsh()
    c.update(name="PC 接管（dsh）", desc="deepseek-harness 无头委托：把复杂桌面任务交给 dsh 自主执行（可选增强）")
    if dsh:
        # 完整路径有 90+ 字符，前端 .comp-ver 只留 60 字会被截断 → 摆"文件名 @ 所在包目录"
        c["version"] = "%s @ %s" % (os.path.basename(dsh), os.path.basename(os.path.dirname(dsh)))
        # 就绪判定复用 dsh_check（node 版本 / 可执行 / 凭据三项），不再自己猜——
        # v1.0.614：上一版这里指向过一个界面上根本不存在的配置区，审查抓出来后改为复用 dsh_check。
        try:
            _st = dsh_check(load_config())
        except Exception:  # noqa: BLE001
            _st = {}
        if _st.get("ready"):
            c["hint"] = "通道可用｜实占约 800MB（dsh 本体 703MB + 便携 Node 95MB，装在 组件/node 下）"
        else:
            c["status"] = "degraded"
            _hs = "；".join(_st.get("hints") or []) or "模型凭据未配"
            c["hint"] = ("已安装但跑不起来：%s｜实占约 800MB（dsh 本体 703MB + 便携 Node 95MB）" % _hs[:120])
    else:
        c["status"] = "degraded"
        c["hint"] = "未安装——agent 不提供 PC 接管通道（其余工具不受影响）。点「一键安装」由软件装（Node 缺失会先自装便携版）"
        c["actions"] = [{"type": "api", "label": "⬇ 一键安装 dsh（npmmirror 优先）",
                         "api": "/api/components/fix", "kind": "install", "body": {"key": "dsh"},
                         "confirm": "将执行 npm install -g @deepseek-ai/dsh（npmmirror 国内镜像优先，失败才回落官方源，最长等 3 分钟）；"
                                    "本机没有 Node 时先自动装便携版再装。"
                                    "实测落盘约 800MB（dsh 本体 703MB + Node 95MB），装在 组件/node 下。继续？"}]


def _probe_wecom(c):
    from wecom_bridge.wecom_proc import detect_wecom_exe
    exe = detect_wecom_exe()
    c.update(name="企业微信", desc="CDP 绑定与页面自动化主体")
    if exe:
        c["version"] = exe
        c["hint"] = "需带 --remote-debugging-port 启动（软件会自动注入）"
    else:
        c["status"] = "missing"
        c["hint"] = "未检测到企微安装——请安装企业微信，或在设置 → 企微绑定里指定 exe 路径"
        c["actions"] = [{"type": "link", "label": "🌐 下载企业微信", "url": "https://work.weixin.qq.com/"}]


_PROBE_FNS = {"ocr": _probe_ocr, "asr": _probe_asr, "git": _probe_git,
              "node": _probe_node, "build_env": _probe_build_env,
              "dsh": _probe_dsh, "wecom": _probe_wecom}


def _probe_component(key):
    """单组件探测：返回 {key,name,status,version,desc,hint,actions}。
    status：ok=可用 / degraded=缺失但功能有兜底 / missing=对应功能不可用 /
    na=本机条件下这项不适用（既不用装也装不了，例如正式包上没有源码目录时的打包环境）。
    actions：v1.0.473 用户反馈「体检后没有具体方案」——未就绪组件给出结构化下一步：
      {type:"api", label, api, body}  前端 POST 调用（软件内一键完成）
      {type:"link", label, url}       前端打开浏览器下载（需系统安装器的外部组件）
      {type:"cmd", label, cmd}        需要用户在终端执行的命令（前端一键复制）
    全程 try/except——任一探测异常不拖垮整份体检报告。
    v1.0.648：各组件的探测体搬进 _probe_*，本函数只留分派与统一兜底。
    """
    c = {"key": key, "status": "ok", "version": None, "hint": "", "actions": []}
    try:
        _fn = _PROBE_FNS.get(key)
        if _fn:
            _fn(c)
    except Exception as e:  # noqa: BLE001
        c["status"] = "unknown"
        c["hint"] = f"探测失败：{type(e).__name__}: {str(e)[:80]}"
    return c


def _share_path_action(key_name, tool_label):
    """v1.0.625「终端共享」开关——只在**本软件自装**的便携运行时上出现。

    返回 (action 或 None, 当前是否已共享)。默认关：软件内部（agent 命令行、dsh、JS 校验）
    早就不依赖系统 PATH（v1.0.623 注入本进程 PATH、v1.0.624 把路径播报进上下文），
    这个开关只服务「软件外的终端也要能用 node」这一种需求，而它是用户机器上的持久副作用，
    所以必须用户显式点、并且可撤销。`kind` 用 "share" 而非 "install"——一键安装只认
    kind==="install"，绝不能把"顺手改你 PATH"混进那个批量动作里。
    """
    from core import portable as _pb
    try:
        if not _pb.portable_exe(key_name):
            return None, False
        on = _pb.user_path_shared()
    except Exception as e:  # noqa: BLE001
        log_event(f"[components] 终端共享状态探测失败（忽略该开关）：{e}")
        return None, False
    confirm_on = ("会把本软件自装的便携目录追加到**你当前账户**的 PATH（注册表 HKCU\\Environment，"
                  "不需要管理员）。只加我们自己的目录，你已有的条目一条不动、顺序不变；"
                  "随时可以用同一个按钮撤销。软件内部本来就能用 node，开这个是给软件外的终端用的。确定开启？")
    return ({
        "type": "api", "kind": "share",
        "label": ("🔌 终端共享：已开启（点此撤销）" if on
                  else "🔌 让软件外的终端也能用 %s" % tool_label),
        "api": "/api/components/share-path", "body": {"on": (not on)},
        "confirm": (confirm_on if not on else
                    "将从你当前账户的 PATH 中移除本软件加过的那几条（你自己加的其它条目一条不动）。"
                    "撤销后软件内仍然可用。确定撤销？"),
    }), on


@bp.route("/components/share-path", methods=["POST"])
def api_components_share_path():
    """v1.0.625：终端共享开关的执行端（body {on:true/false}）。

    实现与安全边界都在 `core.portable.share_user_path`：winreg 不用 setx（setx 在 1024
    字符截断，本机用户 PATH 实测已 1002，用它等于抹掉尾部几条）、保持原注册表类型
    （本机是 REG_SZ，里面还有字面量 `%USERPROFILE%\\...`，改类型会改变展开语义）、
    只追加不重排、撤销按台账只摘我们自己加的、拼出来超限直接拒绝写入并说明原因。
    """
    data = request.get_json(force=True, silent=True) or {}
    from core import portable as _pb
    out = _pb.share_user_path(on=bool(data.get("on")))
    if not out.get("ok"):
        log_event("[components] 终端共享操作未成功：%s" % (out.get("error") or ""))
    return jsonify(out)


@bp.route("/components/status")
def api_components_status():
    """v1.0.473（组件体检）：「运行组件」视图——七类组件状态/版本/安装指引一次汇总。
    永不 500：单项探测异常降级为 unknown，不拖垮整份报告。"""
    comps = [_probe_component(k) for k in ("ocr", "asr", "git", "node", "build_env", "dsh", "wecom")]
    _na_n = sum(1 for c in comps if c["status"] == "na")
    return jsonify({
        "components": comps,
        "ok_n": sum(1 for c in comps if c["status"] == "ok"),
        # v1.0.619：na（本机根本不适用、也装不了）不进分母——否则「N/M 项可用」永远
        # 差一项、待处理清不零，用户会一直以为有个组件没装好。
        "total": len(comps) - _na_n,
        "na_n": _na_n,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


@bp.route("/components/fix", methods=["POST"])
def api_components_fix():
    """v1.0.473（用户反馈：体检后没有具体方案）：组件修复动作执行器。
    body {key}: ocr/asr→按需国内镜像下载安装；dsh→npm 安装 @deepseek-ai/dsh（npmmirror 优先，
    Node 缺失时先自装便携版）；node/git→软件内自装便携运行时（v1.0.612，zip 解到 组件/<key>/，
    不写系统 PATH）；build_env→pip 装构建依赖。
    2026-10-23 用户明确要求：「我要的一键安装是全部安装好，而不是我自己去找前置条件」——
    所以原先"给个官网链接让用户自己装"的 node/git 也收进来了。"""
    data = request.get_json(force=True, silent=True) or {}
    key = str(data.get("key") or "")
    try:
        if key == "ocr":
            from core_utils import install_ocr_async
            return jsonify(install_ocr_async())
        if key == "dsh":
            # v1.0.612：前置条件由软件承担——Node 缺失时先自装便携版并等它就绪，再跑 npm。
            # 一键安装的语义原本是"点一下全装好"，原先在 dsh 这一步断掉（要用户自己去装 Node），
            # 串行顺序由组件顺序保证（node 在 dsh 之前），但单独点 dsh 的修复按钮也必须能跑通。
            from core import portable as _pb
            if not _pb.find_node():
                _pb.install_async("node")
                _deadline = time.time() + 420
                while time.time() < _deadline:
                    st = _pb.install_status("node")
                    if not st.get("running"):
                        break
                    time.sleep(3)
                if not _pb.find_node():
                    return jsonify({"ok": False,
                                    "error": "Node 便携版自动安装未成功："
                                             + (_pb.install_status("node").get("error") or "超时/网络失败")}), 500
            from agent_tools import dsh_bridge
            r = dsh_bridge.dsh_prepare(timeout=180)   # 官方源失败自动切 npmmirror
            return jsonify(r)
        if key in ("node", "git"):
            # v1.0.612：软件内自装便携运行时（zip 直解到 组件/<key>/，不写系统 PATH、不要管理员）
            from core import portable as _pb
            return jsonify(_pb.install_async(key))
        if key == "build_env":
            # v1.0.473（用户反馈改一键升级）：软件内执行 pip 安装构建依赖（阿里云源），
            # 不再让用户复制命令去终端。
            # v1.0.473 修复（用户实测截图 16:40）：解释器定位统一走 _locate_build_python
            # ——frozen exe 的 sys.executable 是软件自己，原内联探测把 exe 当 python
            # 传给 -m pip，必现「pip 正常但未找到 PyInstaller」假失败。
            import subprocess as _sp
            import os as _os
            py_exe, _pi0 = _locate_build_python()
            if not py_exe:
                return jsonify({"ok": False, "error": "未找到可用的 Python 解释器——请先安装 Python 或在 config.json build.python 里指定"}), 400
            # 源码根与依赖清单：与体检同一口径（_build_source_root，别在两处各算一遍）
            src_dir, _has_req = _build_source_root()
            if not _has_req:
                # v1.0.619（用户实测）：面板此时已把这项判为"不适用"、不再给安装按钮；
                # 但直接打这个端点也必须拿到人话——以前甩出来的是
                # 「未找到依赖清单：C:\Users\...\Desktop\requirements-dev.txt」，
                # 看着像装坏了，实际是这台机器只有正式包、没有源码。
                return jsonify({"ok": False,
                                "error": "这台机器只有正式包（exe 上一级没有源码和 requirements-dev.txt），"
                                         "打包构建环境不适用——需要在软件内自己发版时，"
                                         "把项目源码放到 exe 的上一级目录后再试"}), 400
            req = _os.path.join(src_dir, "requirements-dev.txt")
            _ALIYUN = "https://mirrors.aliyun.com/pypi/simple/"
            r = _sp.run([py_exe, "-m", "pip", "install", "-r", req, "-i", _ALIYUN],
                        capture_output=True, timeout=600,
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
            if r.returncode != 0:
                return jsonify({"ok": False, "error": "pip 安装失败（见输出尾部）", "tail": out[-500:]})
            # 装完重新定位 PyInstaller（pip 装进 py_exe 所在 venv，Scripts 布局）
            _, pi = _locate_build_python()
            if pi:
                return jsonify({"ok": True, "msg": "构建依赖安装完成，PyInstaller 就绪——已自动重新体检",
                                "pyinstaller": pi, "tail": out[-300:]})
            # Scripts 下没有 → 用 -m PyInstaller 兜底验证（不依赖 exe 落盘布局）
            try:
                v = _sp.run([py_exe, "-m", "PyInstaller", "--version"], capture_output=True, timeout=60,
                            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
                if v.returncode == 0:
                    return jsonify({"ok": True, "msg": "构建依赖安装完成（PyInstaller 以 -m 方式可用）——已自动重新体检",
                                    "tail": out[-300:]})
            except Exception:  # noqa: BLE001
                pass
            return jsonify({"ok": False,
                            "error": "pip 正常但未找到 PyInstaller——请检查 requirements-dev.txt 是否含 pyinstaller",
                            "tail": out[-500:]})
        return jsonify({"ok": False, "error": f"组件 {key} 无软件内自动安装通道——请按面板给出的链接/命令手动安装"}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"修复动作失败：{e}"}), 500


@bp.route("/ocr/status")
def api_ocr_status():
    from core_utils import ocr_install_status
    return jsonify(ocr_install_status())


@bp.route("/ocr/install", methods=["POST"])
def api_ocr_install():
    from core_utils import install_ocr_async
    return jsonify(install_ocr_async())


@bp.route("/ocr/selfcheck", methods=["POST"])
def api_ocr_selfcheck():
    """v1.0.608：真实识别自检（造图 → 走生产 OCR 入口 → 比相似度）。
    同步执行约 1-5 秒（首次含引擎与模型加载），组件页「🔁 立即自检」与开机自动自检共用。"""
    from core_utils import ocr_selfcheck
    return jsonify(ocr_selfcheck(force=True))


@bp.route("/ocr/updates")
def api_ocr_updates():
    """组件更新检查（联网）：逐包对比已装版本与镜像最新版。
    这是「用户主动要准信」的入口 → force 重算，同时把结果回填给界面路径读的缓存。"""
    from core_utils import ocr_check_updates
    return jsonify(ocr_check_updates(force=True))


@bp.route("/ocr/update", methods=["POST"])
def api_ocr_update():
    """组件更新动作：mode=latest（更新到镜像最新版，实验性）/ recommended（恢复推荐钉定版本）。"""
    from core_utils import install_ocr_async, _OCR_WHEELS, ocr_check_updates
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "latest")
    try:
        if mode == "recommended":
            return jsonify(install_ocr_async(versions=dict(_OCR_WHEELS), force=True))
        upd = ocr_check_updates(force=True)      # 装哪几个包必须按当场真值，不能吃 15 分钟缓存
        want = {p["pkg"]: p["latest"] for p in upd["packages"]
                if p["update_available"] and p["latest"]}
        if not want:
            return jsonify({"state": "ready", "progress": 100,
                            "msg": "全部组件已是最新", "error": ""})
        return jsonify(install_ocr_async(versions=want, force=True))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"更新失败：{e}"}), 500

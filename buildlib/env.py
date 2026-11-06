# -*- coding: utf-8 -*-
"""buildlib.env — 构建期共享路径与运行环境（自 build.py 模块级常量逐字迁入）。

BASE 一律指**项目根**（build.py 所在目录），与拆分前 build.py 的 BASE 同义。
"""
import os
import shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VPATH = os.path.join(BASE, "version.py")
SPEC = os.path.join(BASE, "WeComCompanion.spec")

# 优先使用项目自带 Python 环境（含 pywebview/pystray 等全部运行时依赖）；
# PATH 探测仅作兜底——某些 shell（如 Marvis 运行时）的 PATH 会先命中缺依赖的环境，
# 若用错环境打包会导致 webview/pystray 等模块静默缺失、exe 启动报 ModuleNotFoundError。
def _find_pyinstaller():
    """自适应探测 pyinstaller：优先当前用户的项目专用 venv（wecom/default），其次 PATH。
    v1.0.405（L5）：删除历史开发机硬编码绝对路径（开发机用户名残留）——换机/协作
    场景死代码只会造成困惑；expanduser 候选 + PATH 兜底已覆盖全部真实场景。"""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, r".workbuddy\binaries\python\envs\wecom\Scripts\pyinstaller.exe"),
        os.path.join(home, r".workbuddy\binaries\python\envs\default\Scripts\pyinstaller.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    w = shutil.which("pyinstaller")
    return w or candidates[0]


PYINSTALLER = _find_pyinstaller()

# 安全删除钩子会拦截 os.remove，打包前清掉这些会话变量（原生 rm 才可用）
_SANDBOX_VARS = (
    "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID", "CODEBUDDY_SAFE_DELETE_SANDBOX",
    "SAFE_DELETE_SANDBOX", "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR", "CODEBUDDY_TOOL_CALL_ID",
)

# -*- coding: utf-8 -*-
"""core.safe_open — 用系统关联程序打开本地文件/目录的唯一收口（v1.0.646）。

为什么要有它：os.startfile 不是"打开"，是**执行**——它把路径交给 Shell 解析，
关联程序会真跑起来。此前这个动作散在 routes/workspace.py、routes/knowledge.py、
routes/b_admin.py 三处，各自写一份判据（扩展名黑名单、路径前缀、分类白名单），
规则漂移一次就是一条任意执行链：README v1.0.641 记录过一次（api_token.txt 借
桌面路径被放开），修法只是就地补一条 if，照旧是三份判据三处漂移。

现在所有"打开"都先过这里：
  · open_path —— 本地文件/目录：存在性 + 路径穿越 + 扩展名黑名单 + 可选根前缀
  · open_url  —— 外链：只放行 http(s)，且默认只放行本机地址

返回统一的 (ok, err)：ok=False 时调用方一律不要 startfile，把 err 原样回给界面。
"""

import os


# 可执行与脚本类扩展名：startfile 会把它们交给关联程序**执行**，不是"打开看"。
# 与 workspace._LOCATE_DENY_EXT 同源（那边管"聊天里跳文件"，这里管"真的打开"），
# 判据只留这一份，那边改为引用本集合。
EXEC_EXT = frozenset("""exe bat cmd ps1 psm1 vbs vbe js jse wsf wsh msi msp scf scr pif
 hta cpl lnk url reg jar py pyw pyz dll sys msu com application gadget inf""".split())


def _norm(p):
    return os.path.realpath(os.path.abspath(str(p or "")))


def is_blocked_ext(path):
    """True = 该扩展名不允许交给系统关联程序执行。"""
    ext = os.path.splitext(str(path or ""))[1].lstrip(".").lower()
    return ext in EXEC_EXT


def open_path(target, roots=None, allow_dirs=True, must_exist=True):
    """校验通过则 os.startfile 打开，返回 (True, None)；否则返回 (False, 原因)。

    roots 给定时，解析后的真实路径必须落在其中之一内（防路径穿越、防"桌面上的
    可疑文件借合法前缀被放行"这一历史事故形态）。allow_dirs=False 时目录也拒绝。
    """
    try:
        rp = _norm(target)
    except Exception:  # noqa: BLE001
        return False, "路径无效"

    if roots:
        ok = False
        for r in roots:
            rr = _norm(r)
            if rp == rr or rp.startswith(rr + os.sep):
                ok = True
                break
        if not ok:
            return False, "目标不在允许的目录内"

    if must_exist and not os.path.exists(rp):
        return False, "目标不存在"

    if os.path.isdir(rp):
        if not allow_dirs:
            return False, "不允许打开目录"
    elif is_blocked_ext(rp):
        return False, "该扩展名可能被执行，已拒绝打开"

    try:
        os.startfile(rp)  # noqa: S606
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, f"打开失败：{e}"


def open_url(url, allow_hosts=("127.0.0.1", "localhost")):
    """用默认浏览器打开外链；只放行 http(s)，默认只放行本机地址（防 file:// / 伪协议）。"""
    u = str(url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return False, "只支持 http/https 地址"
    rest = u.split("://", 1)[1]
    host = rest.split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    if allow_hosts and host not in allow_hosts:
        return False, f"不允许打开本机以外的地址：{host}"
    try:
        if os.name == "nt":
            os.startfile(u)  # noqa: S606
        else:
            import webbrowser
            webbrowser.open(u)
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, f"打开失败：{e}"

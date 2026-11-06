# -*- coding: utf-8 -*-
"""buildlib.patching — pywebview WebView2 右键菜单补丁（自 build.py 逐字迁入）。"""
import io
import os

from buildlib.env import PYINSTALLER


def _patch_pywebview_contextmenu(target=None):
    """强制启用 WebView2 原生右键菜单（粘贴/剪切/复制）——v1.0.501 引入，v1.0.623 修正。

    ⚠️ 这条在 623 之前是个**空补丁**，值得留在这里当地物证：
        old = "settings.AreDefaultContextMenusEnabled = _state['debug']"
        new = old + "  # PATCHED(...)"
    `old` 是整条赋值语句，所以写回去的右边仍然是 `_state['debug']`，只是尾巴多了
    一句注释——生产 exe 的右键菜单从来没被打开过。而函数开头 `if marker in src: return`
    让之后每次打包都认定「已打过」，于是这个「绿但什么都不改」的补丁一路活到发货，
    直到用户报「聊天框右键没菜单」（v1.0.623）才现形。现按**整行重写**，
    并识别旧的空补丁形态做迁移；找不到那行时明确报出来，不再静默 return。
    """
    import re as _re
    marker = "PATCHED(企微伴侣 v1.0.623)"
    line_re = _re.compile(r"^([ \t]*)settings\.AreDefaultContextMenusEnabled\s*=.*$")
    paths = [target] if target else []
    if not target:
        for base in [os.path.dirname(PYINSTALLER) or ""]:
            cand = os.path.join(base, os.pardir, "Lib", "site-packages",
                                "webview", "platforms", "edgechromium.py")
            paths.append(os.path.normpath(cand))
    for cand in paths:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            # newline="" 保原行尾：这个文件在 site-packages 里，改一次不该顺手换行符
            with io.open(cand, encoding="utf-8", newline="") as f:
                src = f.read()
        except Exception as e:  # noqa: BLE001
            print(f"[build] ⚠️ 读不到 pywebview 右键菜单配置（{e}）——生产包可能没有右键菜单。")
            return False
        lines = src.splitlines(keepends=True)
        hit = None
        for i, ln in enumerate(lines):
            if line_re.match(ln):
                hit = i
                break
        if hit is None:
            print("[build] ⚠️ pywebview 里找不到 AreDefaultContextMenusEnabled 赋值行"
                  "（版本变了？）——右键菜单这次没启用，请手工核对 edgechromium.py。")
            return False
        m = line_re.match(lines[hit])
        rhs = lines[hit].split("=", 1)[1].split("#", 1)[0].strip()
        if rhs == "True" and marker in lines[hit]:
            return True          # 已是有效形态，幂等退出
        eol = "\r\n" if lines[hit].endswith("\r\n") else "\n"
        new_ln = ("%ssettings.AreDefaultContextMenusEnabled = True  # %s"
                  " 生产环境保留右键菜单；旧形态=%s（v1.0.501 只贴了注释没改赋值，是空补丁）"
                  % (m.group(1), marker, rhs)) + eol
        try:
            with io.open(cand, "w", encoding="utf-8", newline="") as f:
                f.write("".join(lines[:hit]) + new_ln + "".join(lines[hit + 1:]))
        except Exception as e:  # noqa: BLE001
            print(f"[build] ⚠️ pywebview 补丁写入失败（不拦打包）：{e}")
            return False
        what = "已启用" if rhs != "True" else "已修正旧形态"
        print(f"[build] pywebview 右键菜单{what}：`{rhs}` → `True`")
        return True
    print("[build] ⚠️ 没定位到 webview/platforms/edgechromium.py——生产包可能没有右键菜单。")
    return False

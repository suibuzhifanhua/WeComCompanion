# -*- coding: utf-8 -*-
"""tools/check_frontend.py — index.html 前端结构校验的 **命令行壳**（v1.0.619）。

实现已上移到 core.frontend_check（自我修复的补丁验证要在冻结包里用到它，
而 tools/ 是开发期目录、不进包）。本文件只做两件事：
  1) 原样 re-export 四个校验函数 + run_all + check_js_file
     （tests/test_frontend_structure.py、tests/test_self_repair_v2.py 仍按老路径 import）；
  2) 作为 build.py 打包门禁的 subprocess 入口（`python tools/check_frontend.py <项目根>`）。

历史：index.html 结构破坏曾连续 3 次走到用户手里（CSS 花括号错位致未绑定单栏失效、
删块残留 </div> 致导航全部失效）——所以这项校验是打包前的强制门禁，不是调试脚本。

用法：python tools/check_frontend.py [项目根]
"""
import os
import sys

# Windows GBK 控制台下打印 ✅/❌ 会 UnicodeEncodeError。本脚本是 build.py 的打包门禁
# （subprocess 捕获输出同样按 cp936 编码），一旦在「全部通过」的成功行崩掉，退出码
# 非 0，反而把合格构建判成失败。统一改按 UTF-8 + replace 输出。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.frontend_check import (        # noqa: E402,F401（对外表面保持不变）
    check_css_braces,
    check_div_balance,
    check_dup_ids,
    check_js_file,
    check_script_style_close,
    run_all,
)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else _ROOT
    errors = run_all(root)
    if errors:
        for e in errors:
            print(f"[frontend-check] ❌ {e}", file=sys.stderr)
        sys.exit(1)
    print("[frontend-check] ✅ 前端结构校验通过（index.html + js/*.js）")


if __name__ == "__main__":
    main()

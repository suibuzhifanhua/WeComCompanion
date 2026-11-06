# -*- coding: utf-8 -*-
"""tools/scan_silent_except.py — 静默 except 扫描（AST，不看字符串）。

用法：
    python tools/scan_silent_except.py                # 全仓汇总
    python tools/scan_silent_except.py --detail       # 逐条列出文件:行号
    python tools/scan_silent_except.py --only-bare    # 只看 body 只有 pass 的

为什么要它：本仓 903 处 except，其中一批紧跟 pass——"降级但不留痕"。大部分是
有意的（探测失败就当没有），但数据安全路径上的静默（断点保存失败、配置写入失败）
会让故障在长任务里变成排障盲区。人工扫一遍会漏，AST 扫一遍是秒级的。

分级（只看 --detail 时的 ★ 标记）：
    ★★★ 数据/凭据相关：断点、配置、token、台账、会话落盘的失败
    ★★  资源清理：close/锁释放/线程收尾
    ★   普通探测：失败即降级，静默可接受
"""
import ast
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"__pycache__", "build", "dist", "node_modules", ".git", "docs", "产物库", "tests"}

# 数据安全关键词：命中即要求"必须留痕"
CRITICAL = ("breakpoint", "chat_state", "save_chat", "atomic_write", "config", "token",
            "ledger", "台账", "断点", "conversation", "会话", "experience", "经验")
RESOURCE = ("close", "release", "unlink", "remove", "rmtree", "join(", "stop")


def _silent(node):
    """True = 这个 except 分支静默（pass / 只有 docstring / 只有注释性语句）。"""
    body = [n for n in node.body if not (isinstance(n, ast.Pass))]
    body = [n for n in body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    return not body


# v1.0.648：静默不都是错的——"断点文件不存在""ws.close() 失败无所谓""写权限探测失败
# 就回退"这类路径，留痕只是噪声。所以给一个**显式声明**而不是逼人加无用日志：
# 在 except 分支里写 `pass  # silent-ok: <一句理由>`，工具就不再把它算作违规。
# 关键在"显式"：声明要挨着那条 pass 写，理由要写清，于是不留痕的每一处都是被人
# 看过一遍才留下的，而不是"反正没人查"。
SILENT_OK_MARK = "silent-ok:"


def scan():
    hits = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(base, fn)
            try:
                src = io.open(p, encoding="utf-8").read()
                tree = ast.parse(src)
            except Exception:  # noqa: BLE001
                continue
            lines = src.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if not _silent(node):
                    continue
                ctx = (lines[node.lineno - 1] or "") + " " + \
                      " ".join(lines[node.lineno:min(node.lineno + 6, len(lines))])
                low = ctx.lower()
                lvl = 1
                if any(k in low for k in CRITICAL):
                    lvl = 3
                elif any(k in low for k in RESOURCE):
                    lvl = 2
                # 声明标记只看本 handler 的源码行（except 行 → end_lineno），
                # 不取上面那 6 行上下文——否则邻居写了就算数，标记形同虚设。
                own = " ".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
                declared = SILENT_OK_MARK in own
                hits.append((lvl, os.path.relpath(p, ROOT), node.lineno,
                             (lines[node.lineno - 1] or "").strip()[:70], declared))
    return hits


def violations(hits=None):
    """★3 且**没有** silent-ok 声明的静默——数据安全路径上"删了没人知道"的那些。"""
    return [h for h in (scan() if hits is None else hits) if h[0] == 3 and not h[4]]


def main():
    detail = "--detail" in sys.argv
    only_bare = "--only-bare" in sys.argv
    gate = "--gate" in sys.argv
    hits = scan()
    crit = [h for h in hits if h[0] == 3]
    bad = violations(hits)
    if detail or only_bare:
        for lvl, p, ln, txt, declared in sorted(hits, key=lambda x: (-x[0], x[1])):
            if only_bare and lvl != 3:
                continue
            print(f"{'★' * lvl:<3} {p}:{ln}  {txt}{'   （已声明 silent-ok）' if declared else ''}")
    print(f"\n静默 except 合计：{len(hits)}（★★★ 数据/凭据相关 {len(crit)} 处，"
          f"其中未声明 {len(bad)} 处）")
    if gate:
        if bad:
            print("\n[gate] ❌ 数据安全路径的静默 except 未留痕也未声明：")
            for lvl, p, ln, txt, _d in bad:
                print(f"[gate]   {p}:{ln}  {txt}")
            print("[gate] 处理方式二选一：改成 log_event 留痕，或写 "
                  f"pass  # {SILENT_OK_MARK} <为什么这里静默是对的>")
            return 1
        print("[gate] ✓ 数据安全路径无未声明的静默 except")
    return 0


if __name__ == "__main__":
    sys.exit(main())

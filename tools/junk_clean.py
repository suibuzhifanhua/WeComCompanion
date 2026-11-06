# -*- coding: utf-8 -*-
"""tools/junk_clean.py — 项目垃圾清理（白名单 + 黑名单双闸，默认只预览）。

为什么默认不删：本项目**不是 git 仓库**，删错了没有 `git checkout` 兜底。

用法：
    python tools/junk_clean.py                      # 只列清单（dry-run，默认）
    python tools/junk_clean.py --apply              # 按默认范围真删
    python tools/junk_clean.py --apply --include-logs
    python tools/junk_clean.py --root <目录> --apply  # 自检/演练用（测试也走这条）

默认范围（都是纯可再生物）：
    ① `__pycache__/`、`.pytest_cache/`、`.mypy_cache/`
    ② `build/_ui_verify/`、`build/_cv_chrome/` 这类 headless Chrome profile
    ③ TEMP 里台子留下的 `wc_*/`、`wec_ff_*/`、`sr_vfy_*/`、`pytest-of-*/`
    ④ 台子截图产物 `build/_shot_*.png`
需要显式开启的（有取证价值，不默认删）：
    --include-logs   `build/_pack_*.log` 等历次打包/台子日志（本项目靠 `_pack_*.log` 里的
                     `[build] done ->` 判断"后台跑的打包到底成没成"，全删就回溯不了）
    --include-orphans `build/` 下无任何文档引用的 `_*.py` 一次性探针

绝不删（写死在 FORBIDDEN_SEGMENTS，命中即中止该项并计数）：
    dist/ 产物库/ docs/ build/_rollback/ build/_dist_backup/ build/_chat_archive/
    node_modules/ ocr/
    —— dist 与 产物库 是运行数据（含 PII）；_rollback 是无 git 时唯一的旧 exe 回滚手段；
       _dist_backup 是普通打包还原 dist 的来源；_chat_archive 是历次归档的真实对话记录。
"""
import argparse
import os
import re
import shutil
import sys

ROOT_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP = os.environ.get("TEMP", "")

FORBIDDEN_SEGMENTS = ("dist", "产物库", "docs",
                      "build/_rollback", "build/_dist_backup", "build/_chat_archive",
                      "node_modules", "ocr")

DIR_RULES = (
    ("①编译缓存", re.compile(r"^(?:[^/]*/)*__pycache__$")),
    ("①编译缓存", re.compile(r"^\.pytest_cache$")),
    ("①编译缓存", re.compile(r"^\.mypy_cache$")),
    ("①编译缓存", re.compile(r"^\.ruff_cache$")),
    ("②测试浏览器", re.compile(r"^build/_ui_verify$")),
    ("②测试浏览器", re.compile(r"^build/_cv_chrome$")),
)
FILE_RULES = (
    ("④截图产物", re.compile(r"^build/_shot_[\w\-]*\.png$")),
)
LOG_RULE = re.compile(r"^build/_(?:pack_\w+|ui8790\w*|sweep_\w+|pytest_\w+|cv_\w+)\.log$")
TEMP_RULES = (re.compile(r"^wc_[A-Za-z0-9_]+$"),
              # 测试自己 mkdtemp(prefix="wec_...") 造的假 config/假产物库，跑完从不回收
              # （本机实测 3390 个：wec_cfg_dsh_/wec_srcimp_/wec_bind_/wec_dnd_/wec_tpl_… 全是合成 fixture）
              re.compile(r"^wec_[A-Za-z0-9_]+$"),
              re.compile(r"^sr_vfy_[A-Za-z0-9_]+$"),
              re.compile(r"^srt_vfy_[A-Za-z0-9_]+$"),
              re.compile(r"^pytest-of-"))


def human(n):
    for u, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024), ("B", 1.0)):
        if n >= div:
            return "%.1f %s" % (n / div, u)
    return "0 B"


def dsize(p):
    tot = 0
    for dp, dn, fns in os.walk(p, onerror=lambda e: None):
        for f in fns:
            try:
                tot += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return tot


def forbidden(rel):
    r = rel.replace("\\", "/")
    return any(r == s or r.startswith(s + "/") for s in FORBIDDEN_SEGMENTS)


def collect(root, include_logs=False):
    """返回 (计划, 被护栏拦下的项)。计划项 = (类别, 绝对路径, 展示相对路径, 字节, 类型)。"""
    plan, blocked = [], []
    for dp, dn, fns in os.walk(root, onerror=lambda e: None):
        rel_dir = os.path.relpath(dp, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dn[:] = [d for d in dn if not forbidden(("%s/%s" % (rel_dir, d)).lstrip("/"))]
        for d in list(dn):
            if d == ".git":
                continue
            rel = ("%s/%s" % (rel_dir, d)).lstrip("/")
            for cat, rx in DIR_RULES:
                if rx.match(rel):
                    p = os.path.join(dp, d)
                    if forbidden(rel):
                        blocked.append(rel)
                        continue
                    plan.append((cat, p, rel, dsize(p), "dir"))
                    dn.remove(d)
                    break
        for f in fns:
            rel = ("%s/%s" % (rel_dir, f)).lstrip("/")
            kind = None
            for cat, rx in FILE_RULES:
                if rx.match(rel):
                    kind = cat
                    break
            if kind is None and include_logs:
                if LOG_RULE.match(rel):
                    kind = "③日志"
            if kind and not forbidden(rel):
                p = os.path.join(dp, f)
                try:
                    plan.append((kind, p, rel, os.path.getsize(p), "file"))
                except OSError:
                    pass
    if os.path.isdir(TEMP):
        for d in sorted(os.listdir(TEMP)):
            p = os.path.join(TEMP, d)
            if os.path.isdir(p) and any(rx.match(d) for rx in TEMP_RULES):
                plan.append(("②测试浏览器", p, "TEMP/" + d, dsize(p), "dir"))
    return plan, blocked


def main(argv=None):
    ap = argparse.ArgumentParser(description="项目垃圾清理（默认 dry-run）")
    ap.add_argument("--root", default=ROOT_DEFAULT)
    ap.add_argument("--apply", action="store_true", help="真删（缺省只列清单）")
    ap.add_argument("--include-logs", action="store_true", help="连历次打包/台子日志一起删")
    ap.add_argument("--no-temp", action="store_true", help="不动 TEMP 里的台子残留")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)

    plan, blocked = collect(root, include_logs=args.include_logs)
    if args.no_temp:
        plan = [x for x in plan if not x[2].startswith("TEMP/")]

    by_cat = {}
    for cat, _p, rel, size, kind in plan:
        by_cat.setdefault(cat, []).append((rel, size, kind))
    total = sum(x[3] for x in plan)
    mode = "APPLY（真删）" if args.apply else "DRY-RUN（只预览，未删除任何文件）"
    print("=" * 72)
    print("%s｜命中 %d 项，合计 %s" % (mode, len(plan), human(total)))
    print("=" * 72)
    for cat in sorted(by_cat):
        items = sorted(by_cat[cat], key=lambda x: -x[1])
        print("\n【%s】%d 项 · %s" % (cat, len(items), human(sum(x[1] for x in items))))
        for rel, size, _k in items[:10]:
            print("   %9s  %s" % (human(size), rel))
        if len(items) > 10:
            print("   … 另 %d 项" % (len(items) - 10))
    if blocked:
        print("\n!! 护栏拦下 %d 项（不该出现在计划里）：%s" % (len(blocked), blocked[:5]))

    if not args.apply:
        print("\n未删除任何文件。确认无误后加 --apply 执行。")
        return 0

    freed, errs = 0, []
    for cat, p, rel, size, kind in plan:
        if not rel.startswith("TEMP/") and forbidden(rel):
            errs.append("拒绝越界：%s" % rel)
            continue
        try:
            if kind == "dir":
                shutil.rmtree(p)
            else:
                os.remove(p)
            freed += size
        except OSError as e:  # 被进程占用（profile 正被 headless Chrome 拿着等）→ 跳过不致命
            errs.append("%s（%s）" % (rel, type(e).__name__))
    print("\n实际释放 %s；未删掉 %d 项" % (human(freed), len(errs)))
    for e in errs[:10]:
        print("   跳过 " + str(e)[:100])
    return 0


if __name__ == "__main__":
    sys.exit(main())

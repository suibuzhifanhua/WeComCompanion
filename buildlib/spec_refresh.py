# -*- coding: utf-8 -*-
"""buildlib.spec_refresh — hiddenimports 自动生成/写回、onedir spec 派生、冒烟清单。

自 build.py 逐字迁入（v1.0.65x 拆分）。
"""
import io
import os

from buildlib.env import BASE, SPEC

# ---------- v1.0.555（P0-11）：hiddenimports 自动生成 ----------
# 原手工列举漏模块：services.asr/todo_watch/daily_digest/wecom_archive 不在 spec
# hiddenimports 里，PyInstaller 缺模块只出 warning 不失败，发货 exe 里 ASR 三端点
# 连续 14 次 ImportError（日志实证）——而开发环境永远测不出（同根因事故已两次：
# memory_ops 10-07/10-09 亦然）。构建时扫描源码 import + 包内全子模块，写回 spec。
# v1.0.655：buildlib 不进此清单——构建期工具（同 build.py），运行包不需要；拖进来会把
# gates→scan_silent_except→tools/ 一并分析，PyInstaller 判自家缺失中止打包（实测）。
_SPEC_SCAN_PKGS = ("services", "core", "desktop", "agent", "agent_tools", "routes", "wecom_bridge")
_SPEC_SKIP_DIRS = {"build", "dist", "产物库", "__pycache__", ".pytest_cache", ".git",
                   "node_modules", "docs", "tools", "tests", "js", "css"}


def _module_exists(dotted):
    """大小写敏感的模块存在性检查（Windows FS 大小写不敏感，os.path.isfile 会把
    agent_tools.CONNECTORS 误判为 connectors.py → 无效 hiddenimport 触发缺模块
    构建失败）。逐级校验真实文件名。"""
    parts = dotted.split(".")
    cur = BASE
    for i, part in enumerate(parts):
        cur = os.path.join(cur, part)
        parent_listing = os.listdir(os.path.dirname(cur))
        base_name = os.path.basename(cur)
        if base_name + ".py" not in parent_listing and base_name not in parent_listing:
            return False
        if i < len(parts) - 1:
            # 中间段必须是包（目录）
            if not os.path.isdir(cur):
                return False
    return os.path.isfile(cur + ".py") or os.path.isfile(os.path.join(cur, "__init__.py"))


def _autogen_hiddenimports():
    """扫描源码中 from/import 本项目包的语句（含 from pkg import mod 形式的模块名
    判定），加各包全部子模块兜底，返回排序后的 hiddenimports 列表。"""
    import re as _re
    mods = set()
    pat_from = _re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+?)(?:#.*)?$", _re.M)
    pat_import = _re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)", _re.M)
    for root, _dirs, files in os.walk(BASE):
        rel = os.path.relpath(root, BASE)
        if rel.split(os.sep)[0] in _SPEC_SKIP_DIRS:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            try:
                txt = open(os.path.join(root, fn), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for m in pat_from.finditer(txt):
                base = m.group(1)
                if base.split(".")[0] not in _SPEC_SCAN_PKGS:
                    continue
                if _module_exists(base):
                    mods.add(base)
                if "." not in base:   # from pkg import X → X 可能是子模块
                    for nm in _re.split(r"[,\s()]+", m.group(2).replace("(", " ")):
                        nm = nm.strip()
                        if not nm or nm == "as" or not (nm[0].isalpha() or nm[0] == "_"):
                            continue
                        nm = nm.split(" as ")[0].strip()
                        cand = f"{base}.{nm}"
                        if _module_exists(cand):
                            mods.add(cand)
            for m in pat_import.finditer(txt):
                base = m.group(1)
                if base.split(".")[0] in _SPEC_SCAN_PKGS and _module_exists(base):
                    mods.add(base)
    # 包内全部子模块兜底（相对 import / 动态 import 扫不到的也进包；
    # __init__ 跳过——包本身已由 mods.add(pkg) 覆盖，pkg.__init__ 是冗余别名）
    for pkg in _SPEC_SCAN_PKGS:
        pkg_dir = os.path.join(BASE, pkg)
        if not os.path.isdir(pkg_dir):
            continue
        mods.add(pkg)
        for root, _dirs, files in os.walk(pkg_dir):
            if "__pycache__" in root:
                continue
            for fn in files:
                if fn.endswith(".py") and fn != "__init__.py":
                    rel = os.path.relpath(os.path.join(root, fn), BASE)
                    mods.add(rel[:-3].replace(os.sep, "."))
    return sorted(mods)


def _source_module_list():
    """本项目包的全部模块（与 hiddenimports 同源，供 frozen 冒烟当清单用）。
    冻结态里 exe 自己枚举包内模块不可靠（PyiFrozenFinder 是 path finder，登记在
    sys.path_importer_cache 而不是 sys.meta_path）——620/621 两次都因此拿到空清单，
    冒烟只报 9 个模块却打「import 全通」。源码侧清单是现成的，由 build 传进去。"""
    return [m for m in _autogen_hiddenimports() if m.split(".")[0] in _SPEC_SCAN_PKGS]


def refresh_spec_hiddenimports():
    """把扫描结果写回 spec 的 hiddenimports 赋值（保留原手工第三方项，ast 定位
    赋值语句的起止行，避免正则切错嵌套括号）。返回新增模块数（用于构建日志）。"""
    import ast as _ast
    with open(SPEC, encoding="utf-8") as f:
        src = f.read()
    tree = _ast.parse(src)
    node = None
    for stmt in tree.body:
        if isinstance(stmt, _ast.Assign) and any(
                isinstance(t, _ast.Name) and t.id == "hiddenimports" for t in stmt.targets):
            node = stmt
            break
    if node is None:
        raise SystemExit("[build] spec 中未找到 hiddenimports 赋值")
    old = set(_ast.literal_eval(node.value))
    auto = set(_autogen_hiddenimports())
    # 本项目包内条目一律以本次扫描为准（自愈：清掉历史误入的大小写幻影如
    # agent_tools.CONNECTORS / __init__ 别名）；第三方手工项原样保留。
    old_own = {m for m in old if m.split(".")[0] in _SPEC_SCAN_PKGS}
    merged = sorted((old - old_own) | auto)
    added = sorted(auto - old)
    lines = src.splitlines(keepends=True)
    _cmt = ("# v1.0.555（P0-11）：本项目包内模块由 build._autogen_hiddenimports() 构建"
            "时自动生成（第三方项保留手工清单）")
    new_stmt = _cmt + "\n" + "hiddenimports = " + repr(merged) + "\n"
    # v1.0.592 修复：替换区间原来只覆盖 `hiddenimports = ...` 那几行，注释行写在它上面、
    # 不在 ast 节点范围内 → 每次构建都在旧注释上方**再插一条同样的注释**，
    # 实测 WeComCompanion.spec 已堆到 41 行重复注释。现向上吸收连续同文注释行，
    # 一并纳入替换区间（本次构建即会把历史堆叠收敛成 1 行）。
    _start = node.lineno - 1
    while _start > 0 and lines[_start - 1].rstrip("\r\n") == _cmt:
        _start -= 1
    lines[_start:node.end_lineno] = [new_stmt]
    with open(SPEC, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    return added


def _write_onedir_spec():
    """由 onefile spec 派生出 onedir spec（v1.0.646）。

    不另起一份 spec：两份 spec 必然漂移（本项目已因"两份判据"吃过多次亏）。
    做法是把 EXE(...) 里的 a.binaries/a.datas 摘出来交给 COLLECT——除此之外
    与 onefile 完全同源（Analysis/打包/UPX 规则一致），后续 spec 改动自动继承。
    """
    src = io.open(SPEC, encoding="utf-8").read()
    i = src.index("exe = EXE(")
    exe_block = src[i:]
    if "a.binaries," not in exe_block or "a.datas," not in exe_block:
        raise RuntimeError("spec 的 EXE 块结构变了（找不到 a.binaries/a.datas），无法派生 onedir 版")
    exe_block = exe_block.replace("    a.binaries,\n", "", 1).replace("    a.datas,\n", "", 1)
    # v1.0.652（审计 D3）：补 exclude_binaries=True——PyInstaller 官方 onedir 模板的
    # EXE 必带此标志（api.py：该标志决定 exe 落 workpath/distpath、PKG 是否 onedir 形态）。
    # 旧派生只剥 a.binaries/a.datas 实参，exe 仍按 one-file 语义构建（启动解压到 _MEIPASS），
    # 而 COLLECT 把 index.html/js/css 放进 _internal/，resource_path 只查 _MEIPASS →
    # 打出来的 onedir 启动即无 UI（白屏/404）。
    exe_block = exe_block.replace("exe = EXE(pyz, a.scripts, [],",
                                  "exe = EXE(pyz, a.scripts, [], exclude_binaries=True,", 1)
    collect = """


# ---- onedir 派生块（v1.0.646，由 build._write_onedir_spec 生成，勿手改）----
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=["Python.Runtime.dll", "clr_loader/*", "pythonnet/*", "WebView2*", "e_sqlite3.dll"],
    name='WeComCompanion-onedir',
)
"""
    out = src[:i] + exe_block.rstrip() + "\n" + collect
    p = os.path.join(os.path.dirname(SPEC), "WeComCompanion-onedir.spec")
    io.open(p, "w", encoding="utf-8", newline="").write(out)
    return p

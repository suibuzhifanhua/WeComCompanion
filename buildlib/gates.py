# -*- coding: utf-8 -*-
"""buildlib.gates — 打包门禁与棘轮（自 build.py 逐字迁入，v1.0.65x 拆分）。

check_* 的实现在这里；main() 里的 `_run_gate(...)` 调用行是接线锁
（tests/test_build_guard.py），调用点保持在 build.py 的 main() 中。
"""
import io
import os
import subprocess
import sys

from buildlib.env import BASE

# 打包门禁的扫描范围：与 README「标准验证栈」第 1 步 py_compile 的清单一致，
# 外加 build.py/tools（v1.0.632 B8：门禁脚本自己也在闸门里——它坏了表现为"闸门静默放行"，
# 比被检查的代码坏更难发现）
_LINT_TARGETS = [
    "backend.py", "core_utils.py", "llm_client.py", "security_ops.py",
    "build.py", "buildlib", "tools",
    "core", "services", "desktop", "routes", "agent", "agent_tools", "wecom_bridge",
]
# v1.0.647：顶层散件归位进子包（security.py→core/secrets.py、model_bench.py 与
# usage_stats.py→services/、skill_hub_data.py 与 skill_versions.py→agent_tools/），
# 子包目录本身已在扫描范围里，单独条目随之撤销。

# v1.0.632（B8）：硬拦截的"静态噪声"类别（按 pyflakes 的英文原文匹配，它没有输出码字段）
_NOISE_KEYS = ("is assigned to but never used",        # F841 死赋值
               "redefinition of unused",               # F811 同名被重复定义/重复 import
               "f-string is missing placeholders")     # F541 插值已被删空的 f-string


def check_undefined_names():
    """v1.0.588 打包门禁（硬拦截）：pyflakes F821「未定义名」检查。

    背景：历次模块拆分（v1.0.402 拆 core_utils、v1.0.523-7 拆 loop.py）都有把
    「符号留在原模块、调用方漏改 import」的情况带进主干，而这些 NameError 恰好
    都落在 try/except Exception 里，于是被吞成"功能静默失效"而非报错。实测一次性
    扫出 7 处：批量上下文精简永久空转（loop_ops KEEP_LEADING_MSGS）、带 schema 的
    模板运行必 500（json_utils _ensure_jsonschema）、气泡编辑必 500
    （conversations content）、模型导入成功后返回 500（b_models models/jid）、
    关闭自绘对话框缺「下次不再提示」复选框（close_guard BS_AUTOCHECKBOX/log_event）、
    不可写目录启动崩（config _fallback_dir）、wb._log 必抛（wecom_bridge cdp_client）。
    docs/代码优化建议报告_v1.0.561.md 结论"无致命/高危问题"正是漏了这一类。
    F821 没有合法的"以后再说"——命中即中止打包。其他 pyflakes 类别（F401 未用
    import 等）只汇总计数作软提醒，不参与拦截。"""
    here = BASE
    pyflakes_out = None
    _rc = None
    try:
        r = subprocess.run([sys.executable, "-m", "pyflakes"] + _LINT_TARGETS,
                           capture_output=True, cwd=here, timeout=300)
        _rc = r.returncode
        pyflakes_out = (r.stdout or b"").decode("utf-8", "replace") + \
                       (r.stderr or b"").decode("utf-8", "replace")
    except FileNotFoundError:
        pyflakes_out = None
    except Exception as e:  # noqa: BLE001
        print(f"[build] ❌ pyflakes 执行失败：{e}——未定义名门禁无法工作，按 fail-closed 中止打包")
        return -1
    # v1.0.626：这里原来是 `return 0`（=通过）。pyflakes 没装/自身崩掉时，"门禁没跑"
    # 被记成"门禁通过"，正是本项目反复踩的那类空闸门——换机器或 venv 缺包时，历史 7 类
    # NameError 静默失效这一整类回归会一路绿灯进正式包。
    # pyflakes 正常工作时 returncode 只会是 0（干净）或 1（有 finding）；其余都是异常。
    if pyflakes_out is None or _rc not in (0, 1):
        print("[build] ❌ pyflakes 没能正常执行（未安装或自身异常，returncode=%s）——"
              "F821 硬门禁无法工作，按 fail-closed 中止打包。"
              "先装好开发依赖：python -m pip install -r requirements-dev.txt" % _rc)
        return -1
    f821 = [ln for ln in pyflakes_out.splitlines() if "undefined name" in ln]
    other = [ln for ln in pyflakes_out.splitlines() if ln.strip() and ln not in f821]
    if f821:
        print("[check] ❌ 发现未定义名（F821）——这些调用会抛 NameError，"
              "若落在 except Exception 内则表现为功能静默失效：")
        for ln in f821:
            print(f"[check]   {ln}")
        print("[build] ❌ 未定义名门禁未通过——已中止打包（补齐 import 或修正符号后重试）")
        return len(f821)
    # v1.0.632（B8）：三类"静态噪声"从今也硬拦截。它们本身不报错，但每一条都指向
    # 一段**已经不成立的代码**：f-string 没有占位符（说明插值被删过、文案与变量脱钩）、
    # 重复定义/重复 import（后一份遮蔽前一份，测试 patch 前一份就失效）、
    # 赋值后从不使用（要么是真死代码，要么是"本来该用却忘了用"的逻辑漏写——
    # 本项目已实测过多次后者：判据算出来了却没进判定）。
    # 这一类原先只汇总计数不拦截，结果 6 个月里攒到 24 条没人回头看。
    noise = [ln for ln in other if any(k in ln for k in _NOISE_KEYS)]
    if noise:
        print(f"[check] ❌ 静态噪声 {len(noise)} 条（未用赋值 / 重复定义 / 无占位符 f-string）：")
        for ln in noise:
            print(f"[check]   {ln}")
        print("[build] ❌ 已中止打包——删掉这份不再成立的代码，或把该用的值用起来")
        return len(noise)
    rest = [ln for ln in other if ln not in noise]
    print(f"[check] 未定义名（F821）：0 ✓   静态噪声：0 ✓   "
          f"其他 pyflakes 提示：{len(rest)} 条（不拦截）")
    return 0


# 开发期目录：被 _SPEC_SCAN_PKGS 排除、且多数没有 __init__.py，冻结包里根本不存在
_DEV_ONLY_ROOTS = ("tools", "tests", "docs", "js", "css")


def _iter_lint_files():
    """把 _LINT_TARGETS 展开成具体 .py 文件（目录跳过 __pycache__）。

    几条 AST 门禁共用这一个展开口径：以前每条各写一遍 walk，加一个扫描目录要改多处，
    漏掉的那条就成了"看起来有、实际不扫"的空闸门。"""
    for tgt in _LINT_TARGETS:
        p = os.path.join(BASE, tgt)
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for f in filenames:
                    if f.endswith(".py"):
                        yield os.path.join(dirpath, f)
        elif os.path.isfile(p):
            yield p


def check_dev_only_imports():
    """打包门禁（硬拦截，v1.0.619）：生产代码不许 import 开发期目录。

    起因：services/self_repair.py 的前端补丁验证写的是 `from tools.check_frontend import
    check_js_file`。build 的 hiddenimports 扫描显式跳过 tools/，冻结包里这句必抛
    ModuleNotFoundError，而它外面套着 `except Exception` → 被翻译成
    「JS 语法校验失败：No module named 'tools'」——正式包永远修不了前端，日志还把结论
    指向 JS 语法。`--smoke` 也拦不住：它只导入**包里实际存在**的模块，缺的模块不在清单里。
    所以这一类只能在打包前按 import 语句静态判，命中即中止。返回违规条数。"""
    import ast as _ast
    here = BASE
    hits = []
    for fp in _iter_lint_files():
        try:
            tree = _ast.parse(io.open(fp, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            continue       # 语法错误由 pyflakes/py_compile 门禁负责，这里不重复报
        for node in _ast.walk(tree):
            roots = []
            if isinstance(node, _ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, _ast.ImportFrom) and not node.level and node.module:
                roots = [node.module.split(".")[0]]
            for r in roots:
                if r in _DEV_ONLY_ROOTS:
                    hits.append("%s:%d → %s" % (os.path.relpath(fp, here),
                                                node.lineno, r))
    if hits:
        print(f"[check] ❌ 生产代码引用了开发期目录（包里没有，运行期静默失效）{len(hits)} 处：")
        for h in hits[:20]:
            print("[check]   " + h)
        print("[build] ❌ 开发期目录引用门禁未通过——已中止打包（把实现移进随包发布的包，"
              "如 core/，再让 tools/ 里的脚本做壳 re-export）")
        return len(hits)
    print("[check] 开发期目录引用：0 ✓")
    return 0


# v1.0.632（B9）：巨型函数棘轮。不是" cleanliness 洁癖"——本项目历次静默失效
# （agent_loop 950 行、click_element 271 行）都发生在"没人敢读整个函数"的地方：
# 判据算出来没进判定、异常吞掉、权限口径手抄 5 份，全在这种函数里。
# 一次性拆完不现实（也不该为拆而拆），所以只立一条可执行的规矩：**只许少、不许多**。
# 基线是当天实测值；减少后把下面的数字改小，棘轮才会咬住新增。
GIANT_FUNC_MAX_LINES = 100
GIANT_FUNC_BASELINE = 32   # v1.0.648 实测 33；后续拆分再减 1，按棘轮提示下调到当天实测值

# v1.0.648：裸 except-pass 硬棘轮基线（当天 AST 实测，见 check_silent_pass）。
# 注意方向：v1.0.405 的旧基线是 194，二十多个版本里软提醒拦不住任何东西，
# 实测已涨到 257 —— 基线记的是"现在有多少"，不是"曾经有多少"，只许往下改。
BARE_EXCEPT_BASELINE = 255   # v1.0.652 实测 263；后续清理减 8（含 A2/B7 删死端点 api_repair_reports 的 1 条），按棘轮提示下调到当天实测值


def scan_giant_functions(files=None):
    """返回 [(行数, 文件, 函数名), ...]，按行数从大到小。语法错的跳过（另有门禁负责）。"""
    import ast as _ast
    out = []
    for fp in (list(_iter_lint_files()) if files is None else files):
        try:
            tree = _ast.parse(io.open(fp, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            continue
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                span = (getattr(node, "end_lineno", None) or node.lineno) - node.lineno + 1
                if span > GIANT_FUNC_MAX_LINES:
                    out.append((span, fp, node.name))
    out.sort(reverse=True)
    return out


def check_giant_functions():
    """打包门禁（棘轮）：>100 行的函数数量超过基线即中止打包。返回超出条数（0=通过）。"""
    rows = scan_giant_functions()
    rel = lambda p: os.path.relpath(p, BASE).replace("\\", "/")   # noqa: E731
    if len(rows) > GIANT_FUNC_BASELINE:
        print(f"[check] ❌ 巨型函数（>{GIANT_FUNC_MAX_LINES} 行）从 {GIANT_FUNC_BASELINE} 个"
              f"涨到 {len(rows)} 个——棘轮不认「这次例外」：")
        # 升序列出：刚跨过阈值的那几个（最可能是这次新增的）排在最前面，方便一眼定位
        for span, fp, name in sorted(rows)[:12]:
            print(f"[check]   {span}\t行\t{rel(fp)}\t{name}")
        if len(rows) > 12:
            print(f"[check]   …其余 {len(rows) - 12} 个（最大 {rows[0][0]} 行："
                  f"{rel(rows[0][1])} {rows[0][2]}）")
        print("[build] ❌ 已中止打包。要么把新写的这段拆成小块（helper/常量表），"
              "要么先拆掉一个旧的再把基线下调——基线只许往下改。")
        return len(rows) - GIANT_FUNC_BASELINE
    if len(rows) < GIANT_FUNC_BASELINE:
        print(f"[check] 巨型函数 {len(rows)} 个（基线 {GIANT_FUNC_BASELINE}）✓ ——"
              f"已减少 {GIANT_FUNC_BASELINE - len(rows)} 个，建议把 GIANT_FUNC_BASELINE "
              f"下调到 {len(rows)}，否则这条增量又被「免费」用掉了")
        return 0
    print(f"[check] 巨型函数（>{GIANT_FUNC_MAX_LINES} 行）：{len(rows)} 个 = 基线，未增长 ✓")
    return 0


def check_silent_pass():
    """v1.0.405（H3）→ v1.0.648 升级为**硬棘轮**：裸 except-pass 数超过基线即中止打包。

    背景：审查时全项目 758 处 except Exception、其中 194 处静默 pass——历次
    "静默失败"事故（OCR 降级无感/单实例失效/硬停止从未生效）多数早期信号被吃掉。
    v1.0.648 认下的账：这条检查从 405 活到 647 **一直只 print 不拦截**，于是计数
    从基线 194 一路涨到 257——"目标单调递减"写在 docstring 里，实际是反着的。
    现在与巨型函数同一套棘轮语义：超过基线就中止，减少就提示把基线下调。"""
    import ast
    root = BASE
    count, worst = 0, []
    skip_dirs = {"dist", "build", ".pytest_cache", "产物库", "__pycache__", "docs"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
            except Exception:  # noqa: BLE001
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 \
                        and isinstance(node.body[0], ast.Pass):
                    count += 1
                    if len(worst) < 6:
                        worst.append(f"{os.path.relpath(p, root)}:{node.lineno}")
    if count > BARE_EXCEPT_BASELINE:
        print(f"[check] ❌ 裸 except-pass 从 {BARE_EXCEPT_BASELINE} 条涨到 {count} 条——"
              "棘轮不认「这次例外」：")
        print("[check]   示例: " + "; ".join(worst))
        print("[build] ❌ 已中止打包。新增的静默必须改成 log_event 留痕，"
              "或清理掉旧的一批后把 BARE_EXCEPT_BASELINE 下调——基线只许往下改。")
        return count - BARE_EXCEPT_BASELINE
    if count < BARE_EXCEPT_BASELINE:
        print(f"[check] 裸 except-pass {count} 条（基线 {BARE_EXCEPT_BASELINE}）✓ ——"
              f"已减少 {BARE_EXCEPT_BASELINE - count} 条，建议把 BARE_EXCEPT_BASELINE "
              f"下调到 {count}，否则这条增量又被「免费」用掉了")
        return 0
    print(f"[check] 裸 except-pass 计数：{count} 条 = 基线，未增长 ✓")
    return 0


def check_silent_except():
    """v1.0.648 打包门禁：数据安全路径（断点/配置/token/台账/会话）上的静默 except
    必须二选一——log_event 留痕，或写 `pass  # silent-ok: <理由>` 声明。

    为什么不是"★3 数不能涨"：这条工具按上下文关键词分级，命中里本来就混着
    `except FileNotFoundError: pass` 这类"文件不存在就是正常"的路径；逼它们全留痕
    只会把日志灌满噪声。真正的风险是**没人看过的静默**，所以判据定为"未声明"。
    返回违规条数（0=通过）。"""
    sys.path.insert(0, os.path.join(BASE, "tools"))
    try:
        import scan_silent_except as _sc
    except Exception as e:  # noqa: BLE001
        print(f"[build] ❌ 已中止打包：静默 except 扫描器无法导入（门禁本身失效）：{e}")
        return 1
    bad = _sc.violations()
    if bad:
        print(f"[check] ❌ 数据安全路径有 {len(bad)} 处静默 except 既不留痕也不声明：")
        for _lvl, p, ln, txt, _d in bad[:12]:
            print(f"[check]   {p}:{ln}  {txt}")
        print("[build] ❌ 已中止打包。改成 log_event(...)，或在 pass 后写 "
              "`# silent-ok: 为什么这里静默是对的`。")
        return len(bad)
    print("[check] 数据安全路径静默 except：0 处未声明 ✓")
    return 0


def check_frontend_structure(force):
    """v1.0.373：打包前前端结构校验（防同类错误复发关卡 2）——index.html 的
    div 配对/重复 id/CSS 花括号/JS 语法任一不合格直接中止打包。此前
    index.html 结构破坏连续 3 次走到用户手里（CSS 错位/删块残留 </div>），
    坏结构必须拦在 exe 之前，而不是等用户截图。

    返回 True=放行；False=中止打包（main 返回 1）。"""
    try:
        r = subprocess.run([sys.executable, os.path.join(BASE, "tools", "check_frontend.py"), BASE],
                           capture_output=True, timeout=180)
        out = (r.stdout or b"").decode("utf-8", "replace")
        print(out.strip() or "[check] （无输出）")
        if r.returncode != 0:
            print("[build] ❌ 前端结构校验未通过——已中止打包（修复上方问题后重试）")
            return False   # v1.0.652（审计 P1-13）：门禁失败必须非零退出——旧裸 return=0，
                           # CI/一键打包链把"拦住了"当"构建成功"继续用旧 exe 部署
    except Exception as e:  # noqa: BLE001
        # v1.0.652（审计 D8）：门禁自身异常＝拒绝打包（fail-closed）——外层兜异常
        # 不等于通过；--force 显式放行。
        if force:
            print(f"[build] ⚠️ 结构校验脚本执行失败（--force 放行）：{e}")
            return True
        print(f"[build] ❌ 结构校验脚本执行失败（fail-closed，--force 可放行）：{e}")
        return False
    return True

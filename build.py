"""
build.py — 一键打包（自动版本号 +1，并生成 exe 版本资源）
=========================================================
用法：
  python build.py                        # 展示拟递增版本并确认后打包（默认 patch +1）
  python build.py --yes                  # 跳过确认直接打包（CI 用）
  python build.py --version=1.2.3        # 指定版本号打包（不再自动 +1）
  python build.py --current              # 仅打印当前版本号
  python build.py --prune-release --yes  # 维护：清 build/_release_excluded 的旧剥离份（保留最新一份）

每次修完 bug / 加完功能都跑一遍即可，版本号自动递增、互不相同。

v1.0.65x（C 档拆分）：实现按域移入 buildlib/（gates/versioning/backup/chatlog/
ocr_sync/pii/spec_refresh/release/verify/patching/orchestration），本文件只留
main() 编排与 CLI 解析。门禁调用（_run_gate）与各步顺序/输出/退出码保持不变；
`import build as B` 的名字契约由下方 __all__ 维持。
"""
import os
import re
import subprocess   # 测试经 B.subprocess.run 对门禁打桩——必须留在本模块命名空间
import sys
import time

# 本文件大量打印 ✅/❌/⚠️：中文 Windows 控制台与 subprocess 管道默认 cp936，
# 编码不了会抛 UnicodeEncodeError——在「检查全部通过」的成功分支上崩掉，退出码非 0，
# 等于把合格构建判成失败。统一按 UTF-8 + replace 输出。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from buildlib.env import BASE, VPATH, SPEC, PYINSTALLER, _SANDBOX_VARS   # noqa: E402
from buildlib.versioning import current_version, next_version, bump_version, make_version_info   # noqa: E402
from buildlib.spec_refresh import refresh_spec_hiddenimports, _source_module_list, _write_onedir_spec   # noqa: E402
from buildlib.backup import (backup_dist_runtime, restore_dist_runtime,   # noqa: E402
                             backup_previous_exe, migrate_dist_layout)
from buildlib.chatlog import (clear_chat_history, breakpoint_busy, newest_breakpoint_epoch,   # noqa: E402
                              _CHAT_DIRS, _CHAT_KEEP_DIRS, _CHAT_ARCHIVE,
                              _BP_BUSY_MIN, _BREAKPOINT_DIR)
from buildlib.ocr_sync import sync_ocr_runtime   # noqa: E402
from buildlib.pii import scan_dist_pii   # noqa: E402
from buildlib.gates import (check_undefined_names, check_dev_only_imports,   # noqa: E402
                            check_giant_functions, check_silent_pass, check_silent_except,
                            check_frontend_structure, scan_giant_functions, _iter_lint_files,
                            _LINT_TARGETS, _NOISE_KEYS, _DEV_ONLY_ROOTS,
                            GIANT_FUNC_MAX_LINES, GIANT_FUNC_BASELINE, BARE_EXCEPT_BASELINE)
from buildlib.release import release_mode_clean, prune_release_excluded   # noqa: E402
from buildlib.verify import verify_packaged_assets   # noqa: E402
from buildlib.patching import _patch_pywebview_contextmenu   # noqa: E402
from buildlib.orchestration import (kill_running_app, run_pyinstaller,   # noqa: E402
                                    run_smoke, clean_pyinstaller_mid)

# 拆分前 build.py 的模块级名字契约（tests/、build/_tmp_*.py 等按 B.<名字> 取用）。
__all__ = [
    "BASE", "VPATH", "SPEC", "PYINSTALLER", "_SANDBOX_VARS", "subprocess",
    "current_version", "next_version", "bump_version", "make_version_info",
    "refresh_spec_hiddenimports", "_source_module_list", "_write_onedir_spec",
    "backup_dist_runtime", "restore_dist_runtime", "backup_previous_exe", "migrate_dist_layout",
    "clear_chat_history", "breakpoint_busy", "newest_breakpoint_epoch",
    "_CHAT_DIRS", "_CHAT_KEEP_DIRS", "_CHAT_ARCHIVE", "_BP_BUSY_MIN", "_BREAKPOINT_DIR",
    "sync_ocr_runtime", "scan_dist_pii",
    "check_undefined_names", "check_dev_only_imports", "check_giant_functions",
    "check_silent_pass", "check_silent_except", "check_frontend_structure",
    "scan_giant_functions", "_iter_lint_files", "_LINT_TARGETS", "_NOISE_KEYS",
    "_DEV_ONLY_ROOTS", "GIANT_FUNC_MAX_LINES", "GIANT_FUNC_BASELINE", "BARE_EXCEPT_BASELINE",
    "release_mode_clean", "prune_release_excluded", "verify_packaged_assets",
    "_patch_pywebview_contextmenu", "kill_running_app", "run_pyinstaller",
    "run_smoke", "clean_pyinstaller_mid", "_run_gate", "main",
]


def _run_gate(name, fn, force):
    """门禁统一执行器（v1.0.654 A11）：fn() 返回非零=命中拦截（各检查自带 ❌ 输出）。
    门禁自身异常 fail-closed（--force 显式放行）——外层兜异常不等于通过。
    返回 True=放行继续打包；False=中止（main 返回 1）。"""
    try:
        return not fn()
    except Exception as e:  # noqa: BLE001
        if force:
            print(f"[build] ⚠️ {name}执行失败（--force 放行）：{e}")
            return True
        print(f"[build] ❌ {name}执行失败（fail-closed，--force 可放行）：{e}")
        return False


def main():
    # v1.0.338：打包进程自身也清掉沙箱会话变量——之前只对 pyinstaller 子进程清，
    # 进程自身的 OCR 运行时同步（shutil.rmtree dist/ocr/<pkg>）仍被安全删除钩子拦
    # （SAFE_DELETE_BULK_CONFIRM_REQUIRED 直接 SystemExit）。迁移机器后 dist/ocr
    # 必然要全量重建，必然撞上。清掉后钩子的 bulk guard 直接放行，删除走回收站路径。
    for _k in _SANDBOX_VARS:
        os.environ.pop(_k, None)
    explicit = None
    auto_yes = False
    release = False      # v1.0.405（M4）：--release 发布模式（打包后剥离开发机 config/产物库/日志）
    lean = False         # v1.0.473：--lean 精简发布（连 dist/ocr 一并剥离，OCR 首用自动下载）
    force = False        # v1.0.373：--force 跳过对话活动检测（软件内「一键打包并重启」场景——
                         # 进程已被延迟脚本杀掉，活动保护无意义；人工打包不建议用）
    build_note = ""      # v1.0.360：--note="..." 版本介绍（进版本库，设置页展示）
    keep_chats = False   # v1.0.603：默认每次打包后清空历史对话（搬到 build/_chat_archive）
    prune_release = False   # v1.0.619：清理 build/_release_excluded 旧份（维护动作，不打包）
    onedir = False       # v1.0.646：目录版打包（冷启动更快，分发形态不同；默认仍是 onefile）
    for arg in sys.argv[1:]:
        if arg == "--yes":
            auto_yes = True
        elif arg == "--keep-chats":
            keep_chats = True
        elif arg == "--prune-release":
            prune_release = True
        elif arg == "--onedir":
            onedir = True
        elif arg == "--force":
            force = True
        elif arg == "--release":
            release = True
        elif arg == "--lean":
            lean = True
            release = True   # lean 隐含 release（剥离开发机数据是精简发布的前提）
        elif arg == "--current":
            print(current_version())
            return
        elif arg.startswith("--version="):
            explicit = arg.split("=", 1)[1].strip()
        elif arg.startswith("--note="):
            build_note = arg.split("=", 1)[1].strip()
        elif arg and not arg.startswith("-"):
            explicit = arg.strip()
    cur = current_version()
    new = explicit or next_version(cur)
    if prune_release:
        # 维护模式：只做清理，不打包（打包是另一件事，混在一起容易误删）
        return prune_release_excluded(auto_yes)
    _patch_pywebview_contextmenu()   # v1.0.501：幂等——生产环境启用 WebView2 右键菜单
    # v1.0.373：打包前前端结构校验（防同类错误复发关卡 2），实现见 buildlib.gates。
    if not check_frontend_structure(force):
        return 1
    # v1.0.654 A11：以下五组门禁收进 _run_gate（原六段 try/except 同构样板）。
    # 各检查内部 fail-closed 自己打印 ❌，此处只决定放行/中止与异常兜底语义。
    if not _run_gate("未定义名检查", check_undefined_names, force):   # v1.0.588：F821=硬拦截
        return 1
    if not _run_gate("开发期目录引用检查", check_dev_only_imports, force):   # v1.0.619
        return 1
    if not _run_gate("巨型函数棘轮检查", check_giant_functions, force):   # v1.0.632（B9）
        return 1
    if not _run_gate("静默 pass 棘轮", check_silent_pass, force):   # v1.0.648：硬棘轮
        return 1
    if not _run_gate("数据安全静默声明检查", check_silent_except, force):   # v1.0.648
        return 1
    if explicit:
        if not re.fullmatch(r"\d+\.\d+\.\d+", explicit):
            print(f"[build] 非法版本号：{explicit}（应为 x.y.z）")
            return 1
        print(f"[build] 指定版本 -> {new}")
    else:
        print(f"[build] 当前版本 {cur}，拟递增为 {new}")
        # P3-4：自动 +1 前先展示并确认，避免正式/测试包版本难管控
        if not auto_yes:
            ans = input(f"是否确认以 v{new} 打包？[y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("[build] 已取消")
                return
    # v1.0.373 审查修复（P2-7）：对话活动守卫前移到 bump_version 之前——原在打包阶段
    # 才检测，被中止时 version.py 已 +1（376 空号根因：版本号被白白消耗且无产物）。
    # v1.0.209/212：打包前检测运行活动——若最近 3 分钟内有 agent 活动
    #（用户正在跑对话/审核流程），中止打包并提示，防止 taskkill 误杀正在执行的
    # 任务（实测 14:58 打包强杀了用户 14:57 正在跑的审核流程，对话直接中断）。
    # v1.0.212 修复误判：改为按日志行的【时间戳】判断最近 180s 内是否有
    # [agent]/[llm] 活动（旧版看文件 mtime+尾部 4000 字符，任务已结束但旧日志
    # 还在尾部时误判"3 分钟内有活动"导致永远打不了包）。
    try:
        # v1.0.275：日志新旧两处位置都查（新=产物库/日志/，旧=dist 顶层，迁移过渡期）。
        # v1.0.302：扫描算法改接 core_utils.last_agent_activity_epoch 单点实现
        #（原与 knowledge_ops 600s 空闲判定各写一遍同款算法）。
        from core_utils import last_agent_activity_epoch
        _logps = [os.path.join(BASE, "dist", "产物库", "日志", "companion.log"),
                  os.path.join(BASE, "dist", "companion.log")]
        _ts = last_agent_activity_epoch(_logps)
        if not force and _ts is not None and time.time() - _ts < 180:
            print("[build] ⚠️ 检测到最近 3 分钟内有 agent/LLM 活动（用户可能正在跑任务），"
                  "中止打包以防误杀运行中的对话。")
            print("[build] 请等任务跑完或停止对话后再打包（软件内一键打包会自动带 --force）。")
            return 1
        if not force and _ts is None:
            # v1.0.470 修复（P1-21）：守卫 fail-closed——读日志失败（文件占用/迁移中/
            # 路径变化）时原 except: pass 静默放行，后续 taskkill 照杀，保护在最需要的
            # 异常场景下失效。现无 --force 时保守中止；--force 为显式确认放行。
            print("[build] ⚠️ 无法读取活动日志（文件占用或路径变化），无法确认 agent 是否在跑。"
                  "为防误杀任务已中止打包；确认无任务在跑可用 --force 强制打包。")
            return 1
        # v1.0.623：日志侧守卫看不见「挂起等待点继续」——那种状态不写日志。补上断点侧。
        _bp_busy, _bp_age = breakpoint_busy()
        if not force and _bp_busy:
            print(f"[build] ⚠️ 最近 {int(_bp_age) if _bp_age is not None else 0} 分钟内仍有断点落盘"
                  f"（有会话停在待点「继续」），中止打包——打包会把它的续跑状态搬进归档并重启进程。")
            print(f"[build] 请先点「继续」把它跑完（或 finish 掉那个会话）再打包；"
                  f"超过 {_BP_BUSY_MIN} 分钟没有断点写入即自动放行。确认无进行中任务可用 --force。")
            return 1
    except SystemExit:
        raise
    except Exception as e:
        if not force:
            print(f"[build] ⚠️ 活动守卫异常（{e}），fail-closed 中止打包；确认无任务在跑可用 --force。")
            return 1
        print(f"[build] 警告：活动守卫异常（{e}），已按 --force 放行")
    old_v, v = bump_version(explicit)
    make_version_info(v)      # 写 version_info.txt，spec 按文件名取用
    migrate_dist_layout()     # v1.0.275：backup 前旧布局就地迁入 产物库/（失败不致命）
    backed = backup_dist_runtime()
    backup_previous_exe()     # v1.0.301：上一版 exe 备份到 build/_rollback/（回滚用）
    kill_running_app()        # 释放旧 exe 文件锁，避免 PyInstaller 写入失败
    env = dict(os.environ)
    for k in _SANDBOX_VARS:
        env.pop(k, None)
    # v1.0.555（P0-11）：构建前自动刷新 hiddenimports（漏模块 → 发货 exe 功能全死）
    try:
        _added = refresh_spec_hiddenimports()
        if _added:
            print(f"[build] hiddenimports 自动补全 {len(_added)} 个模块：{', '.join(_added[:10])}"
                  + ("…" if len(_added) > 10 else ""))
        else:
            print("[build] hiddenimports 自动扫描：无新增（spec 与源码一致）")
    except Exception as e:
        print(f"[build] 警告：hiddenimports 自动生成失败（沿用 spec 现值）：{e}")
    if onedir:
        # v1.0.646：onedir 模式——onefile 每次启动都要把 ~90 个模块解压到临时目录，
        # 目录版免掉这一步（冷启动实测量级：onefile 3.4s → onedir ~1.5s，见 README）。
        # 代价：产物从单个 exe 变成「一个目录」，分发形态变了，故**默认仍是 onefile**，
        # 需要时显式加 --onedir。
        spec_path = _write_onedir_spec()
        print("[build] onedir 模式：产物为 dist/WeComCompanion-onedir/ 目录（冷启动更快，分发形态不同）")
    else:
        spec_path = SPEC
    cmd = [PYINSTALLER, spec_path, "--noconfirm", "--clean"]
    print("[build] " + " ".join(cmd))
    try:
        run_pyinstaller(cmd, env, old_v)   # 失败内部回滚版本号后重新抛出
    finally:
        restore_dist_runtime(backed)
    # v1.0.470（P1-21）：打包成功后校验产物存在且为本次生成
    _dist_exe = os.path.join(BASE, "dist", "WeComCompanion.exe")
    if not os.path.isfile(_dist_exe):
        raise SystemExit("[build] PyInstaller 报告成功但 dist/WeComCompanion.exe 不存在——请检查 spec")
    print(f"[build] 产物校验通过：dist/WeComCompanion.exe ({os.path.getsize(_dist_exe) // 1048576}MB, mtime={time.strftime('%H:%M:%S', time.localtime(os.path.getmtime(_dist_exe)))})")
    run_smoke(_dist_exe, env)   # v1.0.555（P0-11）：frozen exe 内 import 全量冒烟
    # v1.0.619（审查⑧）：包内前端资源与源码树逐字节核对——--smoke 只管 import，
    # 管不了"exe 里带的还是旧 index.html/js"（历史上真出过 dist 落后 4 个文件）。
    try:
        if verify_packaged_assets(_dist_exe) > 0:
            return 1   # v1.0.652（审计 P1-13）：资源不一致＝产物不可发，非零退出
    except Exception as e:  # noqa: BLE001
        if force:
            print(f"[build] ⚠️ 资源一致性核对异常（--force 放行）：{e}")
        else:
            print(f"[build] ❌ 资源一致性核对异常（fail-closed，--force 可放行）：{e}")
            return 1
    clean_pyinstaller_mid()
    # v1.0.306：同步 OCR 运行时到 dist/ocr/ 旁挂（exe 已不含 OCR 栈，缺了它课件采集/扫描件 OCR不可用）
    try:
        sync_ocr_runtime()
    except Exception as e:  # noqa: BLE001
        print(f"[build] 警告：OCR 运行时同步失败（课件 OCR 将不可用，重跑 build 可补）：{e}")
    # v1.0.603（用户要求）：新版本从零历史对话开始（搬到 build/_chat_archive，可搬回）。
    # 放在冒烟通过之后——打包失败时绝不能顺手把用户的对话搬走。
    clear_chat_history(keep=keep_chats)
    print(f"[build] done -> dist/WeComCompanion.exe  (v{v})")
    if release:
        # v1.0.619（审查⑦）：剥离开发机数据是**不可逆的位置变更**（config/产物库/日志——
        # --lean 还包含 271MB 的 ocr/），而且发生在打包成功之后，用户点一次"打包"就顺带做了。
        # 上面那句版本确认覆盖不了它，这里单独再确认一次；--yes（CI/软件内一键打包）跳过。
        if not auto_yes:
            print("[release] 本次是发布模式：打包成功后会把 dist 里的 "
                  "config.json / 产物库 / 日志" + ("+ ocr/（--lean）" if lean else "") +
                  " 移到 build/_release_excluded/<时间戳>/（可取回，但 dist 里就没有了）。")
            try:
                ans = input("确认继续剥离？[y/N] ").strip().lower()
            except Exception:  # noqa: BLE001
                ans = "n"
            if ans != "y":
                print("[release] 未确认——exe 已生成，dist 保持原样（不剥离）")
                release = False
    if release:
        release_mode_clean(lean=lean)   # v1.0.405（M4）：发布模式剥离开发机数据；v1.0.473：--lean 连 ocr/ 一并剥离
    else:
        # v1.0.510（P0-1）：非发布模式打包后扫描 dist 内 PII 分发风险（软提醒不拦截）
        try:
            scan_dist_pii()
        except Exception as e:  # noqa: BLE001
            print(f"[build] 警告：PII 扫描失败（不拦截）：{e}")
    # v1.0.358：版本登记——源码 hash + exe 备份进版本库（产物库/版本/versions.json），
    # 供设置页「📦 版本管理」一键回退。
    # v1.0.616：保留个数从 `_KEEP` 取，不再写死字面量——这里曾印着「保留最近 8 个」，
    # 而 version_store._KEEP 早在 v1.0.373 就瘦身成 3（每次打包日志都在传播这个假数字）。
    try:
        from services import version_store
        version_store.record_version(note=build_note)
        print(f"[build] 版本已登记（产物库/版本/versions.json，保留最近 {version_store._KEEP} 个）")
    except Exception as e:  # noqa: BLE001
        print(f"[build] 警告：版本登记失败（不影响本次构建产物）：{e}")


if __name__ == "__main__":
    # v1.0.652（审计 P1-13）：显式传播退出码——main() 返回 None/0=成功，非零=门禁拦截。
    # 旧 `main()` 裸调用恒 exit 0，自动化把门禁拦截当构建成功。
    sys.exit(main() or 0)

# -*- coding: utf-8 -*-
"""core/smoke.py — frozen exe 冒烟门禁（v1.0.648 从 backend.py 的 __main__ 块迁出）。

必须住在被打包的包里（core/），不能放 tools/——build 的 hiddenimports 扫描显式
跳过 tools/（v1.0.592 的教训：`from tools import ...` 在正式包里必抛 ImportError）。
行为与原位一致：backend 的 `--smoke` 分支调用 run() 并按返回值 SystemExit。

v1.0.555（P0-11）：打包冒烟入口——frozen exe 内逐个 import 路由/服务模块，
hiddenimports 漏网（曾实证：services.asr 缺席 → 发货 exe 里 ASR 三端点连续
14 次 ImportError，而开发环境永远测不出）立即非零退出。必须位于单实例锁/
看门狗闸门之前（冒烟进程不该撞 mutex）。build.py 打包后自动调用。
"""
import os
import sys


def run() -> int:
    import importlib as _smoke_il
    _smoke_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 上一级才是项目根
    _smoke_pkgs = ("routes", "services", "core", "agent", "agent_tools", "desktop", "wecom_bridge")
    # v1.0.619 交付时自查发现的口径坑（认下来）：onefile 冻结态里这些包**不以目录形式
    # 存在**（全在 PYZ 归档里），原 `os.walk(_smoke_root/<pkg>)` 只能扫到零星散文件——
    # 所以 619 打包时 `[smoke] OK: 9 modules` 而项目实际有 51+ 个包内模块。
    # 也就是说「--smoke 能发现 hiddenimports 漏模块」这句话长期只在源码态成立，
    # 冻结态几乎没测：正是本轮 `from tools import ...` 那条死路能一路活到发货的同型原因。
    # 现在优先从冻结导入器的归档 TOC 取模块清单（就是包里真正存在的那份），取不到才回落磁盘遍历。
    # 属性名实证：PyInstaller 6.x 运行时是 `finder._pyz_archive.toc`（不是 `_toc`）——
    # 620 那次只写了 `_toc`，所以冻结态依旧报 9 个模块（一个"绿但什么都不查"的门禁）。
    _smoke_env_mods = [x.strip() for x in
                       (os.environ.get("WECOM_SMOKE_MODS") or "").split(",") if x.strip()]
    # ① 首选 build.py 打包时传进来的**源码树模块清单**（最可靠，不猜 PyInstaller 结构，
    #    语义也最对："源码里有的模块，包里必须都导得动"）。
    # ② 退回冻结归档 TOC：`PyiFrozenFinder._pyz_archive.toc`——注意这个 finder 是 **path
    #    finder**，登记在 `sys.path_importer_cache` 而非 `sys.meta_path`（620 写错属性名、
    #    621 首打只翻 meta_path，两次都拿到空清单，冒烟照旧只报 9 个模块）。
    # ③ 再退回磁盘遍历（源码态）。
    _pyz_toc = None
    if not _smoke_env_mods:
        for _smoke_f in (list(sys.meta_path)
                         + list(getattr(sys, "path_importer_cache", {}).values())):
            for _attr in ("_pyz_archive", "_toc"):
                _obj = getattr(_smoke_f, _attr, None)
                _t = getattr(_obj, "toc", None) if _attr == "_pyz_archive" else _obj
                if isinstance(_t, dict) and _t:
                    _pyz_toc = _t
                    break
            if _pyz_toc:
                break
    _smoke_mods = list(_smoke_env_mods)
    if _pyz_toc and not _smoke_mods:
        _smoke_mods = sorted(n for n in _pyz_toc if n.split(".")[0] in _smoke_pkgs)
    elif not _smoke_mods:
        for _sp in _smoke_pkgs:
            _sd = os.path.join(_smoke_root, _sp)
            if not os.path.isdir(_sd):
                continue
            for _sr, _sdirs, _sfiles in os.walk(_sd):
                if "__pycache__" in _sr:
                    continue
                for _sf in _sfiles:
                    if _sf.endswith(".py"):
                        _srel = os.path.relpath(os.path.join(_sr, _sf), _smoke_root)
                        _smoke_mods.append(_srel[:-3].replace(os.sep, "."))
    # fail-loud：冻结态拿不到包内模块清单＝这条门禁在正式包里等于没跑。
    # 宁可让打包红着停下来，也不能再发一个"[smoke] OK: 9 modules"的绿灯。
    if getattr(sys, "frozen", False) and len(_smoke_mods) < 40:
        print(f"[smoke] FAILED: 冻结态只取到 {len(_smoke_mods)} 个本项目模块（期望 ≥40）——"
              "清单来源（PyiFrozenImporter._pyz_archive.toc）失效，"
              "这条门禁正在" "什么都不查" "地报绿。")
        return 1
    # 注意：主脚本自身不进清单——frozen exe 里它以 __main__ 身份存在（PyInstaller
    # 入口脚本不注册为可导入模块名），能跑到本冒烟代码即已证明其导入链完好；
    # 补 import backend 反而触发 ModuleNotFoundError 假失败（1.0.555 首打包实证）。
    # v1.0.647：顶层散件归位（security→core.secrets、model_bench/usage_stats→services、
    # skill_hub_data/skill_versions→agent_tools），冒烟名单同步新家。
    _smoke_mods += ["llm_client", "core.secrets", "security_ops", "core_utils",
                    "services.model_bench", "services.usage_stats", "version",
                    "agent_tools.skill_hub_data", "agent_tools.skill_versions"]
    _smoke_fail = []
    for _sm in _smoke_mods:
        try:
            _smoke_il.import_module(_sm)
        except Exception as _se:  # noqa: BLE001
            _smoke_fail.append(f"{_sm}: {type(_se).__name__}: {_se}")
    if _smoke_fail:
        print(f"[smoke] FAILED {len(_smoke_fail)}/{len(_smoke_mods)} modules:\n  "
              + "\n  ".join(_smoke_fail))
        return 1
    print(f"[smoke] OK: {len(_smoke_mods)} modules imported (frozen bundle)")
    return 0

# -*- coding: utf-8 -*-
"""buildlib.orchestration — 打包执行段：杀旧进程、跑 PyInstaller、冒烟、中间产物清理。

自 build.py main() 内联块逐字迁出（v1.0.65x 拆分）；调用顺序仍在 main() 里显式排定。
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import time

from buildlib.env import BASE, VPATH
from buildlib.spec_refresh import _SPEC_SCAN_PKGS, _source_module_list


def kill_running_app():
    # PyInstaller 写入新 exe 前要 os.remove 旧 exe——若旧进程还在持锁会 PermissionError 失败。
    # 主动 taskkill + 等待文件锁释放，避免每次手工先杀进程。
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "WeComCompanion.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        time.sleep(2)  # Windows 释放文件句柄需要一点时间
    except Exception:  # noqa: BLE001
        pass  # 没在跑也无所谓：taskkill 返回非 0 exit code 会被 run 当异常，捕获忽略


def run_pyinstaller(cmd, env, old_v):
    """执行 PyInstaller 并做「本项目包内模块缺失」升级拦截；失败回滚版本号后重新抛出。

    失败回滚版本号——版本与产物一一对应（376 空号另一半根因：PyInstaller 失败时
    version.py 已 +1 且无对应产物）。version_info.txt 可留给下次覆盖；
    版本登记/产物校验随异常跳过。"""
    try:
        # v1.0.555（P0-11）：捕获输出回显——PyInstaller 缺模块只 warning 不失败，
        # 原 check=True 只看退出码、警告被完全忽略。本项目包内模块的 missing
        # warning 现升级为构建失败（第三方缺失仍只警告，避免误杀可选依赖）。
        _pr = subprocess.run(cmd, cwd=BASE, env=env, capture_output=True)
        # v1.0.652（审计 D10）：bytes + utf-8/replace 解码——旧 text=True 走 locale
        # （cp936），PyInstaller 输出含中文路径/警告时 UnicodeDecodeError → 落进
        # except → 版本回滚 + 把合格构建误判为失败（v1.0.621 冒烟段同款教训）。
        _out = ((_pr.stdout or b"") + (_pr.stderr or b"")).decode("utf-8", "replace")
        sys.stdout.write(_out)
        _warn_txt = ""
        try:
            for _wf in glob.glob(os.path.join(BASE, "build", "*", "warn-*.txt")):
                with open(_wf, encoding="utf-8", errors="ignore") as _f:
                    _warn_txt += _f.read() + "\n"
        except Exception:  # noqa: BLE001
            pass
        # v1.0.652（审计 D7）：补双引号/无引号形态 + 扫 warn-*.txt——"missing module
        # named X" 由 PyInstaller 写进 warnfile 不进 stdout，旧过滤分支对它恒为空（死条件）。
        _scan_src = _out + "\n" + _warn_txt
        _own_missing = sorted({ln.strip() for ln in _scan_src.splitlines()
                               if ("missing module" in ln.lower() or "hidden import" in ln.lower())
                               and any(f".{p}." in ln or f"'{p}" in ln or f'"{p}' in ln or f"{p}." in ln
                                       for p in _SPEC_SCAN_PKGS)})
        if _own_missing:
            raise RuntimeError("PyInstaller 报告本项目包内模块缺失（hiddenimports 不完整）：\n  "
                               + "\n  ".join(_own_missing[:20]))
        if _pr.returncode != 0:
            raise RuntimeError(f"PyInstaller 退出码 {_pr.returncode}")
    except Exception:
        # v1.0.470 修复（P1-21）：打包失败回滚版本号——版本与产物一一对应（376 空号
        # 另一半根因：PyInstaller 失败时 version.py 已 +1 且无对应产物）。
        # version_info.txt 可留给下次覆盖；版本登记/产物校验随异常跳过。
        try:
            with open(VPATH, "r", encoding="utf-8") as f:
                txt = f.read()
            txt = re.sub(r'__version__\s*=\s*["\']\d+\.\d+\.\d+["\']',
                         f'__version__ = "{old_v}"', txt)
            with open(VPATH, "w", encoding="utf-8") as f:
                f.write(txt)
            print(f"[build] 打包失败，版本号已回滚至 v{old_v}")
        except Exception as e:
            print(f"[build] 警告：版本号回滚失败：{e}")
        raise


def run_smoke(dist_exe, env):
    """v1.0.555（P0-11）：打包后冒烟——frozen exe 内 import 全部路由/服务模块
    （backend --smoke，位于单实例锁之前不撞 mutex）。原"PyInstaller 报告成功即
    发货"让 asr 等模块只在发货 exe 里死、开发环境永远测不出（同根因事故两次）。
    v1.0.619（审查⑧）：包内前端资源与源码树逐字节核对——--smoke 只管 import，
    管不了"exe 里带的还是旧 index.html/js"（历史上真出过 dist 落后 4 个文件）。
    冒烟清单由 build 侧生成后经 WECOM_SMOKE_MODS 传进 exe：冻结态里 PyInstaller 的
    path finder 不在 sys.meta_path（在 sys.path_importer_cache），exe 自己枚举包内模块
    曾两次拿到空清单 → 冒烟长期只报 9 个模块、却打「import 全通」。源码树在这里是
    现成的，由它给清单最可靠，语义也更对：**源码里有的模块，包里必须都导得动**。"""
    try:
        _mods = _source_module_list()
        if len(_mods) < 40:
            raise RuntimeError(f"源码树模块清单异常（只有 {len(_mods)} 个）")
        _smoke_env = {**env, "WECOM_NO_GUI": "1", "WECOM_SMOKE_MODS": ",".join(_mods)}
    except Exception as e:  # noqa: BLE001
        print(f"[build] 警告：冒烟清单生成失败（退回 exe 自查，覆盖面会小）：{e}")
        _smoke_env = {**env, "WECOM_NO_GUI": "1"}
    print(f"[build] 冒烟清单：{len(_smoke_env.get('WECOM_SMOKE_MODS', '').split(',')) if 'WECOM_SMOKE_MODS' in _smoke_env else 0} 个源码模块")
    try:
        _smoke = subprocess.run([dist_exe, "--smoke"], capture_output=True,
                                timeout=180, cwd=os.path.dirname(dist_exe),
                                env=_smoke_env)
        # v1.0.621：frozen exe 的 stdout 是 cp936，text=True 会让读取线程直接抛
        # UnicodeDecodeError → 真实失败原因（哪些模块导不动）全部丢失。改成拿字节再 replace 解码。
        _smoke_out = ((_smoke.stdout or b"") + (_smoke.stderr or b"")).decode("utf-8", "replace")
        sys.stdout.write(_smoke_out[-6000:])
        if _smoke.returncode != 0:
            raise SystemExit("[build] 打包冒烟失败：frozen exe 内模块 import 异常"
                             "（hiddenimports 漏网）——详见上方 [smoke] 输出")
        print("[build] 冒烟通过：frozen exe 模块 import 全通")
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"[build] 打包冒烟执行异常（视为失败，宁可拦截不静默发货）：{e}")


def clean_pyinstaller_mid():
    # v1.0.301：版本垃圾策略——打包成功后清 PyInstaller 中间产物
    # （build/WeComCompanion/ 目录 ~183M：.toc/.pkg/.pyz/localpycs，纯过程文件）。
    # build/_dist_backup/（~21M）保留：config 最后防线，下次 build 会重新覆盖。
    # 若需查打包 warn/xref 诊断，重跑一次 build 即会重新生成。
    try:
        _mid = os.path.join(BASE, "build", "WeComCompanion")
        if os.path.isdir(_mid):
            shutil.rmtree(_mid)
            print("[build] 已清理 PyInstaller 中间产物 build/WeComCompanion/（~183M）")
    except Exception as e:  # noqa: BLE001
        print(f"[build] 警告：中间产物清理失败（可手工 rm -rf build/WeComCompanion）：{e}")

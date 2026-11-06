# -*- coding: utf-8 -*-
"""buildlib.backup — dist 运行时文件备份/还原、上一版 exe 回滚备份、旧布局迁移。

自 build.py 逐字迁入（v1.0.65x 拆分）。
"""
import os
import shutil

from buildlib.env import BASE

# 运行时产物：用户配置/日志/产物库——打包或手动清理 dist 时绝不能丢（P0 教训：
# 2026-08-27 手动 rm -rf dist 删掉 config.json 导致模型配置丢失，agent 降级运行）
DIST_PRESERVE = ("config.json", "companion.log", "companion.log.1", "companion.log.2",
                 "companion.log.3", "companion.log.4", "companion.log.5", "产物库",
                 "logs")  # logs/ = Token 用量独立存档（v1.0.252）
_BACKUP_DIR = os.path.join(BASE, "build", "_dist_backup")


def backup_dist_runtime():
    """打包前把 dist/ 下的运行时文件复制到 build/_dist_backup/，返回备份清单。

    实现：用 shutil.copytree(..., dirs_exist_ok=True) 覆盖式合并，全程无 rmtree/rename。
    避免沙箱/安全删除钩子劫持 shutil.rmtree / os.remove 时触发 SAFE_DELETE_BULK_CONFIRM_REQUIRED
    直接中止进程（v1.0.172/v1.0.173 两次踩过）。备份目录可能被多次运行污染（混合多个版本的
    运行时文件），但 restore 时只把本次实际备份过的 name 写回 dist，且 src 是最新版本，可接受。

    防御 config.json 丢失：dist/config.json 体积异常小（<500 bytes 视作"空默认"）但
    backup 里已有正常大小（>500 bytes）的有效配置时，保留 backup 不覆盖——
    防止 PyInstaller --clean 后某次 backup 的 src 是空默认，进而 restore 把空写回 dist。
    v1.0.174 实测：build 流程会让 dist/config.json 莫名变 322 bytes 空默认（原因未明），
    必须用"backup 是最后防线"策略避免用户配置（DaoPro 等）无声丢失。
    """
    dist = os.path.join(BASE, "dist")
    backed = []
    if not os.path.isdir(dist):
        return backed
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    for name in DIST_PRESERVE:
        src = os.path.join(dist, name)
        if not os.path.exists(src):
            continue
        # 防御：src 配置是空默认但 backup 里有有效配置 → 保留 backup 不覆盖
        if name == "config.json":
            try:
                src_size = os.path.getsize(src)
                dst_path = os.path.join(_BACKUP_DIR, name)
                dst_size = os.path.getsize(dst_path) if os.path.exists(dst_path) else 0
                if src_size < 500 and dst_size > src_size + 100:
                    print(f"[build] 防御：dist/config.json 疑似空默认（{src_size} bytes）但 backup 有有效配置（{dst_size} bytes），保留 backup 不覆盖")
                    continue
            except OSError:  # noqa: BLE001
                pass
        dst = os.path.join(_BACKUP_DIR, name)
        try:
            if os.path.isdir(src):
                # v1.0.225：产物库备份排除截图垃圾（screenshot_*.png 是前端预览轮询
                # 的落盘产物，11176 张/2.3GB 实测堆积；备份/还原会把它反复搬回 dist）。
                # 预览截图默认已不落盘（routes/wecom.py save=1 才存），存量清理后
                # 不会再产生；这里防止 build 备份把历史残留/手工备份目录再还原回来。
                _ignore = shutil.ignore_patterns("screenshot_*.png", "_截图备份_*")
                shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore)
            else:
                shutil.copy2(src, dst)
            backed.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"[build] 警告：备份 dist/{name} 失败：{e}")
    return backed


def restore_dist_runtime(backed):
    """打包后把备份的运行时文件还原回 dist/（exe 本身不还原）。
    同 backup：覆盖式合并，全程无 rmtree/rename。
    仅当 backup 的源是有效配置（>500 bytes）时 restore config.json——避免回填了空默认。

    主动恢复 config.json：backup 阶段可能因防御逻辑（src 是空默认）跳过 config.json 写入，
    此时 backed 列表里没有 config.json；restore 阶段检测 backup/config.json 有效且 dist 是空默认
    时主动 copy2 回填，把"backup 是最后防线"策略闭环。
    """
    # v1.0.652（审计 D6）：主动恢复 config.json **前移**到 backed 判空之前——旧
    # `if not backed: return` 把它短路：backup 防御分支跳过 config.json 且其余
    # DIST_PRESERVE 都不存在时（backed==[]），"backup 是最后防线"恰恰最需要生效
    # 的场景反而不执行。
    dist = os.path.join(BASE, "dist")
    os.makedirs(dist, exist_ok=True)
    # 主动恢复 config.json（独立于 backed 列表）
    try:
        backup_cfg = os.path.join(_BACKUP_DIR, "config.json")
        dist_cfg = os.path.join(dist, "config.json")
        if os.path.exists(backup_cfg):
            bsz = os.path.getsize(backup_cfg)
            dsz = os.path.getsize(dist_cfg) if os.path.exists(dist_cfg) else 0
            if bsz > 500 and dsz < 500:
                shutil.copy2(backup_cfg, dist_cfg)
                print(f"[build] 主动恢复 config.json：backup {bsz} bytes → dist {dsz} bytes")
                if "config.json" not in backed:
                    backed.append("config.json")
    except Exception as e:  # noqa: BLE001
        print(f"[build] 警告：主动恢复 config.json 失败：{e}")
    if not backed:
        return
    for name in backed:
        src = os.path.join(_BACKUP_DIR, name)
        dst = os.path.join(dist, name)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            print(f"[build] 已还原 dist/{name}")
        except Exception as e:  # noqa: BLE001
            print(f"[build] 警告：还原 dist/{name} 失败：{e}")


def migrate_dist_layout():
    # v1.0.275：backup 前把 dist 里的旧布局散落产物就地迁入 产物库/（此时应用应已退出、文件未占用；
    # 迁移失败不致命——只是本次备份沿用旧布局，下次启动时 app 内迁移兜底）。
    try:
        from core_utils import migrate_legacy_layout
        _m = migrate_legacy_layout(os.path.join(BASE, "dist"))
        if _m:
            print(f"[build] dist 旧布局产物已迁入 产物库/：{_m}")
    except Exception as e:  # noqa: BLE001
        print(f"[build] 警告：dist 旧布局迁移失败（沿用现状继续）：{e}")


def backup_previous_exe():
    # v1.0.301：版本垃圾策略（用户约定：最新是 3 → 保留 2 的版本文件，删 1 及更早垃圾）。
    # build 前把当前 dist exe（即"上一版"）备份到 build/_rollback/——只留一份、每次覆盖，
    # 任何时刻 _rollback 里都是紧邻本次的版本，新版出问题可直接拷回 dist 回滚。
    _rollback_dir = os.path.join(BASE, "build", "_rollback")
    _old_exe = os.path.join(BASE, "dist", "WeComCompanion.exe")
    if os.path.exists(_old_exe):
        try:
            os.makedirs(_rollback_dir, exist_ok=True)
            _rb_exe = os.path.join(_rollback_dir, "WeComCompanion.exe")
            if os.path.exists(_rb_exe):
                os.remove(_rb_exe)  # 只留最近上一版，旧的回滚备份覆盖
            shutil.copy2(_old_exe, _rb_exe)
            print(f"[build] 上一版 exe 已备份 -> build/_rollback/ "
                  f"({os.path.getsize(_rb_exe) // 1048576}MB，回滚用)")
        except Exception as e:  # noqa: BLE001
            print(f"[build] 警告：上一版 exe 回滚备份失败（不影响打包）：{e}")

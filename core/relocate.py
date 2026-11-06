# -*- coding: utf-8 -*-
r"""core.relocate — 便携副本自动归位（v1.0.559，用户需求；v1.0.561 交互改版）

问题：exe 直接放桌面/下载/磁盘根目录运行时，config.json、产物库/、ocr/ 等
运行时文件全部生成在该目录，污染桌面/根目录。

方案（v1.0.561 按用户要求对齐 IPv6Guardian 样式）：启动早期（单实例锁之前）
检测 exe 所在目录是否为"危险位置"——桌面 / 下载 / 磁盘根目录。是则：
  ① 在当前位置创建「企微伴侣」文件夹（桌面 → 桌面\企微伴侣；C:\ → C:\企微伴侣）；
  ② copy 自身 exe 过去，并把已生成的运行时伴随物（config.json、产物库/、ocr/）
     move 过去（跨盘 shutil.move 自动降级 copy+delete）；
  ③ 弹系统信息框：「已把 企微伴侣 搬移到: <新路径>  请从新位置启动 企微伴侣，
     并删除原来位置的旧副本。」——用户点「确定」后本进程退出。
  ④ 用户从新文件夹启动，原位旧副本可删（进程已退出，不再占用）。

安全口径：
  - 仅打包版（frozen exe）；开发模式直接跳过
  - 目标文件夹里已有本软件 exe → 视为已归位，跳过迁移正常启动（幂等）
  - copy/移动任一步失败 → 放弃迁移原地正常运行（绝不阻断使用）
  - 系统关键目录（Windows/Program Files）不做迁移判定
"""
import os
import shutil
import sys

APP_FOLDER_NAME = "企微伴侣"
ENV_MIGRATE_BAK = "WECOM_MIGRATE_BAK"

# 已知会生成在 exe 旁的运行时伴随物（存在才搬）
_COMPANIONS = ("config.json", "产物库", "ocr")

_SYSTEM_DIR_MARKERS = ("windows", "program files", "program files (x86)",
                       "programdata", "system volume information", "$recycle.bin")


def _known_folder(csidl):
    """SHGetFolderPathW 取桌面等真实路径（失败返回空串）。
    v1.0.561 修复：原实现用 SHGetKnownFolderPath + GUID 字符串传参——该 API 需要
    GUID 结构体指针，字符串传参必失败 → 桌面路径永远解析为空 → 迁移判定永不触发
    （v1.0.560 用户实测"桌面打开直接运行"。改用 CSIDL 整数版 SHGetFolderPathW）。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        if shell.SHGetFolderPathW(None, int(csidl), None, 0, buf) == 0:
            return buf.value or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _desktop_dirs():
    # CSIDL_DESKTOPDIRECTORY=0x0010（用户桌面） CSIDL_COMMON_DESKTOPDIRECTORY=0x0019（公共桌面）
    # OneDrive 重定向桌面时该 API 返回重定向后的真实路径，与资源管理器一致
    return [d for d in (_known_folder(0x0010), _known_folder(0x0019)) if d]


def _downloads_dirs():
    # 下载文件夹无 CSIDL：用 %USERPROFILE%\Downloads（存在才认；极少有人改走其它盘）
    prof = os.environ.get("USERPROFILE") or ""
    d = os.path.join(prof, "Downloads") if prof else ""
    return [d] if d and os.path.isdir(d) else []


def _is_drive_root(d):
    drive, rest = os.path.splitdrive(os.path.abspath(d))
    return bool(drive) and os.path.normpath(rest or "\\") == "\\"


def needs_relocation(exe_dir, desktop_dirs=None, downloads_dirs=None):
    """判定 exe 是否处于应归位的目录。返回 (should, reason, target_dir)。
    纯函数（路径参数可注入），便于测试。"""
    exe_dir = os.path.normpath(os.path.abspath(exe_dir))
    low = exe_dir.lower()
    # 系统关键目录一律不动（也基本不可能发生：没写权限）
    if any(seg in low for seg in _SYSTEM_DIR_MARKERS):
        return False, "", ""
    reason = ""
    if _is_drive_root(exe_dir):
        reason = "磁盘根目录"
    if not reason:
        for d in (desktop_dirs if desktop_dirs is not None else _desktop_dirs()):
            if os.path.normcase(os.path.normpath(d)) == low:
                reason = "桌面"
                break
    if not reason:
        for d in (downloads_dirs if downloads_dirs is not None else _downloads_dirs()):
            if os.path.normcase(os.path.normpath(d)) == low:
                reason = "下载文件夹"
                break
    if not reason:
        return False, "", ""
    target = os.path.join(exe_dir, APP_FOLDER_NAME)
    # 目标里已有本软件 exe → 已归位过（用户又复制了一份到外面），不重复折腾
    if os.path.isfile(os.path.join(target, os.path.basename(sys.executable or "WeComCompanion.exe"))):
        return False, "", ""
    return True, reason, target


def perform_relocation(exe_path, target_dir):
    """执行迁移。返回 (new_exe_path, moved_items, errors, bak)。
    copy 自身 → move 伴随物。v1.0.561（用户要求交互形式）：不再 rename 旧副本——
    原位 exe 在本进程退出后即可直接删除，配合提示"删除原来位置的旧副本"。
    任何伴随物失败不致命（记录 errors 继续）；copy 自身失败则整体放弃。"""
    moved, errors = [], []
    os.makedirs(target_dir, exist_ok=True)
    new_exe = os.path.join(target_dir, os.path.basename(exe_path))
    # ① copy 自身（运行中的 exe 不能移动，但可以复制）
    try:
        shutil.copy2(exe_path, new_exe)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"复制程序文件失败（放弃迁移，原地继续运行）：{e}")
    # ② 伴随物逐个搬（move 跨盘自动降级 copy+delete）
    src_dir = os.path.dirname(exe_path)
    for name in _COMPANIONS:
        src = os.path.join(src_dir, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(target_dir, name)
        try:
            shutil.move(src, dst)
            moved.append(name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {str(e)[:80]}")
    return new_exe, moved, errors, ""


def _notify_moved(new_exe, reason=""):
    """弹系统信息框（阻塞至用户点确定），仿用户提供的 IPv6Guardian 样式：
    「已把 企微伴侣 搬移到: <路径>  请从新位置启动，并删除原来位置的旧副本。」"""
    try:
        import ctypes
        msg = (f"已把 企微伴侣 搬移到:\n{new_exe}\n\n"
               "请从新位置启动 企微伴侣，\n并删除原来位置的旧副本。")
        ctypes.windll.user32.MessageBoxW(None, msg, "企微伴侣 已搬移", 0x40)
    except Exception:  # noqa: BLE001
        pass


def maybe_relocate_and_relaunch():
    """启动早期入口（backend __main__，单实例锁之前调用）。
    返回 True = 已发起迁移并拉起新实例，调用方必须立即退出本进程；
    返回 False = 无需迁移（正常继续启动）。"""
    if not getattr(sys, "frozen", False):
        return False
    exe = sys.executable or ""
    if not exe.lower().endswith(".exe"):
        return False
    # 新实例清理旧 .bak（老进程已退出，文件已解锁）——迁移链的收尾
    # （v1.0.561 起不再 rename/Popen，此分支保留作向后兼容，正常不会触发）
    bak = os.environ.get(ENV_MIGRATE_BAK) or ""
    if bak:
        try:
            if os.path.isfile(bak):
                os.remove(bak)
        except Exception:  # noqa: BLE001
            pass   # 删不掉就留着（无害，用户可手删）
        try:
            del os.environ[ENV_MIGRATE_BAK]
        except Exception:  # noqa: BLE001
            pass
        return False
    try:
        should, reason, target = needs_relocation(os.path.dirname(exe))
        if not should:
            return False
        from core_utils import log_event
        new_exe, moved, errors, bak = perform_relocation(exe, target)
        log_event(f"[relocate] 检测到{reason}运行，已自动归位到 {target}"
                  f"（随迁: {', '.join(moved) or '无'}"
                  + (f"；异常: {'; '.join(errors)}" if errors else "") + "）")
        # v1.0.619：产物库改为延后创建，本进程的闸门在旧位置永远不会开 → 上面那行（以及
        # 更早的启动日志）会一直攒在内存里、随 os._exit 蒸发。搬家证据必须跟着 exe 走，
        # 所以显式补写到**新位置**的日志目录；旧位置从此不再被日志顺手捏出一个 产物库/。
        try:
            from core import logging as _lg
            _lg.flush_early_logs(log_dir=os.path.join(target, "产物库", "日志"))
        except Exception:  # noqa: BLE001
            pass
        # v1.0.561（用户要求交互形式）：弹信息框告知新位置（仿 IPv6Guardian 样式），
        # 用户点「确定」后本进程退出——由用户自己从新位置启动，不自动重启。
        _notify_moved(new_exe, reason)
        os._exit(0)
        return True
    except Exception as e:  # noqa: BLE001
        # 迁移是锦上添花：任何失败都原地正常启动，绝不阻断使用
        try:
            from core_utils import log_event
            log_event(f"[relocate] 自动归位失败（原地继续运行）：{e}")
        except Exception:  # noqa: BLE001
            pass
        return False

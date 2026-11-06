# -*- coding: utf-8 -*-
"""core.paths — 路径与产物库/知识库常量（自 core_utils 拆分 v1.0.402）
"""
# -*- coding: utf-8 -*-
import os
import sys
import re
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # v1.0.402：core/ 上一级为项目根

# ---------- 产物库（agent 产生的所有文件统一存放） ----------
WORKSPACE_NAME = "产物库"
_WORKSPACE_SUBS = ("对话记录", "下载", "产出", "技能", "上传")
# v1.0.614（审查 ㉜）→ v1.0.616 做完：产物库文件树的展示口径。
# 台账/日志 只是当时手补的两个段，实际顶层还有 日报/备份/自修复/记忆/宏/会话/版本…
# ——审批弹窗承诺「覆盖前自动备份到 产物库/备份/」却在界面上看不见、点不开，等于没兜底。
# 现在改成「写入名单 + 其余顶层目录一律只读展示」，以后新增目录不用再回来加一行。
# 写入名单 _ALLOWED_SUBS 仍然不动：agent 不能往这些只读段里塞文件。
# 例外 _TREE_HIDE：
#   组件   —— 7 万文件的便携运行时，列进树是噪音；体积口径已单列 runtime_mb
#   系统   —— 里面是 api_token.txt，不在界面列出、更不能被 /workspace/file 读出来
#   知识库 —— 有自己的「知识库」视图（嵌套分类），扁平树里只会显示一个空段
_RO_SUBS = ("台账", "日志")
_TREE_HIDE = ("组件", "系统", "知识库")
_TREE_SUBS = tuple(_WORKSPACE_SUBS) + _RO_SUBS      # 兼容旧引用；动态口径走 tree_subs()


def tree_subs():
    """文件树展示的段：5 个可写段在前，其余顶层目录一律作只读段跟随。"""
    root = workspace_root()
    try:
        names = [n for n in sorted(os.listdir(root))
                 if n not in _TREE_HIDE and not n.startswith((".", "_"))
                 and os.path.isdir(os.path.join(root, n))]
    except OSError:
        names = list(_RO_SUBS)
    return list(_WORKSPACE_SUBS) + [n for n in names if n not in _WORKSPACE_SUBS]

# ---------- 知识库（v1.0.294：产物库/知识库/，分门别类三子目录） ----------
# 课件=视频/PDF OCR 课程素材；题库=答题记录；制度=公司制度/管理办法原文（预留）。
# 与 _WORKSPACE_SUBS（5 个扁平子目录，供「产物库」文件树）分开管理：知识库是嵌套
# 目录（知识库/<分类>/），单独一套，避免破坏既有扁平子目录口径。
KB_ROOT = "知识库"
KB_CATEGORIES = ("课件", "题库", "制度")
_KB_SUBS = tuple(f"{KB_ROOT}/{c}" for c in KB_CATEGORIES)  # ("知识库/课件", ...) 正斜杠，跨平台一致
# save_workspace_file / list_workspace_files / ws_sub 接受的全部子目录（扁平 + 知识库嵌套）
_ALLOWED_SUBS = tuple(_WORKSPACE_SUBS) + _KB_SUBS
# ---------- 路径：开发用脚本目录，冻结用 exe 同级 ----------
def resource_path(rel):
    base = getattr(sys, "_MEIPASS", BASE)
    return os.path.join(base, rel)


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)       # 应用所在文件夹
    return BASE


def _fallback_dir():
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or BASE
    d = os.path.join(local, "WeComCompanion")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


def _create_workspace_dirs():
    """真正建根 + 5 个可写子目录（幂等）。只由闸门调用。"""
    root = os.path.join(_app_dir(), WORKSPACE_NAME)
    try:
        os.makedirs(root, exist_ok=True)
        for s in _WORKSPACE_SUBS:
            os.makedirs(os.path.join(root, s), exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return root


# ---------- 首启产物库闸门（v1.0.619）----------
# 起因：exe 直接放在桌面/下载/磁盘根目录时，core/relocate.py 会建「企微伴侣」文件夹、
# 把 exe 与运行时搬进去并重启（归位）。而产物库原先在**开窗之前**就被建出来——
# 加上任何一次早期 log_event 都会顺手 makedirs，于是旧位置留下一个 产物库/，
# 表现就是"桌面被塞了个文件夹 / 搬家后出现两处产物库"，与归位功能抢时序。
# 现在：产物库目录只在闸门打开后才创建。闸门由 ①开机延时 ②任何一次写操作 ③检测到
# 产物库本就存在（老用户升级，没什么可归位的）三条路打开；读路径一律不建、且容忍缺失。
WORKSPACE_READY_DELAY_S = 12        # 开窗后多久自动打开闸门（期间写操作会立即提前打开）
_WS_READY = threading.Event()
_creation_reason = ""


def workspace_ready():
    """产物库是否已进入"允许创建"状态（不代表目录已存在，用 os.path.isdir 判那个）。"""
    return _WS_READY.is_set()


def allow_workspace_creation(reason="startup-timer"):
    """打开闸门并按需建目录。幂等；返回 True 表示本次是首次打开（调用方据此记一行日志）。"""
    global _creation_reason
    if _WS_READY.is_set():
        return False
    _WS_READY.set()
    _creation_reason = str(reason)[:60]
    _create_workspace_dirs()
    # 目录刚存在，先把闸门期间攒下的启动日志补落盘——必须由开闸方主动补，
    # 不能等"下一条日志顺便带出"：用户可能什么都不做就关软件，启动日志会整段丢失。
    try:
        from core import logging as _lg
        _lg.flush_early_logs()
    except Exception:  # noqa: BLE001
        pass
    return True


def creation_reason():
    return _creation_reason


def _reset_workspace_gate_for_tests():
    """测试专用：把闸门关回去并清空待补写日志。真实进程从不调用它。"""
    global _creation_reason
    _WS_READY.clear()
    _creation_reason = ""      # 闸态要整套复位，否则上一个用例留下的原因会串味
    try:
        from core import logging as _lg
        with _lg._EARLY_LOCK:
            _lg._EARLY_LOGS.clear()
    except Exception:  # noqa: BLE001
        pass


def workspace_root():
    """产物库根目录路径。v1.0.619 起**不再顺手建目录**：闸门未开时只回路径，
    闸门开了则保证存在（幂等重建，兼容被手工删掉子目录的情形）。"""
    root = os.path.join(_app_dir(), WORKSPACE_NAME)
    if _WS_READY.is_set():
        _create_workspace_dirs()
    return root


def ws_sub(name):
    """写入用目录解析——**写路径自带解锁**：闸门没开也先把它打开，保证功能不断。"""
    # v1.0.294：允许知识库嵌套子目录（知识库/课件 等），其余口径不变（未知→产出）。
    # os.makedirs 天然支持多级目录，嵌套路径无需特殊处理。
    # 归一化斜杠方向：模型/调用方常用正斜杠，os.path.join 在 Windows 出反斜杠，统一转正斜杠再比对。
    name = str(name or "").replace("\\", "/").strip()
    if name not in _ALLOWED_SUBS:
        name = "产出"
    allow_workspace_creation("write:" + name)
    # 基准路径必须走 workspace_root()：全项目测试靠 patch core.paths.workspace_root 改道，
    # 直接拼 _app_dir() 会让这些测试写进真实目录（v1.0.619 自查时抓到自己埋的这颗雷）。
    d = os.path.join(workspace_root(), name)
    os.makedirs(d, exist_ok=True)
    return d


def kb_dir(cat="课件"):
    """返回 产物库/知识库/<cat>/ 绝对路径并确保存在。cat 非法回退 课件。"""
    if cat not in KB_CATEGORIES:
        cat = "课件"
    return ws_sub(os.path.join(KB_ROOT, cat))


def ws_rel_path(sub, filename):
    """安全地拼出产物库内某文件路径，禁止路径穿越。"""
    name = os.path.basename(str(filename)).strip() or "untitled.txt"
    name = re.sub(r'[\/\\:*?"<>|]', "_", name)[:120]   # 去除 Windows 非法字符
    return os.path.join(ws_sub(sub), name)

# v1.0.402：日志目录归属路径域（logging 依赖它，避免 workspace_io<-logging 环）
def companion_log_dir():
    """运行日志目录：产物库/日志。

    v1.0.619：闸门未开时**只回路径、不建目录**——原先任何一次早期 log_event 都会顺手
    makedirs 出 `产物库/日志`，正是"归位前的旧位置冒出产物库"的主路径之一。
    目录不存在时由 `core.logging.log_event` 在内存里缓冲，闸门打开后一次性补写。
    """
    d = os.path.join(workspace_root(), "日志")
    if _WS_READY.is_set():
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
    return d


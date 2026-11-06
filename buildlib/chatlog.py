# -*- coding: utf-8 -*-
"""buildlib.chatlog — 历史对话归档（断点不动）与断点新鲜度守卫（自 build.py 逐字迁入）。"""
import io
import json as _json
import os
import shutil
import time

from buildlib.env import BASE

# v1.0.603（用户要求）：每次打包后，新版本从「零历史对话」开始。
_CHAT_DIRS = ("会话", "对话记录")
# v1.0.623：断点/ 从此**不跟着清**。v603 那条口径的原话是「打包出来的新版本不要带
# 历史对话」，而断点不是历史——它是一次「停在待点继续」的任务状态。622 把正在跑的
# 答题断点连根搬走过一次，623 这次守卫没拦到（那份断点已 89 分钟没写，早于 30 分钟
# 窗口），但「三小时前暂停、现在想继续」本来就是正常使用形态，按新鲜度拦不住。
# 体积上也无须拦：断点由 agent/session 的 LRU（10 个 pane）自管。
_CHAT_KEEP_DIRS = ("断点",)
_CHAT_ARCHIVE = os.path.join(BASE, "build", "_chat_archive")

# v1.0.623（事故复盘中来的守卫）：断点＝有一次会话停在「等用户点继续」的状态。
# v1.0.622 那次打包把**正在进行**的一轮答题断点连同历史对话一起搬进归档，用户点
# 「继续」时文件已不在原地（300 个文件手工搬回）。当时的活动守卫只看日志尾部
# 180s——而「挂起等待」恰恰不写日志，于是闸门在最需要的场景是空的。
_BP_BUSY_MIN = 30
_BREAKPOINT_DIR = os.path.join("产物库", "断点")


def newest_breakpoint_epoch(dist_dir=None):
    """返回 dist 里最近一次断点写入的时间戳（epoch 秒）；没有断点/读不到返回 None。"""
    d = os.path.join(dist_dir or os.path.join(BASE, "dist"), _BREAKPOINT_DIR)
    newest = None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for nm in names:
        p = os.path.join(d, nm)
        try:
            if not os.path.isfile(p):
                continue
            m = os.path.getmtime(p)
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    return newest


def breakpoint_busy(now=None, dist_dir=None, window_min=None):
    """活动守卫的断点侧：(busy, 距最近一次断点写入的分钟数)。

    busy=最近 window_min 分钟内还有断点落盘 → 有任务停在「待点继续」，打包会搬走
    它的续跑状态并重启杀进程，必须放行前显式确认。读不到目录按「不忙」处理
    （None），让日志侧守卫继续兜底，而不是把没跑过应用的机器也挡住。
    """
    _w = float(window_min if window_min is not None else _BP_BUSY_MIN)
    _ts = newest_breakpoint_epoch(dist_dir)
    if _ts is None:
        return False, None
    age = ((now if now is not None else time.time()) - _ts) / 60.0
    return age < _w, age


def clear_chat_history(keep=False):
    """把 dist/产物库 里的**对话数据**整簇搬到 build/_chat_archive/<时间戳>/。

    只搬两类：`会话/`（任务栏与上下文，就是用户看到的"历史对话"）、
    `对话记录/`（导出的 md 逐份记录）。**`断点/` 不搬**（v1.0.623 起，见
    _CHAT_KEEP_DIRS 注释）——那是「待点继续」的任务状态，搬走就等于把用户
    暂停中的答题/批审删了半截。
    **不是删除**——随时可以把那个目录整体搬回去（本机没有 git，删了就没了）。
    配置、模型、技能、经验、宏、定时任务、台账、产物、OCR/ASR 组件一概不动。

    同时把 dist/config.json 的 `chat_epoch` +1：前端 localStorage 里还有一份
    `hist_<pane>` 窗口缓存，不失效的话「临时对话」那一栏会把上一版的旧气泡复活，
    用户看到的就是"没清干净"。前端只删 hist_*，模型/权限/思考/深色等偏好保留。
    """
    dist = os.path.join(BASE, "dist")
    lib = os.path.join(dist, "产物库")
    if keep:
        print("[build] --keep-chats：保留历史对话（不清 会话/对话记录）")
        return
    if not os.path.isdir(lib):
        print("[build] 未找到 dist/产物库，跳过历史对话清理")
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(_CHAT_ARCHIVE, stamp)
    moved = {}
    for name in _CHAT_DIRS:
        src = os.path.join(lib, name)
        if not os.path.exists(src):
            continue
        try:
            os.makedirs(dest, exist_ok=True)
            shutil.move(src, os.path.join(dest, name))
            n = 0
            p2 = os.path.join(dest, name)
            if os.path.isdir(p2):
                n = sum(len(fs) for _, _, fs in os.walk(p2))
            else:
                n = 1
            moved[name] = n
            os.makedirs(src, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            print(f"[build] 警告：清理 {name} 失败（不拦截打包）：{e}")
    if moved:
        print("[build] 历史对话已归档（不是删除，可整体搬回）→ build/_chat_archive/%s" % stamp)
        for k, v in moved.items():
            print(f"[build]   · {k}/ 移出 {v} 个文件")
    else:
        print("[build] 历史对话本来就是空的，无需清理")
    # 断点原地留着，只报个数：用户看不到它就不知道自己「还有活儿没点继续」
    for name in _CHAT_KEEP_DIRS:
        p2 = os.path.join(lib, name)
        if os.path.isdir(p2):
            n2 = len([f for f in os.listdir(p2) if os.path.isfile(os.path.join(p2, f))])
            if n2:
                print(f"[build]   · {name}/ 原地保留 {n2} 份（待点「继续」的任务状态，不清）")
    # 前端缓存失效纪元 +1
    cfgp = os.path.join(dist, "config.json")
    try:
        cfg = {}
        if os.path.isfile(cfgp):
            cfg = _json.loads(io.open(cfgp, encoding="utf-8").read() or "{}")
        cfg["chat_epoch"] = int(cfg.get("chat_epoch") or 0) + 1
        tmp = cfgp + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(_json.dumps(cfg, ensure_ascii=False, indent=2))
        os.replace(tmp, cfgp)
        print(f"[build] chat_epoch → {cfg['chat_epoch']}（前端据此清掉 localStorage 的 hist_* 缓存）")
    except Exception as e:  # noqa: BLE001
        print(f"[build] 警告：chat_epoch 递增失败（旧版气泡可能仍被缓存带回来）：{e}")

# -*- coding: utf-8 -*-
"""core.logging — 日志轮转/事件/活动时间戳（自 core_utils 拆分 v1.0.402）
"""
# -*- coding: utf-8 -*-
import os
import re
import datetime
import threading
import collections
# v1.0.412 重大修复：原写法 `from core.paths import companion_log_dir` 导入的是函数名，
# 而 log_event 函数体调用 `paths.companion_log_dir()`——模块内不存在 `paths` 名字，
# 每次调用必抛 NameError 且被最外层 except: pass 静默吞掉 → **v1.0.402 拆分以来
# 所有业务日志（[agent]/[llm]/[bind]/[sched]/[workspace]/[repair]...）一行未落盘**，
# 排障全盲，连带 last_agent_activity_epoch（打包活动守卫/知识库空闲判定）失效。
# 改回 `from core import paths` 属性查找语义（与全项目测试 patch paths.xxx 的口径一致）。
from core import paths  # noqa: F401（属性查找：测试 patch core.paths.xxx 即全局生效）

# ---------- 日志轮转常量 ----------
_LOG_MAX_BYTES = 1024 * 1024     # 单日志文件 1MB
_LOG_BACKUP_COUNT = 5            # 最多保留 5 份轮转

# 工具结果回灌给模型的最大字符数（工具层/循环共用）
TOOL_RESULT_MAX_LEN = 20000

# read_workspace_file 读取超过此大小截断(约50KB)
READ_TRUNCATE_BYTES = 50 * 1024
def _timestamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_LOCK = threading.Lock()   # v1.0.361 审查修复（P18）：并发追加与轮转 rename 竞态
# v1.0.619：产物库闸门未开期间的启动日志先攒在这里（有界，最多 500 行），闸门一开补写。
_EARLY_LOGS = collections.deque(maxlen=500)
_EARLY_LOCK = threading.Lock()
# 补写会把本批日志的落盘时间推到开闸那一刻之后（行首时间戳仍是各条自己的真实时间）。
# 每次启动打一条分隔线，避免看日志的人把它误读成"上一次运行末尾丢了一批"。
_START_MARK = "# ---- 本次启动的日志从这里开始（启动期若干行系开闸后补写，行首时间为真实时间）----\n"
_START_MARK_DONE = False


class _LazyRequests:
    """requests 懒加载代理（v1.0.364 M8 收敛：原 7 个模块各复制 8 行）。
    每次 __getattr__ 走 sys.modules 缓存查找（~µs 级），对网络调用无感知开销。"""

    def __getattr__(self, name):
        import requests as _r
        return getattr(_r, name)


_lazy_requests = _LazyRequests()


def _write_log_line(line, log_dir=None):
    """真正落盘的一行。v1.0.361（P18）：全程持锁——原实现多线程并发写 + 轮转 rename 竞态
    （轮转静默失效/丢日志）。

    v1.0.619 两处新增：
      · log_dir —— 仅供「归位搬家」把启动日志写到新家（旧位置的闸门永远不会开，
        默认路径故意不建目录，否则会重新在桌面捏出一个 产物库/）。
      · 每次进程的第一行落盘前打一条「本次启动」分隔线——启动期日志现在是延后补写的，
        没有这条线，看日志的人会把它误读成"上一次运行末尾丢了一批"。"""
    global _START_MARK_DONE
    try:
        with _LOG_LOCK:
            d = log_dir if log_dir else paths.companion_log_dir()
            if log_dir:
                try:
                    os.makedirs(d, exist_ok=True)
                except Exception:  # noqa: BLE001
                    pass
            log_path = os.path.join(d, "companion.log")
            # 简单轮转：超过阈值时滚动保留 5 份
            try:
                if os.path.exists(log_path) and os.path.getsize(log_path) > _LOG_MAX_BYTES:
                    for i in range(_LOG_BACKUP_COUNT, 0, -1):
                        src = f"{log_path}.{i - 1}" if i > 1 else log_path
                        dst = f"{log_path}.{i}"
                        if os.path.exists(src):
                            if os.path.exists(dst):
                                os.remove(dst)
                            os.rename(src, dst)
            except Exception:  # noqa: BLE001
                pass  # 日志轮转失败不致命：保留旧日志即可
            with open(log_path, "a", encoding="utf-8") as f:
                if not _START_MARK_DONE:
                    _START_MARK_DONE = True
                    f.write(_START_MARK)
                f.write(line)
    except Exception:  # noqa: BLE001
        pass  # 日志系统自身异常：静默，避免递归刷日志


def log_event(msg):
    """写日志到 产物库/日志/companion.log（v1.0.275 整合，原在应用根目录）。1MB×5 轮转。

    v1.0.619：产物库闸门未开时**先在内存里攒着**（不落盘、也就不去 makedirs 目录）——
    早建目录正是与「exe 放桌面→自动归位」抢时序的根因之一。闸门一开（开机延时到点、
    或任何一次写产物库的操作），开闸方立即调 `flush_early_logs()` 补写，**启动期一行日志都不丢**。
    """
    line = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg) + "\n"
    if not paths.workspace_ready():
        with _EARLY_LOCK:
            _EARLY_LOGS.append(line)
        return
    flush_early_logs()      # 开闸瞬间若有线程正好插进来攒了一条，这里兜底补上
    _write_log_line(line)


def flush_early_logs(log_dir=None):
    """把闸门未开期间攒下的启动日志一次性补写，返回补写行数。

    由 `core.paths.allow_workspace_creation()` 在开闸建目录后立即调用——补写必须发生在
    开闸这一刻，而不是"等下一条日志顺便带出来"：用户可能启动后什么都不触发就关掉软件，
    那种写法会把整个启动期的日志留在内存里丢掉。log_event 里仍保留一次惰性补写，
    用于兜住"另一条线程恰好在开闸瞬间判定为未开闸"这个小窗口。
    log_dir：归位搬家时旧位置的闸门永远不会再开，日志要跟 exe 一起落到新家目录。"""
    with _EARLY_LOCK:
        if not _EARLY_LOGS:
            return 0
        pend = list(_EARLY_LOGS)
        _EARLY_LOGS.clear()
    for p in pend:
        _write_log_line(p, log_dir=log_dir)
    return len(pend)


def early_log_count():
    """还攒在内存里、尚未落盘的启动期日志条数（核验"一行没丢"用）。"""
    with _EARLY_LOCK:
        return len(_EARLY_LOGS)


def last_agent_activity_epoch(log_paths, limit_lines=400, tail_bytes=1 << 20):
    """v1.0.302：日志尾部最近一条 [agent]/[llm] 活动行的时间戳（epoch 秒）。
    「活动探测」单点实现——此前 build.py 的 180s 打包守卫与 knowledge_ops 的 600s 空闲
    判定各写了一遍同款算法（扫尾部 400 行找时间戳），口径要调时改两处。
    log_paths 可为单个路径或列表（新旧日志位置都查），返回其中最新时间戳。

    v1.0.592 修两个口径问题：
      ① 旧实现只回看最后 400 行。启动/退出噪音（[system]/[window-tucker]/[config] 等）
         动辄几百行，会把真正的活动行挤出窗口 → 返回 None → build 守卫把它当成
         「日志文件读不到」而 fail-closed 拒打包（打 1.0.592 实测就是这么被卡住的）；
         反过来也可能把「3 分钟内有活动」漏判成真误杀。改为按字节回看尾部 1MB
         （limit_lines 只作行数护栏），一次扫描能覆盖的活动时间跨度大两个数量级。
      ② 「文件读到了、但确实没有活动行」≠「读不到」。前者返回 0.0（远早于任何窗口，
         语义即空闲），后者才返回 None，调用方终于能区分两者。两个现有调用方语义不变：
         build 的 `time.time()-ts < 180` 与 knowledge_ops 的 is_idle() 对 0.0 与 None
         都判为「无近期活动」。"""
    if isinstance(log_paths, (str, os.PathLike)):
        log_paths = [log_paths]
    latest = None
    _readable = False
    for p in log_paths:
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                sz = f.tell()
                if sz > tail_bytes:
                    f.seek(sz - tail_bytes)
                    f.readline()      # 丢掉可能被截断的半行
                else:
                    f.seek(0)         # 小文件从头读（seek(0,2) 已把指针留在末尾）
                data = f.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        _readable = True
        lines = data.splitlines()
        _cap = max(limit_lines, 1) * 500   # 护栏：1MB 尾部也只需回看有限行
        if len(lines) > _cap:
            lines = lines[-_cap:]
        for ln in reversed(lines):
            if "[agent]" not in ln and "[llm]" not in ln:
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
            if not m:
                continue
            try:
                ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:  # noqa: BLE001
                break
            if latest is None or ts > latest:
                latest = ts
            break
    if latest is None and _readable:
        return 0.0        # 读到了但无活动行 —— 不是「读不到」
    return latest

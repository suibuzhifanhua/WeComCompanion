# -*- coding: utf-8 -*-
"""tests/test_logging_writes.py — v1.0.412 重大回归修复护航：log_event 必须真实落盘。

背景：v1.0.402 拆 core/logging.py 时 import 写错（`from core.paths import companion_log_dir`
导成名字，函数体却调 `paths.companion_log_dir()`），模块内无 `paths` 名字 → 每次调用
NameError 被 except: pass 静默吞掉 → **402 起所有业务日志一行未落盘**（[agent]/[llm]/
[bind]/[sched] 全丢），连带 last_agent_activity_epoch（打包守卫/知识库空闲判定）失效。
本测试 patch core.paths.companion_log_dir 后断言落盘——若 import 回退成坏写法直接红。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_log_event_writes_disk(tmp_path):
    import core.logging as cl
    from core import paths

    log_dir = tmp_path / "日志"
    log_dir.mkdir()
    orig = paths.companion_log_dir
    paths.companion_log_dir = lambda: str(log_dir)
    try:
        cl.log_event("[agent] 测试事件 abc123")
        content = (log_dir / "companion.log").read_text(encoding="utf-8")
        assert "[agent] 测试事件 abc123" in content, "log_event 未落盘——import 语义可能回退"
        assert "2026-" in content, "日志行缺时间戳"
    finally:
        paths.companion_log_dir = orig


def test_log_event_rotation_not_fatal(tmp_path):
    """日志目录不可写时 log_event 必须静默降级（不抛异常——写日志失败不能带崩业务线程）。"""
    import core.logging as cl
    from core import paths

    bad_dir = os.path.join(str(tmp_path), "不存在", "深层", "目录")
    orig = paths.companion_log_dir
    paths.companion_log_dir = lambda: bad_dir
    try:
        cl.log_event("[agent] 这条会失败但不应抛")   # 不抛 = 通过
    finally:
        paths.companion_log_dir = orig


def test_last_agent_activity_reads_log(tmp_path):
    """活动探测（打包守卫/知识库空闲判定依赖）能从真实落盘的日志读到 [agent] 时间戳。"""
    import time as _t
    import core.logging as cl
    from core import paths

    log_dir = tmp_path / "日志"
    log_dir.mkdir()
    log_file = log_dir / "companion.log"
    orig = paths.companion_log_dir
    paths.companion_log_dir = lambda: str(log_dir)
    try:
        cl.log_event("[agent] 活动行")
        ep = cl.last_agent_activity_epoch(str(log_file))
        assert ep is not None, "活动探测没读到刚写入的 [agent] 行"
        assert abs(ep - _t.time()) < 120, f"时间戳异常: {ep}"
    finally:
        paths.companion_log_dir = orig


if __name__ == "__main__":
    import tempfile
    tp = tempfile.mkdtemp()
    test_log_event_writes_disk(tp)
    test_log_event_rotation_not_fatal(tp)
    test_last_agent_activity_reads_log(tp)
    print("PASS logging 3 场景")

# -*- coding: utf-8 -*-
"""tests/test_sse_stream.py — SSE 包装器的两条存活契约（v1.0.646）。

① 心跳：生成器长时间不产出时，主线程必须持续发 `: keepalive`，否则 WebView2
   约 30s 自动断流（财务审核跑到一半 net error 的历史根因）。
② 背压：客户端不消费 → 队列满 → **不许无限空转**。旧实现的泵线程在 Full 时
   10ms 重试永不放弃，任务就"界面转圈、后端停摆"且毫无报错。现在超过
   _STALL_TIMEOUT 判定失联：发 error 事件 + 置 abort + 清断点。
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import sse_stream as S  # noqa: E402


def test_heartbeat_keeps_the_stream_alive(monkeypatch):
    """生成器 3s 不说话 → 期间必须至少发一次心跳注释行。"""
    monkeypatch.setattr(S, "_HEARTBEAT", 0.2)

    def slow():
        yield "data: 1\n\n"
        time.sleep(1.0)
        yield "data: 2\n\n"

    out, t0 = [], time.time()
    for chunk in S._sse_stream(slow()):
        out.append(chunk)
        if len(out) >= 2 and any(c.startswith(": keepalive") for c in out):
            break
        if time.time() - t0 > 5:
            break
    assert any(c.startswith(": keepalive") for c in out), f"空闲期没发心跳：{out}"


def test_backpressure_aborts_instead_of_spinning_forever(monkeypatch):
    """客户端不消费（队列顶到 maxsize）→ 必须主动判死，而不是永久空转。"""
    monkeypatch.setattr(S, "_STALL_TIMEOUT", 0.5)
    monkeypatch.setattr(S, "_HEARTBEAT", 0.2)
    pane = {"id": "pane-stall-1"}
    aborted, dropped = [], []
    monkeypatch.setattr(S, "_set_abort", lambda pid, v: aborted.append((pid, v)))
    monkeypatch.setattr(S, "drop_chat_state", lambda pid: dropped.append(pid))

    def endless():
        i = 0
        while True:
            i += 1
            yield "data: %d\n\n" % i

    stream = S._sse_stream(endless(), pane_id=pane["id"])
    first = next(stream)          # 只取一条，之后装作客户端卡住不读
    assert first.startswith("data:")
    time.sleep(2.0)               # 队列顶满并持续不消费：泵线程应在此期间判死

    deadline = time.time() + 15
    err_chunk = None
    try:
        while time.time() < deadline:
            chunk = next(stream)
            if '"type": "error"' in chunk:
                err_chunk = chunk
                break
    except RuntimeError as e:
        # 生成器抛错也是可接受的收尾（Flask 会转成 500），关键是**不再空转**
        err_chunk = err_chunk or str(e)

    assert err_chunk, "队列满后既没报错也没结束 = 又变成永久空转（任务静默停摆）"
    # 判死必须带上清理：置 abort + 清断点，否则用户点「继续」会接到一条死任务
    assert any(p == pane["id"] for p, _ in aborted), f"没置 abort：{aborted}"
    assert pane["id"] in dropped, f"没清断点：{dropped}"


def test_normal_finish_does_not_touch_state(monkeypatch):
    """正常走完不能误判成背压：不置 abort、不清断点（保留「继续」能力）。"""
    aborted, dropped = [], []
    monkeypatch.setattr(S, "_set_abort", lambda pid, v: aborted.append((pid, v)))
    monkeypatch.setattr(S, "drop_chat_state", lambda pid: dropped.append(pid))

    got = list(S._sse_stream(iter(["data: a\n\n", "data: b\n\n"]), pane_id="ok-pane"))
    assert got == ["data: a\n\n", "data: b\n\n"]
    assert not aborted and not dropped

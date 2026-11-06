"""agent.sse_stream — SSE 包装器（GeneratorExit 清理 + 心跳防断流，P1-3/P2-5）。
逐字自 backend.py 迁出（T04）；_HEARTBEAT 提为模块常量（值不变）。
契约：空闲超阈值时 yield 注释行 ": keepalive\n\n"（前端忽略注释行）。"""

import time
import queue as _q
import threading as _th

from .session import _set_abort, drop_chat_state

_HEARTBEAT = 8.0          # 空闲超过 8s 发一次心跳（远小于 WebView2 ~30s 断连阈值）
# v1.0.646：背压兜底。队列满 = 主线程卡在 socket 写（客户端不读）或消费极慢，
# 此时泵线程会一直 10ms 空转重试，任务就这样"看似在跑、实际永久停摆"——
# 既占着 LLM/tool，也没有任何报错（界面只剩转圈）。超过本阈值判定客户端已失联，
# 主动中止任务（置 abort + 清断点 + 发 error 事件），把"卡死"变成"可感知的失败"。
_STALL_TIMEOUT = 30.0

def _sse_stream(gen, pane_id=None):
    """包装 SSE 生成器（P1-3/P2-5 + 心跳防断流）：
    - 客户端断开/被 close（GeneratorExit）→ 置位 abort 并清理该 pane 断点，防后端空耗；
    - 生成异常 → 同样清理（断点状态不可信）；
    - 正常走完（含到达轮次上限已存断点）→ 不清理，保留「继续」能力；
    - 心跳防断流：agent_loop 在「等待 LLM 首 token」「长工具执行」等阶段会长时间
      不产出任何 SSE 数据，WebView2/Chromium 的流式 fetch 对持续无数据（约 30s）的
      连接会自动断开，前端即报 network error（财务审核跑到一半中断的根因）。本函数
      把生成器推进放后台线程，主线程在空闲超过阈值时主动发出 SSE 注释行
      `: keepalive`（非 data 事件，前端自动忽略），保持连接活跃，杜绝中途断流。
    """
    _STOP, _ERR = object(), object()

    q = _q.Queue(maxsize=64)
    stop = _th.Event()
    err_holder = {"exc": None}

    def _pump():
        try:
            for item in gen:
                if stop.is_set():
                    return
                t0 = time.monotonic()
                while True:
                    try:
                        q.put_nowait(item)
                        break
                    except _q.Full:
                        if stop.is_set():
                            return
                        if time.monotonic() - t0 > _STALL_TIMEOUT:
                            # 客户端长时间不消费：不等了，把任务判死（见 _STALL_TIMEOUT 注释）
                            err_holder["exc"] = RuntimeError(
                                f"SSE 背压超时：{_STALL_TIMEOUT:.0f}s 内客户端未消费任何事件，判定已失联")
                            try:
                                q.put(_ERR, timeout=1.0)
                            except _q.Full:
                                try:
                                    q.get_nowait()      # 挤掉一条最老的渲染事件也要把错误送出去
                                    q.put_nowait(_ERR)
                                except Exception:  # noqa: BLE001
                                    pass
                            return
                        time.sleep(0.01)
            try:
                # v1.0.652（审计 P2-7）：收尾 put 加超时——客户端早退+队列积压时
                # 无超时 put 永久阻塞泵线程（daemon 泄漏且持有 gen 引用）。
                q.put(_STOP, timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        except GeneratorExit:
            pass  # 生成器自身被关闭，静默退出即可
        except Exception as e:  # noqa: BLE001
            err_holder["exc"] = e
            try:
                q.put_nowait(_ERR)
            except _q.Full:
                pass

    _th.Thread(target=_pump, daemon=True).start()
    try:
        while True:
            try:
                item = q.get(timeout=_HEARTBEAT)
            except _q.Empty:
                # 空闲：发心跳保活（SSE 注释行，客户端解析时忽略）
                yield ": keepalive\n\n"
                continue
            if item is _STOP:
                break
            if item is _ERR:
                # 生成器内部异常：清理断点后原样抛出，交由 Flask/前端转 error
                if pane_id:
                    _set_abort(pane_id, True)
                    # v1.0.405（M5）：内存+盘上文件同步清
                    drop_chat_state(pane_id)
                # 2026-09-10 修复：裸抛异常会让 SSE 流「无终止事件」中断——前端收不到
                # done/error，typing 类残留（气泡塌成「每字一行」窄列）且用户看不到报错
                # （只看到半截内容）。故先补发一条干净的 SSE error 事件，让前端走 error
                # 分支（移除 typing + 显示错误 + 提供「继续」按钮），再抛出交由 Flask 收尾。
                import json as _json
                _err_text = "agent 内部异常：" + str(err_holder["exc"])
                yield "data: " + _json.dumps({"type": "error", "text": _err_text}, ensure_ascii=False) + "\n\n"
                raise RuntimeError(f"agent 生成器内部异常：{err_holder['exc']}") from err_holder["exc"]
            yield item
    except GeneratorExit:
        # 客户端断开：停止泵线程，避免后端空耗
        if pane_id:
            _set_abort(pane_id, True)
            # v1.0.405（M5）：内存+盘上文件同步清
            drop_chat_state(pane_id)
        stop.set()
        raise
    finally:
        stop.set()

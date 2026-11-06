"""
agent — 对话运行时层包（backend.py 的 agent 段就地升级，T04）
====================================================================
结构：
  session.py    — abort 标志 / 轮次与熔断常量 / 压缩阈值 / 断点存储
  messages.py   — 消息压缩（字符统计 / 高价值摘要 / 中间往返压缩）
  sys_prompts.py— 三大系统提示词（行为本体，逐字）
  context.py    — 对话上下文拼装（build_chat_messages）
  （v1.0.646：原 tools_call.py 的工具执行白名单常量已并入 agent_tools/registry.py）
  sse_stream.py — SSE 包装器（心跳防断流）
  loop.py       — agent_loop 主循环（逐字）
"""
from .loop import agent_loop, _auto_loop_chain
from .sse_stream import _sse_stream
from .session import (
    chat_states, _chat_state_lock, _set_abort, _is_aborted, _save_chat_state,
    _abort_lock, _abort_flags, drop_chat_state, restore_persisted_chat_state,
)
from .sys_prompts import SYSTEM_PROMPT
from .context import build_chat_messages
from .team import team_pipeline

__all__ = [
    "agent_loop", "_auto_loop_chain", "_sse_stream", "chat_states", "_chat_state_lock",
    "_set_abort", "_is_aborted", "_save_chat_state",
    "_abort_lock", "_abort_flags", "SYSTEM_PROMPT", "build_chat_messages",
    "team_pipeline", "drop_chat_state", "restore_persisted_chat_state",
]

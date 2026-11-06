"""
agent_tools — 企微伴侣 · 工具注册表层包（原 agent_tools.py 就地升级）
====================================================================
对外接口与原模块完全一致：import agent_tools / from agent_tools import *
后所有旧符号可用（TOOLS / SKILLS / CONNECTORS / build_agent_tools /
dispatch_tool / run_tool 等 41 个公开符号）。

结构：
  registry.py   — 内置工具定义（TOOLS 42 项，含 6 个 pc_* OS 工具 + 3 个 dsh 接管通道工具）
  skills.py     — 内置技能定义（SKILLS 25 项 + 插件体系设计注释）
  connectors.py — 连接器定义与 HTTP 执行（CONNECTORS / run_connector）
  executors.py  — 工具执行器（常量 / _run_* / _TOOL_HANDLERS / run_tool）
  dispatch.py   — 技能覆盖与工具路由（build_agent_tools / dispatch_tool）
"""
import os
import re
import json

import wecom_bridge as wb
from services import rule_check

from core_utils import (log_event, append_ledger, _timestamp, _pick_int, _safe_ws_path, _parse_file_content, ws_rel_path, ws_sub, _WORKSPACE_SUBS, READ_TRUNCATE_BYTES)
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）

from .registry import TOOLS, FILE_PRODUCER_TOOLS, FILE_PRODUCER_OPS, is_file_producer
from .skills import SKILLS
from .connectors import CONNECTORS, is_connector_configured, run_connector
from . import os_ops  # v1.0.297 PC 操控底层（pc_screenshot/pc_files/pc_run/pc_apps/pc_clipboard/pc_system_info）
from . import dsh_bridge  # v1.0.298 PC 接管通道（deepseek-harness：dsh_check/dsh_prepare/dsh_run）
from .executors import (
    DEFAULT_SCROLL_AMOUNT, DEFAULT_MSG_TYPE, DOWNLOAD_TIMEOUT,
    _tool_by_name, _finish_tool, _cdp_port, _run_create_skill, _run_save_file,
    _run_download_file, _run_read_workspace_file, _run_list_workspace_files,
    _run_web_search, _run_fetch_webpage, _TOOL_HANDLERS, run_tool,
)
from .dispatch import (_effective_skill, _custom_skills, _extract_task_text,
                       hub_missing_skills, build_agent_tools, dispatch_tool,
                       set_current_pane, current_pane, pane_abort_hook)

__all__ = [
    "TOOLS", "SKILLS", "CONNECTORS", "os_ops", "dsh_bridge",
    "DEFAULT_SCROLL_AMOUNT", "DEFAULT_MSG_TYPE", "DOWNLOAD_TIMEOUT",
    "build_agent_tools", "dispatch_tool", "run_tool", "run_connector",
    "is_connector_configured", "_effective_skill", "_cdp_port",
    "set_current_pane", "current_pane", "pane_abort_hook",
    "_tool_by_name", "_finish_tool", "_TOOL_HANDLERS",
    "_run_create_skill", "_run_save_file", "_run_download_file",
    "_run_read_workspace_file", "_run_list_workspace_files",
    "_run_web_search", "_run_fetch_webpage",
    "json", "os", "re", "requests", "wb", "rule_check",
    "log_event", "append_ledger", "_timestamp", "_pick_int", "_safe_ws_path",
    "_parse_file_content", "save_workspace_file", "ws_rel_path", "ws_sub",
    "workspace_root", "_WORKSPACE_SUBS", "READ_TRUNCATE_BYTES",
    # v1.0.634：产出文件口径（agent.loop 收尾拼「本次生成的文件」清单要用）
    "FILE_PRODUCER_TOOLS", "FILE_PRODUCER_OPS", "is_file_producer",
]

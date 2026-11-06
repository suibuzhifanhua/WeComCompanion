# -*- coding: utf-8 -*-
"""split_management.py — 阶段二 Part B 最终版：management.py 按域切 6 蓝图 + 组装壳。
源：git 存档 _old_mgmt.txt（43c6119 的 1581 行版本）。区间已逐段人工核对。"""
import io
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)

src = io.open('_old_mgmt.txt', encoding='utf-8', errors='replace').read()
lines = src.split('\n')


def seg(a, b):
    return '\n'.join(lines[a - 1:b]).rstrip('\n')


HDR = '''# -*- coding: utf-8 -*-
import json
import os
import re
import sys
import time
import threading
import uuid

from flask import Blueprint, request, Response, jsonify, stream_with_context

from core_utils import _lazy_requests as requests
from core_utils import log_event, extract_json, mask_key, workspace_root
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）
from services import rule_check
from agent_tools import CONNECTORS, is_connector_configured, _cdp_port
from core.config_store import (load_config, _save_config_safe, _mask_connector_config,
                          _load_experiences, _save_experiences, _exp_rec, APP_PORT, save_config)
from llm_client import (resolve_model, _llm_complete, _probe_local_models,
                        judge_model, _LOCAL_MODEL_ENDPOINTS)
from agent import _sse_stream
from agent.sys_prompts import _QUICK_AGENT_SYSTEM
from .wecom import bind_status
from .templates import _tpl_agent_questions

try:
    from version import __version__ as VERSION
except Exception:  # noqa: BLE001
    VERSION = "0.0.0"

'''

# 行区间（基于 _old_mgmt.txt 1581 行版本，逐段人工核对过边界）
MODS = {
 'routes/b_models.py': ('routes.b_models — 模型管理 API（列表/评测/扫描/导入导出）',
   [(330, 747), (937, 993), (1008, 1038)]),
 'routes/b_repair.py': ('routes.b_repair — 自我修复 API（体检/报告/取消/一键打包）',
   [(1150, 1360)]),
 'routes/b_ocr_admin.py': ('routes.b_ocr_admin — OCR 组件管理 API（状态/安装/更新）',
   [(1040, 1081)]),
 'routes/b_memory.py': ('routes.b_memory — 记忆管理 API（列表/删行/查看/清空）',
   [(1083, 1147)]),
 'routes/b_macro.py': ('routes.b_macro — 任务宏 API（存档/重放/删除）',
   [(750, 855)]),
 'routes/b_admin.py': ('routes.b_admin — 管理员/自启动/经验/连接器/状态/配置/用量/快捷链/通知/版本/规则',
   [(42, 84), (85, 125), (126, 215), (216, 329), (994, 1007), (1361, 1582)]),
}

# v1.0.401 归位后的 import 路径替换（源数据是归位前的 43c6119 版本）
REPL = {
    'import rule_check': 'from services import rule_check',
    'from config_store import': 'from core.config_store import',
    'from self_repair import': 'from services.self_repair import',
    'from scheduler import': 'from services.scheduler import',
    'from version_store import': 'from services.version_store import',
    'from conversations import': 'from services.conversations import',
    'from knowledge_ops import': 'from services.knowledge_ops import',
    'import close_guard': 'from desktop import close_guard',
}

for path, (doc, ranges) in MODS.items():
    body = '\n\n'.join(seg(a, b) for a, b in ranges)
    name = path.split('b_')[1].split('.')[0]
    # v1.0.401 归位后的 import 路径替换
    for _old, _new in REPL.items():
        body = body.replace(_old, _new)
    content = ('# -*- coding: utf-8 -*-\n"""%s\n"""\n' % doc) + HDR + '\n' + \
              'bp = Blueprint("mgmt_%s", __name__)\n\n' % name + body + '\n'
    io.open(path, 'w', encoding='utf-8').write(content)
    print('written', path, len(content), 'chars')

# b_admin 需要 b_models 的 mask_models（status/config 脱敏复用）
ba = io.open('routes/b_admin.py', encoding='utf-8').read()
if 'from routes.b_models import mask_models' not in ba:
    ba = ba.replace('bp = Blueprint("mgmt_admin", __name__)',
                    'from routes.b_models import mask_models  # status/config 脱敏复用\n\n'
                    'bp = Blueprint("mgmt_admin", __name__)', 1)
    io.open('routes/b_admin.py', 'w', encoding='utf-8').write(ba)
    print('b_admin: mask_models import added')

# management.py 重组：bp 定义 + 子蓝图嵌套注册 + 测试符号转发
mgmt = seg(1, 35) + '''

# v1.0.402 阶段二：53 条路由按域拆入 6 个子蓝图（URL 全部不变，嵌套注册在 /api 下）
from routes.b_models import bp as models_bp        # noqa: E402
from routes.b_repair import bp as repair_bp        # noqa: E402
from routes.b_ocr_admin import bp as ocr_bp        # noqa: E402
from routes.b_memory import bp as memory_bp        # noqa: E402
from routes.b_macro import bp as macro_bp          # noqa: E402
from routes.b_admin import bp as admin_bp          # noqa: E402

for _sub in (models_bp, repair_bp, ocr_bp, memory_bp, macro_bp, admin_bp):
    bp.register_blueprint(_sub)

# 存量测试的符号转发（定义处已随域迁移）
from routes.b_admin import (  # noqa: F401,E402
    _autostart_status, _parse_autostart_value, _admin_task_exists,
    _runas_schtasks, _AUTOSTART_TASK, _AUTOSTART_RUN_KEY, _AUTOSTART_HIDE_ARG)
from routes.b_repair import (  # noqa: F401,E402
    _rebuild_script, _source_has_changes)
'''
io.open('routes/management.py', 'w', encoding='utf-8').write(mgmt)
print('management.py reorganized:', len(mgmt.split('\n')), 'lines')
print('PART B SPLIT DONE')

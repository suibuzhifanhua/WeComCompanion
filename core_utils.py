# -*- coding: utf-8 -*-
"""core_utils.py — 兼容转发壳（v1.0.402 阶段二拆分）

原 1395 行大杂烩已按域拆入 core/ 包：
  paths（路径/产物库常量）logging（日志）config（配置/脱敏/Schema）
  workspace_io（产物库写入/迁移/台账/DND）ocr（引擎/安装/更新）
  instance_lock（单实例，v1.0.646 改名）notify（通知中心/Toast）
  files（文件内容解析，v1.0.647 起含老格式 Office 转换）
  secrets（密钥保护，v1.0.647 自顶层 security.py 归位——壳不转发它，直接 core.secrets）

本文件仅为**存量 import 兼容**保留——所有符号从 core.* 转发。
新代码请直接 `from core.xxx import yyy`。

v1.0.655（B4）：AST 级引用分析 + 消费方逐名文本复核，撤除 30 个经壳零引用
转发项（BASE/_LOG_*/_OCR_ENGINE/_DND_* 等），转发面 85→55（_ocr_latest_version
因 test_ocr_install 经 cu 桩打保留）。需要时直接 from core.<域> import
（与 v1.0.647 撤除 office 转发同口径）。 """
import threading  # v1.0.402：测试 patch cu.threading.Thread（OCR 安装线程 mock）需要
from core.paths import (resource_path,  # noqa: F401
                        workspace_root, ws_sub, kb_dir, ws_rel_path, companion_log_dir,
                        _WORKSPACE_SUBS, KB_ROOT, KB_CATEGORIES,
                        _ALLOWED_SUBS)
from core.logging import (_timestamp, _lazy_requests,  # noqa: F401
                          log_event, last_agent_activity_epoch,
                          READ_TRUNCATE_BYTES)
from core.config import (config_path, mask_key,  # noqa: F401
                         _mask_args, _pick_int, _pick_flex)
from core.workspace_io import (migrate_legacy_layout, migrate_knowledge_base,  # noqa: F401
                               save_workspace_file, append_ledger,
                               workspace_size_info, save_chat_log,
                               save_template_result, _safe_ws_path,
                               dnd_allow_paths, dnd_allowed, _safe_ws_read_path)
from core.ocr import (_OCR_RUNTIME_STATE, _OCR_WHEELS, _OCR_SUB2PKG, _OCR_DL,  # noqa: F401
                      ensure_ocr_runtime,
                      _ocr_needed_pkgs, _ocr_pick_wheel,
                      _ocr_write_versions, _verkey, _ocr_latest_version,  # 后者测试经 cu 桩打
                      ocr_install_status, install_ocr_async,
                      ocr_versions, ocr_check_updates, ocr_ready_or_install,
                      ocr_selfcheck, ocr_size_hint)  # v1.0.609 实测体积
from core.instance_lock import acquire_single_instance_mutex  # noqa: F401
# v1.0.646 改名：原 core/security.py 与顶层 security.py（DPAPI 密钥保护）同名不同义，
# 前者是进程单实例 mutex，改名消除导航混淆；符号名与转发表面不变。
from core.notify import native_toast  # noqa: F401（兼容壳转发）
# v1.0.647：原 core/office.py 并入 core/files.py（唯一消费者），其 4 个 office 符号
# 在本壳的转发面全库 0 人取用，一并撤除（_LEGACY_OFFICE_MAP/convert_legacy_office 需用时
# 直接 from core.files import，与 core.ocr 系列同口径）。
from core.files import _parse_file_content  # noqa: F401
from core.config import extract_json, validate_schema, _valid_json_schema  # noqa: F401

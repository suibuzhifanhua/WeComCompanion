"""
routes/management — 管理域聚合蓝图（status/config/模型/连接器/经验库/规则等）
====================================================================
从 backend.py 逐字迁入（T05）。v1.0.402 阶段二起本体只是「把 6 个域子蓝图
嵌套注册在 /api 下」的聚合器；域内实现分别在 b_models/b_repair/b_ocr_admin/
b_memory/b_macro/b_admin，符号转发见文末。
"""
from flask import Blueprint

bp = Blueprint("management", __name__, url_prefix="/api")

# v1.0.402 阶段二：路由按域拆入 6 个子蓝图（URL 全部不变，嵌套注册在 /api 下）。
# 条数不写在注释里——它会过期（这里曾挂着一个与实际差近三倍的旧数字，没人回来改）。
# 要准确值：len(backend.app.url_map.iter_rules())，tests/test_v619_doc_truth.py 就是这么算的。
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

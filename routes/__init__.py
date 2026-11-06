"""
routes — HTTP 路由蓝图（从 backend.py 抽离，T05）
====================================================================
所有蓝图统一 url_prefix=/api；路由字符串已去 /api 前缀。
  management/skills/templates/chat/wecom/workspace
"""
from .management import bp as management_bp
from .skills import bp as skills_bp
from .templates import bp as templates_bp
from .chat import bp as chat_bp
from .wecom import bp as wecom_bp
from .workspace import bp as workspace_bp
from .study import bp as study_bp
from .knowledge import bp as knowledge_bp
from .security import bp as security_bp
from .conversations import bp as conversations_bp
# v1.0.647：schedule 蓝图（定时任务 CRUD，5 个端点）并入 b_admin（设置页域），
# 不再单列；管理域蓝图均经 management 聚合注册（见 management.py）。
from .personal import bp as personal_bp

__all__ = ["management_bp", "skills_bp", "templates_bp", "chat_bp",
           "wecom_bp", "workspace_bp", "study_bp", "knowledge_bp",
           "security_bp", "conversations_bp", "personal_bp"]


def register_all(app):
    """注册全部蓝图到 app（统一 /api 前缀）。"""
    for _bp in (management_bp, skills_bp, templates_bp, chat_bp, wecom_bp, workspace_bp,
                study_bp, knowledge_bp, security_bp, conversations_bp,
                personal_bp):
        app.register_blueprint(_bp)

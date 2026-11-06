# -*- coding: utf-8 -*-
"""routes/study — 内置学习中心跑课引擎控制（v1.0.276）。
start / stop / status 三个端点，UI「🎓 学习中心」页轮询 status 刷新。

注意：engine 必须【惰性导入】——study_engine 顶层 import routes.chat，
若此处模块级 from services import study_engine 会形成循环导入（routes/__init__ →
routes.study → study_engine → routes.chat），首次请求时才加载。
"""
from flask import Blueprint, jsonify

bp = Blueprint("study", __name__, url_prefix="/api")


def _engine():
    from services.study_engine import engine
    return engine


@bp.route("/study/status")
def study_status():
    return jsonify(_engine().status())


@bp.route("/study/start", methods=["POST"])
def study_start():
    return jsonify(_engine().start())


@bp.route("/study/stop", methods=["POST"])
def study_stop():
    return jsonify(_engine().stop())

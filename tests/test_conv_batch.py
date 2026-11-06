# -*- coding: utf-8 -*-
"""test_conv_batch — 会话批量删除接口（v1.0.375）回归。

背景：会话下拉列表加「批量管理」模式（多选/全选/反选 → 批量删除）。
后端 POST /api/conversations/batch_delete {ids:[...]}：逐条调 conversations.delete，
删除/不存在分别汇报；单次上限 200；空/非列表 400。

用例：
1. 建 3 条 → 批量删 2 条 → deleted=2、missing=[]、列表只剩 1 条
2. 混入不存在 id → missing 汇报、不影响已存在条目的删除
3. 空 ids / 非列表 → 400
4. >200 条截断为前 200（不 500）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

from core import config_store  # noqa: E402
import core_utils  # noqa: E402
from services import conversations  # noqa: E402


def _setup(prefix="wec_convbatch_"):
    tmpdir = tempfile.mkdtemp(prefix=prefix)
    config_store.config_path = lambda: os.path.join(tmpdir, "config.json")
    ws = os.path.join(tmpdir, "产物库")
    os.makedirs(ws, exist_ok=True)
    core.paths.workspace_root = lambda: ws
    conversations.workspace_root = lambda: ws   # 名字绑定：模块内 import 需单独换桩
    return tmpdir


def _make_client():
    from flask import Flask
    from routes.conversations import bp as conv_bp
    app = Flask(__name__)
    app.register_blueprint(conv_bp)
    return app.test_client()


import tempfile  # noqa: E402


def test_batch_delete_basic():
    _setup()
    c = _make_client()
    ids = [conversations.create("t%d" % i) for i in range(3)]
    r = c.post("/api/conversations/batch_delete", json={"ids": ids[:2]})
    assert r.status_code == 200 and r.get_json().get("ok") is True, r.get_json()
    d = r.get_json()
    assert sorted(d["deleted"]) == sorted(ids[:2]), d
    assert d["missing"] == [], d
    rest = c.get("/api/conversations").get_json()["conversations"]
    assert [m["id"] for m in rest] == [ids[2]], rest
    print("PASS 批量删除 2/3 条，剩余列表正确")


def test_batch_delete_missing_mixed():
    _setup("wec_convbatch2_")
    c = _make_client()
    cid = conversations.create("keep-me")
    r = c.post("/api/conversations/batch_delete",
               json={"ids": [cid, "conv_not_exist_1", "conv_not_exist_2"]})
    d = r.get_json()
    assert d.get("ok") is True, d
    assert d["deleted"] == [cid] and len(d["missing"]) == 2, d
    rest = c.get("/api/conversations").get_json()["conversations"]
    assert rest == [], rest
    print("PASS 混入不存在 id：missing 汇报且不影响真实删除")


def test_batch_delete_validation():
    _setup("wec_convbatch3_")
    c = _make_client()
    assert c.post("/api/conversations/batch_delete", json={"ids": []}).status_code == 400
    assert c.post("/api/conversations/batch_delete", json={"ids": "x"}).status_code == 400
    assert c.post("/api/conversations/batch_delete", json={}).status_code == 400
    # >200 条截断执行，不 500
    r = c.post("/api/conversations/batch_delete", json={"ids": ["x%d" % i for i in range(300)]})
    assert r.status_code == 200, r.status_code
    assert len(r.get_json()["missing"]) == 200
    print("PASS 空/非列表 400；>200 条截断为 200 不 500")

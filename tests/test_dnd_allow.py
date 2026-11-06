# -*- coding: utf-8 -*-
"""test_dnd_allow — 拖拽路径白名单（v1.0.369）回归。

背景：聊天框支持拖拽本地文件/文件夹。通道 A（WebView2 File.path）只引用原路径、
零拷贝——但 agent 只读工具被 _safe_ws_path 限制在产物库内，外部路径读不了。
v1.0.369 引入进程级只读白名单：前端拖入时 POST /api/workspace/allow 登记，
read_workspace_file / list_workspace_files(path=...) 对白名单路径放行；
写类工具（save_workspace_file 走 _safe_ws_path）不放行。

用例：
1. 未登记的外部文件 read_workspace_file → __error__（原安全边界不变）
2. dnd_allow_paths 登记 → read 直接读到外部文件内容（含中文）
3. list_workspace_files(path=白名单目录) 列出外部文件夹内容（含子目录标记）
4. 未登记的其它路径仍拒绝（白名单是精确匹配，不扩散）
5. _safe_ws_path 行为不变（产物库内收敛 / 外部仍 None——写工具安全边界不变）
6. POST /api/workspace/allow 接口：正常登记 ok=true；空/脏数据 400
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.result import is_err, err_msg  # noqa: E402
import core  # noqa: E402

from core import config_store  # noqa: E402
import core_utils  # noqa: E402
import agent_tools.executors as ex  # noqa: E402
from core_utils import dnd_allow_paths, dnd_allowed, _safe_ws_path, _safe_ws_read_path  # noqa: E402


def _setup(tmp_prefix="wec_dnd_"):
    tmpdir = tempfile.mkdtemp(prefix=tmp_prefix)
    config_store.config_path = lambda: os.path.join(tmpdir, "config.json")
    ws = os.path.join(tmpdir, "产物库")
    os.makedirs(ws, exist_ok=True)
    core.paths.workspace_root = lambda: ws
    ex.workspace_root = lambda: ws          # executors 独立绑定，需同步换桩
    return tmpdir, ws


def _ext_tree():
    """建一个「外部」目录树（模拟用户拖入的文件夹）。"""
    ext = tempfile.mkdtemp(prefix="wec_dnd_ext_")
    with open(os.path.join(ext, "a.txt"), "w", encoding="utf-8") as f:
        f.write("你好，白名单世界 ABC123")
    os.makedirs(os.path.join(ext, "sub"), exist_ok=True)
    with open(os.path.join(ext, "sub", "b.txt"), "w", encoding="utf-8") as f:
        f.write("nested")
    return ext


def test_read_blocked_before_allow():
    _setup("wec_dnd_a_")
    ext = _ext_tree()
    r = ex._run_read_workspace_file({"path": os.path.join(ext, "a.txt")}, {})
    assert is_err(r), r
    assert "产物库" in err_msg(r), r
    print("PASS 未登记的外部路径 read_workspace_file 拒绝")


def test_read_allowed_after_dnd_allow():
    _setup("wec_dnd_b_")
    ext = _ext_tree()
    n = dnd_allow_paths([ext, os.path.join(ext, "a.txt"), os.path.join(ext, "sub", "b.txt")])
    assert n == 3, n
    r = ex._run_read_workspace_file({"path": os.path.join(ext, "a.txt")}, {})
    assert r.get("ok") is True, r
    assert "白名单世界" in (r.get("content") or ""), r.get("content")
    # 子目录文件同样可读
    r2 = ex._run_read_workspace_file({"path": os.path.join(ext, "sub", "b.txt")}, {})
    assert r2.get("ok") is True and "nested" in (r2.get("content") or ""), r2
    print("PASS 登记白名单后 read_workspace_file 直读外部原路径（含子目录）")


def test_list_dnd_dir():
    _setup("wec_dnd_c_")
    ext = _ext_tree()
    assert dnd_allow_paths([ext]) == 1
    r = ex._run_list_workspace_files({"path": ext}, {})
    assert r.get("ok") is True, r
    names = {f["name"]: f["is_dir"] for f in r.get("files", [])}
    assert names.get("a.txt") is False and names.get("sub") is True, names
    # 未登记目录仍拒绝
    other = tempfile.mkdtemp(prefix="wec_dnd_other_")
    r2 = ex._run_list_workspace_files({"path": other}, {})
    assert is_err(r2), r2
    print("PASS list_workspace_files(path=) 白名单目录列出 / 未登记目录拒绝")


def test_whitelist_exact_match_and_write_boundary():
    _setup("wec_dnd_d_")
    ext = _ext_tree()
    dnd_allow_paths([os.path.join(ext, "a.txt")])
    # 精确匹配：登记的是 a.txt，b.txt / 目录本身不可读
    assert dnd_allowed(os.path.join(ext, "a.txt")) is True
    assert dnd_allowed(os.path.join(ext, "sub", "b.txt")) is False
    assert dnd_allowed(ext) is False
    # 写边界不变：_safe_ws_path 对外部路径恒 None（save_workspace_file 等写工具不受白名单影响）
    assert _safe_ws_path(os.path.join(ext, "a.txt")) is None
    # 产物库内路径行为不变
    ws = core.paths.workspace_root()
    p = os.path.join(ws, "上传", "x.txt")
    assert _safe_ws_read_path(p) == os.path.realpath(p)
    print("PASS 白名单精确匹配不扩散 / 写边界（_safe_ws_path）行为不变")


def test_allow_endpoint():
    tmpdir, _ws = _setup("wec_dnd_e_")
    from flask import Flask
    from routes.workspace import bp as workspace_bp
    app = Flask(__name__)
    app.register_blueprint(workspace_bp)
    c = app.test_client()
    ext = _ext_tree()
    r = c.post("/api/workspace/allow", json={"paths": [ext, os.path.join(ext, "a.txt")]})
    assert r.status_code == 200 and r.get_json().get("ok") is True, r.get_json()
    assert r.get_json().get("allowed") == 2
    # v1.0.370：items 含 realpath/is_dir/name（前端 chip 渲染依赖）
    items = r.get_json().get("items") or []
    assert len(items) == 2, items
    by = {i["name"]: i for i in items}
    assert by["a.txt"]["is_dir"] is False, by
    assert by[os.path.basename(ext)]["is_dir"] is True, by
    assert all(os.path.isabs(i["path"]) for i in items), items
    assert dnd_allowed(ext) is True
    # 空/脏数据 → 400
    r2 = c.post("/api/workspace/allow", json={"paths": []})
    assert r2.status_code == 400, r2.status_code
    r3 = c.post("/api/workspace/allow", json={"paths": ["", None, "   "]})
    assert r3.status_code == 400, r3.status_code
    print("PASS POST /api/workspace/allow 正常登记（含 items 类型标注）/ 空脏数据 400")

# -*- coding: utf-8 -*-
"""tests/test_route_map.py — v1.0.407 路由表断言（防 v1.0.402 拆分事故再犯）。

背景：v1.0.402 拆 management → 6 子蓝图时发生两类结构性错误，均**不报错、只错位**：
  ①孤儿装饰器：b_admin 遗留 @bp.route("/models") GET（函数体已搬走），Flask 把它
    装饰到下方 api_rules → GET /api/models 返回规则模块列表，模型管理页全链失效
    （v1.0.407 用户实测「模型添加不了」）；
  ②装饰器遗失：get_models / api_experiences_list 的 GET 装饰器丢失 → 404。
本测试断言关键端点的「方法+路径 → 端点函数」映射唯一且正确；新增路由时补进
_EXPECTED 即可长期护航。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 方法+路径 → endpoint 后缀（蓝图名前缀省略，只需唯一）
_EXPECTED = {
    ("GET", "/api/models"): "get_models",
    ("POST", "/api/models"): "post_models",
    ("POST", "/api/models/probe"): "api_models_probe",
    ("POST", "/api/models/scan"): "api_models_scan",
    ("POST", "/api/models/test"): "api_models_test",
    ("POST", "/api/models/bench7"): "api_models_bench7",
    ("GET", "/api/experiences"): "api_experiences_list",
    ("POST", "/api/experiences"): "api_experiences_add",
    ("POST", "/api/rules/check"): "api_rules",
    ("GET", "/api/rules/list"): "api_rules",
    ("GET", "/api/schedules"): None,          # 只断言存在
    ("GET", "/api/workspace"): None,
    ("POST", "/api/workspace/allow"): None,
    ("GET", "/api/macro/list"): None,
    ("POST", "/api/webui/open"): None,
    # v1.0.408 版本管理端点接线（此前业务层完整但 HTTP 层从未注册——契约校验抓出）
    ("GET", "/api/versions"): None,
    ("POST", "/api/versions/switch"): None,
    # v1.0.614 批6：个人信息/后台开关（此前这些值只在 config.json 里，界面无处可填）
    ("GET", "/api/personal/config"): "api_personal_get",
    ("POST", "/api/personal/config"): "api_personal_post",
    # v1.0.627 聊天里「AI 产出文件」一键定位（路径只认产物库内，越界 404）
    ("POST", "/api/workspace/locate"): "api_workspace_locate",
    ("POST", "/api/components/share-path"): "api_components_share_path",
}


def _build_map():
    import backend
    m = {}
    for r in backend.app.url_map.iter_rules():
        for meth in r.methods - {"HEAD", "OPTIONS"}:
            m[(meth, str(r))] = r.endpoint
    return m


def test_route_map():
    got = _build_map()
    problems = []
    for (meth, path), want in _EXPECTED.items():
        ep = got.get((meth, path))
        if ep is None:
            problems.append(f"缺失路由: {meth} {path}")
        elif want and not ep.endswith("." + want):
            problems.append(f"{meth} {path} 映射错误: 期望 *.{want}，实际 {ep}")
    # 关键端点必须唯一命中正确函数（防孤儿装饰器遮蔽——映射错误已覆盖，这里再断言
    # GET /api/models 返回方不是规则端点）
    assert not problems, "路由表异常：\n  " + "\n  ".join(problems)


def test_get_models_returns_model_list():
    """端到端防遮蔽：GET /api/models 返回的必须是模型数组（不是 {"modules":[...]}）。"""
    import backend
    backend._AUTH_DISABLED = True   # conftest 已置，双保险
    c = backend.app.test_client(use_cookies=False)
    d = c.get("/api/models").get_json()
    assert isinstance(d, list), f"GET /api/models 应返回模型数组，实际: {str(d)[:120]}"
    if d:
        assert "module" not in d[0] or "base_url" in d[0], "返回的不是模型结构"


def test_no_orphan_route_decorators():
    """AST 扫描 routes/：@bp.route 调用总数 vs 实际挂到函数上的数量，差集 = 孤儿装饰器
    （会装饰到下一个无关函数、错位路由——v1.0.402 拆分事故的精确特征）。
    注意：一函数叠多个 @bp.route（如 api_rules 服务 check POST + list GET）是合法的。"""
    import ast
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "routes")
    orphans = []
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(root, fn), encoding="utf-8").read())
        declared, attached = set(), set()
        for node in ast.walk(tree):
            is_bp_route = (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "route"
                           and isinstance(node.func.value, ast.Name) and node.func.value.id == "bp")
            if is_bp_route:
                declared.add(node.lineno)
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                for d in fnode.decorator_list:
                    if (isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route"
                            and isinstance(d.func.value, ast.Name) and d.func.value.id == "bp"):
                        attached.add(d.lineno)
        for ln in sorted(declared - attached):
            orphans.append(f"{fn}:{ln} @bp.route(...) 未挂载到任何函数")
    assert not orphans, "存在孤儿装饰器（会错位路由）: " + "; ".join(orphans)


if __name__ == "__main__":
    test_route_map()
    test_get_models_returns_model_list()
    test_no_orphan_route_decorators()
    print("PASS route map 3 场景")

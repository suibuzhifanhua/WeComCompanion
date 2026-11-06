# -*- coding: utf-8 -*-
"""test_self_repair_v2 — 自我修复优化（v1.0.373）回归：聚类/定位/验证分流/失败沉淀。

背景（2026-09-24 七项优化）：
- _cluster_log_errors：错误行指纹聚类（同一错误重复 N 次归并为「[出现 N 次]」）
- _autofix_locate_file：优先解析【文件: 相对路径】约定标记；_autofix_allowed 放开 .js/.html
- attempt_code_fix 验证分流：.js→node --check、.html→结构校验（此前只支持 .py）
- 补丁被还原时沉淀失败教训进经验库
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import self_repair as SR  # noqa: E402


def test_cluster_log_errors():
    """错误行指纹聚类：时间戳/数字归并，出现次数降序，封顶 max_groups。"""
    lines = [
        "2026-09-24 10:00:01 [tool-error] read failed: attempt 1",
        "2026-09-24 10:00:05 [tool-error] read failed: attempt 22",
        "2026-09-24 10:00:09 [tool-error] read failed: attempt 333",
        "2026-09-24 10:01:00 [repair] 流式错误：timeout",
    ]
    out = SR._cluster_log_errors(lines)
    assert len(out) == 2, out
    assert out[0].startswith("[出现 3 次]"), out   # 重复 3 次的排最前
    assert "read failed: attempt #" in out[0], out[0]   # 数字已归并为 #
    assert out[1].startswith("[出现 1 次]"), out[1]
    # 超过 max_groups 时截断（用文本差异制造 50 个不同指纹）
    many = ["2026-09-24 10:00:00 err kind_%s happened badly" % chr(97 + i % 26) + "x" * (i // 26)
            for i in range(50)]
    assert len(SR._cluster_log_errors(many, max_groups=10)) == 10
    print("PASS 错误指纹聚类（计数/归并/降序/封顶）")


def test_locate_file_marker_and_fallback():
    """定位：优先解析【文件: 相对路径】约定标记；禁改清单仍生效。"""
    tmp = tempfile.mkdtemp(prefix="sr_loc_")
    orig_root = SR._PROJECT_ROOT
    try:
        SR._PROJECT_ROOT = tmp   # 指向临时"项目"
        os.makedirs(os.path.join(tmp, "js"))
        target = os.path.join(tmp, "js", "10-chat.js")
        with io.open(target, "w", encoding="utf-8") as f:
            f.write("// chat js\n")
        with io.open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        # ①【文件: js/10-chat.js】→ 直接命中
        got = SR._autofix_locate_file("按钮错位。【文件: js/10-chat.js】需修正 class")
        assert got and os.path.samefile(got, target), got
        # ② 禁改清单（config.json）→ 拒绝
        got2 = SR._autofix_locate_file("【文件: config.json】改一下")
        assert got2 is None, got2
        # ③ 路径穿越 → 拒绝（abspath 后不在项目根内）
        got3 = SR._autofix_locate_file("【文件: ../outside.py】")
        assert got3 is None, got3
    finally:
        SR._PROJECT_ROOT = orig_root
    print("PASS 定位：【文件:】标记命中/禁改拒绝/穿越拒绝")


def test_autofix_allowed_extensions():
    """白名单放开 .js/.html，仍拒绝禁改清单与项目外文件。"""
    tmp = tempfile.mkdtemp(prefix="sr_alw_")
    orig_root = SR._PROJECT_ROOT
    try:
        SR._PROJECT_ROOT = tmp
        ok_js = os.path.join(tmp, "js", "a.js")
        os.makedirs(os.path.dirname(ok_js), exist_ok=True)
        io.open(ok_js, "w", encoding="utf-8").close()
        assert SR._autofix_allowed(ok_js) is True
        ok_html = os.path.join(tmp, "index.html")
        io.open(ok_html, "w", encoding="utf-8").close()
        assert SR._autofix_allowed(ok_html) is True
        assert SR._autofix_allowed(os.path.join(tmp, "config.json")) is False
        assert SR._autofix_allowed(os.path.join(tmp, "build.py")) is False
        assert SR._autofix_allowed(os.path.join(tmp, "self_repair.py")) is False
        assert SR._autofix_allowed("C:/Windows/notepad.exe") is False
    finally:
        SR._PROJECT_ROOT = orig_root
    print("PASS 白名单：.js/.html 放开，禁改清单与项目外仍拒绝")


def test_verify_patch_js_branch():
    """验证分流：.js 补丁在 strict 模式下**必须拿不到"通过"**——
    要么被 node --check 判语法错误，要么因"本环境没有校验器"而拒绝放行。
    （v1.0.618 之前它是长期红：check_js_file 在缺 node 时返回 (True, "跳过")，
    于是"真坏文件"和"没法检查"长得一样，自检分支形同虚设。）"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "services", "self_repair.py"), encoding="utf-8").read()
    # 静态断言：attempt_code_fix 内含 js/html 分流（防后续误删），且实现来自随包发布的
    # core.frontend_check（tools/ 不进冻结包，正式包里 import 必失败）
    assert "check_js_file" in src and "check_div_balance" in src, "验证分流被移除"
    assert "from core.frontend_check import check_js_file" in src, "JS 校验仍指向包外的 tools/"
    from core.frontend_check import check_js_file
    tmp = tempfile.mkdtemp(prefix="sr_vfy_")
    bad = os.path.join(tmp, "bad.js")
    with io.open(bad, "w", encoding="utf-8") as f:
        f.write("function ( { var var var;")
    ok, tail = check_js_file(bad, strict=True)
    assert ok is False, tail
    from core import portable
    if portable.find_node():
        # 有 node 时必须是真的语法拦截，而不是"跑不起来"
        assert "未找到 node" not in tail and "node 不可执行" not in tail, tail
    print("PASS 验证分流：坏 JS（或无校验器）都被 strict 判否")

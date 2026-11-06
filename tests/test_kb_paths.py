"""tests/test_kb_paths.py — v1.0.294 知识库（产物库/知识库/课件·题库·制度）路径逻辑单测。

覆盖知识库特性的关键路径处理（回归防护，重点在「斜杠归一化」这个隐蔽坑）：
  1) _ALLOWED_SUBS 含扁平 5 子目录 + 知识库 3 个嵌套子目录（正斜杠，跨平台一致）；
  2) ws_sub 斜杠归一化：正斜杠 / 反斜杠 都解析到 知识库/<分类>/（而非误落到 产出）；
     —— 反斜杠是 Windows 上 os.path.join 的产物，若不归一化会被当作「未知子目录」回退到 产出；
  3) ws_sub 未知子目录回退 产出（既有口径不变）；
  4) kb_dir 返回正确分类目录、非法分类回退 课件；
  5) save_workspace_file 写入 知识库/题库 真实落盘并读回（随后清理）；
  6) migrate_knowledge_base 在【临时目录】上验证（不碰真实 dist 数据）：
     知识/*.md→知识库/课件/、产出/答题记录_*.md→知识库/题库/、非答题记录不迁移、制度/ 预留。
运行：python tests/test_kb_paths.py"""
import os
import sys
import shutil
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # v1.0.657 发布去痕：旧机器硬编码路径改 __file__ 推导
import core_utils as C  # noqa: E402


def _norm(p):
    return str(p).replace("\\", "/")


_ALLOWED_SUBS_NEEDLES = ["产出", "知识库/课件", "知识库/题库", "知识库/制度"]


@pytest.mark.parametrize("needle", _ALLOWED_SUBS_NEEDLES)
def test_allowed_subs(needle):
    assert needle in C._ALLOWED_SUBS
    print("PASS _ALLOWED_SUBS 含扁平 5 目录 + 知识库 3 嵌套子目录")


def test_ws_sub_forward_slash():
    d = C.ws_sub("知识库/课件")
    assert _norm(d).endswith("知识库/课件"), f"正斜杠应解析到 知识库/课件，实际 {d}"
    print(f"PASS ws_sub 正斜杠 → {_norm(d)}")


def test_ws_sub_backslash():
    d = C.ws_sub("知识库\\课件")
    assert _norm(d).endswith("知识库/课件"), f"反斜杠应归一化解析到 知识库/课件，实际 {d}"
    print(f"PASS ws_sub 反斜杠（归一化）→ {_norm(d)}")


def test_ws_sub_unknown_fallback():
    d = C.ws_sub("不存在的目录")
    assert _norm(d).endswith("/产出") or _norm(d).endswith("产物库/产出"), f"未知子目录应回退 产出，实际 {d}"
    print(f"PASS ws_sub 未知 → 回退 {_norm(d)}")


def test_ws_sub_flat_compat():
    d = C.ws_sub("产出")
    assert _norm(d).endswith("产出"), f"产出 应保持不变，实际 {d}"
    print(f"PASS ws_sub 扁平兼容 → {_norm(d)}")


def test_kb_dir():
    d = C.kb_dir("题库")
    assert _norm(d).endswith("知识库/题库"), f"kb_dir(题库) 应为 知识库/题库，实际 {d}"
    d2 = C.kb_dir("非法分类")
    assert _norm(d2).endswith("知识库/课件"), f"非法分类应回退 课件，实际 {d2}"
    print(f"PASS kb_dir 题库→{_norm(d)}；非法分类→课件")


def test_save_to_kb_quiz():
    out = C.save_workspace_file("知识库/题库", "__kb_test__.md", "知识库写入测试")
    assert isinstance(out, dict) and out.get("ok"), f"save_workspace_file 到 知识库/题库 应成功：{out}"
    p = out["path"]
    assert _norm(p).endswith("知识库/题库/__kb_test__.md"), f"应落到 知识库/题库：{p}"
    with open(p, encoding="utf-8") as f:
        assert "知识库写入测试" in f.read()
    os.remove(p)  # 清理临时文件
    print(f"PASS save_workspace_file 写入 知识库/题库 并读回（{_norm(p)}）")


def test_migrate_knowledge_base_temp():
    tmp = tempfile.mkdtemp(prefix="kb_migrate_")
    try:
        ws = os.path.join(tmp, "产物库")
        old_know = os.path.join(ws, "知识")
        out_dir = os.path.join(ws, "产出")
        os.makedirs(old_know)
        os.makedirs(out_dir)
        with open(os.path.join(old_know, "课程A.md"), "w", encoding="utf-8") as f:
            f.write("课件A")
        with open(os.path.join(out_dir, "答题记录_考试B_2026-09-01.md"), "w", encoding="utf-8") as f:
            f.write("答题B")
        with open(os.path.join(out_dir, "学习进度_2026-09-01.md"), "w", encoding="utf-8") as f:
            f.write("进度（不应迁移）")
        moved = C.migrate_knowledge_base(data_dir=tmp)
        assert os.path.exists(os.path.join(ws, "知识库", "课件", "课程A.md")), "课程A.md 应迁到 知识库/课件/"
        assert os.path.exists(os.path.join(ws, "知识库", "题库", "答题记录_考试B_2026-09-01.md")), "答题记录 应迁到 知识库/题库/"
        assert os.path.exists(os.path.join(out_dir, "学习进度_2026-09-01.md")), "学习进度 应留在 产出/（非答题记录不迁移）"
        assert os.path.isdir(os.path.join(ws, "知识库", "制度")), "知识库/制度 应存在（预留）"
        assert not os.path.exists(old_know), "旧 知识/ 目录清空后应被删除"
        assert len(moved) == 2, f"应迁移 2 项，实际 {moved}"
        print(f"PASS migrate_knowledge_base（临时目录）：{moved}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for _needle in _ALLOWED_SUBS_NEEDLES:
        test_allowed_subs(_needle)
    test_ws_sub_forward_slash()
    test_ws_sub_backslash()
    test_ws_sub_unknown_fallback()
    test_ws_sub_flat_compat()
    test_kb_dir()
    test_save_to_kb_quiz()
    test_migrate_knowledge_base_temp()
    print("ALL PASS（知识库路径逻辑 + 斜杠归一化 + 幂等迁移）")

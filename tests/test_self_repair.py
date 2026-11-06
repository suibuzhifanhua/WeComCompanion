# -*- coding: utf-8 -*-
"""tests/test_self_repair.py — v1.0.348 自我修复子系统单测。
fake _llm_complete + 临时产物库，验证：采集、白名单自动落地（skill_lesson/experience）、
去重跳过、code_fix 只出报告不落任何代码、报告文件生成。"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

import core_utils  # noqa: E402

TMP = tempfile.mkdtemp(prefix="wec_repair_")
# v1.0.348 隔离修正：self_repair 在模块顶层 from-import 了 workspace_root/_load_experiences/
# _save_experiences（绑定的是各模块的原始函数对象），只 patch core_utils 不会生效——
# 必须 patch SR 自己的引用，否则测试会读写真实的经验库/配置/报告目录（已实测造成污染）。
core.paths.workspace_root = lambda: TMP
from services import self_repair as SR  # noqa: E402
from core import config_store  # noqa: E402

SR.workspace_root = lambda: TMP
_EXP_STORE = []
SR._load_experiences = lambda: [dict(x) for x in _EXP_STORE]
SR._save_experiences = lambda items: _EXP_STORE.clear() or _EXP_STORE.extend(items)
SR.save_config = lambda cfg: None   # v1.0.348 事故教训：绝不让学生测试写真实 config.json
config_store.workspace_root = lambda: TMP

# v1.0.619（测试卫生）：自带日志样本。此前本文件不写 日志/companion.log，
# 而 _read_log_errors 读的就是这个文件——4 个用例（applies_whitelist / dedup /
# progress / cancel）**只在全量跑时通过**，靠的是别的测试模块把
# core.paths.workspace_root 泄漏到一个恰好有 companion.log 的目录；单独跑
# `pytest tests/test_self_repair.py` 一直是红的（采集到 0 行 → "无需体检" 直接返回）。
_LOG_DIR = os.path.join(TMP, "日志")
os.makedirs(_LOG_DIR, exist_ok=True)
with open(os.path.join(_LOG_DIR, "companion.log"), "w", encoding="utf-8") as _f:
    _f.write("2026-01-01 09:00:00 [agent] tool-error: 点击「下一步」超时，页面未响应\n"
             "2026-01-01 09:00:05 [agent] tool-error: 某网站选项需用坐标点击，序号点不中\n"
             "2026-01-01 09:00:09 [llm] 异常：附件上传后页面不刷新，重复上传\n")

FAKE_LLM_JSON = json.dumps({"findings": [
    {"type": "skill_lesson", "skill_id": "exam-solver",
     "lesson": "\u67d0\u7f51\u7ad9\u9009\u9879\u9700\u7528\u5750\u6807\u70b9\u51fb\uff0c\u5e8f\u53f7\u70b9\u4e0d\u4e2d",
     "title": "\u7b54\u9898\u70b9\u51fb\u65b9\u5f0f\u6559\u8bad", "severity": "high"},
    {"type": "experience", "module": "oa",
     "problem": "\u9644\u4ef6\u4e0a\u4f20\u540e\u9875\u9762\u4e0d\u5237\u65b0\u4f1a\u91cd\u590d\u4e0a\u4f20",
     "solution": "\u4e0a\u4f20\u540e\u5148 read_wecom_page \u786e\u8ba4\u518d\u7ee7\u7eed", "severity": "mid"},
    {"type": "code_fix", "title": "\u4e0b\u8f7d\u8d85\u65f6\u5e94\u53ef\u914d\u7f6e",
     "detail": "DOWNLOAD_TIMEOUT \u56fa\u5b9a 90s\uff0c\u5927\u6587\u4ef6\u4e0d\u591f\u7528\uff0c\u5efa\u8bae\u653e\u8fdb\u914d\u7f6e", "severity": "low"},
]}, ensure_ascii=False)


def _make_cfg():
    return {"enabled_skills": None, "skill_overrides": {}, "custom_skills": [],
            "models": [{"id": "m1", "name": "T", "base_url": "http://127.0.0.1:59999/v1",
                        "api_key": "", "model": "t", "enabled": True}],
            "connectors": {}, "wecom": {}, "feature_flags": {}}


def _fake_stream(text):
    """v1.0.354 体检切流式后：mock stream_llm_events（yield 事件元组）。"""
    return iter([("token", text), ("done", {"text": text})])


def test_run_self_repair_applies_whitelist():
    cfg = _make_cfg()
    with mock.patch("llm_client.stream_llm_events", return_value=_fake_stream(FAKE_LLM_JSON)):
        r = SR.run_self_repair(cfg)
    assert r["ok"], r
    # skill_lesson 已落地 skill_overrides（带去重标记）
    ov = cfg["skill_overrides"].get("exam-solver", {}).get("prompt", "")
    assert SR._SKILL_MARK in ov and "\u5750\u6807\u70b9\u51fb" in ov, ov[:120]
    # experience 已落经验库（patch 后的临时存储，非真实 experiences.json）
    exps = _EXP_STORE
    assert any("\u4e0d\u5237\u65b0" in str(x.get("problem", "")) for x in exps), exps
    # code_fix 只进待人工，未写任何代码文件
    assert any(m["type"] == "code_fix" for m in r["manual"]), r["manual"]
    # 报告已落盘
    assert r["report"] and os.path.isfile(r["report"]), r.get("report")
    print("PASS 白名单自动落地（技能教训/经验）+ code_fix 只出报告 + 报告落盘")


def test_dedup_second_run_skips():
    cfg = _make_cfg()
    with mock.patch("llm_client.stream_llm_events", return_value=_fake_stream(FAKE_LLM_JSON)):
        SR.run_self_repair(cfg)
        with mock.patch("llm_client.stream_llm_events", return_value=_fake_stream(FAKE_LLM_JSON)):
            r2 = SR.run_self_repair(cfg)
    skipped_types = [s.get("type") for s in r2.get("skipped", [])]
    assert "skill_lesson" in skipped_types and "experience" in skipped_types, r2
    print("PASS 二次体检同内容去重跳过")


def test_empty_data_note():
    with mock.patch.object(SR, "_read_log_errors", return_value=[]), \
         mock.patch.object(SR, "_load_experiences", lambda: []), \
         mock.patch("llm_client.stream_llm_events", return_value=_fake_stream(FAKE_LLM_JSON)):
        r = SR.run_self_repair(_make_cfg())
    assert r["ok"] and r.get("note"), r
    print("PASS 无数据时直接返回无需体检")


def test_bad_llm_output():
    with mock.patch("llm_client.stream_llm_events", return_value=_fake_stream("这不是JSON")), \
         mock.patch.object(SR, "_read_log_errors", return_value=["2026-09-17 10:00:00 [tool-error] x"]):
        r = SR.run_self_repair(_make_cfg())
    assert not r["ok"] and "JSON" in r.get("error", ""), r
    print("PASS 非法模型输出返回结构化错误")


def test_progress_file_written():
    """v1.0.354：体检过程写 progress.json（阶段/字符数/耗时），前端据此展示进度。"""
    import json as _json
    cfg = _make_cfg()
    with mock.patch("llm_client.stream_llm_events", return_value=_fake_stream(FAKE_LLM_JSON)):
        SR.run_self_repair(cfg)
    p = os.path.join(TMP, "\u81ea\u4fee\u590d", "progress.json")
    assert os.path.isfile(p), "progress.json 缺失"
    d = _json.load(open(p, encoding="utf-8"))
    assert d["phase"] == "done" and d["tokens"] > 0, d
    print("PASS progress.json 阶段/字符数/耗时落盘")


def test_cancel_mid_run():
    """v1.0.356：流式阶段置取消位 → 体检即时中断返回 cancelled，last_run 标记取消。"""
    import threading as _th
    import time as _tm

    def _slow_stream(_text):
        for _ in range(5000):
            yield ("token", "x")
            _tm.sleep(0.002)

    cfg = _make_cfg()
    result = {}

    def _run():
        result["r"] = SR.run_self_repair(cfg)

    th = _th.Thread(target=_run, daemon=True)
    with mock.patch("llm_client.stream_llm_events", return_value=_slow_stream(FAKE_LLM_JSON)):
        th.start()
        _tm.sleep(0.4)                            # 让运行进入流式阶段
        assert SR.request_repair_cancel() is True  # 确有运行中的体检
        th.join(timeout=15)
    assert not th.is_alive(), "取消后运行线程必须退出"
    r = result.get("r") or {}
    assert r.get("cancelled") is True and not r.get("ok"), r
    import json as _json
    lr = _json.load(open(os.path.join(TMP, "\u81ea\u4fee\u590d", "last_run.json"), encoding="utf-8"))
    assert lr.get("cancelled") is True and "\u53d6\u6d88" in lr.get("error", ""), lr
    print("PASS 流式阶段中途取消即时中断并落盘")


# ---------- v1.0.357 自动代码修复闭环 ----------

_PATCH_PATCH = (  # 块替换补丁：把占位常量改成修复后的值
    '{"mode":"block","search":"_OLD_VALUE = 1","replace":"_OLD_VALUE = 2"}')


def _mk_scratch_file():
    """在工作区根造一个可修复的源码文件（非禁改清单）。"""
    p = os.path.join(TMP, "scratch_mod.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("_OLD_VALUE = 1\n\n\ndef get_value():\n    return _OLD_VALUE\n")
    return p


def _autofix_cfg(flag=True):
    cfg = _make_cfg()
    cfg["feature_flags"]["self_auto_fix"] = flag
    return cfg


def test_autofix_gate_disabled_and_forbidden():
    """开关关 → 拒绝；开关开但命中禁改清单 → 拒绝。"""
    SR._AUTO_FIX_DAY["count"] = 0
    with mock.patch.object(SR, "_PROJECT_ROOT", TMP):
        r1 = SR.attempt_code_fix({"title": "t", "detail": "fix scratch_mod.py"}, _autofix_cfg(False))
        r2 = SR.attempt_code_fix({"title": "t", "detail": "fix config.json please"}, _autofix_cfg(True))
    assert r1["status"] == "rejected" and "开关" in r1["detail"], r1
    assert r2["status"] == "rejected" and "定位" in r2["detail"], r2
    print("PASS 自动修复闸门：默认关 + 禁改清单")


def test_autofix_success_commits():
    """补丁应用 + 验证通过 → applied/committed，文件内容已更新，无残留 bak。"""
    SR._AUTO_FIX_DAY["count"] = 0
    SR._CANCEL_EVENT.clear()   # 上个测试可能置过取消位（attempt 不经过 run 入口的清位）
    p = _mk_scratch_file()
    bak = p + ".__autofix_bak__"
    try:
        with mock.patch.object(SR, "_PROJECT_ROOT", TMP), \
             mock.patch.object(SR, "_llm_complete_with_fallback", return_value=(_PATCH_PATCH, "test-model")), \
             mock.patch.object(SR, "_run_py_compile_file", return_value=(True, "")), \
             mock.patch.object(SR, "_run_pytest_full", return_value=(True, "155 passed")):
            r = SR.attempt_code_fix({"title": "t", "detail": "【文件: scratch_mod.py】scratch_mod.py 常量错误"}, _autofix_cfg(True))
        assert r["status"] in ("committed", "applied"), r
        assert "_OLD_VALUE = 2" in open(p, encoding="utf-8").read()
        assert not os.path.exists(bak), "验证通过后应清理备份"
    finally:
        if os.path.isfile(p):
            os.remove(p)
        if os.path.exists(bak):
            os.remove(bak)
    print("PASS 自动修复成功路径：应用+验证通过+清理备份")


def test_autofix_reverts_on_failed_regression():
    """回归不通过 → 自动还原原文件（内容与备份一致），状态 reverted。"""
    SR._AUTO_FIX_DAY["count"] = 0
    SR._CANCEL_EVENT.clear()
    p = _mk_scratch_file()
    orig = open(p, encoding="utf-8").read()
    bak = p + ".__autofix_bak__"
    try:
        with mock.patch.object(SR, "_PROJECT_ROOT", TMP), \
             mock.patch.object(SR, "_llm_complete_with_fallback", return_value=(_PATCH_PATCH, "test-model")), \
             mock.patch.object(SR, "_run_py_compile_file", return_value=(True, "")), \
             mock.patch.object(SR, "_run_pytest_full", return_value=(False, "3 failed")):
            r = SR.attempt_code_fix({"title": "t", "detail": "【文件: scratch_mod.py】scratch_mod.py 常量错误"}, _autofix_cfg(True))
        assert r["status"] == "reverted", r
        assert open(p, encoding="utf-8").read() == orig, "失败后必须还原原文件"
        assert not os.path.exists(bak)
    finally:
        if os.path.isfile(p):
            os.remove(p)
        if os.path.exists(bak):
            os.remove(bak)
    print("PASS 自动修复失败路径：回归不过自动还原")


def test_autofix_bad_anchor_rejected():
    """锚点片段在源码中出现 0 次或多次 → 拒绝应用且原文件不动。"""
    SR._AUTO_FIX_DAY["count"] = 0
    SR._CANCEL_EVENT.clear()
    p = _mk_scratch_file()
    orig = open(p, encoding="utf-8").read()
    bad = '{"mode":"block","search":"不存在的锚点","replace":"x"}'
    try:
        with mock.patch.object(SR, "_PROJECT_ROOT", TMP), \
             mock.patch.object(SR, "_llm_complete_with_fallback", return_value=(bad, "test-model")):
            r = SR.attempt_code_fix({"title": "t", "detail": "【文件: scratch_mod.py】scratch_mod.py 常量错误"}, _autofix_cfg(True))
        assert r["status"] == "rejected" and "锚点" in r["detail"], r
        assert open(p, encoding="utf-8").read() == orig
    finally:
        if os.path.isfile(p):
            os.remove(p)
    print("PASS 自动修复锚点不唯一拒绝")


if __name__ == "__main__":
    test_run_self_repair_applies_whitelist()
    test_dedup_second_run_skips()
    test_empty_data_note()
    test_bad_llm_output()
    test_progress_file_written()
    test_cancel_mid_run()
    print("== self_repair ALL PASS ==")

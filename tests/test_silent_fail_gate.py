# -*- coding: utf-8 -*-
"""tests/test_silent_fail_gate.py — 按域归并的回归锁合集

v1.0.648 由若干"按版本号起的文件"按域合并而来。每条 class/函数的 docstring 保留
原始出处与事故现场——版本编号是这些锁的**理由**，抹掉等于把锁降级成断言。
版本出处见各段上方的 `# ---- 来自 xxx ----` 标记。
"""
import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.result import is_err, err_msg  # noqa: E402


# ============================================================ 来自 test_v604_silent_and_caliber.py（v1.0.604）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _src  # noqa: E402


# ---------- ① 下载零弹窗 ----------

def test_attachment_download_default_is_silent_no_click(monkeypatch, tmp_path):
    from wecom_bridge import download as dl

    calls = []
    monkeypatch.setattr(dl, "_fetch_download",
                        lambda *a, **k: {"__error__": "页面非 OA KM 结构，找不到附件行"})
    monkeypatch.setattr(dl, "click_element", lambda *a, **k: calls.append("click") or {"ok": True})
    monkeypatch.setattr(dl, "set_download_behavior",
                        lambda *a, **k: calls.append("setdb") or {"ok": True})
    monkeypatch.setattr(dl, "_page_ws", lambda *a, **k: ("ws://fake", {"title": "x", "url": "http://x"}))
    monkeypatch.setattr(dl, "_require_page", lambda *a, **k: (None, "ws://fake", {"title": "x", "url": "http://x"}))   # v1.0.654 A6 配套
    monkeypatch.setattr(dl, "_cdp_eval_retry", lambda *a, **k: '{"ok":true,"filename":"x.xlsx"}')

    r = dl.download_attachment("pick", "设备清单202609.xlsx", str(tmp_path), timeout=0)

    assert isinstance(r, dict) and r.get("silent_only") is True, r
    assert is_err(r), r
    assert calls == [], "默认路径绝不能触发点击下载或下载行为设置（都会弹原生保存框）：%s" % calls
    assert "静默" in err_msg(r) or "静默" in str(r.get("hint")), r


def test_click_fallback_requires_explicit_opt_in(monkeypatch, tmp_path):
    """allow_click=True 才允许走点击通道——证明默认值确实在管，而不是两条路都在跑。"""
    from wecom_bridge import download as dl

    calls = []
    monkeypatch.setattr(dl, "_fetch_download", lambda *a, **k: {"__error__": "非 KM 结构"})
    monkeypatch.setattr(dl, "click_element", lambda *a, **k: calls.append("click") or {"ok": True})
    monkeypatch.setattr(dl, "set_download_behavior",
                        lambda *a, **k: calls.append("setdb") or {"ok": True})
    monkeypatch.setattr(dl, "_page_ws", lambda *a, **k: ("ws://fake", {"title": "x", "url": "http://x"}))
    monkeypatch.setattr(dl, "_require_page", lambda *a, **k: (None, "ws://fake", {"title": "x", "url": "http://x"}))   # v1.0.654 A6 配套
    monkeypatch.setattr(dl, "_cdp_eval_retry", lambda *a, **k: '{"ok":true,"filename":"x.xlsx"}')

    dl.download_attachment("pick", "设备清单202609.xlsx", str(tmp_path), timeout=0, allow_click=True)

    assert "setdb" in calls, "显式 opt-in 时才该进入点击下载分支：%s" % calls


def test_audit_flow_no_longer_prearms_save_dialog_watcher():
    """批审不再为「另存为」框预置文件名（源头已停用点击，不该再有弹框预期）。
    按调用形态断言，不按裸词——注释里提到 save_dialog_auto 是说明「为什么」，不是还在用。"""
    s = _src("services/batch_flow.py")
    assert "set_next_filename" not in s, "batch_flow 仍在预置保存框文件名——意味着还有点击下载回退"
    assert "save_dialog_auto." not in s, "batch_flow 不应再调用另存为自动应答"
    assert "from desktop import save_dialog_auto" not in s


def test_save_dialog_daemon_default_off():
    """守护线程默认关闭：我们不再制造弹框，留着只会替用户点他自己打开的保存框。"""
    s = _src("backend.py")
    assert '"auto_save_dialogs", False' in s, "auto_save_dialogs 默认应为 False"
    assert '"auto_save_dialogs", True' not in s


# ---------- ② 判定口径 ----------

def _monthly(**kw):
    d = {"expect_days": "23", "actual_days": "23"}
    d.update(kw)
    return d


def test_missing_list_is_red():
    from services.rule_check import check_monthly_report as f
    r = f(_monthly(list_found=False))
    assert r["result"] == "red" and "未上传设备清单" in r["reason"], r


def test_uploaded_but_unfetchable_is_pending():
    """机构传了清单、我们静默取不到 → 机制问题，不误判机构不合规。"""
    from services.rule_check import check_monthly_report as f
    r = f(_monthly(list_found=True, list_ok=False, list_note="重试 20 次仍失败"))
    assert r["result"] == "pending", r


def test_pcs_mismatch_no_longer_affects_verdict():
    """核心口径变化：在用计算机数量对不上不再影响红绿。"""
    from services.rule_check import check_monthly_report as f
    r = f(_monthly(list_found=True, list_ok=True, page_pcs="30", excel_pcs="11"))
    assert r["result"] == "green", r
    items = [c["item"] for c in r["checks"]]
    assert not any("计算机" in i for i in items), "在用计算机数判定项应已移除：%s" % items


def test_report_columns_show_list_not_pcs(tmp_path, monkeypatch):
    from services import batch_flow
    monkeypatch.setattr(batch_flow, "workspace_root", lambda: str(tmp_path))
    results = [
        {"title": "A 已取到", "expect_days": "23", "actual_days": "23",
         "list_found": True, "list_ok": True, "excel_pcs": 23, "verdict": "green"},
        {"title": "B 没传", "expect_days": "23", "actual_days": "23",
         "list_found": False, "list_ok": False, "verdict": "red"},
        {"title": "C 传了没取到", "expect_days": "23", "actual_days": "23",
         "list_found": True, "list_ok": False, "verdict": "pending"},
    ]
    rel = batch_flow._write_report(results, 1, 1, 1, 0)
    md = io.open(os.path.join(str(tmp_path), rel), encoding="utf-8").read()
    assert "设备清单" in md and "在用台数(参考)" in md
    assert "页面在用" not in md and "Excel在用" not in md
    assert "✅ 已上传" in md and "❌ 未上传" in md and "⚠️ 已上传未取到" in md
    assert "不参与红绿" in md, "报告应写明在用台数只是参考"


def test_no_page_pcs_left_in_pipeline_and_prompts():
    """源码级锁：page_pcs 提取链与提示词里的数量比对口径一起摘掉，防复发。"""
    assert "page_pcs" not in _src("services/batch_flow.py")
    assert "page_pcs" not in _src("agent_tools/skills.py")
    assert "在用计算机" not in _src("services/batch_flow.py").split("REPORT_FIELDS")[1].split("]")[0]
    reg = _src("agent_tools/registry.py")
    assert "不弹下载框" in reg or "不点任何按钮" in reg, "download_wecom_attachment 应声明只走静默直传"
    assert "不弹任何对话框" in reg, "run_audit_flow 描述应声明全程零弹框"

# ============================================================ 来自 test_v616_silent_fail.py（v1.0.616）


import core.config_store as cs   # noqa: E402

from conftest import _read  # noqa: E402


# ---------------- ③ 后台拒绝必须改变任务状态 ----------------

def test_background_denial_marks_task_blocked(monkeypatch):
    import services.scheduler as S
    import agent

    def _fake_loop(msgs, cfg, pane_id=None, notify=False):
        yield 'data: {"type": "token", "text": "已处理 3 单"}\n\n'
        yield ('data: {"type": "approval_denied", "tool": "run_audit_flow",'
               ' "reason": "该操作会真实提交审批单（不可撤销），而当前是后台会话"}\n\n')

    monkeypatch.setattr(agent, "agent_loop", _fake_loop)
    monkeypatch.setattr(S, "_mark", lambda *a, **k: None)
    monkeypatch.setattr("core_utils.save_workspace_file", lambda *a, **k: "")
    r = S._run_one({"id": "t1", "name": "夜间批审", "prompt": "跑批审"}, {})
    assert r["status"] == "blocked", r
    assert "拒绝" in r["note"], r


def test_loop_emits_denial_event_in_source():
    src = _read("agent", "loop.py")
    i = src.index("后台会话拒绝不可撤销写工具")
    assert "approval_denied" in src[i:i + 400], "拒绝又变回只回灌模型、流上无痕"


def test_scheduler_counts_denial(monkeypatch):
    src = _read("services", "scheduler.py")
    assert "approval_denied" in src
    i = src.index("approval_denied")
    assert "_blocked.append" in src[i:i + 300]


# ---------------- ④ 正则保存前试编译 ----------------

def test_bad_regex_rejected_at_save(app_client):
    c, cfg = app_client
    r = c.post("/api/security/config",
               json={"security": {"cmd_whitelist": ["git ", "(unclosed"]}})
    assert r.status_code == 400, r.get_json()
    msg = r.get_json()["error"]
    assert "第 2 行" in msg and "正则" in msg, msg
    assert not cs.load_config()["security"].get("cmd_whitelist"), "非法规则仍被写进配置"


def test_good_regex_saves_and_protected_paths_not_treated_as_regex(app_client):
    c, cfg = app_client
    r = c.post("/api/security/config", json={"security": {
        "cmd_whitelist": ["^git "], "protected_paths": ["D:\\重要资料(备份)"]}})
    assert r.get_json()["ok"] is True, r.get_json()
    sec = cs.load_config()["security"]
    assert sec["cmd_whitelist"] == ["^git "]
    # 保护路径是路径不是正则：带括号必须能存（按正则校验会把正常路径判成非法）
    assert sec["protected_paths"] == ["D:\\重要资料(备份)"], sec["protected_paths"]


# ---------------- ⑤⑨ 版数文案与"锁自己锁错" ----------------

def test_copy_lock_regex_strips_tags_first():
    """回归测试的测试：旧正则对 `最近 <b>8 个版本</b>` 完全不匹配（这就是它一路绿灯的原因）。"""
    old = re.findall(r"(?:近|保留最近)\s*(\d+)\s*(?:个)?(?:版本|版)", '最近 <b>8 个版本</b>')
    assert old == [], "旧写法本应漏检——若它现在能匹配，说明新锁的必要性消失了"
    stripped = re.sub(r"<[^>]+>", "", '最近 <b>8 个版本</b>')
    assert re.findall(r"(?:近|保留最近)\s*(\d+)\s*(?:个)?(?:版本|版)", stripped) == ["8"]


def test_version_count_copy_is_three_everywhere():
    from services import version_store
    keep = str(version_store._KEEP)
    assert keep == "3"
    html = re.sub(r"<[^>]+>", "", _read("index.html"))
    assert "最近 8 个版本" not in html and ("最近 " + keep + " 个版本") in html
    build = _read("build.py")
    # 先剥注释：本项目注释惯例是「WHY + 引用被替换掉的旧代码原文」，不剥就会被自己写的
    # 说明文字判成"残留未清"（build-delivery 技能里明列的假阳性第一条，本轮自己踩了一次）。
    build = re.sub(r"(?m)^\s*#.*$", "", build)
    build = re.sub(r"#.*$", "", build)
    assert "保留最近 8 个" not in build, "打包日志又印死数字"
    assert "version_store._KEEP" in _read("build.py")


# ---------------- ⑥⑦⑧ 其余口径 ----------------

def test_digest_copy_says_it_calls_the_model():
    html = _read("index.html")
    assert "纯本地汇总，不调模型" not in html
    i = html.find("工作小结_日期.md")
    seg = html[i - 200:i + 300]
    assert "默认会调用大模型润色" in seg, seg


def test_no_task_level_model_fallback_claim():
    html = _read("index.html")
    assert "多模型自动兜底" not in html, "又出现把换模型宣称到任务主循环的文案"
    i = html.find("容错")
    seg = html[i:i + 260]
    assert "自我修复体检" in seg and "备用模型" in seg


def test_model_test_button_says_default_model():
    html = _read("index.html")
    i = html.find("testModel()")
    assert "默认模型" in html[i:i + 200] and "当前选中模型" not in html[i - 60:i + 200]


def test_empty_state_points_at_current_entry():
    js = _read("js", "10-chat.js")
    assert "打开工作台 · 点「新建」" not in js
    i = js.find('class="empty"')
    assert "新建任务" in js[i:i + 400], js[i:i + 200]

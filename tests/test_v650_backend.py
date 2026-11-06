# -*- coding: utf-8 -*-
"""v1.0.650（用户："全部优化"）后端锁

覆盖四件事，每件都写成「改回旧写法必红」：
  F2  done 收据 → 统计块落库（_stat_block 只收数字）
  F4  任务资源软预算进设置界面（默认值单一事实源 + 白名单写入 + 0 是合法档）
  F5  进度台账开新一轮（归档改名，且归档名不能被"跳过判定"与一次性迁移误认）
  F3  不通过清单推群（只推 red、不拆行、无 webhook 就拒发、绝不接进自动化）
  F6  财务脚本流水线（只读：没有 auto_approve 这条路径；单据编号口径与 rule_check 同源）
"""
import io
import json
import os
import re

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


from conftest import _src  # noqa: E402
from core.result import is_err, err_msg  # noqa: E402


@pytest.fixture
def ws_tmp(tmp_path, monkeypatch):
    """产物库改道。

    两种绑定都要堵：`core.paths.workspace_root`（走属性查找的调用方）与
    `from core_utils import workspace_root` 的**模块级直接绑定**（batch_flow /
    b_macro 就是这样）——只 patch 前者时后者仍写真实目录，第一版这条锁就真的往
    dist/产物库 里落了一份财务核查报告和断点文件。
    """
    from core import paths
    root = tmp_path / "dist" / "产物库"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "workspace_root", lambda: str(root))
    for mod in ("services.batch_flow", "routes.b_macro", "core.workspace_io"):
        pytest.importorskip(mod)
        m = __import__(mod, fromlist=["workspace_root"])
        if hasattr(m, "workspace_root"):
            monkeypatch.setattr(m, "workspace_root", lambda: str(root))
    return root


# ==================== F2：统计块 ====================

def test_clamp_blocks_keeps_stat_numbers():
    """存储层的数字白名单要放行统计块，否则采集方写了也存不下去。"""
    from services.conversations import _clamp_blocks
    out = _clamp_blocks([{"k": "stat", "rounds": 3, "tool_calls": 8, "elapsed_s": 12.34,
                          "prompt_tokens": 123456, "completion_tokens": 789,
                          "tool_errors": True, "junk": {"a": 1}}])
    assert out == [{"k": "stat", "rounds": 3, "tool_calls": 8, "elapsed_s": 12.3,
                    "prompt_tokens": 123456, "completion_tokens": 789}], out
    # 0 必须留住（零失败/零轮次是事实，不是缺省）
    assert _clamp_blocks([{"k": "stat", "tool_errors": 0}])[0]["tool_errors"] == 0


# ==================== F4：任务资源软预算 ====================

def test_soft_budget_defaults_have_one_source():
    from core import config_store as CS
    from agent import loop as AL
    assert AL.SOFT_BUDGET_DEFAULTS is CS.AGENT_SOFT_BUDGET_DEFAULTS, \
        "默认值又变成两份——设置页显示的数字和 agent 用的数字会漂"
    assert CS.DEFAULT_CONFIG.get("agent") == CS.AGENT_SOFT_BUDGET_DEFAULTS, \
        "顶层段没在 DEFAULT_CONFIG 声明：按本文件自己的规矩，手改 config.json 的 agent 段会被 save 抹掉"


def test_budget_num_keeps_zero_and_clamps_junk():
    from routes.personal import _budget_num, AGENT_SOFT_BUDGET_DEFAULTS as D
    assert _budget_num(0, 400000) == 0, "0＝关闭该项，不能被吞成默认（v1.0.622 那个坑）"
    assert _budget_num(None, 400000) == 400000
    assert _budget_num(-5, 120) == 0
    assert _budget_num("abc", 120) == 120
    assert _budget_num("300000", 120) == 300000
    assert _budget_num(10 ** 15, 120) == 10 ** 9
    assert _budget_num(True, 120) == 1, "bool 走 int 分支——前端不会传，传了也只当 1，不炸"


def test_budget_post_whitelist_comes_from_the_shared_table():
    """写入必须逐键过 AGENT_SOFT_BUDGET_DEFAULTS 白名单：界面能写进任意 cfg["agent"] 键，
    而 agent 一个都不读，存的就是幻觉（v1.0.617 feature_flags 同型事故）。"""
    src = _src("routes", "personal.py")
    assert "for k, default in AGENT_SOFT_BUDGET_DEFAULTS.items()" in src
    assert '"budget": {k: _budget_num(ag.get(k), v) for k, v in AGENT_SOFT_BUDGET_DEFAULTS.items()}' in src, \
        "GET 下发的是生效值——界面所见即 agent 实际用的数字"


def test_budget_round_trip_through_the_route(ws_tmp, monkeypatch):
    from core import config_store as CS
    import routes.personal as P
    app = Flask(__name__)
    cfg = {"schema_version": 2, "models": []}
    monkeypatch.setattr(P, "load_config", lambda: cfg)
    monkeypatch.setattr(P, "update_config", lambda mutate: mutate(cfg) and True)
    with app.test_request_context("/api/personal/config", method="POST",
                                  json={"budget": {"soft_budget_tokens": 0,
                                                   "soft_budget_minutes": 55,
                                                   "随便什么键": 999}}):
        r = P.api_personal_post()
    assert r.status_code == 200, r.get_json()
    assert cfg["agent"]["soft_budget_tokens"] == 0
    assert cfg["agent"]["soft_budget_minutes"] == 55
    assert "随便什么键" not in cfg["agent"], "白名单没生效"
    with app.test_request_context("/api/personal/config"):
        g = json.loads(P.api_personal_get().get_data(as_text=True))
    assert g["budget"]["soft_budget_tokens"] == 0
    assert g["budget"]["soft_budget_tokens_per_item"] == 500000 or \
        g["budget"]["soft_budget_tokens_per_item"] == CS.AGENT_SOFT_BUDGET_DEFAULTS["soft_budget_tokens_per_item"], \
        "未配置项要回落到生效默认，不能显示成 0"


def test_budget_inputs_exist_in_settings_ui():
    html = _src("index.html")
    js = _src("js", "11-workspace-kb-study.js")
    for k in ("soft_budget_tokens", "soft_budget_minutes",
              "soft_budget_tokens_per_item", "soft_budget_minutes_per_item"):
        assert 'id="bg_%s"' % k in html, k
    assert "SOFT_BUDGET_KEYS" in js and "bg_\" + k" in js or 'set("bg_"+k' in js
    assert "async function saveBudget(btn)" in js
    assert "任务预算还没加载成功，先别保存" in js, "未加载就保存会把生效值写成界面默认——要有闸门"


# ==================== F5：进度台账开新一轮 ====================

def _mk_ledger(root, name="学习进度_进度.md", n=3):
    d = os.path.join(str(root), "产出")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("---\n第一条\n\n" + "".join("---\n第%d条\n\n" % i for i in range(2, n + 1)))
    return p


def test_archive_progress_round_moves_and_counts(ws_tmp):
    from core.workspace_io import archive_progress_round, ROUND_ARCHIVE_PREFIX
    p = _mk_ledger(ws_tmp)
    out = archive_progress_round("产出", "学习进度_进度.md")
    assert out["ok"] and out["items"] == 3, out
    assert not os.path.exists(p), "当前轮文件必须腾空，否则下一轮照旧读到旧记录"
    arch = os.path.join(str(ws_tmp), "产出", out["archived"])
    assert os.path.isfile(arch) and out["archived"].startswith(ROUND_ARCHIVE_PREFIX)
    assert "学习进度_进度.md" in out["archived"], "归档名要认得出是哪把 key"


def test_round_archive_escapes_the_skip_glob_and_the_migration(ws_tmp):
    """两条静默误食路径：
    ① 快捷入口提示词让模型读 `<key>_*.md`（学 习 进度_ 前缀通配）——归档若跟着这个前缀，
       刚归档的上一轮会被读回来继续当"已处理"跳过，等于没开新一轮；
    ② 一次性迁移按 `fn.startswith("答题记录_")` 把产出/答题记录_*.md 挪进 知识库/题库/。"""
    from core.workspace_io import archive_progress_round
    _mk_ledger(ws_tmp, "学习进度_进度.md", 2)
    _mk_ledger(ws_tmp, "答题记录_进度.md", 2)
    a1 = archive_progress_round("产出", "学习进度_进度.md")["archived"]
    a2 = archive_progress_round("产出", "答题记录_进度.md")["archived"]
    assert not a1.startswith("学习进度_"), a1
    assert not a2.startswith("答题记录_"), a2


def test_archive_refuses_non_progress_and_missing(ws_tmp):
    from core.workspace_io import archive_progress_round
    assert is_err(archive_progress_round("产出", "月报.md"))
    assert is_err(archive_progress_round("产出", "没写过_进度.md"))


def test_new_round_reachable_by_tool_route_and_ui():
    """三条路都要通，且共用同一个后端函数（两条各写一遍归档规则必漂）。"""
    from agent_tools.registry import TOOLS
    from agent_tools.executors import _TOOL_HANDLERS
    names = {t["function"]["name"] for t in TOOLS}
    assert "new_progress_round" in names
    assert "new_progress_round" in _TOOL_HANDLERS, "工具声明了却没接执行器——模型调用即 unknown tool"
    src = _src("core", "workspace_io.py")
    assert "archive_progress_round" in src
    assert "archive_progress_round" in _src("routes", "workspace.py")
    assert "archive_progress_round" in _src("agent_tools", "executors.py")
    assert '/workspace/new-round' in _src("routes", "workspace.py")
    js = _src("js", "11-workspace-kb-study.js")
    assert 'f.name.endsWith("_进度.md")' in js and "newWsRound" in js


def test_every_ledger_skill_can_open_a_new_round():
    """横向锁：给了 append_progress 扳机的技能，就必须给开新一轮的出口——
    只给记不给清，等于把"跳过判定喂错"永久留在台账里。"""
    from agent_tools.skills import SKILLS
    for s in SKILLS:
        tools = s.get("tools") or []
        if "append_progress" in tools:
            assert "new_progress_round" in tools, (s["id"], tools)
            assert len(tools) == len(set(tools)), "工具名重复：脚本批量插入时容易双写" % s["id"]


# ==================== F3：不通过清单推群 ====================

def _fake_results():
    return [{"title": "甲营业部", "verdict": "red", "reason": "天数不一致",
             "checks": [{"item": "运维天数一致", "pass": False},
                        {"item": "设备清单", "pass": True},
                        {"item": "另一个未通过项", "pass": False}]},
            {"title": "丁营业部", "verdict": "green", "reason": "符合", "checks": []},
            {"title": "戊营业部", "verdict": "pending", "reason": "数据缺失", "checks": []},
            {"title": "莆田营业部", "verdict": "red", "reason": "未上传设备清单", "checks": []}]


def test_reject_lines_only_red_with_reason(monkeypatch):
    import routes.b_macro as M
    lines = M.reject_lines(_fake_results())
    assert len(lines) == 2, lines
    assert all(l.startswith("· ") for l in lines)
    assert "甲营业部" in lines[0] and "天数不一致" in lines[0]
    assert "未通过项：运维天数一致、另一个未通过项" in lines[0], "checks 里 pass=False 的项要带上"
    assert all("丁营业部" not in l and "戊营业部" not in l for l in lines), "green/pending 混进不通过清单"


def test_chunk_lines_never_splits_a_line():
    import routes.b_macro as M
    long_line = "· " + "机构" * 300
    lines = [long_line, "· 短的一条"]
    groups = M.chunk_lines(lines, size=200)
    assert any(g == long_line for g in groups), "一条机构记录被截半比不推更糟"
    assert all(g in lines or set(g.split("\n")) <= set(lines) for g in groups)


def test_push_rejects_requires_webhook_and_confirms(ws_tmp, monkeypatch):
    import routes.b_macro as M
    calls = []
    monkeypatch.setattr(M, "workspace_root", lambda: str(ws_tmp))
    os.makedirs(os.path.join(str(ws_tmp), "台账"), exist_ok=True)
    with open(os.path.join(str(ws_tmp), "台账", "月报判定.json"), "w", encoding="utf-8") as f:
        json.dump({"runs": [{"ts": "2026-11-03 10:00:00", "results": _fake_results()}]}, f)
    monkeypatch.setattr(M, "load_config", lambda: {"wecom": {}})          # 没配 webhook
    app = Flask(__name__)

    def _resp(r):
        """这些视图出错时返回 (jsonify, code)，测试里统一拆成 (数据, 状态码)。"""
        return (r[0], r[1]) if isinstance(r, tuple) else (r, r.status_code)

    with app.test_request_context("/api/audit/push_rejects", method="POST", json={}):
        body, code = _resp(M.api_audit_push_rejects())
    assert code == 400
    assert "webhook" in body.get_json()["error"]

    sent = []
    import wecom_bridge
    monkeypatch.setattr(wecom_bridge, "send_webhook",
                        lambda url, content, msgtype="text": sent.append(content) or {"ok": True})
    monkeypatch.setattr(M, "load_config", lambda: {"wecom": {"webhook": "https://example/hook"}})
    with app.test_request_context("/api/audit/reject_list"):
        g = json.loads(M.api_audit_reject_list().get_data(as_text=True))
    assert g["red"] == 2 and len(g["lines"]) == 2
    with app.test_request_context("/api/audit/push_rejects", method="POST", json={}):
        r2 = M.api_audit_push_rejects()
    assert r2.get_json()["ok"] and r2.get_json()["sent"] == 1
    assert len(sent) == 1 and "不通过清单" in sent[0] and "甲营业部" in sent[0]


def test_push_rejects_is_not_wired_into_automation():
    """发群是不可撤回的对外动作：只许用户点出来。
    定时任务/技能提示词/流水线里出现这个接口，就是把它变成了自动广播。"""
    for path in ("services/schedule_ops.py", "services/scheduled.py", "agent_tools/skills.py",
                 "agent_tools/executors.py", "agent/loop.py", "services/batch_flow.py"):
        if not os.path.isfile(os.path.join(ROOT, path)):
            continue
        assert "push_rejects" not in _src(*path.split("/")), path
    js = _src("js", "11-workspace-kb-study.js")
    assert "pushRejectList" in js and 'onclick="pushRejectList()"' in js
    # 而且发之前必须过一次确认（confirmAsk），不能一点就发
    body = js[js.index("async function pushRejectList"):]
    body = body[:body.index("\n}\n") + 3]
    assert "confirmAsk(" in body and "/api/audit/push_rejects" in body


# ==================== F6：财务脚本流水线 ====================

def test_finance_flow_is_read_only():
    """红线：财务流水线不能有任何审批点击路径——
    run_audit_flow 的 auto_approve 靠弹窗守，而 dispatch 里注明它"不走那道闸"，
    所以新 flow 从入口就没有这个参数才算安全。"""
    src = _src("services", "batch_flow.py")
    start = src.index("def audit_finance_bills")
    body = src[start:src.index("def _default_port", start)]
    assert "_approve_in_detail" not in body
    assert "auto_approve" not in body
    assert "wb.click" not in body and "run_js" not in body and "type_text" not in body
    single = src[src.index("def _audit_one_finance"):start]
    assert "_approve_in_detail" not in single and "auto_approve" not in single
    ex = _src("agent_tools", "executors.py")
    assert 'if flow == "finance"' in ex and "auto_approve 不适用" in ex
    assert '"finance"' in _src("agent_tools", "registry.py")


def test_finance_pipeline_processes_links_and_writes_report(ws_tmp, monkeypatch):
    import services.batch_flow as BF
    fields_seen = []

    def fake_read_fields(fp, fields=None, port=None):
        fields_seen.append(list(fields or []))
        # snippets 按真实页面形态给（`字段名：值`）——rule_check._extract_val 靠关键词定位取值，
        # 只塞裸值会让每张单据都判"未取到"，这条断言顺带钉住流水线喂给规则引擎的数据形状。
        vals = [("专管部门", "信息技术部"), ("明细项目", "运维服务"), ("业务类型", "费用类付款（电子）"),
                ("支出类别", "专管费用"), ("预算执行单", "PMP-2026-1"), ("PMP项目", "PMP-2026-1"),
                ("附件", "发票.pdf")]
        return {"fields": [{"key": k, "value": v, "snippets": [k + "：" + v], "count": 1}
                           for k, v in vals],
                "all_fields_missed": False}

    monkeypatch.setattr(BF, "_find_list_page", lambda port: ("待办列表", [{"title": "待办"}]))
    monkeypatch.setattr(BF, "_load_all_report_links",
                        lambda pick, port, **kw: [{"text": "BX2026-0001", "url": "http://x/1"},
                                                  {"text": "BX2026-0002", "url": "http://x/2"}])
    monkeypatch.setattr(BF, "_open_detail", lambda port, pick, link, issues: link["text"] + ".page")
    monkeypatch.setattr(BF, "wb", type("W", (), {"read_fields": staticmethod(fake_read_fields),
                                                 "close_page": staticmethod(lambda *a, **k: {"ok": True})})())
    monkeypatch.setattr(BF, "requests", type("R", (), {"get": staticmethod(
        lambda *a, **k: type("Resp", (), {"status_code": 200})())}))
    monkeypatch.setattr(BF, "_bg_state", lambda **kw: None)
    monkeypatch.setattr(BF, "_bg_update", lambda results: None)
    out = BF.audit_finance_bills(port=9999)
    assert out["ok"] and out["total"] == 2 and out["done"] == 2, out
    assert out["green"] == 2 and out["red"] == 0, out["results"]
    assert fields_seen[0] == BF.FINANCE_FIELDS, "读的字段必须与 rule_check.check_finance 的规则一一对应"
    assert out["report_path"] and os.path.isfile(os.path.join(str(ws_tmp), out["report_path"]))
    assert not os.path.isfile(os.path.join(str(ws_tmp), "台账", BF.FINANCE_PROGRESS)), \
        "跑完要清断点，否则下一次被误当 resume 起点"


def test_finance_progress_file_is_separate_from_monthly():
    """两条流水线的断点不能互吃：月报的 results 是机构、财务的是单据，
    resume_partition 按判重键跳过——串了会整批漏审。"""
    import services.batch_flow as BF
    assert BF.FINANCE_PROGRESS != "月报进度.json"
    assert BF._progress_path(BF.FINANCE_PROGRESS).endswith(BF.FINANCE_PROGRESS)
    assert BF._progress_path().endswith("月报进度.json"), "月报默认路径不能变"


def test_background_channel_dispatches_by_flow(ws_tmp, monkeypatch):
    import services.batch_flow as BF
    called = {}
    monkeypatch.setattr(BF, "audit_finance_bills",
                        lambda **kw: called.update(finance=kw) or {"ok": True, "green": 0, "red": 0,
                                                                   "pending": 0, "summary": "x",
                                                                   "total": 0, "done": 0,
                                                                   "results": []})
    monkeypatch.setattr(BF, "audit_monthly_reports",
                        lambda **kw: called.update(monthly=kw) or {"ok": True, "green": 0, "red": 0,
                                                                   "pending": 0, "summary": "x",
                                                                   "total": 0, "done": 0,
                                                                   "results": []})
    monkeypatch.setattr("core.notify.native_toast", lambda *a, **k: None, raising=False)
    BF.start_background_audit(port=9999, flow="finance")
    for _ in range(50):
        if called:
            break
        import time as _t
        _t.sleep(0.05)
    assert "finance" in called and "monthly" not in called, called


def test_doc_number_pattern_has_one_source():
    from services import rule_check as RC
    assert hasattr(RC, "DOC_NO_RE")
    assert RC.DOC_NO_RE.search("编号 BX2026-0001 已付款")
    # 认"哪条列表链接是一张单据"的正则不许在 batch_flow 里再抄一份
    assert "BX\\\\w" not in _src("services", "batch_flow.py"), "同一条格式在两处各写一遍"
    assert "from services.rule_check import DOC_NO_RE" in _src("services", "batch_flow.py")
    assert "re.finditer(" in _src("services", "rule_check.py")

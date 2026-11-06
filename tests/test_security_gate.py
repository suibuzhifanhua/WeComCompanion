# -*- coding: utf-8 -*-
"""tests/test_security_gate.py — 安全闸门（写操作审批）回归锁合集（v1.0.646 按域归并）。

本文件由 2 个「按版本号起的文件」合并而来，两条锁守的是**同一条红线的两段**：
v1.0.614 管"参数级"（`run_audit_flow` 的 `auto_approve`），v1.0.632 管"语义级"
（任何点击/JS 里出现审批动作词）。合在一起的理由是它们共用同一套审批管道
（挂起→弹窗→一次性令牌→原样重试），分文件反而看不出全貌。

| 原文件                          | 版本出处  | 守护对象                     | 用例数 |
|---------------------------------|-----------|------------------------------|--------|
| test_v614_audit_gate.py         | v1.0.614  | 批审写操作的参数级闸门       |  12    |
| test_v632_approval_gate.py      | v1.0.632  | 审批动作词语义级闸门         |  22    |
"""
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.result import is_err, err_msg  # noqa: E402

import security_ops as SO   # noqa: E402


def io_read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _no_audit_write(monkeypatch):
    """审计落盘指向内存 + 令牌表清空：测试不该往真 产物库/日志/安全审计.jsonl 写条目，
    也不该读到上一条测试留下的批准令牌。（v1.0.646：原为 v614 文件私有，上提为本域共用——
    本域两条锁都走同一套令牌机制，读到上一条测试残留的令牌会让"必须重新问"的断言假绿。）"""
    rec = []
    monkeypatch.setattr(SO, "audit", lambda *a, **k: rec.append(a))
    with SO._lock:
        SO._grants.clear()
    return rec


# ============================================================ v1.0.614 参数级闸门
class TestAuditFlowGate:
    """批审写操作安全显形（v1.0.614，审查报告 P0 两条）。

    改前的真实状态（逐条回源核过）：
      · `run_audit_flow` 不在 `_TOOL_ACTION_LABELS` 里 → 审批弹窗显示裸工具名"执行 run_audit_flow"；
      · `tool_action_label` 只取 target/path/command/text/name，而它的参数是 `{flow, auto_approve}`
        → **批准时看不出"这会把判定通过的 N 张单据真实点「通过」提交、不可撤销"**；
      · `gate_write_tool` 只在 `ask_every` 档询问，而默认档是 `auto`
        （`core/config_store.py:111`）→ 模型自己传 `auto_approve:true` 就零询问，
        工具描述里那句"仅用户明确要求时传 true"只是提示词约束，不是代码约束。
    """

    # ---------- ① 标签显形 ----------
    def test_label_names_the_real_consequence(self):
        lab = SO.tool_action_label("run_audit_flow", {"flow": "monthly-report", "auto_approve": True})
        assert "run_audit_flow" not in lab, lab          # 不再显示裸工具名
        assert "批量审核" in lab, lab
        assert "真实点击" in lab and "不可撤销" in lab, lab  # 后果写在标签上

    def test_label_without_flag_has_no_scare(self):
        lab = SO.tool_action_label("run_audit_flow", {"flow": "monthly-report"})
        assert "不可撤销" not in lab, lab                 # 只读判定不该被渲染成危险操作
        assert "批量审核" in lab

    # ---------- ② 参数级闸门 ----------
    def _gate(self, level, args, background=False, name="run_audit_flow", is_read=False):
        return SO.gate_write_tool(name, args, {}, level=level, is_read=is_read, background=background)

    def test_auto_level_now_asks_for_auto_approve(self):
        r = self._gate("auto", {"auto_approve": True})
        assert r and r.get("__approval_needed__"), r      # 改前这里是 None（零询问直接提交）

    def test_auto_level_unchanged_without_the_flag(self):
        assert self._gate("auto", {"flow": "monthly-report"}) is None
        assert self._gate("auto", {}) is None

    def test_full_level_still_no_ask(self):
        """「完全访问」的语义就是不再确认——这条边界不能顺手收紧。"""
        assert self._gate("full", {"auto_approve": True}) is None

    def test_background_refuses_instead_of_hanging(self):
        r = self._gate("auto", {"auto_approve": True}, background=True)
        assert r and is_err(r) and not r.get("__approval_needed__"), r
        assert "定时任务" in err_msg(r), r            # 说清为什么拒、该怎么继续

    def test_read_tools_never_gated_even_with_flag(self):
        assert self._gate("auto", {"auto_approve": True}, is_read=True) is None

    def test_ask_every_still_gates_ordinary_writes(self):
        r = SO.gate_write_tool("click_wecom_element", {"target": "同意按钮"}, {}, level="ask_every")
        assert r and r.get("__approval_needed__"), r      # 老行为不变

    def test_ask_every_does_not_gate_never_gate_tools(self):
        assert SO.gate_write_tool("finish", {}, {}, level="ask_every") is None

    def test_granted_token_lets_the_dangerous_call_through(self):
        """批准一次 → 原样重试放行（复用既有一次性令牌机制，不另起一套）。
        v1.0.652：grant 改双因子（hash+nonce）——单凭 hash 自铸必须被拒。"""
        args = {"flow": "monthly-report", "auto_approve": True}
        r = self._gate("auto", args)
        h = r["hash"]
        assert not SO.grant(h), "无 nonce 的自铸必须被拒（审计 P0-1）"
        _n = SO.begin_approval(h)
        assert SO.grant(h, _n), "带合法 nonce 应发放令牌"
        assert self._gate("auto", args) is None
        # 令牌是一次性的：再用一次必须重新问
        assert self._gate("auto", args) and self._gate("auto", args).get("__approval_needed__")

    # ---------- ③ 接线：闸门不再只在 ask_every 档计算 ----------
    def test_loop_calls_gate_for_all_levels(self):
        src = io_read("agent/loop.py")
        i = src.find("_gate = None if _ask_block else")
        assert i > 0, "闸门调用点找不到了"
        seg = src[i:i + 320]
        assert 'if permission_level == "ask_every" else None' not in seg, \
            "loop 又退回只在 ask_every 档算闸门 → auto_approve 会重新变成零询问"
        assert "background=" in seg, "没把后台会话身份传给闸门（定时任务会挂起而不是被拒）"

    def test_registry_description_matches_code_reality(self):
        """工具描述不能再是唯一防线：代码已经会问，描述也要同步说清。"""
        s = io_read("agent_tools/registry.py")
        i = s.find('"auto_approve"')
        seg = s[i:i + 320]
        assert "确认" in seg or "询问" in seg, seg[:160]


# ============================================================ v1.0.632 语义级闸门
class TestApprovalWordGate:
    """审批红线落到代码层（v1.0.632 B1）。

    用户环境里这个软件驱动的是**有真实业务后果**的财务单据审批。此前"绝不自动点同意/提交"
    只存在于两处：`_ALWAYS_ASK = {"run_audit_flow": ("auto_approve",)}` 和工具描述里的一句话。
    `click_wecom_element` 点"同意"、`wecom_run_js("…[data-btn=approve]').click()")`
    都不经过任何按**语义**设的拦截——auto 档与定时/刷课等后台会话可全程无审批提交真实单据。

    现在按"目标文案/JS 表达式里是否出现审批动作词"判定，命中即复用既有审批管道
    （挂起→弹窗→一次性令牌→原样重试；后台无弹窗则直接拒绝），不新增第二套机制。

    词表刻意保守：不收"提交/通过/确定/下一题"这类在答题与填表里高频的泛词——
    `wecom_run_js` 的 `.click(` 是跑课答题的主机制， blanket 拦截等于把功能实质下线
    （v1.0.470 的注释里已经写过这个教训）。本文件同时锁住这条边界。
    """

    CFG_AUTO = {"security": {"permission_level": "auto"}}
    CFG_ASK = {"security": {"permission_level": "ask_every"}}
    CFG_FULL = {"security": {"permission_level": "full"}}

    # ---------------------------------------------------------------- 命中判定
    @pytest.mark.parametrize("name,args", [
        ("click_wecom_element", {"target": {"text": "同意"}}),
        ("click_wecom_element", {"target": {"xpath": "//button[contains(.,'审批通过')]"}}),
        ("click_wecom_element", {"target": {"text": "驳回"}}),
        ("wecom_run_js", {"expression": "document.querySelector('[data-btn=approve]').click()"}),
        # JS 里文案在引号里、动作在后面，也要抓到
        ("wecom_run_js", {"expression": "Array.from(document.querySelectorAll('button'))"
                                        ".find(b=>b.innerText.includes('确认支付')).click()"}),
    ])
    def test_approval_actions_are_caught(self, name, args):
        assert SO.approval_word_hit(name, args) is True, args

    @pytest.mark.parametrize("name,args", [
        # 答题/刷课主机制：点选项、点下一题，绝不能被审批词误伤
        ("wecom_run_js", {"expression": "document.querySelectorAll('.option')[0].click()"}),
        ("wecom_run_js", {"expression": "btn.innerText='下一题'; btn.click()"}),
        ("click_wecom_element", {"target": {"text": "A 正确"}}),
        ("click_wecom_element", {"target": {"text": "提交答案"}}),
        ("click_wecom_element", {"target": {"text": "查看详情"}}),
        # 把"同意"填进意见框不是提交动作，不参与文案判定
        ("type_wecom_text", {"text": "同意"}),
        ("save_file", {"path": "产出/同意书.txt"}),
    ])
    def test_legitimate_actions_are_not_caught(self, name, args):
        assert SO.approval_word_hit(name, args) is False, (name, args)

    def test_generic_words_must_not_enter_the_list(self):
        """放宽词表前先看这条：泛词一旦进来，跑课/填表会被逐次推去人工确认（功能实质下线）。"""
        pat = SO._APPROVAL_TEXT.pattern
        for generic in ("|提交|", "|通过|", "|确定|", "|下一题|", "|保存|"):
            assert generic not in pat, f"词表混入泛词 {generic}"
        # 泛词单独出现不得命中
        assert not SO._APPROVAL_TEXT.search("提交答案")
        assert not SO._APPROVAL_TEXT.search("审核已通过，请查看")
        assert SO._APPROVAL_TEXT.search("提交审批")

    # ---------------------------------------------------------------- 闸门行为
    def test_auto_level_still_asks_for_approval_click(self):
        r = SO.gate_write_tool("click_wecom_element", {"target": {"text": "同意"}},
                               self.CFG_AUTO, level="auto", is_read=False, background=False)
        assert isinstance(r, dict) and r.get("__approval_needed__"), \
            "auto 档点「同意」竟然无需人工确认——红线仍只在提示词层"

    def test_background_session_is_rejected_not_hung(self):
        """定时/刷课会话没有弹窗：命中审批词要**明确拒绝**，挂起等于静默结束任务。"""
        r = SO.gate_write_tool("wecom_run_js",
                               {"expression": "document.querySelector('#agree').click()"},
                               self.CFG_AUTO, level="auto", is_read=False, background=True)
        assert is_err(r), r
        assert "后台" in err_msg(r) or "定时" in err_msg(r)

    def test_full_level_is_the_explicit_opt_out(self):
        """「完全访问」档的语义就是用户显式放弃询问——保持与 run_audit_flow 一致，不额外加戏。"""
        assert SO.gate_write_tool("click_wecom_element", {"target": {"text": "同意"}},
                                  self.CFG_FULL, level="full", is_read=False, background=False) is None

    def test_read_tools_never_blocked(self):
        assert SO.gate_write_tool("click_wecom_element", {"target": {"text": "同意"}},
                                  self.CFG_ASK, level="ask_every", is_read=True, background=False) is None

    def test_param_level_gate_still_works(self):
        """v1.0.614 原有的参数级名单不能被这次改动弄丢。"""
        assert SO._always_ask_hit("run_audit_flow", {"auto_approve": True}) is True
        assert SO._always_ask_hit("run_audit_flow", {"auto_approve": False}) is False

    def test_approval_reason_shown_on_the_dialog(self):
        """审批弹窗必须说清"为什么一定要问"，否则用户只看到"执行 click_wecom_element"。"""
        label = SO.tool_action_label("click_wecom_element", {"target": {"text": "审批通过"}})
        assert "审批通过" in label and "不可撤销" in label, label
        label2 = SO.tool_action_label("click_wecom_element", {"target": {"text": "下一题"}})
        assert "不可撤销" not in label2, label2

    def test_gate_is_wired_into_the_loop(self):
        """闸门必须在主循环的调用点上（否则词表再对也不生效）。"""
        src = open(os.path.join(ROOT, "agent", "loop.py"), encoding="utf-8").read()
        assert "gate_write_tool(" in src
        assert "from security_ops import" in src and "gate_write_tool" in src

    def test_click_like_tools_covered_and_input_tools_excluded(self):
        assert "click_wecom_element" in SO._CLICK_LIKE_TOOLS
        assert "wecom_run_js" in SO._CLICK_LIKE_TOOLS
        assert "type_wecom_text" not in SO._CLICK_LIKE_TOOLS, \
            "把'同意'填进意见框不是提交动作，纳入会误伤填单流程"

    def test_pattern_is_case_insensitive_free_of_regex_errors(self):
        # 词表改动最容易写出非法正则，这条保证它能编译且可搜索
        assert isinstance(SO._APPROVAL_TEXT, re.Pattern)
        assert SO._APPROVAL_TEXT.search("请确认付款")

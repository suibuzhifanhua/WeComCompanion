# -*- coding: utf-8 -*-
"""tests/test_v649_assistant_and_skills.py — 未绑定=AI 助手形态 + 聊天装的技能要在技能中心管得住（v1.0.649）。

两条都是用户这轮直接提的：
① 「默认（首次使用）直接进入 ai 助手模式，除非用户手动绑定」——
   工具级门控其实 v1.0.373 就有（未绑定不注入任何 *_wecom_*），但缺两样：
   模型看不到"为什么少了这批工具"，会照 SOP 硬调甚至声称"已点击通过"；
   界面文案仍把"绑定企微"写成必经第一步，与"不绑也能用"的事实矛盾。
   本轮按用户选的档（**只关碰企微，本机能力保留**）把这两样补上。
② 「agent 聊天过程中安装的技能也要在技能中心中可以管理」——
   聊天里点「⬇️ 安装并继续」走的是和面板同一个 /api/skills/hub/install，
   但装完既不回读也不刷新列表，且列表里它和手工自建的条目挂同一个「自定义」徽标，
   用户认不出"这就是我刚装的那个"。本轮：回读确认 + 刷新 + 单独的「📚 技能库」徽标 + 删除位（=卸载）。

判"旧文案已不在"的断言一律先剥注释/HTML 注释再扫（本项目注释会原样引用被替换掉的旧写法）。
"""
import io
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config_store                        # noqa: E402
from agent_tools import build_agent_tools            # noqa: E402
import agent_tools.dispatch as DSP                   # noqa: E402


from conftest import _src  # noqa: E402


def _code(text, kind="js"):
    s = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    if kind == "js":
        s = re.sub(r"(?m)^[ \t]*//.*$", " ", s)
    elif kind == "html":
        s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    return s


JS00, JS04 = _src("js", "00-boot.js"), _src("js", "04-skills.js")
JS05, JS09 = _src("js", "05-connectors-status.js"), _src("js", "09-chat-render.js")
JS10 = _src("js", "10-chat.js")


@pytest.fixture
def tmp_cfg(tmp_path, monkeypatch):
    p = str(tmp_path / "config.json")
    monkeypatch.setattr(config_store, "config_path", lambda: p)
    io.open(p, "w", encoding="utf-8").write(json.dumps(
        {"schema_version": 2, "models": [], "custom_skills": [], "skill_overrides": {}},
        ensure_ascii=False))
    return p


# ---------------------------------------------------------------- ① 未绑定 = AI 助手形态
def _tools_of(cfg):
    tools, extra = build_agent_tools(cfg)
    return [t["function"]["name"] for t in tools], extra


def test_unbound_drops_every_wecom_tool_but_keeps_local_ones(monkeypatch):
    """用户选的档：只关"碰企微"，本机/联网/生成文件一律照常。"""
    monkeypatch.setattr(DSP, "_wecom_tools_allowed", lambda cfg: False)
    cfg = {"custom_skills": [{"id": "c1", "name": "答题", "prompt": "x" * 1200,
                              "tools": ["read_wecom_page", "click_wecom_element",
                                        "save_workspace_file", "web_search"]}],
           "feature_flags": {"skills": True, "connectors": True}}
    names, extra = _tools_of(cfg)
    assert not [n for n in names if "wecom" in n.lower()], names
    assert "finish" in names, "收尾工具被误杀"
    assert "save_workspace_file" in names or "web_search" in names, \
        "企微没绑就把本机/联网能力一起砍了（用户要的不是这个）：%s" % names
    assert extra is not None


def test_unbound_says_so_in_the_prompt(monkeypatch):
    """光过滤工具不够：模型看不见解释就会硬调，甚至回答"我已经点了通过"。"""
    monkeypatch.setattr(DSP, "_wecom_tools_allowed", lambda cfg: False)
    names, extra = _tools_of({"custom_skills": [], "feature_flags": {"skills": True}})
    assert "AI 助手形态" in extra and "不要假装已执行" in extra, extra[:200]
    assert "设置 → 企业微信" in extra, "没给出可行动的出路"


def test_bound_still_injects_wecom_tools(monkeypatch):
    """反向锁：绑定后这批工具必须回来——否则这条改动就成了新的能力缺口。"""
    monkeypatch.setattr(DSP, "_wecom_tools_allowed", lambda cfg: True)
    cfg = {"custom_skills": [{"id": "c1", "name": "答题", "prompt": "x",
                              "tools": ["read_wecom_page", "save_workspace_file"]}],
           "feature_flags": {"skills": True}}
    names, _ = _tools_of(cfg)
    assert "read_wecom_page" in names, names


def test_first_run_copy_no_longer_makes_binding_a_prerequisite():
    js09 = _code(JS09)
    assert "② 绑定企微 → ③ 左侧选中窗口" not in js09, "空态引导仍把绑定列为必经步骤"
    assert "不用先绑定企业微信" in js09, js09[js09.find("开始前先完成一次配置") - 60:][:400]
    js00 = _code(JS00)
    assert "正在连接企业微信" not in js00, "首启遮罩仍在暗示「必须先连上企微」"
    assert "AI 助手就绪" in js00
    js05 = _code(JS05)
    assert "照常可用" in js05, "未绑定提示条没把「少了什么 / 没少什么」说清"


# ---------------------------------------------------------------- ② 聊天装的技能要看得见
def test_hub_install_then_visible_then_uninstall(tmp_cfg):
    """整条链一次跑穿：装 → 列表里读得到（custom=True）→ 删 → 读不到。
    这是"技能中心可以管理"的最小可验证闭环，缺任何一环用户看到的就是"装了却没了"。"""
    import backend
    from agent_tools.skill_hub_data import HUB_SKILLS
    client = backend.app.test_client()
    hid = HUB_SKILLS[0]["id"]
    r = client.post("/api/skills/hub/install", json={"id": hid})
    d = r.get_json()
    assert d.get("ok"), d
    sid = d["id"]
    assert sid == "hub_" + hid, sid
    listed = client.get("/api/skills").get_json()["skills"]
    hit = [s for s in listed if s.get("id") == sid]
    assert hit, "装完在技能中心列表里读不到（正是用户报的现象）"
    assert hit[0].get("custom") is True and hit[0].get("auto") is False, hit[0]
    assert hit[0].get("name") == (HUB_SKILLS[0].get("name") or hit[0]["name"])
    assert client.delete("/api/skills/custom/" + sid).get_json().get("ok")
    left = [s for s in client.get("/api/skills").get_json()["skills"] if s.get("id") == sid]
    assert not left, "卸载后仍在列表里"


def test_hub_install_of_unknown_id_fails_loudly(tmp_cfg):
    """agent 报的技能 id 不在清单里时必须明确失败——前端原先会退化成"名字=id"继续弹安装钮。"""
    import backend
    r = backend.app.test_client().post("/api/skills/hub/install", json={"id": "no_such_skill"})
    assert r.status_code == 404, r.status_code
    assert "不存在" in r.get_json().get("error", ""), r.get_json()


def test_chat_install_reloads_the_center_and_confirms():
    """聊天里装成功后：① 回读 /api/skills 确认真的可见；② 清技能库面板缓存；③ 刷新技能列表。
    少了①，「toast 说装好了但列表里没有」就永远只有用户能发现。"""
    seg = JS10[JS10.index("skReqInstall").__str__() and JS10.index('$("skReqInstall").onclick'):]
    seg = seg[:seg.index("function showSkillSourceModal")]
    code = _code(seg)
    assert 'getJson("/api/skills")' in code, "装完没回读列表"   # v1.0.655 C档：fetch 收敛为 getJson，锁随新形态（语义不变）
    assert 'hl.dataset.loaded=""' in code, "技能库面板的缓存没清，重开还是旧状态"
    assert "loadSkills()" in code, "技能中心列表没刷新"
    assert "技能中心" in code and "warn" in code, "读不到时没有诚实的降级提示"


def test_hub_skills_get_their_own_badge():
    body = _code(JS04)
    i = body.index("customBadge")
    seg = body[i:i + 320]
    assert 'startsWith("hub_")' in seg and "技能库" in seg, seg
    assert "🧬 自学" in seg and "自定义" in seg, "三类来源的徽标不能互相覆盖"


def test_hub_installed_skill_can_be_deleted_from_the_center():
    """「可管理」的落点：编辑 + 版本回滚 + 删除（=卸载）三件都得在，删除位原先就有，
    这里锁的是它**对 hub_ 条目也生效**（判据是 custom，不是 auto）。"""
    body = _code(JS04)
    assert "s.custom?`<button class=\"ghost\" onclick=\"deleteCustomSkill(" in body
    assert "openSkillVersions(" in body and "openSkillEditor(" in body

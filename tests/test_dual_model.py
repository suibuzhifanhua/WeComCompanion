"""
test_dual_model — 双模型分工集成验证（v1.0.235）

不依赖运行中的 exe，直接调用源码里的真实委派逻辑，对本地 Ollama 实跑：
1. judge_model(cfg) 正确解析「判断大模型」（大模型）。
2. _run_ask_judge 把判断问题委派给大模型并拿到 advice（证明小模型→大模型委派链路通）。
3. build_agent_tools 在存在 judge:model 技能 + 配置了 judge 模型时，自动注入 ask_judge 工具。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_client import judge_model, resolve_model
from agent_tools.executors import _run_ask_judge
from agent_tools.dispatch import build_agent_tools
from agent_tools.registry import TOOLS as REGISTERED_TOOLS


OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/v1")   # v1.0.657 发布去痕：实测地址走环境变量

# 小模型（默认工具循环） + 大模型（判断）
SMALL = {"id": "m_small", "name": "qwen3.5:9b", "model": "qwen3.5:9b",
         "base_url": OLLAMA, "enabled": True, "is_default": True}
JUDGE = {"id": "m_judge", "name": "qwen3.8:latest", "model": "qwen3.8:latest",
         "base_url": OLLAMA, "enabled": True, "is_default": False}

cfg = {
    "models": [SMALL, JUDGE],
    "judge_model_id": "m_judge",          # 配置了判断模型 → 双模型
    "enabled_skills": None,               # None = 全技能启用（含 exam-solver: judge=model）
    "skill_overrides": {},
}

print("=" * 60)
print("[1] judge_model 解析")
jm = judge_model(cfg)
assert jm is not None, "judge_model 返回 None（应解析出大模型）"
assert jm["id"] == "m_judge", f"judge 解析错：{jm['id']}"
print("  OK -> 判断大模型 =", jm["name"])

print("[2] resolve_model 默认仍为小模型")
sm = resolve_model(cfg)
assert sm["id"] == "m_small", f"默认模型错：{sm['id']}"
print("  OK -> 默认(小)模型 =", sm["name"])

print("[3] _run_ask_judge 实调大模型拿建议（委派链路）")
q = "判断题：公司月营收100万、成本80万，利润率是多少？选项：A.20% B.25% C.80%"
ctx = "利润=营收-成本=20万；利润率=利润/营收。"
res = _run_ask_judge({"question": q, "context": ctx}, cfg)
assert "advice" in res, f"ask_judge 未返回 advice：{res}"
print("  大模型(judge)返回建议：")
print("   ", res["advice"].strip().replace("\n", "\n    "))
assert res.get("judge") == "qwen3.8:latest"

print("[3b] 同问二次调用命中缓存（不重复烧大模型）")
from agent_tools.executors import _ASK_JUDGE_CACHE
n_before = len(_ASK_JUDGE_CACHE)
res2 = _run_ask_judge({"question": q, "context": ctx}, cfg)
assert res2.get("cached") is True, f"二次调用未命中缓存：{res2}"
assert res2["advice"] == res["advice"], "缓存返回与首次不一致"
assert len(_ASK_JUDGE_CACHE) == n_before, "缓存条数异常增长"
print("  OK -> cached=True，advice 与首次一致")

print("[4] build_agent_tools 自动注入 ask_judge")
tools, prompt_extra = build_agent_tools(cfg)
names = [t["function"]["name"] for t in tools]
assert "ask_judge" in names, f"ask_judge 未注入，工具集：{names}"
print(f"  OK -> 工具数={len(names)}，含 ask_judge；协作 prompt 长度={len(prompt_extra)}")
print("  协作约束片段：", prompt_extra[:80].replace("\n", " "), "...")

print("[5] 单模型兼容：judge_model_id 留空时不应注入 ask_judge")
cfg_single = {"models": [SMALL, JUDGE], "judge_model_id": "", "enabled_skills": None}
assert judge_model(cfg_single) is None, "空 judge 应退回单模型(None)"
tools2, _ = build_agent_tools(cfg_single)
names2 = [t["function"]["name"] for t in tools2]
assert "ask_judge" not in names2, "单模型下不应出现 ask_judge"
print("  OK -> judge_model_id='' 时 dual_model 退回单模型，ask_judge 不注入")

print("=" * 60)
print("ALL PASS ✅  双模型分工委派链路（小模型工具 / 大模型判断）实跑验证通过")

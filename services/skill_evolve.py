# -*- coding: utf-8 -*-
"""skill_evolve.py — 技能自进化（v1.0.329，对标 Hermes「自动 Skill 沉淀」）
================================================================================
任务完成后把「可复用的成功工作流」提炼为技能，写入 custom_skills（id=auto_ 前缀、
auto=True）。技能经 build_agent_tools 常规注入——下次同类任务自动生效，即
「越用越聪明」的技能层。三层沉淀体系：记忆层（memory_ops）· 会话层（conversations
归档总结）· 技能层（本模块）。

防垃圾四闸：
  1. 总闸   feature_flags.evolve（默认开，设置页可关）
  2. 冷却   每日最多 DAILY_LIMIT 次（产物库/自进化/state.json 记当日计数）
  3. 上限   auto_ 技能总数 AUTO_SKILL_CAP（防技能库膨胀，超出停学并提示）
  4. 质量   LLM 判断 worth + 名称 2-gram Jaccard≥0.5 去重 + 工具名白名单强校验
"""
import datetime
import io
import json
import os
import re
import threading
import uuid

from core_utils import log_event, workspace_root
from core.config import atomic_write_json  # v1.0.510（P2-13）

DAILY_LIMIT = 5
AUTO_SKILL_CAP = 30
_NAME_SIM = 0.5          # 与现有技能名的 2-gram Jaccard 阈值
_MIN_TOOLS = 1           # 至少用过 1 次工具（纯聊天没有可沉淀的工作流）
_MIN_FINAL = 80          # 完成答复最少字数
_MIN_TASK = 6            # 任务描述最少字数

_EV_LOCK = threading.Lock()
# v1.0.515 修复（审计 P1-4）：in-flight 上限——原实现每条合格对话都起线程排队在
# _EV_LOCK 上，LLM 故障日（每次调用最坏 ~1h）随对话数量无界积累。对齐 memory_ops
# 的 inflight≤2 模式：积压即丢弃（设计如此，沉淀是锦上添花，不丢主流程）。
_EV_INFLIGHT = {"n": 0}
_EV_INFLIGHT_LOCK = threading.Lock()


def _bump_quota(reason):
    """消耗一次当日沉淀额度（v1.0.515：LLM 失败也计数——原 DAILY_LIMIT 只在
    成功入库时 +1，LLM 持续失败/输出非 JSON 时日闸永不触发，一天可烧 N 次失败调用）。
    在 _EV_LOCK 内调用。返回更新后的 state。"""
    st = _load_state()
    today = datetime.date.today().isoformat()
    if st.get("date") != today:
        st = {"date": today, "count": 0}
    st["count"] = st.get("count", 0) + 1
    _save_state(st)
    log_event(f"[evolve] 消耗沉淀额度（{reason}）：今日 {st['count']}/{DAILY_LIMIT}")
    return st


def _state_path():
    return os.path.join(workspace_root(), "自进化", "state.json")


def _load_state():
    try:
        with io.open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(st):
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # v1.0.510（P2-13）：tmp+os.replace 样板收敛到 atomic_write_json
    atomic_write_json(p, st, indent=1)


def _norm_name(s):
    return re.sub(r"[\s，。、；：！？\-—\[\]()（）'\"_/]", "", s or "")


def _ngrams(s):
    # 技能名只有 4-10 字：用单字集合（每字=语义单元）比 2-gram 敏感且更准——
    # 「下载整理 vs 下载文件整理」1-gram Jaccard=0.67（拦），2-gram 仅 0.33（漏）。
    # v1.0.364 审查修复（M13）：删除本文件内被本定义覆盖的旧 2-gram 死代码，
    # 消除"改了上面那份不生效"的维护陷阱。
    s = _norm_name(s)
    return set(s)


def _similar(a, b):
    na, nb = _norm_name(a), _norm_name(b)
    # 子串包含：短名嵌在长名里（如「周报」⊂「周报生成」）视为同族
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 3 and short in long_:
        return 1.0
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _real_tool_names():
    try:
        from agent_tools import TOOLS
        return {t.get("function", {}).get("name", "") for t in TOOLS} - {""}
    except Exception:  # noqa: BLE001
        return set()


def _existing_skill_names(cfg):
    names = []
    try:
        from agent_tools.skills import SKILLS
        names += [s.get("name", "") for s in SKILLS]
    except Exception:  # noqa: BLE001
        pass
    for s in (cfg.get("custom_skills") or []):
        names.append(s.get("name", ""))
    return [n for n in names if n]


def maybe_distill_async(user_text, final, tool_names, cfg):
    """后台线程入口：条件闸全过才调 LLM 沉淀。永不抛异常、永不阻塞主流程。"""
    try:
        ff = cfg.get("feature_flags") or {}
        if ff.get("evolve") is False:
            return
        task = str(user_text or "").strip()
        final = str(final or "").strip()
        tools = [str(t) for t in (tool_names or []) if t and not str(t).startswith("👥")]
        if len(task) < _MIN_TASK or len(final) < _MIN_FINAL or len(tools) < _MIN_TOOLS:
            return  # 纯问答/太琐碎，不进 LLM 省 token
        # v1.0.515（审计 P1-4）：in-flight ≤2，积压直接丢弃
        with _EV_INFLIGHT_LOCK:
            if _EV_INFLIGHT["n"] >= 2:
                log_event("[evolve] 已有 2 个沉淀任务排队/执行中，本次跳过（防 LLM 故障日无界积累）")
                return
            _EV_INFLIGHT["n"] += 1
        try:
            threading.Thread(target=_distill, args=(task, final, tools, cfg),
                             daemon=True, name="skill-evolve").start()
        except Exception:
            with _EV_INFLIGHT_LOCK:
                _EV_INFLIGHT["n"] -= 1
            raise
    except Exception as e:  # noqa: BLE001
        log_event(f"[evolve] 调度异常（忽略）：{e}")


def _distill(task, final, tools, cfg):
    """全程持 _EV_LOCK 串行（v1.0.333）：冷却检查→LLM→落盘之间不允许插入其他
    distill——旧实现冷却与落盘分两次持锁，LLM 期间另一线程可插入改变判定基础，
    偶发丢技能。每日 ≤5 次且 LLM 有超时，串行最坏几分钟完全可接受。
    v1.0.515：全程递减 _EV_INFLIGHT（maybe_distill_async 递增）。"""
    try:
        try:
            _distill_inner(task, final, tools, cfg)
        finally:
            with _EV_INFLIGHT_LOCK:
                _EV_INFLIGHT["n"] -= 1
    except Exception as e:  # noqa: BLE001
        log_event(f"[evolve] 沉淀异常（忽略）：{e}")


def _distill_inner(task, final, tools, cfg):
    """原 _distill 主体（v1.0.515 拆出，外层负责 inflight 递减）。"""
    try:
        with _EV_LOCK:
            # 条件闸（同步版也设防）：纯问答/太琐碎不进 LLM
            task = str(task or "").strip()
            final = str(final or "").strip()
            tools = [str(t) for t in (tools or []) if t and not str(t).startswith("👥")]
            if len(task) < _MIN_TASK or len(final) < _MIN_FINAL or len(tools) < _MIN_TOOLS:
                return
            st = _load_state()
            today = datetime.date.today().isoformat()
            if st.get("date") != today:
                st = {"date": today, "count": 0}
            if st.get("count", 0) >= DAILY_LIMIT:
                log_event(f"[evolve] 今日已沉淀 {DAILY_LIMIT} 次，冷却跳过")
                return

            from core.config_store import load_config, update_config
            cfg = load_config()          # 锁内取最新
            if (cfg.get("feature_flags") or {}).get("evolve") is False:
                return
            customs = cfg.get("custom_skills") or []
            auto_n = sum(1 for s in customs if str(s.get("id", "")).startswith("auto_"))
            if auto_n >= AUTO_SKILL_CAP:
                log_event(f"[evolve] 自学技能已达上限 {AUTO_SKILL_CAP}，停止沉淀")
                return

            from llm_client import resolve_model, _llm_complete
            model = resolve_model(cfg)
            if not model:
                return
            skill_names = _existing_skill_names(cfg)
            real_tools = _real_tool_names()
            prompt = (
                "你在分析一次【已成功完成】的 agent 任务，判断是否值得把它沉淀为可复用技能。\n\n"
                f"【任务】{task[:300]}\n"
                f"【用过的工具】{'、'.join(tools[:12])}\n"
                f"【完成答复摘录】{final[:700]}\n\n"
                "沉淀标准（三条全满足才 worth=true）：\n"
                "1. 任务类型会重复出现（同类请求之后还可能再来），不是一次性事务；\n"
                "2. 解决过程有固定可复用的套路（明确步骤/固定格式/工具组合）；\n"
                "3. 现有技能清单没有覆盖这个套路。\n"
                f"现有技能：{'、'.join(skill_names[:25]) or '（无）'}\n\n"
                "值得沉淀就只输出一个 JSON（无其他文字）：\n"
                '{"worth": true, "name": "技能名(≤10字)", "icon": "一个emoji", '
                '"desc": "一句话说明(≤40字)", '
                '"prompt": "技能提示词：编号化步骤，写清输入输出、每步用什么工具、结果放哪，第三人称通用化（≤450字，不要带这次任务的具体文件名）", '
                '"tools": ["实际需要的工具名"]}\n'
                '不值得就输出 {"worth": false, "reason": "一句话原因"}'
            )
            out = (_llm_complete(model, [{"role": "user", "content": prompt}]) or "").strip()
            if not out:
                # v1.0.515（审计 P1-4）：LLM 失败也消耗当日额度——否则持续失败时
                # DAILY_LIMIT 永不触发，一天随对话数量烧 N 次失败调用
                _bump_quota("LLM 空响应")
                return
            data = None
            try:
                from core_utils import extract_json
                data = extract_json(out)
            except Exception:  # noqa: BLE001
                data = None
            if not isinstance(data, dict):
                log_event(f"[evolve] 输出非 JSON，放弃：{out[:80]}")
                _bump_quota("输出非 JSON")
                return
            if not data.get("worth"):
                log_event(f"[evolve] 判定不值得沉淀：{str(data.get('reason', ''))[:60]}")
                return

            name = str(data.get("name") or "").strip()[:12]
            desc = str(data.get("desc") or "").strip()[:60]
            sprompt = str(data.get("prompt") or "").strip()
            icon = str(data.get("icon") or "🧬").strip()[:3]
            stools = [t for t in (data.get("tools") or []) if t in real_tools]
            if not name or len(name) < 2 or not sprompt or len(sprompt) < 60 or not stools:
                log_event("[evolve] 技能定义不完整（name/prompt/tools 校验不过），放弃")
                return
            # 去重：与所有现有技能名比相似度（持锁内数据，无并发窗口）
            for n in skill_names:
                if _similar(n, name) >= _NAME_SIM:
                    log_event(f"[evolve] 与现有技能「{n}」相似，跳过沉淀")
                    return

            sid = "auto_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4]
            _new_skill = {"id": sid, "name": name, "icon": icon, "desc": desc,
                          "prompt": sprompt[:600], "tools": stools[:8], "auto": True}
            # v1.0.555（P0-10）：load→改→save 改 update_config 事务——原无整体锁，
            # 与技能中心/设置页的并发写互相覆盖（沉淀的技能静默丢失）；且原 cfg 是
            # LLM 调用前加载的（跨分钟级调用持有），事务内基于 fresh cfg 追加，
            # 不覆盖并发写入的新技能。
            def _mutate(c):
                cs = c.get("custom_skills") or []
                # 并发窗口内可能已有同名/同 id 沉淀（双保险去重）
                if not any(s.get("id") == sid for s in cs):
                    cs.append(_new_skill)
                c["custom_skills"] = cs
                return True
            update_config(_mutate)
            # 技能入库与计数同锁更新（原子：不会出现入库了但没计数导致超量）
            st2 = _load_state()
            d2 = datetime.date.today().isoformat()
            if st2.get("date") != d2:
                st2 = {"date": d2, "count": 0}
            st2["count"] = st2.get("count", 0) + 1
            _save_state(st2)
            log_event(f"[evolve] ✨ 自学新技能「{name}」（{sid}，{len(stools)} 工具）——下次同类任务自动可用")
    except Exception as e:  # noqa: BLE001
        log_event(f"[evolve] 沉淀异常（忽略）：{e}")

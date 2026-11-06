# -*- coding: utf-8 -*-
"""self_repair - LLM self-maintenance subsystem (v1.0.348).

Collects runtime evidence (error lines from companion.log, experience library,
skill roster), asks the LLM for structured findings, and applies ONLY whitelisted
low-risk fixes:
  auto (apply="auto"):
    - skill_lesson : append actionable lesson to a builtin skill prompt
                     (stored via skill_overrides, marked + deduped; reversible
                     in the skill center UI)
    - experience   : save a problem/solution pair into experiences.json
  manual only (report, never auto-applied):
    - code_fix     : code-level bug suggestions (NO self code modification)
    - config_suggest: config/whitelist tuning suggestions
Safety: LLM output can only enter the whitelist channels above; anything touching
self code/config goes to the report. Reports are written under the workspace
self-repair dir for traceability.

NOTE: all user-facing Chinese strings live in _CN (populated via Edit tool to
guarantee UTF-8 on disk; this file's source is kept ASCII-only because the local
editor mangles non-ASCII on file creation).
"""

import io
import json  # noqa: F401  (kept for parity with sibling modules)
import os
import re
import sys
import threading
import time
import datetime

from core.config_store import load_config, update_config, _load_experiences, _save_experiences   # v1.0.570 清理：save_config 未用摘除
from core.config import atomic_write_json  # v1.0.510（P2-13）
from core.daemon import start_periodic   # A13+B1：守护骨架收敛
from core_utils import (log_event, workspace_root, companion_log_dir,
                        extract_json)

# ---- Chinese runtime strings (populated via Edit; ASCII source by design) ----
_CN_SUBDIR = "自修复"
_CN_REPORT_PREFIX = "健康报告_"
_CN_SKILL_MARK = "【自修复沉淀"
_RUN_ACTIVE = False               # v1.0.350 防重入：手动+每日守护+重试曾叠加并发跑三遍
_CN_ERR_ACTIVE = "已有一个体检正在运行，请等它完成（结果会自动显示）"
_CANCEL_EVENT = threading.Event()  # v1.0.356 中途取消：request_repair_cancel 置位，流式循环内即时响应
_CN_ERR_CANCELLED = "已被用户取消"
_CN_MSG_CANCELLED = "已按用户要求取消本次体检"


def _cancelled_result(start_ts=None, tokens=0):
    """取消收尾：progress + last_run 落盘，返回统一返回结构。"""
    _write_progress("cancelled", tokens, (time.time() - start_ts) if start_ts else 0, "", _CN_MSG_CANCELLED)
    _write_last_run({"ts": int(time.time()), "ok": False,
                     "error": _CN_ERR_CANCELLED, "cancelled": True})
    return {"ok": False, "cancelled": True, "error": _CN_ERR_CANCELLED}


# ---------- v1.0.357 自动代码修复闭环（code_fix 发现 → 补丁 → 验证 → 提交/回滚） ----------
# 设计原则：多层闸门 + 永可回滚。①开关默认关 ②每日/单次限额 ③禁改清单（配置/构建/数据/自身）
# ④补丁必须为「唯一片段替换」或整文件重写 ⑤应用前写 .__autofix_bak__ 备份
# ⑥验证 = py_compile（始终）+ 全量回归 pytest（可用时）；任何一步失败立即还原原文件。
_AUTO_FIX_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()      # v1.0.361 审查修复（P15）：防重入改原子 check+set
_AUTO_FIX_DAY = {"date": "", "count": 0}
_AUTO_FIX_DAILY_CAP = 3           # 每天最多尝试次数（防修复风暴）
_AUTO_FIX_PER_RUN = 2             # 单次体检最多尝试条数
_AUTO_FIX_SRC_CAP = 40000         # 喂给模型的源码字符上限
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # v1.0.401 归位：上一级为项目根
_AUTO_FIX_FORBIDDEN = ("config.json", "build.py", "self_repair.py", "feature_flags.py",
                       "core_utils.py", "test_", "产物库", "dist", "build", ".git", "__autofix_bak__")
_CN_PROMPT_AUTOFIX = ("你是软件自我修复引擎。下面给出一条运行问题与相关源码文件内容。\n"
                      "请只输出一个 JSON 补丁（不要 markdown 代码块、不要解释文字），格式二选一：\n"
                      '{"mode":"block","search":"<原文件中精确出现且仅出现一次的代码片段>","replace":"<替换后的片段>"}\n'
                      '{"mode":"file","new_content":"<完整的新文件内容>"}\n'
                      "规则：改动最小化，只修复给定问题；不要顺手重构或重新格式化；"
                      "保持既有代码风格；不确定就输出 {\"mode\":\"none\"}。")


def _autofix_gate_ok(cfg):
    """闸门：开关 feature_flags.self_auto_fix + 每日次数上限。返回 (ok, reason)。
    v1.0.361 审查修复（P15）：日计数读写统一走 _AUTO_FIX_LOCK（原 gate 无锁、attempt 持锁，口径不一）。"""
    flags = (cfg or {}).get("feature_flags") or {}
    if not flags.get("self_auto_fix"):
        return False, "自动代码修复开关未开启（设置 → 自我修复）"
    today = datetime.datetime.now().strftime("%Y%m%d")
    with _AUTO_FIX_LOCK:
        if _AUTO_FIX_DAY["date"] != today:
            _AUTO_FIX_DAY["date"] = today
            _AUTO_FIX_DAY["count"] = 0
        if _AUTO_FIX_DAY["count"] >= _AUTO_FIX_DAILY_CAP:
            return False, f"已达今日自动修复尝试上限（{_AUTO_FIX_DAILY_CAP} 次/天）"
    return True, ""


def _autofix_locate_file(detail):
    """从 finding detail 提取项目内可修复文件（.py/.js/.html）。v1.0.373：解析
    LLM 按约定输出的【文件: 相对路径】标记（分析 prompt 已给出源码清单）。
    v1.0.470 修复（P1-14）：删除文件名猜测回退——LLM 在描述里提到「参考 xx.py 的
    写法」时，原 os.walk 全项目匹配会定位到问题无关的文件并打补丁。定位不到约定
    标记就拒绝（返回 None → 记 rejected）。宁可不修，不能修错文件。"""
    root = _PROJECT_ROOT
    text = str(detail) or ""
    # 约定标记：【文件: js/10-chat.js】（来自体检 prompt 的源码清单约定）
    m = re.search(r"【文件[:：]\s*([^\】\]]+)】", text)
    if m:
        rel = m.group(1).strip().replace("\\", "/").lstrip("/")
        cand = os.path.join(root, rel)
        if os.path.isfile(cand) and _autofix_allowed(cand):
            return cand
    return None


def _autofix_allowed(path):
    """白名单：必须是本项目文件，扩展名限 .py/.js/.html，且不在禁改清单。
    v1.0.373：放开 .js/.html——近期的缺陷几乎全是前端，且已有对应验证手段
    （node --check / tools.check_frontend）。"""
    p = os.path.abspath(path)
    root = os.path.abspath(_PROJECT_ROOT)
    if not p.startswith(root) or not p.endswith((".py", ".js", ".html")):
        return False
    low = p.replace(root, "").lower()
    if any(seg and seg.lower() in low for seg in _AUTO_FIX_FORBIDDEN):
        return False
    return True


def _autofix_git_available():
    """找可用的 git（PATH → WorkBuddy 便携版 → 软件自装便携版）。返回 exe 路径或 None。
    v1.0.612：这套找法原先在 b_repair / self_repair / version_store 各抄一遍，
    加第三种来源要改三处 —— 收敛到 core.portable.find_git()。"""
    from core import portable
    return portable.find_git()


def _autofix_git_commit(title):
    """提交自动修复（best-effort：git 不可用/无变化都静默跳过）。返回短 hash 或 ""。"""
    gi = _autofix_git_available()
    if not gi:
        return ""
    try:
        import subprocess
        root = os.path.abspath(_PROJECT_ROOT)

        def _run(args):
            return subprocess.run([gi, "-C", root] + args, capture_output=True, timeout=90,
                                  creationflags=0x08000000 if os.name == "nt" else 0)
        _run(["add", "-A"])
        r = _run(["-c", "user.name=autofix", "-c", "user.email=autofix@local",
                  "commit", "-m", "auto-fix: " + str(title)[:60]])
        if r.returncode == 0:
            r2 = _run(["rev-parse", "--short", "HEAD"])
            return (r2.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _run_py_compile_file(path):
    """语法编译验证（冻结 exe 也可用）。返回 (ok, tail)。"""
    import py_compile
    try:
        py_compile.compile(path, doraise=True)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _pytest_available():
    try:
        import pytest  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _run_pytest_full():
    """全量回归（开发环境才有 pytest；冻结 exe 返回 (False, "pytest 不可用")）。
    v1.0.470 修复（P2-12）：frozen 环境显式跳过——原只查 import pytest，打包环境若
    带 pytest，sys.executable 是 WeComCompanion.exe，subprocess 会拉起一个 GUI 实例
    跑"回归"（单实例互斥令其立即退出 → 回归永远失败 → 补丁被误还原）。"""
    if getattr(sys, "frozen", False):
        return False, "frozen 环境跳过 pytest 回归（sys.executable 非解释器）"
    try:
        import subprocess
        r = subprocess.run([sys.executable, "-m", "pytest", os.path.join(_PROJECT_ROOT, "tests"), "-q",
                            "--ignore=" + os.path.join(_PROJECT_ROOT, "tests", "test_dual_model.py")],
                           capture_output=True, timeout=600, cwd=_PROJECT_ROOT,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        out = (r.stdout or b"").decode("utf-8", "replace")
        # pytest 约定：退出码 0 = 全部通过（非 0 = 有失败/错误/收集问题）
        return (r.returncode == 0), out[-800:]
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _model_candidates(cfg, model_id=None, limit=3):
    """体检/补丁共用的模型候选链：指定模型/默认模型优先 + 已启用模型去重兜底（最多 limit 个）。"""
    from llm_client import resolve_model
    model = resolve_model(cfg, model_id) if model_id else resolve_model(cfg)
    candidates, seen = [], set()
    for m in [model] + [m for m in ((cfg or {}).get("models") or []) if m.get("enabled", True)]:
        k = (m.get("id") or "", m.get("name") or "", m.get("base_url") or "", m.get("model") or "")
        if k in seen:
            continue
        seen.add(k)
        candidates.append(m)
        if len(candidates) >= limit:
            break
    return model, candidates


def _stream_answer_with_fallback(cfg, messages, on_model=None, on_token=None,
                                 model_id=None, checkup=False):
    """流式回答 + 多模型兜底骨架（B3：体检主分析与自动补丁生成双实现合并，单源）。

    事件消费单源：token 累计 / done 收尾 / error 判定（is_stream_interrupt 是可重试
    信号 continue，其余 break 换下一候选）/ _CANCEL_EVENT 每个事件边界即时退出。
    两处历史差异全部收进参数——checkup=True 走体检侧行为（补丁生成侧为默认）：
      · 调用日志「LLM 体检调用」（补丁侧「LLM 调用」），错误日志带「（不可重试）」
      · poll_abort 把取消位透传给 llm_client 流内轮询（v1.0.515：取消只在事件到达时
        生效的话，流静默挂 120~1170s 期间点取消完全无效；透传后 5s 轮询即掐断）
      · 备用模型命中打印「默认模型失败，已用备用模型 …」；done 后取消位已置仍按
        取消收尾（补丁侧此时按成功返回——历史行为，原样保留）
    on_model(mn) 换模型时回调；on_token(raw_len, mn) 每 token 回调（节流写盘在调用方）。
    返回 (raw, used_mn, tried, cancelled)：未产出时 raw/used_mn 为 ""。"""
    from llm_client import stream_llm_events
    model, candidates = _model_candidates(cfg, model_id)
    call_tag = "LLM 体检调用" if checkup else "LLM 调用"
    err_tag = "（不可重试）" if checkup else ""
    raw, tried = "", []
    for m in candidates:
        mn = m.get("name") or m.get("model") or "?"
        log_event("[repair] " + call_tag + "：" + mn)
        if on_model:
            try:
                on_model(mn)
            except Exception:  # noqa: BLE001
                pass
        raw = ""
        err_last = ""
        ev_kw = {"poll_abort": (lambda: _CANCEL_EVENT.is_set())} if checkup else {}
        for ev in stream_llm_events(m, messages, **ev_kw):
            if _CANCEL_EVENT.is_set():   # v1.0.356：流式阶段即时响应取消（不等当前块消化完）
                return raw, "", tried, True
            if ev[0] == "token":
                raw += ev[1]
                if on_token:
                    on_token(len(raw), mn)
            elif ev[0] == "done":
                raw = ev[1].get("text") or raw
                break
            elif ev[0] == "error":
                err_last = str(ev[1])[:120]
                # v1.0.362：stream_interrupt（空闲/无产出/断流熔断）是可重试信号——
                # continue 消费 stream_llm_events 的自动重发流，不 break 不换模型
                from llm_client import is_stream_interrupt   # v1.0.654 A3：判定单源化
                if is_stream_interrupt(ev):
                    log_event("[repair] 流式中断（等待自动重试）：" + err_last)
                    continue
                log_event("[repair] 流式错误" + err_tag + "：" + err_last)
                break
        if checkup and _CANCEL_EVENT.is_set():
            return raw, "", tried, True   # 取消：不再尝试下一个候选模型，直接走取消收尾
        if raw:
            if checkup and m is not model:
                log_event("[repair] 默认模型失败，已用备用模型 " + mn + " 完成体检")
            return raw, mn, tried, False
        if mn not in tried:
            tried.append(mn)
        log_event("[repair] 模型未返回内容：" + mn + (err_last and ("（" + err_last + "）") or ""))
    return "", "", tried, False


def _llm_complete_with_fallback(cfg, messages, on_model=None):
    """带兜底的 LLM 调用（体检主分析同款链）：逐候选尝试，流式事件消费
    （stream_interrupt 可重试信号 continue；error break 换下一个）。
    返回 (raw, used_model_name) 或 (None, "")。
    B3：流事件骨架单源化到 _stream_answer_with_fallback（本侧 checkup=False）。"""
    raw, mn, _tried, cancelled = _stream_answer_with_fallback(cfg, messages,
                                                              on_model=on_model)
    if cancelled:   # 取消返回形态不变：(None, "")
        return None, ""
    return (raw, mn) if raw else (None, "")


def attempt_code_fix(finding, cfg):
    """对单条 code_fix 发现尝试闭环修复，返回记录 dict：
    {"finding": 标题, "file": 路径, "status": committed|applied|reverted|rejected, "detail": 说明}
    v1.0.373：①验证按文件类型分流（py→py_compile+pytest；js→node --check；
    html→tools.check_frontend 全套）——此前只支持 .py，前端缺陷无法自动修复；
    ②补丁生成走多模型兜底链（与体检主分析一致，默认模型空转不再卡死）；
    ③补丁被还原时沉淀经验库（下次分析能看到"上次这么修失败了"）。"""
    rec = {"finding": str(finding.get("title") or finding.get("detail") or "")[:80], "status": "rejected", "detail": ""}
    ok, reason = _autofix_gate_ok(cfg)
    if not ok:
        rec["detail"] = reason
        return rec
    path = _autofix_locate_file(finding.get("detail"))
    if not path:
        rec["detail"] = "未能从问题描述定位到可修复的源码文件（或命中禁改清单）"
        return rec
    rec["file"] = os.path.relpath(path, _PROJECT_ROOT)
    with _AUTO_FIX_LOCK:
        _AUTO_FIX_DAY["count"] += 1
        if _CANCEL_EVENT.is_set():
            rec["status"], rec["detail"] = "skipped", "已取消"
            return rec
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                src = f.read()
        except Exception as e:  # noqa: BLE001
            rec["detail"] = f"读取源码失败：{e}"
            return rec
        # ① LLM 生成补丁（多模型兜底链，与体检主分析一致）
        prompt = (_CN_PROMPT_AUTOFIX + "\n\n【问题描述】" + str(finding.get("detail") or rec["finding"])[:1200]
                  + "\n\n【源码文件 " + os.path.relpath(path, _PROJECT_ROOT) + "】\n" + src[:_AUTO_FIX_SRC_CAP])
        raw, used_m = _llm_complete_with_fallback(
            cfg, [{"role": "user", "content": prompt}],
            on_model=lambda mn: _write_progress("apply", 0, 0, mn, "正在生成补丁（" + mn + "）…"))
        if not raw:
            rec["detail"] = "补丁生成失败（所有候选模型均未返回内容）"
            return rec
        parsed = extract_json(raw or "") or {}
        if not isinstance(parsed, dict) or parsed.get("mode") not in ("block", "file"):
            rec["detail"] = "模型未返回可用补丁（mode=none 或格式不符）"
            return rec
        # ② 计算新内容（block 必须唯一命中；file 直接整文件）
        if parsed["mode"] == "block":
            search, replace = str(parsed.get("search") or ""), str(parsed.get("replace") or "")
            if not search or search == replace:
                rec["detail"] = "补丁无效（空片段或无变化）"
                return rec
            if src.count(search) != 1:
                rec["detail"] = f"补丁锚点不唯一（出现 {src.count(search)} 次），拒绝应用"
                return rec
            new_src = src.replace(search, replace, 1)
        else:
            new_src = str(parsed.get("new_content") or "")
            if len(new_src) < 10 or new_src == src:
                rec["detail"] = "补丁无效（空内容或无变化）"
                return rec
        # ③ 应用前备份 → 写入
        bak = path + ".__autofix_bak__"
        with io.open(bak, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(new_src)
            # ④ 验证：按文件类型分流
            #    .py  → py_compile + 全量回归 pytest（可用时）
            #    .js  → node --check 单文件语法
            #    .html→ tools.check_frontend 全套（div/id/CSS/JS）
            ext = os.path.splitext(path)[1].lower()
            if ext == ".py":
                okc, tailc = _run_py_compile_file(path)
                if not okc:
                    raise RuntimeError("语法编译失败：" + tailc[:200])
                if _pytest_available():
                    okt, tailt = _run_pytest_full()
                    if not okt:
                        raise RuntimeError("全量回归未通过：" + tailt[-300:])
                    verify = "py_compile + 全量回归通过"
                else:
                    verify = "py_compile 通过（打包环境无 pytest，仅语法级验证）"
            elif ext == ".js":
                try:
                    # v1.0.619：实现从 tools/ 上移到 core.frontend_check——冻结包里
                    # `from tools import ...` 必 ModuleNotFoundError，被 except 吞掉后
                    # 报成"JS 语法校验失败：<导入错误>"，正式包从此修不了任何前端文件。
                    # strict=True：没有 node 就判不通过（宁可放弃自动补丁，也不能把
                    # 未经任何校验的 JS 落进源码）。
                    from core.frontend_check import check_js_file
                    okj, tailj = check_js_file(path, strict=True)
                except Exception as ve:
                    okj, tailj = False, str(ve)[:200]
                if not okj:
                    raise RuntimeError("JS 校验未通过：" + tailj[:200])
                verify = "node --check 通过"
            else:   # .html
                try:
                    from core import frontend_check as cf
                    _html_src = None
                    with io.open(path, "r", encoding="utf-8") as f:
                        _html_src = f.read()
                    errs = []
                    cf.check_div_balance(_html_src, errs)
                    cf.check_dup_ids(_html_src, errs)
                    cf.check_script_style_close(_html_src, errs)
                    cf.check_css_braces(_html_src, errs)
                except Exception as ve:
                    errs = [str(ve)[:200]]
                if errs:
                    raise RuntimeError("HTML 结构校验失败：" + "; ".join(errs)[:300])
                verify = "HTML 结构校验通过（div/id/CSS）"
        except Exception as e:  # noqa: BLE001
            # ⑤ 失败：立即还原 + 沉淀经验（下次分析能看到"上次这么修失败了"）
            try:
                with io.open(path, "w", encoding="utf-8") as f:
                    f.write(src)
                os.remove(bak)
            except Exception:  # noqa: BLE001
                pass
            log_event(f"[autofix] 补丁验证失败已还原 {rec['file']}：{e}")
            try:
                _save_failure_lesson(rec["finding"], str(e)[:300])
            except Exception:  # noqa: BLE001
                pass
            rec["status"], rec["detail"] = "reverted", str(e)[:300]
            return rec
        # ⑥ 验证通过：先试 git 提交，**提交成功才删备份**（v1.0.616 改的时序）
        commit = _autofix_git_commit(rec["finding"])
        if commit:
            try:
                os.remove(bak)
            except OSError:  # noqa: BLE001
                pass
            tail = f"；已提交 {commit}"
        else:
            kept = _stash_backup(path, bak)
            tail = "；git 不可用、未提交版本记录，原文件已存 " + kept + "（要还原把它覆盖回 " + path + "）"
        rec["status"] = "committed" if commit else "applied"
        rec["detail"] = verify + "（模型：" + (used_m or "?") + "）" + tail
        log_event(f"[autofix] 自动修复已应用 {rec['file']}：{rec['detail']}")
        return rec


def _save_failure_lesson(finding, fail_reason):
    """补丁被还原时把失败写入经验库（去重），让下一次分析知道这条修复路径走不通。"""
    prob = ("自动修复失败回顾：" + str(finding or ""))[:200]
    items = _load_experiences() or []
    if any(prob[:40] in str(x.get("problem", "")) for x in items):
        return
    from agent_tools.executors import _exp_rec
    items.append(_exp_rec({"module": _CN_EXP_MODULE,
                           "problem": prob,
                           "solution": ("上次自动补丁验证失败已还原，失败原因：" + str(fail_reason or "")
                                        + "。不要重复相同改法，换思路或转人工处理。")[:400]}))
    _save_experiences(items)
    log_event("[autofix] 失败教训已沉淀经验库")
_CN_ERR_PAT = r"tool-error|失败|错误|异常|死循环|Traceback|EXC|超时|中断|500|502"
_CN_PROMPT = (
    "你是「企微伴侣」的自我修复引擎。下面是软件运行日志中的错误相关行与经验库近期条目。"
    "请分析并只输出一个 JSON 对象（不要代码块标记、不要多余文字）：\n"
    '{"findings": [{"type": "skill_lesson|experience|code_fix|config_suggest", '
    '"skill_id": "技能id（type=skill_lesson 时必填，且必须在下方技能清单中）", '
    '"lesson": "要追加到技能提示词的一条可执行教训（≤200字，skill_lesson 用）", '
    '"module": "模块名（experience 用）", "problem": "一句话问题（experience 用）", '
    '"solution": "可执行解法（experience 用）", '
    '"title": "一句话标题", "detail": "分析依据（引用日志关键行）与建议", '
    '"severity": "high|mid|low"}]}\n'
    "分析规则：\n"
    "- 只基于给出的数据判断，不臆测；同一问题只报一条，反复出现的优先（在 detail 注明出现次数）\n"
    "- skill_lesson：仅当教训具体可执行、能避免同类踩坑时输出（如某类按钮需改用坐标点击、"
    "某字段必须用全称匹配）；skill_id 必须从技能清单中选\n"
    "- experience：问题与解法成对沉淀，供后续同类任务复用\n"
    "- code_fix：描述问题现象、疑似根因与修改思路。**必须在 detail 开头用【文件: 相对路径】标明要改的文件**"
    "（只能从下方「可修复源码文件清单」中选），不要输出大段代码\n"
    "- config_suggest：参数/白名单类调整建议\n"
    "- 若数据不足以支撑任何结论，或一切健康，返回 {\"findings\": []}\n\n"
    "技能清单（skill_lesson 的 skill_id 只能从中选）：\n{skill_list}\n\n"
    "【运行日志·错误相关行（最近 {err_n} 条，[出现 N 次] 表示同类错误聚类计数）】\n{errors}\n\n"
    "【经验库近期条目】\n{exps}\n"
    "【可修复源码文件清单（code_fix 的【文件:】只能从中选）】\n{src_list}\n"
)
_CN_EXP_MODULE = "自修复"
_CN_NOTE_NODATA = "无错误记录与经验条目，无需体检"
_CN_ERR_NOMODEL = "未配置任何模型，无法自我体检"
_CN_ERR_LLMEMPTY = "模型未返回内容"
_CN_ERR_BADJSON = "模型输出不是预期的 JSON 结构"

DAILY_INTERVAL_S = 24 * 3600
NEW_ERR_THRESHOLD = 5          # min new error lines since last check to trigger auto run
_MAX_ERR_LINES = 60           # max error lines fed to the LLM（v1.0.362：120→60 瘦身，
                              # 降低大 prompt 触发本地/共享模型空转的概率，模式识别足够）
_ERR_PAT = re.compile(_CN_ERR_PAT, re.IGNORECASE)
_SKILL_MARK = _CN_SKILL_MARK


def _report_dir():
    d = os.path.join(workspace_root(), _CN_SUBDIR)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


def _stash_backup(src_path, bak_path):
    """v1.0.616（审查 P1）：把自动修复的原文件备份收进 产物库/自修复/还原备份/。

    原先补丁"验证通过"后**先删备份再试 git 提交**，而本项目源码树不是 git 仓库
    （`_autofix_git_commit` 必然失败）→ 每次自动修复都在改完的同一刻烧掉唯一退路，
    遇到"语法没问题但语义错了"的补丁只能人工恢复。现在：提交成功才删，
    提交不了就把备份挪进产物库（看得见、报告里给路径），而不是留在源码树里碍事。"""
    import shutil
    try:
        d = os.path.join(_report_dir(), "还原备份")
        os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(d, stamp + "_" + os.path.basename(src_path))
        shutil.move(bak_path, dst)
        return dst
    except Exception:  # noqa: BLE001
        return bak_path          # 挪不动就原地留着，绝不删


def _read_log_errors(limit=_MAX_ERR_LINES):
    """Tail companion.log and keep error-related lines (most recent last).
    v1.0.353：每行截断 200 字——超长错误行会让体检 prompt 过大，触发本地模型
    空转（v1.0.287 同款问题）；错误行截断不影响定位（完整行在日志里）。"""
    path = os.path.join(companion_log_dir(), "companion.log")
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    errs = [ln.rstrip()[:200] for ln in lines if _ERR_PAT.search(ln)]
    return errs[-limit:]


def _cluster_log_errors(lines, max_groups=30):
    """v1.0.373（优化4）：错误行指纹聚类——同一错误重复 N 次只占 1 条
    （模型更容易抓主要矛盾，token 也省）。指纹 = 去时间戳前缀 + 数字归并为 #。
    输出 "[出现 N 次] 首行原文" 列表（按出现次数降序，最多 max_groups 组）。"""
    groups = {}   # 指纹 -> [count, 归并后文本]
    order = []
    for ln in lines:
        body = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*", "", ln)
        fp = re.sub(r"\d+", "#", body)[:160]
        if fp in groups:
            groups[fp][0] += 1
        else:
            groups[fp] = [1, fp]   # 显示归并文本（避免误导性的具体数字）
            order.append(fp)
    out = ["[出现 %d 次] %s" % (groups[fp][0], groups[fp][1]) for fp in order]
    out.sort(key=lambda t: -int(re.match(r"\[出现 (\d+) 次\]", t).group(1) or 1))
    return out[:max_groups]


def _count_new_errors(since_epoch):
    """Count error lines written after since_epoch (trigger for the daily run)."""
    path = os.path.join(companion_log_dir(), "companion.log")
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return 0
    n = 0
    for ln in reversed(lines):
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
        if not m:
            continue
        try:
            ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001
            continue
        if ts < since_epoch:
            break
        if _ERR_PAT.search(ln):
            n += 1
    return n


def _last_report_epoch():
    d = _report_dir()
    try:
        reps = [f for f in os.listdir(d) if f.startswith(_CN_REPORT_PREFIX) and f.endswith(".md")]
        if not reps:
            return 0
        latest = max(reps)
        return os.path.getmtime(os.path.join(d, latest))
    except Exception:  # noqa: BLE001
        return 0


def _builtin_skill_ids():
    from agent_tools.skills import SKILLS
    return [{"id": s["id"], "name": s.get("name", "")} for s in SKILLS]


def _source_file_list():
    """v1.0.373（优化5）：可修复源码文件相对路径清单（喂给分析模型，code_fix
    按【文件: 路径】直给，定位不再靠猜）。排除禁改清单/产物目录。"""
    root = _PROJECT_ROOT
    out = []
    for dirpath, dirs, files in os.walk(root):
        rel_dir = dirpath.replace(root, "").replace(os.sep, "/").strip("/")
        if any(seg in ("dist", "build", "产物库", ".git", "__pycache__", "tools", "venv")
               for seg in rel_dir.split("/")):
            dirs[:] = []
            continue
        for fn in sorted(files):
            if fn.endswith((".py", ".js")) and not fn.startswith("test_"):
                rel = (rel_dir + "/" + fn) if rel_dir else fn
                if not any(seg.lower() in ("/" + rel.lower()) for seg in _AUTO_FIX_FORBIDDEN):
                    out.append(rel)
    return out[:60]


def _scheduled_task_failures():
    """v1.0.373（优化7）：定时任务失败记录进体检视野（last_result 含异常/失败）。"""
    out = []
    try:
        from services.scheduler import list_schedules
        for it in list_schedules():
            lr = str(it.get("last_result") or "")
            if any(k in lr for k in ("异常", "失败", "超时", "error", "Error")):
                out.append("[定时任务:%s] %s" % (it.get("name", "?"), lr[:160]))
    except Exception:  # noqa: BLE001
        pass
    return out


def collect_health(cfg, limit=_MAX_ERR_LINES):
    """Gather all evidence fed to the LLM (size-capped).
    v1.0.373：错误行先指纹聚类（重复错误归并为「[出现 N 次]」）；补定时任务
    失败记录；附可修复源码文件清单（code_fix 按约定直给文件路径）。"""
    clustered = _cluster_log_errors(_read_log_errors(limit))
    sched_fails = _scheduled_task_failures()
    return {
        "log_errors": clustered + sched_fails,
        "experiences": [
            {k: str(e.get(k, ""))[:120] for k in ("module", "problem", "solution")}
            for e in (_load_experiences() or [])[-30:]
        ],
        "skills": _builtin_skill_ids(),
        "source_files": _source_file_list(),
    }


def _build_prompt(data):
    skill_list = "\n".join(f"- {s['id']} ({s['name']})" for s in data["skills"])
    errors = "\n".join(data["log_errors"]) or "-"
    exps = "\n".join(f"- [{e.get('module','')}] {e.get('problem','')} -> {e.get('solution','')}"
                     for e in data["experiences"]) or "-"
    src_list = "\n".join("- " + f for f in (data.get("source_files") or [])) or "-"
    return _CN_PROMPT.replace("{skill_list}", skill_list) \
                     .replace("{err_n}", str(len(data["log_errors"]))) \
                     .replace("{errors}", errors[-12000:]) \
                     .replace("{exps}", exps[-3000:]) \
                     .replace("{src_list}", src_list)


def _apply_skill_lesson(cfg, skill_id, lesson, record=True):
    """Append a lesson into the skill prompt (skill_overrides store; visible and
    revertible in the skill center). Deduped by the first 40 chars of the lesson.
    Returns (applied_bool, note).
    v1.0.652（审计 P2-D3）：record=False 跳过 record_version——本函数被调两次
    （陈旧 cfg 判定 + 事务内 fresh cfg 重放），旧两次都记版本快照，一次沉淀记两条。"""
    from agent_tools.skills import SKILLS
    from agent_tools.dispatch import _effective_skill
    base = next((s for s in SKILLS if s["id"] == skill_id), None)
    if not base:
        return False, "unknown skill id: " + str(skill_id)
    cur = _effective_skill(base, cfg).get("prompt") or ""
    key = lesson.strip()[:40]
    if _SKILL_MARK in cur and key in cur:
        return False, "duplicate lesson, skipped"
    ov = (cfg.setdefault("skill_overrides", {}).setdefault(skill_id, {}))
    # v1.0.349：追加前快照当前提示词（版本历史，可在技能中心回滚）
    if record:
        try:
            from agent_tools.skill_versions import record_version
            record_version(skill_id, cur, "self_repair", lesson.strip()[:60])
        except Exception:  # noqa: BLE001
            pass
    stamp = datetime.datetime.now().strftime("%m-%d")
    ov["prompt"] = (cur.rstrip() + "\n\n" + _SKILL_MARK + " " + stamp + "\n- " + lesson.strip()).strip()
    return True, "appended to skill " + str(skill_id)


def _write_progress(phase, tokens=0, elapsed_s=0, model="", note=""):
    """v1.0.354：体检进度状态文件（前端轮询展示）。百分比无法可靠预测，
    用「阶段 + 已接收字符数 + 已耗时」如实反映——token 在涨就证明在跑。"""
    try:
        p = os.path.join(_report_dir(), "progress.json")
        # v1.0.510（P2-13）：tmp+os.replace 样板收敛到 atomic_write_json
        atomic_write_json(p, {"phase": phase, "tokens": int(tokens),
                              "elapsed_s": round(float(elapsed_s), 1),
                              "model": str(model or ""), "note": str(note or "")[:160],
                              "ts": int(time.time())}, indent=1)
    except Exception:  # noqa: BLE001
        pass


def _write_last_run(payload):
    """v1.0.350：把最近一次体检结果写入固定状态文件（前端轮询用）。
    成功/失败都写——失败不可见正是"未收到体检结果"误报的根因之一。"""
    try:
        p = os.path.join(_report_dir(), "last_run.json")
        atomic_write_json(p, payload, indent=1)
    except Exception as e:  # noqa: BLE001
        log_event("[repair] last_run write failed: " + str(e))


def _write_failure_report(reason, model_name=""):
    """体检失败也落一份报告（前端轮询据此展示真实原因，而不是猜"模型可能不可达"）。"""
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        rep_path = os.path.join(_report_dir(), _CN_REPORT_PREFIX + ts + ".md")
        with io.open(rep_path, "w", encoding="utf-8") as f:
            f.write("# " + _CN_REPORT_PREFIX + ts + "\n\n- 体检未完成\n- 原因：" + str(reason)
                    + (("\n- 模型：" + model_name) if model_name else "") + "\n")
        return rep_path
    except Exception:  # noqa: BLE001
        return ""


def request_repair_cancel():
    """v1.0.356：请求取消正在运行的体检（幂等）。返回是否确有运行中的体检。
    未在运行时置位无害——下一轮 run_self_repair 启动时会清掉。"""
    _CANCEL_EVENT.set()
    return _RUN_ACTIVE


def run_self_repair(cfg=None, model_id=None):
    """Run one self-check cycle. Returns {ok, findings, applied, skipped, manual, report}.
    v1.0.350：运行中防重入（手动+每日守护+重试叠加曾导致同一分钟跑三遍浪费模型调用）；
    结果（含失败）始终写 last_run.json 与报告文件——前端轮询据 last_run 判定完成。
    v1.0.356：启动时清取消位；运行中可在流式阶段随时被 request_repair_cancel 中断。"""
    global _RUN_ACTIVE
    # v1.0.361 审查修复（P15）：check-then-set 改锁内原子操作——原实现手动触发与
    # daily_loop 并发时可同时通过检查、双跑体检。
    with _RUN_LOCK:
        if _RUN_ACTIVE:
            return {"ok": False, "error": _CN_ERR_ACTIVE, "busy": True}
        _RUN_ACTIVE = True
    _CANCEL_EVENT.clear()
    try:
        return _run_self_repair_inner(cfg, model_id)
    finally:
        with _RUN_LOCK:
            _RUN_ACTIVE = False

def _run_self_repair_inner(cfg=None, model_id=None):
    cfg = cfg or load_config()
    model_name = ""
    _run_start = time.time()
    _write_progress("collect", 0, 0, "")
    data = collect_health(cfg)
    _write_progress("collect", 0, time.time() - _run_start, "",
                    "日志 %d 行 / 经验 %d 条" % (len(data["log_errors"]), len(data["experiences"])))
    if not data["log_errors"] and not data["experiences"]:
        _write_last_run({"ts": int(time.time()), "ok": True, "applied": 0,
                         "skipped": 0, "manual": 0, "note": _CN_NOTE_NODATA})
        return {"ok": True, "findings": [], "applied": [], "skipped": [],
                "manual": [], "note": _CN_NOTE_NODATA}
    prompt = _build_prompt(data)
    from llm_client import resolve_model
    model = resolve_model(cfg, model_id) if model_id else resolve_model(cfg)
    if not model:
        rep = _write_failure_report(_CN_ERR_NOMODEL)
        _write_last_run({"ts": int(time.time()), "ok": False, "error": _CN_ERR_NOMODEL})
        return {"ok": False, "error": _CN_ERR_NOMODEL, "report": rep}
    if _CANCEL_EVENT.is_set():   # v1.0.356：进入模型调用前响应取消
        return _cancelled_result(_run_start)
    model_name = model.get("name") or model.get("model") or "?"
    # v1.0.353 多模型兜底 + v1.0.354 切流式：默认模型空转换备选重试；已接收字符数
    # 实时写 progress.json。B3：流事件骨架单源化到 _stream_answer_with_fallback
    # （checkup=True 侧），prompt 组装与进度落盘留在本函数。
    _last_flush = [0.0]

    def _on_model(mn):
        _last_flush[0] = 0.0
        _write_progress("llm", 0, time.time() - _run_start, mn)

    def _on_token(n, mn):
        now = time.time()
        if now - _last_flush[0] >= 0.6:   # 节流写盘（0.6s）
            _last_flush[0] = now
            _write_progress("llm", n, now - _run_start, mn)

    raw, used_mn, tried, cancelled = _stream_answer_with_fallback(
        cfg, [{"role": "user", "content": prompt}], on_model=_on_model,
        on_token=_on_token, model_id=model_id, checkup=True)
    model_name = used_mn or model_name
    if cancelled or _CANCEL_EVENT.is_set():   # v1.0.356：取消收尾（progress+last_run 均落盘，前端立即可见）
        return _cancelled_result(_run_start, len(raw or ""))
    if not raw:
        _reason = _CN_ERR_LLMEMPTY + "（已尝试：" + "、".join(tried) + "）"
        _write_progress("failed", 0, time.time() - _run_start, "", _reason)
        rep = _write_failure_report(_reason)
        _write_last_run({"ts": int(time.time()), "ok": False, "error": _reason})
        return {"ok": False, "error": _CN_ERR_LLMEMPTY, "report": rep}
    parsed = extract_json(raw) or {}
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    if not isinstance(findings, list):
        rep = _write_failure_report(_CN_ERR_BADJSON, model_name)
        _write_last_run({"ts": int(time.time()), "ok": False, "error": _CN_ERR_BADJSON})
        return {"ok": False, "error": _CN_ERR_BADJSON, "raw": str(raw)[:400]}

    applied, skipped, manual = [], [], []
    valid_ids = {s["id"] for s in data["skills"]}
    changed_cfg = False
    # v1.0.555（P0-10）：待落盘的 skill lesson delta——原实现在函数入口 load_config
    # 后跨数分钟 LLM 流式持有 cfg，末尾 save_config(cfg) 用陈旧快照整体覆盖磁盘
    # （自修复体检期间用户的其它配置保存被静默回滚）。改为收集 delta，落盘前在
    # update_config 锁内基于 fresh cfg 重放。
    _pending_lessons = []
    for f in findings[:8]:
        if not isinstance(f, dict):
            continue
        ftype = str(f.get("type", ""))
        title = str(f.get("title") or f.get("detail") or "")[:80]
        if ftype == "skill_lesson":
            sid = str(f.get("skill_id") or "")
            lesson = str(f.get("lesson") or "")[:300]
            if sid in valid_ids and lesson:
                # v1.0.652（审计 P2-D3）：判定调用 record=False——本次改动不落盘
                # （由下方事务重放真正写入），只在这里做去重/可行性判定，版本快照
                # 只在重放时记一次。
                ok, note = _apply_skill_lesson(cfg, sid, lesson, record=False)
                (applied if ok else skipped).append(
                    {"type": ftype, "skill_id": sid, "title": title, "note": note})
                changed_cfg = changed_cfg or ok
                if ok:
                    _pending_lessons.append((sid, lesson))
            else:
                skipped.append({"type": ftype, "title": title,
                                "note": "skill_id missing or not in roster"})
        elif ftype == "experience":
            items = _load_experiences() or []
            prob = str(f.get("problem") or "")[:200]
            if prob and not any(prob[:40] in str(x.get("problem", "")) for x in items):
                from agent_tools.executors import _exp_rec
                items.append(_exp_rec({"module": str(f.get("module") or _CN_EXP_MODULE),
                                       "problem": prob,
                                       "solution": str(f.get("solution") or "")[:400]}))
                _save_experiences(items)
                applied.append({"type": ftype, "title": title, "note": "saved to experiences"})
            else:
                skipped.append({"type": ftype, "title": title, "note": "duplicate experience"})
        elif ftype in ("code_fix", "config_suggest"):
            manual.append({"type": ftype, "title": title,
                           "detail": str(f.get("detail") or "")[:500],
                           "severity": str(f.get("severity") or "mid")})
    if changed_cfg and _pending_lessons:
        # v1.0.555（P0-10）：delta 在 update_config 锁内基于 fresh cfg 重放——
        # 不再用跨 LLM 调用的陈旧快照整体覆盖 config.json。
        def _mutate(cfg):
            for _sid, _lesson in _pending_lessons:
                _apply_skill_lesson(cfg, _sid, _lesson)
            return True
        try:
            update_config(_mutate)
        except Exception as _ce:  # noqa: BLE001
            log_event(f"[self-repair] 配置 delta 落盘失败（不影响其它修复记录）：{_ce}")

    # v1.0.357 自动代码修复闭环：code_fix 发现 → 补丁 → 验证（编译+回归）→ 提交/回滚。
    # 需 feature_flags.self_auto_fix 开启；有日限额与禁改清单；失败自动还原原文件。
    autofix_records = []
    if manual and (cfg.get("feature_flags") or {}).get("self_auto_fix"):
        _write_progress("apply", len(raw), time.time() - _run_start, model_name, "正在尝试自动代码修复…")
        for _mf in [m for m in manual if m.get("type") == "code_fix"][:_AUTO_FIX_PER_RUN]:
            if _CANCEL_EVENT.is_set():
                break
            try:
                autofix_records.append(attempt_code_fix(_mf, cfg))
            except Exception as e:  # noqa: BLE001
                autofix_records.append({"finding": str(_mf.get("title"))[:80],
                                        "status": "error", "detail": str(e)[:200]})

    _write_progress("apply", len(raw), time.time() - _run_start, model_name,
                    "正在应用修复（自动 %d / 待人工 %d）" % (len(applied), len(manual)))
    # report to disk (traceable); filename to the SECOND（同分钟重跑曾同名覆盖，
    # 前端按文件名变化轮询会永远等不到——v1.0.350 改秒级 + last_run.json）
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rep_path = os.path.join(_report_dir(), _CN_REPORT_PREFIX + ts + ".md")
    try:
        with io.open(rep_path, "w", encoding="utf-8") as f:
            f.write("# " + _CN_REPORT_PREFIX + ts + "\n\n")
            f.write("- errors sampled: " + str(len(data["log_errors"])) + "\n")
            f.write("- findings: " + str(len(findings[:8]))
                    + " (auto " + str(len(applied))
                    + " / skipped " + str(len(skipped))
                    + " / manual " + str(len(manual)) + ")\n\n")
            for a in applied:
                f.write("- [AUTO][" + str(a.get("type")) + "] " + str(a.get("title"))
                        + " : " + str(a.get("note")) + "\n")
            for s_ in skipped:
                f.write("- [SKIP][" + str(s_.get("type")) + "] " + str(s_.get("title"))
                        + " : " + str(s_.get("note")) + "\n")
            for m in manual:
                f.write("- [MANUAL][" + str(m.get("type")) + "] " + str(m.get("title"))
                        + " (" + str(m.get("severity")) + ") : " + str(m.get("detail")) + "\n")
            for r_ in autofix_records:
                f.write("- [AUTOFIX][" + str(r_.get("status")) + "] " + str(r_.get("finding"))
                        + " -> " + str(r_.get("file") or "-") + " : " + str(r_.get("detail")) + "\n")
            if not (applied or skipped or manual):
                f.write("- all healthy, nothing to do.\n")
    except Exception as e:  # noqa: BLE001
        log_event("[repair] report write failed: " + str(e))
        rep_path = ""
    log_event("[repair] self-check done: findings " + str(len(findings[:8]))
              + ", auto " + str(len(applied)) + ", manual " + str(len(manual))
              + (", autofix " + str(len(autofix_records)) if autofix_records else ""))
    _write_progress("done", len(raw), time.time() - _run_start, model_name,
                    "自动 %d / 待人工 %d" % (len(applied), len(manual)))
    _write_last_run({"ts": int(time.time()), "ok": True,
                     "applied": len(applied), "skipped": len(skipped),
                     "manual": len(manual),
                     "autofix": [{"status": r_.get("status"), "finding": r_.get("finding"),
                                  "file": r_.get("file")} for r_ in autofix_records],
                     "report": os.path.basename(rep_path)})
    return {"ok": True, "findings": findings[:8], "applied": applied,
            "skipped": skipped, "manual": manual, "autofix": autofix_records,
            "report": rep_path}


def daily_check_if_due():
    """Daily auto-check entry (called by the daemon thread): flag on + >=24h since
    last report + new error lines >= threshold before actually calling the LLM."""
    cfg = load_config()
    flags = cfg.get("feature_flags") or {}
    if not flags.get("self_repair_daily", True):
        return None
    last = _last_report_epoch()
    if time.time() - last < DAILY_INTERVAL_S:
        return None
    if _count_new_errors(last) < NEW_ERR_THRESHOLD:
        return None
    log_event("[repair] daily auto check triggered (new errors reached threshold)")
    return run_self_repair(cfg)


def daily_loop():
    """Daemon thread: check hourly whether a daily run is due (never raises)."""
    # v1.0.656（B1）：守护循环骨架收敛到 core.daemon.start_periodic（原 while True +
    # try/except 吞异常 + time.sleep(3600)）。backend 以 Thread(target=daily_loop)
    # 启动，join 保持原"该线程不返回"的形态；tick 异常由原语吞掉留痕。
    start_periodic("self-repair-daily", daily_check_if_due, 3600).join()

# -*- coding: utf-8 -*-
"""memory_ops — 本地记忆沉淀（v1.0.312，对齐 WorkBuddy「本地技能与记忆沉淀」）
=================================================================
对话完成后后台提炼「值得长期记住的事实」追加到 产物库/记忆/MEMORY.md，
后续会话 build_chat_messages 自动注入尾部记忆，数据全部本地。

纪律：
- 只在 feature_flags.memory=True 时工作；
- 提炼走 LLM（主模型），失败静默 log 不影响对话；
- 去重（同义行跳过——用简单包含匹配，不搞向量）、总量上限 MAX_LINES 裁最旧。
"""
import os
import re
import threading

from core_utils import workspace_root, log_event

MEM_DIR = os.path.join("产物库", "记忆")
MAX_LINES = 200          # 记忆条目上限（超出裁最旧）
INJECT_BYTES = 3500      # 注入上下文的尾部字节数
_mem_lock = threading.Lock()
_inflight = 0            # 简单并发闸：同时只跑一个提炼线程
_inflight_lock = threading.Lock()   # v1.0.364 审查修复（M11）：check-and-set 原子化


def memory_path():
    return os.path.join(workspace_root(), MEM_DIR, "MEMORY.md")


def memory_text(max_bytes=INJECT_BYTES):
    """读取记忆尾部供上下文注入；文件不存在/为空返回 None。"""
    p = memory_path()
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "ignore")
        # 掐掉可能截断的首行
        nl = data.find("\n")
        if size > max_bytes and nl >= 0:
            data = data[nl + 1:]
        return data.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _extract_one_memory(text, cfg):
    """调 LLM 判断对话是否含值得长期记住的信息；有则返回一行记忆（≤120 字），无则 None。
    刻意窄口径：偏好/事实/约定/教训才记，闲聊与一次性任务结果不记。"""
    try:
        from llm_client import resolve_model, _llm_complete
        model = resolve_model(cfg)
        if not model:
            return None
        prompt = (
            "判断下面这轮对话是否包含值得【长期记住】的信息（用户的偏好/身份背景/项目事实/"
            "工作约定/踩过的教训）。一次性任务结果、闲聊、临时上下文、单次任务的执行结果都不要记。\n"
            "如果有，只输出一行：【可用性评分】|【记忆内容】——评分 1-10 表示这条信息 6 个月后"
            "仍有用的概率（10=稳定的用户偏好/环境事实，7-9=有用的项目事实或教训，"
            "6 以下=一次性任务结果/临时状态）；记忆内容 ≤120 字、第三人称陈述句、含具体事实，"
            "不要任何前后缀。\n"
            "如果没有，只输出两个字：无\n"
            "示例：8|用户的服务器 9950 是纯 IPv6 环境，SSH 连接需用 IPv6 地址\n\n"
            f"【用户说】{text.get('user', '')[:800]}\n\n"
            f"【助手最终答复】{text.get('answer', '')[:800]}"
        )
        out = _llm_complete(model, [{"role": "user", "content": prompt}])
        out = (out or "").strip()
        if not out or out in ("无", "无。", "没有", "没有。"):
            return None
        out = out.splitlines()[0].strip().lstrip("-• ").strip()
        # v1.0.328 评分过滤：LLM 自评可用性，<7 分的垃圾直接丢
        m = re.match(r"^(\d{1,2})\s*[分|｜|]\s*(.+)$", out) or re.match(r"^(\d{1,2})\s*\|\s*(.+)$", out)
        if m:
            score = int(m.group(1))
            if score < 7:
                log_event(f"[memory] 低分记忆丢弃（{score} 分）：{m.group(2)[:50]}")
                return None
            out = m.group(2).strip()
        if len(out) < 8 or len(out) > 160:
            return None
        return f"({score if m else 7}分) {out}" if m else out
    except Exception as e:  # noqa: BLE001
        log_event(f"[memory] 提炼失败（忽略）：{e}")
        return None


def cap_lines(lines):
    """条目上限裁剪（v1.0.328 拆出供单测）：超 MAX_LINES 裁最旧。"""
    if len(lines) > MAX_LINES:
        return lines[-MAX_LINES:]
    return lines



def _append_memory(line):
    """追加一行记忆（带时间戳）；去重（已有行包含新行核心内容则跳过）；超上限裁最旧。"""
    p = memory_path()
    with _mem_lock:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        import datetime
        stamp = datetime.datetime.now().strftime("%m-%d %H:%M")
        new = f"- [{stamp}] {line}"
        old_lines = []
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as _mf:   # v1.0.652：with 收口句柄（原裸 open 依赖 GC）
                old_lines = [l for l in _mf.read().splitlines() if l.strip()]
        # 去重（v1.0.328 增强）：①包含匹配（原有）②2-gram Jaccard ≥0.55——
        # 同义不同词的重复记忆也能拦（如"服务器是 IPv6"vs"机器仅支持 IPv6"）
        core = re.sub(r"^-\s*\[[^\]]+\]\s*", "", new)
        def _ngrams(s):
            s = re.sub(r"\((\d+)分\)\s*", "", s)
            s = re.sub(r"\d+", "", s)  # 数字不参与相似度（编号/日期差异不代表语义差异）
            s = re.sub(r"[\s，。、；：！？\-—\[\]()（）']", "", s)
            return {s[i:i+2] for i in range(len(s) - 1)} if len(s) > 1 else {s}
        new_ng = _ngrams(core)
        for l in old_lines:
            oc = re.sub(r"^-\s*\[[^\]]+\]\s*", "", l)
            if oc == core or (len(core) >= 20 and core[:20] in oc):
                return False
            if new_ng:
                oc_ng = _ngrams(oc)
                jac = len(new_ng & oc_ng) / max(1, len(new_ng | oc_ng))
                if jac >= 0.55:
                    return False
        old_lines.append(new)
        old_lines = cap_lines(old_lines)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(old_lines) + "\n")
        os.replace(tmp, p)
        return True


def remember_async(user_text, answer_text, cfg):
    """对话完成后调用：后台线程提炼并落盘。永不抛异常、不阻塞调用方。"""
    global _inflight
    if not (cfg.get("feature_flags") or {}).get("memory", True):
        return
    if not (user_text or "").strip() or not (answer_text or "").strip():
        return
    if _inflight >= 2:          # 积压保护：丢弃本次（对话密集时不堆积 LLM 调用）
        return
    with _inflight_lock:        # v1.0.364（M11）：check-and-set 原子化
        if _inflight >= 2:
            return
        _inflight += 1

    def _work():
        global _inflight
        try:
            line = _extract_one_memory({"user": user_text, "answer": answer_text}, cfg)
            if line and _append_memory(line):
                log_event(f"[memory] 已沉淀：{line[:60]}")
        finally:
            _inflight -= 1

    threading.Thread(target=_work, daemon=True, name="memory-distill").start()


def clear_memory():
    """清空记忆文件（设置页按钮）。返回删除的行数。"""
    p = memory_path()
    with _mem_lock:
        if not os.path.isfile(p):
            return 0
        n = 0
        with open(p, encoding="utf-8") as _mf:   # v1.0.652：with 收口句柄
            n = len([l for l in _mf.read().splitlines() if l.strip()])
        os.remove(p)
        return n


def summarize_conversation(conv_data):
    """会话级经验总结（v1.0.327 需求⑦）：归档对话时把整段对话交给 LLM 提炼
    有用的经验/事实/教训（≤3 条），逐条沉淀到 MEMORY.md。返回总结说明文本。"""
    try:
        from llm_client import resolve_model, _llm_complete
        from core.config_store import load_config
        cfg = load_config()
        if not (cfg.get("feature_flags") or {}).get("memory", True):
            return ""
        model = resolve_model(cfg)
        if not model:
            return ""
        # 压缩对话为可读文本（截断防上下文爆炸）
        parts = []
        for m in (conv_data.get("messages") or [])[-40:]:
            role = "用户" if m.get("role") == "user" else "助手"
            parts.append(f"{role}：{str(m.get('content', ''))[:600]}")
        if not parts:
            return ""
        prompt = (
            "下面是一段完整的 agent 工作对话。请提炼其中值得【长期记住】的经验、事实、"
            "教训或工作方法（用户偏好、项目事实、操作流程中的有效做法、踩坑教训），"
            "最多 3 条，每条一行，格式「评分|内容」——评分 1-10 表示 6 个月后仍有用的概率，"
            "只保留 ≥7 分的；内容不超过 120 字、第三人称、只留可复用经验。\n"
            "如果整段对话都是一次性琐碎操作、没有任何可复用价值，只输出两个字：无\n\n"
            "【对话全文】\n" + "\n".join(parts)[:8000]
        )
        out = (_llm_complete(model, [{"role": "user", "content": prompt}]) or "").strip()
        if not out or out in ("无", "无。", "没有", "没有。"):
            return ""
        # v1.0.328：每条带评分过滤（prompt 要求"评分|内容"格式），<7 分丢弃
        saved = 0
        title = (conv_data.get("title") or "").strip()
        for l in out.splitlines()[:6]:
            l = l.strip().lstrip("-• ").strip()
            if not l or l == "无":
                continue
            m = re.match(r"^(\d{1,2})\s*[分|｜|]\s*(.+)$", l) or re.match(r"^(\d{1,2})\s*\|\s*(.+)$", l)
            score, content = (int(m.group(1)), m.group(2).strip()) if m else (7, l)
            if score < 7 or len(content) < 8:
                continue
            tag = f"（来自对话「{title[:16]}」的总结）" if title else "（对话总结）"
            _append_memory(f"({score}分) {content[:140]} {tag}")
            saved += 1
        log_event(f"[memory] 会话总结沉淀 {saved} 条（对话「{title[:20]}」）")
        return str(saved) + " 条" if saved else ""
    except Exception as e:  # noqa: BLE001
        log_event(f"[memory] 会话总结异常：{e}")
        return ""

"""agent.messages — 消息压缩：字符统计 / 高价值摘要 / 中间工具往返压缩。
逐字自 backend.py 迁出（T04）。"""

import json

from core_utils import log_event
from core.result import err_msg   # v1.0.655 B5
from .session import CONTEXT_COMPRESS_THRESHOLD, KEEP_LEADING_MSGS, KEEP_RECENT_MSGS

def _msg_chars(m):
    """统计单条消息的上下文字符数：content + tool_calls 中 arguments 的字符数。
    工具参数可能含大 JSON，只统计 content 会低估真实上下文长度导致压缩阈值失效。"""
    n = len(str(m.get("content") or ""))
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        n += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
    return n


def _high_value_summary(name, obj):
    """对高价值工具结果做结构化摘要（用于上下文压缩时保留关键判定与证据，防失真）。
    命中返回摘要串；非高价值工具/结构不符返回 None（由通用截断兜底，不影响压缩流程）。"""
    if not isinstance(obj, dict):
        return None
    err = err_msg(obj)   # v1.0.655 B5 单出口
    if err:
        return f"{name}: 失败 {str(err)[:80]}"
    if name == "read_wecom_fields":
        fields = obj.get("fields")
        if isinstance(fields, list):
            parts = []
            wait = "（已等待加载）" if obj.get("waited") else ""
            labels = [str(x) for x in (obj.get("all_labels") or [])[:6]]
            for f in fields:
                if not isinstance(f, dict):
                    continue
                key = f.get("key", "")
                cnt = f.get("count", 0)
                # P0-2 修复：保留 snippets 实际内容，压缩后审核仍需精确字段值
                snippets = f.get("snippets") or []
                if cnt > 0 and snippets:
                    vals = " | ".join(s[:80] for s in snippets[:2])
                    parts.append(f"{key}={cnt}次【{vals}】")
                elif cnt > 0:
                    parts.append(f"{key}={cnt}次")
                else:
                    parts.append(f"{key}=0（未找到）")
            s = "字段命中 " + ("、".join(parts) if parts else "全部未命中") + wait
            if labels:
                s += "；页面字段: " + "、".join(labels)
            return f"read_wecom_fields: {s}"
    if name == "run_rule_check":
        if obj.get("result") is not None:
            parts = [f"判定={obj.get('result')}"]
            if obj.get("verdict"):
                parts.append(f"结论={str(obj['verdict'])[:80]}")
            hits = obj.get("hits")
            if isinstance(hits, list) and hits:
                parts.append("命中=" + "、".join(str(h) for h in hits[:6]))
            fails = obj.get("fails")
            if isinstance(fails, list) and fails:
                parts.append(f"不符项={len(fails)}")
            return "run_rule_check: " + "；".join(parts)
    if name in ("read_wecom_page", "capture_wecom_window"):
        title = (str(obj.get("title") or ""))[:30]
        bl = obj.get("body_len")
        if bl is None and isinstance(obj.get("body"), str):
            bl = len(obj["body"])
        struct_n = len(obj.get("struct") or []) if isinstance(obj.get("struct"), list) else 0
        s = f"窗口「{title}」正文 {bl} 字符"
        if struct_n:
            s += f"，可交互元素 {struct_n} 个"
        return f"{name}: {s}"
    if name == "read_workspace_file":
        p = obj.get("path") or obj.get("filename") or ""
        cc = obj.get("char_count") or 0
        if p:
            return f"read_workspace_file: 文件 {p}（{cc} 字符）"
    return None


def _compress_loop_msgs(msgs, threshold=CONTEXT_COMPRESS_THRESHOLD,
                        keep_leading=KEEP_LEADING_MSGS, keep_recent=KEEP_RECENT_MSGS,
                        prev_total=None):
    """上下文压缩：总字符数超阈值时，保留最前原始指令 + 最近 N 条消息原文，
    中间的工具往返历史折叠为一条 user 摘要消息。注意对齐轮次边界，
    保证 assistant 的 tool_calls 与其后 tool 结果消息保持成对完整。
    返回 (new_msgs, total)；prev_total 为调用方维护的增量字符计数，
    未超阈值时直接返回，避免每轮对全量消息做 O(n) 求和扫描（P3-8）。"""
    if not msgs:
        return msgs, 0
    if prev_total is not None:
        # 调用方增量计数未超阈值 → 无需压缩，直接返回（P3-8：省掉每轮全量求和）
        if prev_total <= threshold:
            return msgs, prev_total
    total = sum(_msg_chars(m) for m in msgs)
    if total <= threshold:
        return msgs, total

    # 1) 保留最前面的原始指令（连续 system/user 消息）
    leading = []
    for m in msgs:
        if m.get("role") in ("system", "user"):
            leading.append(m)
            if len(leading) >= keep_leading:
                break
        else:
            break
    # 保底：极端情况首条非 system/user 时，至少保留首条原始消息，避免指令丢失（P3-3）
    if not leading:
        leading = msgs[:1]

    # 2) 保留最近 keep_recent 条原文，并向前对齐到非 tool 消息，
    #    避免 recent 头部是 tool 时缺失其 assistant tool_calls 配对。
    recent = list(msgs[-keep_recent:])
    while recent and recent[0].get("role") == "tool":
        idx = len(msgs) - len(recent) - 1
        if idx < len(leading):
            break
        recent.insert(0, msgs[idx])
    # v1.0.652（审计 P1-3 对齐）：walk-back 越过 leading 边界时头部可能仍是孤儿
    # tool（其 assistant(tool_calls) 落在被丢弃的中段）——统一为"丢弃孤儿"语义
    # （与 loop_ops._align_tool_pairing 一致），绝不拼出无配对 tool 头（严格后端 400）。
    while recent and recent[0].get("role") == "tool":
        recent.pop(0)

    start = len(leading)
    end = len(msgs) - len(recent)
    if end <= start:
        # 中间无可压缩消息但上下文仍超阈值 → 对大 tool 结果直接截断（v1.0.205）
        # 场景：leading(6) + recent(20) 已占满全部消息，read_workspace_file 等
        # 超大 tool 结果在 recent 里，不截断则上下文只增不减（实测 28→28 没减量）
        if total > threshold:
            shaved = _shave_tool_results(msgs, threshold)   # v1.0.344：只算一次（原式重复计算两遍）
            return shaved, sum(_msg_chars(m) for m in shaved)
        return msgs, total

    # 3) 中间部分折叠为摘要（提取用户消息 + 已执行工具名 + 高价值工具结果结构化保留 + 最近一步结果）
    middle = msgs[start:end]
    user_lines, tool_lines = [], []
    # call_id -> 工具名 映射：tool 结果消息只带 tool_call_id，需关联工具名才能按类型提取结构化信息
    call_name = {}
    for m in middle:
        if m.get("role") == "user":
            t = str(m.get("content") or "").strip()
            if t and t not in user_lines:
                user_lines.append(t)
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = (tc.get("function") or {}).get("name") or "?"
                if fn not in tool_lines:
                    tool_lines.append(fn)
                cid = tc.get("id")
                if cid:
                    call_name[cid] = fn
    # P3-4 增强：高价值工具（字段读取/规则判定/页面抓取）结果做结构化提取而非整体截断，
    # 压缩后模型仍能拿到关键判定与证据，避免失真导致续跑重复或误判。
    hv_parts = []
    for m in middle:
        if m.get("role") != "tool":
            continue
        fn = call_name.get(m.get("tool_call_id") or "", "")
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        try:
            obj = json.loads(content)
        except Exception:  # noqa: BLE001
            continue
        part = _high_value_summary(fn, obj)
        if part and part not in hv_parts:
            hv_parts.append(part)
    # P3-4：提取最近一步工具结果的关键片段，压缩后模型仍能感知重要中间产出
    last_tool_result = ""
    for m in reversed(middle):
        if m.get("role") == "tool" and m.get("content"):
            last_tool_result = str(m["content"]).strip()
            break
    summary_parts = []
    if user_lines:
        summary_parts.append("期间用户/系统消息摘要：" + "；".join(u[:60] for u in user_lines[:5]))
    if tool_lines:
        summary_parts.append("期间已执行工具：" + "、".join(tool_lines))
    if hv_parts:
        summary_parts.append("关键结果：" + "；".join(hv_parts))
    if last_tool_result:
        summary_parts.append("最近一步结果：" + last_tool_result[:100])
    summary = "；".join(summary_parts) or "（无）"
    if len(summary) > 1500:
        summary = summary[:1500] + "…"
    summary_msg = {
        "role": "user",
        "content": f"[系统] 为控制上下文长度，中间的 {len(middle)} 条消息已压缩。{summary}。"
                   "请基于保留的原始指令与最近对话继续推进任务，不要重复已完成步骤。",
    }
    new_msgs = leading + [summary_msg] + recent
    # v1.0.205：压缩后仍超阈值 → 二次截断（read_workspace_file 等单条可达 20K）
    # v1.0.588：同一份 new_msgs 的字符总数只算一遍——_msg_chars 会遍历每条消息的
    # content + 全部 tool_calls.arguments，原实现在未触发二次截断时把同一个列表
    # 连算两次（压缩路径每条消息被完整字符串化 2-3 次），长任务里纯属白耗。
    _new_chars = sum(_msg_chars(m) for m in new_msgs)
    if _new_chars > threshold:
        new_msgs = _shave_tool_results(new_msgs, threshold)
        _new_chars = sum(_msg_chars(m) for m in new_msgs)
    log_event(f"[agent] 上下文压缩：{len(msgs)} 条 → {len(new_msgs)} 条（{total} 字符 > 阈值 {threshold}）")
    return new_msgs, _new_chars


def _shave_tool_results(msgs, threshold, cap=4000):
    """v1.0.205：把超长 tool 结果截断到 cap 字符（保留头部，标注原文长度）。
    只动 role=tool 消息，assistant/user/system 保持原样。"""
    _shaved = []
    for _m in msgs:
        if _m.get("role") == "tool":
            _c = str(_m.get("content") or "")
            if len(_c) > cap:
                _m2 = dict(_m)
                _m2["content"] = _c[:cap] + f"…（已截断，原 {len(_c)} 字符，详见日志）"
                _shaved.append(_m2)
                continue
        _shaved.append(_m)
    return _shaved


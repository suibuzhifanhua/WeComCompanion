# -*- coding: utf-8 -*-
"""model_bench — 模型综合评测（v1.0.319）：7 项常用能力打分制
=================================================================
覆盖平时用得到的方面：基础响应/指令遵循(JSON)/中文生成/逻辑推理/
工具调用意图(JSON)/长文要点提取/响应速度。每项独立 prompt+确定性校验，
0-100 分，加权总分 100。本地小模型友好（题目不难，区分度来自基本能力）。
某项失败/超时不影响后续项。生成器逐项 yield 进度（SSE 友好）。
"""
import json
import re
import time

ITEMS_DEF = [
    # (key, 名称, 权重, 超时s)
    ("resp", "基础响应", 5, 30),
    ("instr", "指令遵循", 20, 30),
    ("chinese", "中文生成", 10, 30),
    ("logic", "逻辑推理", 15, 40),
    ("toolcall", "工具调用意图", 25, 40),
    ("extract", "长文要点提取", 15, 40),
    ("speed", "响应速度", 10, 30),
]

_NOTIFY = (
    "各部门请注意：定于本周五（9 月 20 日）下午 14:30 在三楼大会议室召开三季度经营分析会，"
    "请各部门负责人提前准备汇报材料，会议预计两小时，结束后将安排季度优秀员工颁奖环节，"
    "如有特殊情况不能参会，请提前向综合办公室王主任报备，联系电话 8001。"
)

_PROMPTS = {
    "resp": "请回复两个字：好的",
    "instr": '只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。格式：{"status": "ok", "n": 3}',
    "chinese": '用一句话介绍什么是"数据备份"。要求：必须以「备份是」三个字开头，不超过 60 个字。',
    "logic": "小明有 12 个苹果，给了小红 5 个，后来又买了 8 个。请问小明现在有几个苹果？只回答一个数字，不要其他文字。",
    "toolcall": (
        '你可以调用一个工具：send_message(to=收件人姓名, content=消息内容)。\n'
        '现在我要给张三发消息内容是"明天上午开会"，请输出调用这个工具的 JSON（格式：'
        '{"name": "send_message", "arguments": {...}}），只输出 JSON。'
    ),
    "extract": f"阅读以下通知后回答问题：会议时间是几点？只回答时间。\n\n{_NOTIFY}",
}
_EXTRACT_HINT = "14:30"


def _ask(model, prompt, timeout):
    """单次调用（与 /models/test 同款协议处理）。返回 (text, cost_ms) 或 (None, cost_ms)。"""
    import requests as _rq
    from llm_client import _chat_url, _apply_reasoning_params
    url = _chat_url(model)
    proto = model.get("protocol") or "openai"
    if proto == "anthropic":
        headers = {"x-api-key": model.get("api_key", ""), "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        payload = {"model": model.get("model"), "messages": [{"role": "user", "content": prompt}],
                   "stream": False, "max_tokens": 512}
    else:
        headers = {"Authorization": f"Bearer {model.get('api_key', '')}", "Content-Type": "application/json"}
        payload = {"model": model.get("model"), "messages": [{"role": "user", "content": prompt}],
                   "stream": False, "max_tokens": 512}
    try:
        _apply_reasoning_params(payload, model)
    except Exception:  # noqa: BLE001
        pass
    t0 = time.time()
    try:
        r = _rq.post(url, headers=headers, json=payload, timeout=timeout)
        cost = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            return None, cost
        jr = r.json()
        if proto == "anthropic":
            text = ((jr.get("content") or [{}])[0].get("text") or "")
        else:
            text = (jr.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
        return text.strip(), cost
    except Exception:  # noqa: BLE001
        return None, int((time.time() - t0) * 1000)


def _extract_json(text):
    """宽松提取首个 JSON 对象（容忍 ```json 包裹/前后废话）。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def _score_item(key, text, cost_ms):
    """校验一项 → (score 0-100, 说明)。"""
    if text is None:
        return 0, "调用失败或超时"
    t = (text or "").strip()
    if not t:
        return 0, "空响应"
    if key == "resp":
        return (100, "正常响应") if len(t) >= 1 else (0, "空响应")
    if key == "instr":
        j = _extract_json(t)
        if isinstance(j, dict) and j.get("status") == "ok" and j.get("n") == 3:
            extra = len(re.sub(r"[{}\[\]\s]", "", t)) > 40
            return (100 if not extra else 80), ("严格遵循格式" if not extra else "JSON 正确但夹带多余文字")
        return 20, "未能输出符合要求的 JSON"
    if key == "chinese":
        ok_start = t.startswith("备份是")
        ok_len = len(t) <= 80
        has_cn = bool(re.search(r"[\u4e00-\u9fff]", t))
        if ok_start and ok_len and has_cn:
            return 100, "格式与内容符合"
        score = (40 if has_cn else 0) + (30 if ok_start else 0) + (30 if ok_len else 0)
        return score, f"开头{'' if ok_start else '未'}按要求/长度{'' if ok_len else '超'}/{'中文' if has_cn else '非中文'}"
    if key == "logic":
        return (100, "答案正确") if "15" in t else (0, f"答案错误（期望 15，答：{t[:30]}）")
    if key == "toolcall":
        j = _extract_json(t)
        if not isinstance(j, dict):
            return 20, "未输出 JSON"
        args = j.get("arguments") or j.get("parameters") or {}
        name_ok = "send_message" in str(j.get("name", ""))
        to_ok = "张三" in str(args.get("to", ""))
        ct_ok = "开会" in str(args.get("content", "")) or "明天" in str(args.get("content", ""))
        got = (40 if name_ok else 0) + (30 if to_ok else 0) + (30 if ct_ok else 0)
        detail = f"函数名{'✓' if name_ok else '✗'} 参数 to{'✓' if to_ok else '✗'} content{'✓' if ct_ok else '✗'}"
        return got, detail
    if key == "extract":
        return (100, "正确提取关键信息") if _EXTRACT_HINT in t or "14：30" in t else (0, f"未找到时间（答：{t[:30]}）")
    if key == "speed":
        # 首项耗时在 run_benchmark 里换算，这里不直接判
        return 0, "（由总耗时换算）"
    return 0, "未知项"


def _speed_score(cost_ms):
    """响应速度：≤1.5s=100，线性递减到 15s=0。"""
    if cost_ms <= 1500:
        return 100
    if cost_ms >= 15000:
        return 0
    return int(100 - (cost_ms - 1500) / 135)


def run_benchmark(model):
    """生成器：逐项 yield 进度事件 dict，最后 yield {"type":"bench_done", "total":…, "items":[…]}。"""
    name = model.get("name") or model.get("model") or "?"
    yield {"type": "bench_start", "model": name, "total_items": len(ITEMS_DEF)}
    items = []
    speed_cost = None
    for key, label, weight, timeout in ITEMS_DEF:
        t0 = time.time()
        prompt = _PROMPTS.get(key) or _PROMPTS["resp"]   # speed 无独立题目：重发基础请求取耗时
        text, cost_ms = _ask(model, prompt, timeout)
        if key == "resp":
            speed_cost = cost_ms
        score, detail = _score_item(key, text, cost_ms)
        if key == "speed":
            score = _speed_score(speed_cost or cost_ms)
            detail = f"耗时 {speed_cost or cost_ms}ms"
        items.append({"key": key, "name": label, "weight": weight, "score": score,
                      "cost_ms": cost_ms, "detail": detail})
        yield {"type": "bench_item", "key": key, "name": label, "score": score,
               "detail": detail, "cost_ms": cost_ms, "elapsed": int((time.time() - t0) * 1000)}
    total = sum(it["score"] * it["weight"] for it in items) / sum(it["weight"] for it in items)
    summary = {
        "type": "bench_done", "total": round(total), "model": name, "items": items,
        "grade": ("优秀" if total >= 85 else "良好" if total >= 70 else "可用" if total >= 50 else "较差"),
    }
    yield summary

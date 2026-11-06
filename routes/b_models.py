# -*- coding: utf-8 -*-
"""routes.b_models — 模型管理 API（列表/评测/扫描/导入导出）
"""
# -*- coding: utf-8 -*-
import json
import re
import time
import uuid

from flask import Blueprint, request, Response, jsonify, stream_with_context

from core_utils import _lazy_requests as requests
from core_utils import log_event, mask_key, workspace_root
from core.config_store import load_config, update_config, _save_config_safe  # noqa: F401（_save_config_safe 兼容旧导入）
from core.strings import clean_url, strip_invisible as clean_text   # v1.0.621：零宽字符/BOM 清洗单点


class _ModelApplyError(Exception):
    """v1.0.555（P0-10）：update_config 事务内 _apply_models 校验失败——调用方转 400。"""
from llm_client import resolve_model, _probe_local_models, _LOCAL_MODEL_ENDPOINTS

from version import get as _version_get  # v1.0.510（P2-13）：版本兜底块收敛到 version.get
VERSION = _version_get()


bp = Blueprint("mgmt_models", __name__)

# v1.0.407 修复：v1.0.402 拆分 management→b_models 时 GET 装饰器遗失（POST /models 在，
# GET 404）——前端 loadModels 拿不到列表，表现为「模型添加不了/列表加载失败」。
# 同期 b_admin.api_experiences_list 同款丢失，一并修复（AST 扫描 routes/ 全量确认无第三处）。
@bp.route("/models", methods=["GET"])
def get_models():
    cfg = load_config()
    return jsonify([mask_models(m) for m in cfg.get("models", [])])


@bp.route("/models/bench7", methods=["POST"])
def api_models_bench7():
    """模型综合评测（v1.0.319）：7 项常用能力打分制，SSE 逐项推送进度。
    body: {model_id 或 model(参数名)}。精确匹配，找不到返回 404（不静默测默认模型）。
    与 v1.0.270 的 /models/benchmark（速度+质量多模型对比）互补：本端点是单模型细粒度打分。"""
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    mid = str(data.get("model_id") or "")
    mparam = str(data.get("model") or "")
    model = next((m for m in cfg.get("models", []) if m.get("id") == mid), None) \
        or next((m for m in cfg.get("models", []) if m.get("model") == mparam), None)
    # v1.0.364 审查修复（M6）：删除第三个重复 next()（与第一个条件完全相同的死代码）
    if not model:
        return jsonify({"ok": False, "error": "未找到该模型（可能未保存成功）"}), 404
    from services.model_bench import run_benchmark

    def gen():
        # v1.0.342：客户端「⏹ 停止」断开 SSE 后，werkzeug 在下一次 yield 处对生成器
        # 抛 GeneratorExit——在途那一项 LLM 调用跑完后即中止，不再继续后续项。
        try:
            for ev in run_benchmark(model):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield "data: {\"type\": \"bench_end\"}\n\n"
        except GeneratorExit:
            log_event("[bench] 客户端断开，综合评测中止")
            raise

    return Response(stream_with_context(gen()),
                    mimetype="text/event-stream; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/models/test", methods=["POST"])
def api_models_test():
    """真实验证一个模型是否可用（非流式单次调用）。"""
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    model = resolve_model(cfg, data.get("model_id")) if data.get("model_id") else resolve_model(cfg)
    if not model:
        return jsonify({"ok": False, "error": "未配置模型"})
    from llm_client import _chat_url, build_headers
    url = _chat_url(model)
    headers = build_headers(model)   # v1.0.355：请求头构造收敛到 llm_client
    payload = {"model": model.get("model"), "messages": [{"role": "user", "content": "ping"}],
               "stream": False, "max_tokens": 8}
    # 推理参数注入（思考强度/开关/预算/自定义扩展/max_tokens），测试时真实生效
    try:
        from llm_client import _apply_reasoning_params
        _apply_reasoning_params(payload, model)
    except Exception:  # noqa: BLE001
        pass
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            try:
                jr = r.json()
                got = (jr.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()[:40]
            except Exception as e:  # noqa: BLE001
                log_event(f"[model-test] 解析模型测试响应失败：{e}")
                got = ""
            return jsonify({"ok": True, "model": model.get("name"), "reply": got})
        return jsonify({"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)})


def _bench_one_model(model):
    """单模型完整跑分（v1.0.270）：①流式速度(TTFT/tok/s) ②3 道质量题 ③工具调用能力。
    返回 {"model": 名称, "speed": {...}, "quality": {...}, "tool": {...}, "elapsed_s": 总耗时}
    或 {"model": 名称, "error": 原因, ...}（速度测试硬性失败时）。"""
    from llm_client import _chat_url, _apply_reasoning_params, build_headers
    t_start = time.time()
    url = _chat_url(model)
    headers = build_headers(model)   # v1.0.355：请求头构造收敛到 llm_client
    proto = model.get("protocol") or "openai"   # 后续分支仍需协议判断

    def _payload(messages, stream):
        p = {"model": model.get("model"), "messages": messages, "stream": stream, "max_tokens": 512}
        if stream and proto != "anthropic":
            p["stream_options"] = {"include_usage": True}   # v1.0.254：流式要拿到 usage
        try:
            _apply_reasoning_params(p, model)
        except Exception:  # noqa: BLE001
            pass
        return p

    def _fail(err):
        return {"model": model.get("name"), "error": err, "elapsed_s": round(time.time() - t_start, 1)}

    # ---------- ① 速度测试（流式；anthropic 协议退化为非流式） ----------
    speed = {}
    try:
        t0 = time.time()
        ttft = None
        last = t0
        comp_tokens = 0
        chunk_n = 0
        if proto == "anthropic":
            r = requests.post(url, headers=headers,
                              json=_payload([{"role": "user", "content": "请用三句话介绍一下你自己的能力特点。"}], False),
                              timeout=90)
            if r.status_code != 200:
                return _fail(f"HTTP {r.status_code}: {r.text[:200]}")
            jr = r.json()
            got = (jr.get("content", [{}])[0].get("text", "") if proto == "anthropic"
                   else jr.get("choices", [{}])[0].get("message", {}).get("content", "")) or ""
            total = time.time() - t0
            u = jr.get("usage") or {}
            comp_tokens = u.get("completion_tokens") or 0
            speed = {"ttft_ms": None, "total_s": round(total, 2), "completion_tokens": comp_tokens,
                     "tokens_per_s": round(comp_tokens / total, 1) if comp_tokens else None, "reply": got[:40]}
        else:
            r = requests.post(url, headers=headers,
                              json=_payload([{"role": "user", "content": "请用三句话介绍一下你自己的能力特点。"}], True),
                              stream=True, timeout=90)
            if r.status_code == 400:
                # stream_options 兼容兜底：去掉重试（v1.0.254 同款降级）
                p2 = _payload([{"role": "user", "content": "请用三句话介绍一下你自己的能力特点。"}], True)
                p2.pop("stream_options", None)
                r = requests.post(url, headers=headers, json=p2, stream=True, timeout=90)
            if r.status_code != 200:
                return _fail(f"HTTP {r.status_code}: {r.text[:200]}")
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                try:
                    jr = json.loads(d)
                except Exception:  # noqa: BLE001
                    continue
                if jr.get("choices") and (jr["choices"][0].get("delta", {}) or {}).get("content"):
                    chunk_n += 1
                    if ttft is None:
                        ttft = time.time() - t0
                u = jr.get("usage")
                if u:
                    comp_tokens = u.get("completion_tokens") or comp_tokens
                last = time.time()
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass  # silent-ok: 探测连接关闭失败无所谓，进程退出即回收
            total = last - t0
            gen_t = max(0.001, last - (t0 + (ttft or 0)))
            est = comp_tokens or chunk_n  # usage 缺失时按 chunk 数估算（≈1 token/chunk）
            # 伪流式防护：部分网关把整段响应缓冲后一次性吐出（所有 chunk 同刻到达），
            # gen_t 趋近 0 → tok/s 天文数字无意义。生成窗口 <0.3s 时不给出速度。
            tps = round(est / gen_t, 1) if est and gen_t >= 0.3 else None
            speed = {"ttft_ms": round((ttft or total) * 1000), "total_s": round(total, 2),
                     "completion_tokens": comp_tokens or None,
                     "tokens_per_s": tps,
                     "pseudo_stream": gen_t < 0.3}
    except Exception as e:  # noqa: BLE001
        return _fail(f"速度测试失败: {e}")

    # ---------- ② 质量测试（3 道固定题，非流式） ----------
    quiz = [
        {"q": "计算 127 × 89 等于多少？只回答数字。", "expect": "11303"},
        {"q": "小明比小红高，小红比小刚高，谁最矮？只回答名字。", "expect": "小刚"},
        {"q": "中国的首都是哪座城市？只回答城市名。", "expect": "北京"},
    ]
    details = []
    passed = 0
    for item in quiz:
        try:
            r = requests.post(url, headers=headers, json=_payload([{"role": "user", "content": item["q"]}], False),
                              timeout=60)
            got = ""
            if r.status_code == 200:
                jr = r.json()
                if proto == "anthropic":
                    got = (jr.get("content", [{}])[0].get("text", "") or "")
                else:
                    got = (jr.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
                got = got.strip()
            ok = item["expect"] in got
            passed += 1 if ok else 0
            details.append({"q": item["q"][:24], "expect": item["expect"], "got": got[:60] or f"HTTP {r.status_code}", "ok": ok})
        except Exception as e:  # noqa: BLE001
            details.append({"q": item["q"][:24], "expect": item["expect"], "got": f"请求失败: {str(e)[:40]}", "ok": False})

    # ---------- ③ 工具调用能力（v1.0.270：固定 1 道 get_weather 函数调用题） ----------
    params_schema = {"type": "object",
                     "properties": {"city": {"type": "string", "description": "城市名，如：北京"}},
                     "required": ["city"]}
    tool_def = ({"name": "get_weather", "description": "查询指定城市的当前天气", "input_schema": params_schema}
                if proto == "anthropic" else
                {"type": "function", "function": {"name": "get_weather",
                                                  "description": "查询指定城市的当前天气",
                                                  "parameters": params_schema}})
    tool = {"passed": False, "latency_s": None, "detail": "未执行"}
    try:
        p = _payload([{"role": "user", "content": "北京今天天气怎么样？请调用天气工具查询。"}], False)
        p["tools"] = [tool_def]
        p["max_tokens"] = 256
        t0 = time.time()
        r = requests.post(url, headers=headers, json=p, timeout=90)
        tool["latency_s"] = round(time.time() - t0, 2)
        if r.status_code == 400:
            tool["detail"] = "网关/模型不支持 tools 字段（HTTP 400）"
        elif r.status_code == 200:
            jr = r.json()
            if proto == "anthropic":
                blocks = jr.get("content") or []
                tu = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"
                           and b.get("name") == "get_weather"), None)
                if tu:
                    args_s = json.dumps(tu.get("input") or {}, ensure_ascii=False)
                    tool["passed"] = "北京" in args_s
                    tool["detail"] = ("get_weather" + str(tu.get("input") or {}))[:80] if tool["passed"] \
                        else f"调用了但参数错误: {args_s[:60]}"
                else:
                    txt = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                    tool["detail"] = ("未调用工具，直接回答: " + (txt or "")[:50]).strip()
            else:
                msg = (jr.get("choices") or [{}])[0].get("message", {}) or {}
                calls = msg.get("tool_calls") or []
                hit = next((c for c in calls if (c.get("function") or {}).get("name") == "get_weather"), None)
                if hit:
                    args_s = (hit.get("function") or {}).get("arguments") or ""
                    tool["passed"] = "北京" in args_s
                    tool["detail"] = ("get_weather(" + args_s[:60] + ")") if tool["passed"] \
                        else f"调用了但参数错误: {args_s[:60]}"
                elif calls:
                    tool["detail"] = "调用了错误工具: " + ",".join((c.get("function") or {}).get("name", "?") for c in calls)[:60]
                else:
                    tool["detail"] = ("未调用工具，直接回答: " + ((msg.get("content") or "").strip()[:50])).strip()
        else:
            tool["detail"] = f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:  # noqa: BLE001
        tool["detail"] = f"请求失败: {str(e)[:80]}"

    return {"model": model.get("name"), "speed": speed,
            "quality": {"passed": passed, "total": len(quiz), "details": details},
            "tool": tool, "elapsed_s": round(time.time() - t_start, 1)}


@bp.route("/models/benchmark", methods=["POST"])
def api_models_benchmark():
    """模型跑分 v1.0.270：单模型（速度+质量+工具调用）/ 多模型（逐个或并发）。
    body：{} 或 {"model_id"} → 单模型（缺省默认模型，返回旧版单结果结构）；
          {"model_ids": [...], "mode": "sequential"|"parallel"} → 多模型，返回 {"results": [...]}。
    单模型约 10-40s；多模型按 mode 串行/并发执行。"""
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    ids = data.get("model_ids") or []
    if not ids:
        model = resolve_model(cfg, data.get("model_id")) if data.get("model_id") else resolve_model(cfg)
        if not model:
            return jsonify({"ok": False, "error": "未配置模型"})
        r = _bench_one_model(model)
        return jsonify({"ok": not r.get("error"), "model": r["model"], "speed": r.get("speed"),
                        "quality": r.get("quality"), "tool": r.get("tool"),
                        "elapsed_s": r.get("elapsed_s"), "error": r.get("error")})
    # ---- 多模型模式 ----
    mode = data.get("mode") or "sequential"
    if mode not in ("sequential", "parallel"):
        mode = "sequential"
    models = [resolve_model(cfg, i) for i in ids]
    models = [m for m in models if m]
    if not models:
        return jsonify({"ok": False, "error": "没有有效模型"})
    t0 = time.time()
    if mode == "parallel":
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(models))) as ex:
            results = list(ex.map(_bench_one_model, models))
    else:
        results = [_bench_one_model(m) for m in models]
    log_event(f"[bench] 多模型跑分完成：{len(results)} 个（{mode}），总耗时 {round(time.time() - t0, 1)}s")
    return jsonify({"ok": True, "mode": mode, "total_s": round(time.time() - t0, 1), "results": results})


@bp.route("/models/scan", methods=["POST"])
def api_models_scan():
    """自动扫描本机 LM Studio / Ollama 本地模型 API，返回探测结果（含模型列表）。"""
    t0 = time.time()
    results = []
    reachable_by_provider = set()   # 同一 provider 只保留首个可达结果（127.0.0.1/localhost 去重）
    for provider, label, api_base, probes in _LOCAL_MODEL_ENDPOINTS:
        models, err = _probe_local_models(api_base, probes)
        if models is not None and provider in reachable_by_provider:
            continue
        if models is not None:
            reachable_by_provider.add(provider)
        results.append({
            "provider": provider, "label": label, "base_url": api_base,
            "reachable": models is not None, "models": models or [],
            "error": err or "",
        })
    return jsonify({"ok": True, "results": results,
                    "elapsed": round(time.time() - t0, 2)})


# jsonschema 可用性探测已迁移至 core_utils.py（v1.0.97）
# backend.py 通过 from core_utils import _valid_json_schema 使用


@bp.route("/models/probe", methods=["POST"])
def api_models_probe():
    """远程模型探测（v1.0.188）：POST {base_url, api_key} →
    调 GET {base}/models 列出该服务全部可用模型 + 按模型名启发式推断能力/思考强度档位。
    可选 probe_effort=true 时对首个模型真实试探 reasoning_effort 是否被接受。"""
    data = request.get_json(force=True, silent=True) or {}
    base_url = (data.get("base_url") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    from llm_client import probe_remote_models, probe_reasoning_effort
    t0 = time.time()
    models, err = probe_remote_models(base_url, api_key)
    if models is None:
        return jsonify({"ok": False, "error": err or "探测失败", "elapsed": round(time.time() - t0, 2)})
    # 可选：对首个模型真实试探 reasoning_effort（默认开启——只发一次 max_tokens=2 的小请求）
    effort_probe = None
    if models and data.get("probe_effort", True) is not False:
        effort_probe = probe_reasoning_effort(base_url, api_key, models[0]["id"])
    return jsonify({"ok": True, "models": models,
                    "effort_probe": effort_probe,
                    "elapsed": round(time.time() - t0, 2)})


@bp.route("/models", methods=["POST"])
def post_models():
    data = request.get_json(force=True, silent=True) or []
    if not isinstance(data, list):
        return jsonify({"ok": False, "error": "请求体必须是数组"}), 400
    # v1.0.555（P0-10）：update_config 事务——_apply_models 只做纯变换（读 cfg
    # 仅为拿旧 models 做合并基准），事务内用 fresh cfg 重算，杜绝并发覆盖。
    # v1.0.621：校验类错误此前一律变成 500「配置写入异常」——`update_config` 把所有异常
    # 都吞掉返回 False（core/config_store.py），所以 `except _ModelApplyError` 永远不触发。
    # 用户填错 base_url 看到的却是"配置写入异常（详见日志）"，等于把输入错误推给排障。
    # 现在把校验信息在事务里存一份带出来：仍然是"校验失败就不写盘"，但界面拿到人话。
    caught = []

    def _mutate(cfg):
        models, err = _apply_models(cfg, data)
        if err:
            caught.append(err)
            raise _ModelApplyError(err)
        cfg["models"] = models
        return True

    try:
        ok = update_config(_mutate)
    except _ModelApplyError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if caught:
        return jsonify({"ok": False, "error": caught[0]}), 400
    if not ok:
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    cfg2 = load_config()
    return jsonify([mask_models(m) for m in cfg2.get("models", [])])


def _apply_models(cfg, data):
    """校验并规范化模型列表（post_models 与 /models/import 导入共用）。
    返回 (models, err)：err 非 None 表示校验失败（此时 models 为 None）。"""
    old = {m.get("id"): m for m in cfg.get("models", [])}
    models = []
    for m in data:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ("m_" + uuid.uuid4().hex[:8])
        # v1.0.621（用户实测）：从别的软件/网页复制过来的地址常带 U+200B 之类零宽字符，
        # `str.strip()` 去不掉（它不算空白）→ 探测请求抛 InvalidSchema、别的却能正常用。
        # 写入侧统一清洗：base_url/api_key/model/name 都过一遍（key 带 BOM 更隐蔽——
        # 表现是 401，谁也想不到是复制带入的字符）。
        base_url = clean_url(m.get("base_url", ""))
        if base_url and not re.match(r"^https?://", base_url):
            return None, (f"模型「{m.get('name', mid)}」的 base_url 必须以 http:// 或 https:// 开头"
                          "（已剔除粘贴带入的零宽字符/BOM 后仍不是合法地址）")
        rec = {
            "id": mid,
            "name": clean_text(m.get("name", mid)),
            "base_url": base_url,
            "api_key": clean_text(m.get("api_key")) or old.get(mid, {}).get("api_key", ""),  # 空=保留旧 key
            "model": clean_text(m.get("model", "")),
            "enabled": bool(m.get("enabled", True)),
            "is_default": bool(m.get("is_default", False)),
            # 推理参数（可选，v1.0.186 新增）：
            #   reasoning_effort: "low"|"medium"|"high"|None（OpenAI o3/gpt-5 等）
            #   enable_thinking:  True|False|None（DeepSeek R1 / Qwen thinking 模式）
            #   thinking_budget:  int|None（Gemini thinking_config 思考预算 token）
            #   extra_params:     dict|None（任意自定义扩展参数，最灵活，合并进请求体）
            "reasoning_effort": m.get("reasoning_effort") if m.get("reasoning_effort") in ("low", "medium", "high") else None,
            "enable_thinking": m.get("enable_thinking") if m.get("enable_thinking") in (True, False) else None,
            "thinking_budget": m.get("thinking_budget") if isinstance(m.get("thinking_budget"), int) and 0 < m.get("thinking_budget") < 200000 else None,
            "extra_params": m.get("extra_params") if isinstance(m.get("extra_params"), dict) and m.get("extra_params") else None,
            # 模型能力与协议（v1.0.187 新增）：
            #   protocol:                "openai" | "anthropic" | "custom" — 决定 llm_client 请求格式
            #   caps:                    模型能力位：tool_call / vision / thinking / thinking_only /
            #                            can_disable_thinking / custom_protocol
            #                            (前端「高级配置」三列 checkbox 来源；前端可手动微调)
            #   thinking_supported_levels: ["low","medium","high","xhigh","max"]
            #                              模型支持哪些思考强度档位（多选 checkbox 来源）
            #   default_thinking_effort: "low"|"medium"|"high"|"xhigh"|"max"|None
            #                              默认思考强度（自动选项展示）
            #   max_input_tokens:        int|None — 上下文窗口 token 数（输入档位按钮来源）
            #   max_output_tokens:       int|None — 单次输出上限 token 数（输出档位按钮来源）
            "protocol": m.get("protocol") if m.get("protocol") in ("openai", "anthropic", "custom") else "openai",
            "caps": _norm_caps(m.get("caps")),
            "thinking_supported_levels": _norm_levels(m.get("thinking_supported_levels")),
            "default_thinking_effort": m.get("default_thinking_effort") if m.get("default_thinking_effort") in ("low", "medium", "high", "xhigh", "max") else None,
            "max_input_tokens": m.get("max_input_tokens") if isinstance(m.get("max_input_tokens"), int) and 0 < m.get("max_input_tokens") < 10000000 else None,
            "max_output_tokens": m.get("max_output_tokens") if isinstance(m.get("max_output_tokens"), int) and 0 < m.get("max_output_tokens") < 1000000 else None,
        }
        models.append(rec)
    # 默认模型唯一性（v1.0.188 修复）：多个 is_default=true 时只保留第一个启用的，
    # 其余全部清掉——旧逻辑只补默认不清理，前端编辑/新增勾「设为默认」后会出现多默认。
    enabled = [m for m in models if m["enabled"]]
    default_seen = False
    for m in models:
        if m["is_default"]:
            if not m["enabled"]:
                m["is_default"] = False  # 禁用模型不能当默认
            elif default_seen:
                m["is_default"] = False  # 重复默认 → 清掉，只留第一个
            else:
                default_seen = True
    if enabled and not any(m["is_default"] for m in models):
        enabled[0]["is_default"] = True
    return models, None


def _export_payload(cfg):
    """v1.0.355：模型导出 payload 收敛（/models/export 与 /export/save 原逐字重复）。"""
    from datetime import datetime
    return {
        "__wecom_companion_models__": True,
        "app_version": VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "judge_model_id": cfg.get("judge_model_id") or "",
        "models": cfg.get("models", []),
    }

@bp.route("/models/export", methods=["GET"])
def export_models():
    """模型配置一键导出（v1.0.238）：完整模型定义（含 api_key，本机自用备份）+ 判断大模型。"""
    cfg = load_config()
    payload = _export_payload(cfg)
    return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                    mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=wecom_companion_models.json"})


@bp.route("/models/export/save", methods=["POST"])
def export_models_save():
    """导出配置落盘（v1.0.270 修复）：pywebview 下前端 blob+a.download 点击不触发下载
    （只弹 toast 没有文件）→ 改为后端直接把 JSON 写到 产物库/产出/，返回文件路径。"""
    from datetime import datetime
    import os
    cfg = load_config()
    payload = _export_payload(cfg)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        out_dir = os.path.join(workspace_root(), "产出")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"模型配置备份_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"写入失败：{e}"}), 500
    log_event(f"[config] 模型配置已导出到 {path}（{len(payload['models'])} 个模型）")
    return jsonify({"ok": True, "path": path, "count": len(payload["models"])})


@bp.route("/models/import", methods=["POST"])
def import_models():
    """模型配置一键导入（v1.0.238）：接受 /models/export 的导出文件，整体覆盖 models + judge_model_id。"""
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict) or data.get("__wecom_companion_models__") is not True:
        return jsonify({"ok": False, "error": "不是有效的伴侣模型配置导出文件（缺 __wecom_companion_models__ 标记）"}), 400
    mlist = data.get("models")
    if not isinstance(mlist, list) or not mlist:
        return jsonify({"ok": False, "error": "导出文件中没有模型"}), 400
    # v1.0.555（P0-10）：update_config 事务（同上）
    imported, chosen_jid = [], ""

    def _mutate(cfg):
        nonlocal imported, chosen_jid
        models, err = _apply_models(cfg, mlist)
        if err:
            raise _ModelApplyError(err)
        cfg["models"] = models
        # 判断大模型：必须在导入后的启用模型中，否则静默置空（避免指向不存在的 id）
        jid = data.get("judge_model_id") or ""
        if jid and not any(m["id"] == jid and m["enabled"] for m in models):
            jid = ""
        cfg["judge_model_id"] = jid
        imported, chosen_jid = models, jid
        return True

    try:
        ok = update_config(_mutate)
    except _ModelApplyError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not ok:
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    log_event("[config] 模型配置导入完成（update_config 事务）")
    return jsonify({"ok": True, "count": len(imported), "judge_model_id": chosen_jid})

def mask_models(m):
    m = dict(m)
    m["api_key_masked"] = mask_key(m.get("api_key", ""))
    m.pop("api_key", None)
    return m


_VALID_CAPS = ("tool_call", "vision", "thinking", "thinking_only",
               "can_disable_thinking", "custom_protocol")
_VALID_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _norm_caps(raw):
    """规范化 caps 字段：仅保留合法 key、全转 bool、未知 key 丢弃；输入不是 dict 时返回空 dict。"""
    if not isinstance(raw, dict):
        return {}
    return {k: bool(v) for k, v in raw.items() if k in _VALID_CAPS}


def _norm_levels(raw):
    """规范化思考强度档位列表：去重 + 仅保留合法档位 + 保持顺序；输入不是 list 时返回空列表。"""
    if not isinstance(raw, list):
        return []
    seen = set()
    out = []
    for v in raw:
        if v in _VALID_LEVELS and v not in seen:
            seen.add(v)
            out.append(v)
    return out

"""
routes/templates — 模板 CRUD / 模板 AI 生成 / 结构化模板运行（SSE）
====================================================================
从 backend.py 逐字迁入（T05）。SSE 字节契约（R2）不变。
"""
import json
import uuid

import wecom_bridge as wb
from flask import Blueprint, request, Response, jsonify, stream_with_context

from core_utils import (extract_json, validate_schema, save_template_result,
                        log_event, _valid_json_schema)
from core.config_store import load_config, update_config, _save_config_safe  # noqa: F401（_save_config_safe 兼容旧导入）
from core.result import err_msg
from llm_client import resolve_model, stream_llm_events, _llm_complete, judge_model
from agent import _sse_stream
from agent.sys_prompts import _TPL_AGENT_SYSTEM

bp = Blueprint("templates", __name__, url_prefix="/api")

# ---------- v1.0.366 内置场景模板（出厂即用；id 以 builtin_ 前缀，不随 POST 全量保存丢失） ----------

BUILTIN_TEMPLATES = [
    {"id": "builtin_summary", "name": "📊 数据摘要", "builtin": True,
     "description": "把粘贴的数据/长文提炼成摘要与要点",
     "input_fields": [{"key": "content", "label": "数据或长文内容", "type": "textarea", "required": True},
                      {"key": "focus", "label": "重点关注（可选）", "type": "text", "required": False}],
     "system_prompt": "你是数据分析助手。把用户给出的数据/长文提炼为：一段 100 字以内的摘要，以及 3-6 条要点。忠实原文，不要编造数字。",
     "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}, "key_points": {"type": "array", "items": {"type": "string"}}}, "required": ["summary", "key_points"]}},
    {"id": "builtin_weekly", "name": "📝 周报生成", "builtin": True,
     "description": "把本周做过的事整理成结构化周报",
     "input_fields": [{"key": "items", "label": "本周做过的事（一行一件）", "type": "textarea", "required": True},
                      {"key": "next", "label": "下周计划（可选）", "type": "textarea", "required": False}],
     "system_prompt": "你是周报撰写助手。把用户列的事项整理为结构化周报：本周完成（分类归组、突出成果与数字）、问题与风险、下周计划。语言精炼、正式。",
     "output_schema": {"type": "object", "properties": {"week_report": {"type": "string"}, "highlights": {"type": "array", "items": {"type": "string"}}}, "required": ["week_report"]}},
    {"id": "builtin_minutes", "name": "📄 会议纪要", "builtin": True,
     "description": "把会议记录整理成决议/待办/责任分工",
     "input_fields": [{"key": "notes", "label": "会议记录原文", "type": "textarea", "required": True}],
     "system_prompt": "你是会议纪要助手。把会议记录整理为：会议结论（逐条）、待办事项（每条含责任人与时限，未提及则标「待确认」）、遗留问题。忠实原文，不添加会议外内容。",
     "output_schema": {"type": "object", "properties": {"decisions": {"type": "array", "items": {"type": "string"}}, "todos": {"type": "array", "items": {"type": "string"}}, "open_questions": {"type": "array", "items": {"type": "string"}}}, "required": ["decisions", "todos"]}},
    {"id": "builtin_email", "name": "✉️ 邮件润色", "builtin": True,
     "description": "按指定语气润色邮件/消息并说明改动",
     "input_fields": [{"key": "draft", "label": "原文", "type": "textarea", "required": True},
                      {"key": "tone", "label": "期望语气（如：正式/委婉/催办）", "type": "text", "required": False}],
     "system_prompt": "你是商务写作助手。按用户期望的语气润色邮件/消息：保持事实与诉求不变，优化结构与措辞，并列出主要改动点。默认语气：专业、礼貌、清晰。",
     "output_schema": {"type": "object", "properties": {"polished": {"type": "string"}, "changes": {"type": "array", "items": {"type": "string"}}}, "required": ["polished"]}},
    {"id": "builtin_todo", "name": "✅ 待办清单", "builtin": True,
     "description": "把杂乱的想法/要求整理成可执行待办",
     "input_fields": [{"key": "brainstorm", "label": "想法/要求（随意写）", "type": "textarea", "required": True}],
     "system_prompt": "你是任务整理助手。把用户的杂乱输入整理为可执行待办清单：每条含动作动词、交付物与建议顺序（P1/P2/P3 标优先级）。不确定的事项标注「需确认」。",
     "output_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "string"}}}, "required": ["todos"]}},
    {"id": "builtin_translate", "name": "🌐 翻译润色", "builtin": True,
     "description": "翻译并润色为中/英自然表达",
     "input_fields": [{"key": "source", "label": "原文", "type": "textarea", "required": True},
                      {"key": "lang", "label": "目标语言（默认英文）", "type": "text", "required": False}],
     "system_prompt": "你是专业翻译。把原文翻译为用户指定的目标语言（默认英文）：准确传达事实与语气，专业术语一致，输出地道自然的译文。不要解释翻译过程。",
     "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]

def template_run(template, inputs, cfg):
    """结构化运行模板：走 response_format=json_schema，产出 JSON。SSE 流。"""
    model = resolve_model(cfg, template.get("model_id"))
    if not model:
        yield f"data: {json.dumps({'type':'error','text':'模板未指定有效模型，请在「模型管理」添加并设为默认或指定。'}, ensure_ascii=False)}\n\n"
        return

    # 组装用户输入
    lines = []
    for f in template.get("input_fields", []):
        key = f.get("key") or f.get("label")
        val = inputs.get(key, "")
        if val not in (None, ""):
            lines.append(f"- {f.get('label', key)}：{val}")
    user_text = "输入参数：\n" + ("\n".join(lines) if lines else "（无）")

    if template.get("use_wecom_context"):
        cap = wb.capture()
        ctx = (cap.get("body") or err_msg(cap) or "")
        user_text = (f"[企微当前窗口《{cap.get('title')}》URL={cap.get('url')}]\n"
                     f"正文前 6000 字：\n{(ctx or '')[:6000]}\n\n") + user_text

    messages = [
        {"role": "system", "content": template.get("system_prompt", "") +
         "\n\n你必须严格按照给定的 JSON Schema 输出，不要包含任何解释性文字，只输出合法 JSON。"},
        {"role": "user", "content": user_text},
    ]
    schema = template.get("output_schema")
    rf = None
    if schema and isinstance(schema, dict) and schema.get("type") == "object":
        rf = {"type": "json_schema", "json_schema": {"name": "result", "strict": False, "schema": schema}}

    final_text = ""
    for ev in stream_llm_events(model, messages, response_format=rf):
        if ev[0] == "token":
            yield f"data: {json.dumps({'type':'token','text':ev[1]}, ensure_ascii=False)}\n\n"
        elif ev[0] == "error":
            yield f"data: {json.dumps({'type':'error','text':ev[1]}, ensure_ascii=False)}\n\n"
            return
        elif ev[0] == "done":
            final_text = ev[1].get("text") or ""

    data = extract_json(final_text)
    if data is None:
        # P1-6: 提供更友好的 JSON 提取失败提示
        snippet = final_text.strip()[:4000] if final_text.strip() else "(模型未输出任何文本)"
        hint = "JSON 解析失败。"
        if final_text.strip():
            stripped = final_text.strip()
            if stripped.startswith("```"):
                hint = "模型输出含代码块标记，请确认 JSON 是否被包裹在 markdown 代码块中。"
            elif not stripped.startswith(("{", "[")):
                hint = f"模型输出以「{stripped[:20]}」开头，不是合法 JSON 开头（应为 {{ 或 [）。请检查系统提示词。"
            elif stripped.count("{") != stripped.count("}"):
                hint = "JSON 中花括号数量不匹配，可能缺少闭合的 }}"
            elif stripped.count("[") != stripped.count("]"):
                hint = "JSON 中方括号数量不匹配，可能缺少闭合的 ]"
        yield f"data: {json.dumps({'type':'result','ok':False,'raw':snippet,'hint':hint}, ensure_ascii=False)}\n\n"
        return
    err = validate_schema(data, schema) if schema else None
    # 自动把结构化产出保存到产物库/产出
    try:
        save_template_result(template.get("name", "模板"), data)
    except Exception as e:  # noqa: BLE001
        log_event(f"[template] 模板结果保存失败：{e}")
    # P1-6: schema 校验失败提供具体提示
    if err:
        hint = "JSON 结构与 Schema 不匹配。"
        if "required property" in err:
            hint = "缺少必需字段，请检查模型是否完整输出所有必需属性。"
        elif "is not of type" in err:
            hint = "字段值类型错误（如字符串代替数字）。请检查字段类型定义。"
        elif "is not one of" in err:
            hint = "字段值不在允许的枚举范围内。"
        else:
            hint = f"Schema 校验错误：{err[:100]}"
    else:
        hint = None
    yield f"data: {json.dumps({'type':'result','ok': err is None,'valid': err is None,'warn': err,'hint': hint,'data': data}, ensure_ascii=False)}\n\n"

@bp.route("/templates", methods=["GET"])
def get_templates():
    cfg = load_config()
    # v1.0.303：`or []` 防御——旧磁盘 config 的 "templates": null 会让 .get 的缺省 [] 失效
    # （键存在、值为 None），返回 JSON null 致前端 .length 崩溃
    user_tpls = cfg.get("templates") or []
    # v1.0.366 模板重塑（P1）：内置场景模板（出厂即用，随代码分发）+ 使用计数。
    # GET 返回 = 内置（去重）+ 用户模板，附 used；选择器按 used 排序。
    usage = cfg.get("template_usage") or {}
    user_ids = {t.get("id") for t in user_tpls if isinstance(t, dict)}
    out = []
    for t in BUILTIN_TEMPLATES:
        if t["id"] in user_ids:
            continue  # 用户保存过同名 id 的自定义版本，以用户版为准
        out.append({**t, "builtin": True, "used": int(usage.get(t["id"]) or 0)})
    for t in user_tpls:
        if isinstance(t, dict):
            out.append({**t, "builtin": False, "used": int(usage.get(t.get("id")) or 0)})
    out.sort(key=lambda t: -(t.get("used") or 0))
    return jsonify(out)


@bp.route("/templates/use", methods=["POST"])
def api_templates_use():
    """v1.0.366：模板使用计数（运行一次 +1），选择器按「常用优先」排序。"""
    data = request.get_json(force=True, silent=True) or {}
    tid = str(data.get("id") or "")
    if not tid:
        return jsonify({"ok": False, "error": "缺少模板 id"}), 400
    # v1.0.555（P0-10）：update_config 事务——计数增量在锁内基于 fresh cfg 计算，
    # 原 load→改→save 并发时后写覆盖先写（使用计数丢失/回退）。
    _used = {"n": 0}
    def _mutate(cfg):
        usage = cfg.setdefault("template_usage", {})
        usage[tid] = int(usage.get(tid) or 0) + 1
        cfg["template_usage"] = usage
        _used["n"] = usage[tid]
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True, "used": _used["n"]})


@bp.route("/templates", methods=["POST"])
def post_templates():
    data = request.get_json(force=True, silent=True) or []
    if not isinstance(data, list):
        return jsonify({"ok": False, "error": "请求体必须是数组"}), 400
    tpls = []
    for t in data:
        if not isinstance(t, dict):
            continue
        tid = t.get("id") or ("t_" + uuid.uuid4().hex[:8])
        if str(tid).startswith("builtin_"):
            continue   # v1.0.366：内置模板由代码提供，不落用户配置（防重复/防篡改）
        name = t.get("name", "未命名模板")
        schema = t.get("output_schema")
        if schema is not None and not _valid_json_schema(schema):
            return jsonify({"ok": False, "error": f"模板「{name}」的 output_schema 不是合法 JSON Schema"}), 400
        ifields = t.get("input_fields", [])
        if ifields is None:
            ifields = []
        if not isinstance(ifields, list):
            return jsonify({"ok": False, "error": f"模板「{name}」的 input_fields 必须是数组"}), 400
        tpls.append({
            "id": tid,
            "name": name,
            "description": t.get("description", ""),
            "model_id": t.get("model_id", ""),
            "use_wecom_context": bool(t.get("use_wecom_context", True)),
            "system_prompt": t.get("system_prompt", ""),
            "input_fields": [f for f in ifields if isinstance(f, dict)],
            "output_schema": schema,
        })
    # v1.0.555（P0-10）：update_config 事务（tpls 已在上面构建好，纯覆盖写）
    if not update_config(lambda cfg: (cfg.__setitem__("templates", tpls), True)[1]):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify(tpls)

def _tpl_agent_questions(qs):
    out = []
    for i, q in enumerate(qs):
        if not isinstance(q, dict):
            continue
        opts = []
        for o in (q.get("options") or []):
            if isinstance(o, dict):
                opts.append({"value": str(o.get("value") or ""), "label": str(o.get("label") or o.get("value") or ""),
                             "desc": str(o.get("desc") or "")})
            elif isinstance(o, str):
                opts.append({"value": o, "label": o, "desc": ""})
        if not opts:
            opts = [{"value": "custom", "label": "自定义…", "desc": ""}]
        if not any(o["value"] == "custom" for o in opts):
            opts.append({"value": "custom", "label": "自定义…", "desc": ""})
        out.append({"key": str(q.get("key") or ("q" + str(i))), "label": str(q.get("label") or "待确认"),
                    "desc": str(q.get("desc") or ""), "options": opts[:6]})
    return out


def _agent_gen(msgs, cfg, model, *, sys_prompt, schema_name, result_schema, on_result):
    """AI 设计助手（模板/快捷入口）共用流式生成器（A2 合并，原 _tpl_agent_gen/_quick_agent_gen
    约 75% 逐字克隆）。消息组装 → _llm_complete(json_schema) → extract_json 降级 →
    message/questions 分发 → on_result(res) 清洗产出 result 事件 → done。
    事件序列与字段（token/options/result/error/done）与合并前逐事件等价。
    on_result(res)：调用方清洗 result 并 yield 后续 SSE 载荷 dict（0..n 个）。"""
    messages = [{"role": "system", "content": sys_prompt}]
    for m in msgs:
        role = m.get("role")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": str(m.get("content") or "")})

    def _sse(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    rf = {"type": "json_schema", "json_schema": {"name": schema_name, "strict": False,
          "schema": {"type": "object",
                     "properties": {
                         "message": {"type": "string"},
                         "questions": {"type": "array", "items": {"type": "object",
                             "properties": {"key": {"type": "string"}, "label": {"type": "string"},
                                            "desc": {"type": "string"},
                                            "options": {"type": "array", "items": {"type": "object",
                                                "properties": {"value": {"type": "string"}, "label": {"type": "string"},
                                                               "desc": {"type": "string"}},
                                                "required": ["value", "label"]}}},
                             "required": ["key", "label", "options"]}},
                         "result": result_schema,
                     },
                     "required": ["message", "questions"]}}}
    text = _llm_complete(model, messages, response_format=rf)
    if not text:
        yield _sse({"type": "error", "text": "模型未返回有效内容，请重试。"})
        return
    d = extract_json(text)
    if not d or not isinstance(d, dict):
        # 模型未按 JSON 输出：降级为普通消息展示，用户可继续对话
        yield _sse({"type": "token", "text": text})
        yield _sse({"type": "done"})
        return
    msg = str(d.get("message") or "")
    if msg:
        yield _sse({"type": "token", "text": msg})
    qs = d.get("questions")
    if isinstance(qs, list) and qs:
        qs = _tpl_agent_questions(qs)
        if qs:
            yield _sse({"type": "options", "questions": qs})
    for obj in on_result(d.get("result")):
        yield _sse(obj)
    yield _sse({"type": "done"})


def _run_agent_chat_endpoint(data, make_stream):
    """AI 设计助手聊天端点共用入口（模板/快捷入口，A2 合并）：消息校验 → 双模型选型 → SSE 包装。
    make_stream(msgs, cfg, model) 返回事件生成器。"""
    msgs = data.get("messages", [])
    if not isinstance(msgs, list) or not msgs:
        return Response("data: " + json.dumps({"type": "error", "text": "消息不能为空"}, ensure_ascii=False) + "\n\n",
                        mimetype="text/event-stream; charset=utf-8")
    cfg = load_config()
    # 双模型分工（v1.0.236）：AI 设计助手要设计 prompt/schema，判断密集——用户显式选了模型则尊重，
    # 否则优先判断大模型，再退回默认模型。未配置 judge 时行为与旧版一致。
    model = (resolve_model(cfg, data.get("model_id")) if data.get("model_id")
             else (judge_model(cfg) or resolve_model(cfg)))
    if not model:
        return Response("data: " + json.dumps({"type": "error", "text": "未配置任何模型，请在「模型管理」添加。"}, ensure_ascii=False) + "\n\n",
                        mimetype="text/event-stream; charset=utf-8")
    return Response(stream_with_context(_sse_stream(make_stream(msgs, cfg, model))),
                    mimetype="text/event-stream; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _tpl_agent_gen(msgs, cfg, model):
    """模板 AI 生成流：注入「可选模型列表」+ 模板 result 清洗，其余走共用生成器。"""
    model_opts = [{"id": m.get("id"), "name": m.get("name"), "default": bool(m.get("is_default"))}
                  for m in cfg.get("models", []) if m.get("enabled", True)]
    sys_prompt = _TPL_AGENT_SYSTEM + "\n可选模型列表：" + json.dumps(model_opts, ensure_ascii=False)

    def _on_result(res):
        if isinstance(res, dict) and res.get("name"):
            valid_ids = {m.get("id") for m in model_opts}
            if res.get("model_id") not in valid_ids:
                res["model_id"] = model.get("id") or ""
            res["name"] = str(res.get("name"))[:80]
            res["description"] = str(res.get("description") or "")
            res["use_wecom_context"] = bool(res.get("use_wecom_context", True))
            res["system_prompt"] = str(res.get("system_prompt") or "")
            if not isinstance(res.get("input_fields"), list):
                res["input_fields"] = []
            res["input_fields"] = [f for f in res["input_fields"] if isinstance(f, dict) and f.get("key")]
            os_ = res.get("output_schema")
            if os_ is not None and not (isinstance(os_, dict) and _valid_json_schema(os_)):
                os_ = None
            res["output_schema"] = os_
            yield {"type": "result", "template": res}

    return _agent_gen(msgs, cfg, model, sys_prompt=sys_prompt,
                      schema_name="template_gen_reply",
                      result_schema={"type": ["object", "null"]},
                      on_result=_on_result)

@bp.route("/template/agent/chat", methods=["POST"])
def api_template_agent_chat():
    """模板 AI 生成：无状态聊天（前端每次全量传 messages），SSE 返回 token/options/result/error/done。"""
    data = request.get_json(force=True, silent=True) or {}
    return _run_agent_chat_endpoint(data, _tpl_agent_gen)

@bp.route("/template/run", methods=["POST"])
def api_template_run():
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    tid = data.get("template_id")
    # v1.0.303：`or []` 防御 "templates": null（旧磁盘配置）
    tpl = next((t for t in (cfg.get("templates") or []) if isinstance(t, dict) and t.get("id") == tid), None)
    if not tpl:
        # v1.0.366：内置场景模板也可运行（id= builtin_ 前缀）
        tpl = next((t for t in BUILTIN_TEMPLATES if t.get("id") == tid), None)
    if not tpl:
        return Response("data: " + json.dumps({"type": "error", "text": "模板不存在"}, ensure_ascii=False) + "\n\n",
                        mimetype="text/event-stream; charset=utf-8")
    return Response(stream_with_context(_sse_stream(template_run(tpl, data.get("inputs", {}), cfg))),
                    mimetype="text/event-stream; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
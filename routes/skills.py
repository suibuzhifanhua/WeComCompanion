"""
routes/skills — 技能列表/详情/覆盖/LLM 优化（从 backend.py 逐字迁入，T05）
====================================================================
蓝图 url_prefix=/api；路由去 /api 前缀。
v1.0.303 新增：自定义技能（技能中心用户自建）——
  POST   /skills/custom          创建
  PUT    /skills/custom/<sid>    更新
  DELETE /skills/custom/<sid>    删除
  （GET /skills、GET /skills/<sid>、/skills/<sid>/optimize 同时兼容自定义技能）
"""
import json
import uuid
import datetime

from flask import Blueprint, request, jsonify

from core_utils import extract_json, log_event
from agent_tools import TOOLS, SKILLS, _effective_skill, _custom_skills
from core.config_store import load_config, update_config, _save_config_safe  # noqa: F401（_save_config_safe 兼容旧导入）
from llm_client import resolve_model, judge_model, _llm_complete

bp = Blueprint("skills", __name__, url_prefix="/api")

@bp.route("/skills", methods=["GET"])
def get_skills():
    cfg = load_config()
    enabled = cfg.get("enabled_skills")
    overrides = cfg.get("skill_overrides") or {}
    out = []
    for base in SKILLS:
        s = _effective_skill(base, cfg)
        is_on = True if enabled is None else (s["id"] in enabled)
        out.append({"id": s["id"], "name": s["name"], "icon": s.get("icon", ""),
                    "desc": s.get("desc", ""), "enabled": is_on, "tools": s.get("tools", []),
                    "overridden": s["id"] in overrides, "custom": False})
    # v1.0.303：自定义技能并入列表（custom=True，前端据此显示徽标/删除/编辑路由）
    for s in _custom_skills(cfg):
        is_on = True if enabled is None else (s["id"] in enabled)
        out.append({"id": s["id"], "name": s.get("name", ""), "icon": s.get("icon", ""),
                    "desc": s.get("desc", ""), "enabled": is_on, "tools": s.get("tools", []),
                    "overridden": False, "custom": True, "auto": bool(s.get("auto"))})
    return jsonify({"skills": out, "enabled_skills": enabled})


@bp.route("/skills", methods=["POST"])
def post_skills():
    data = request.get_json(force=True, silent=True) or {}
    skills = data.get("enabled_skills")
    # v1.0.555（P0-10）：update_config 事务——合法 id 校验用 fresh cfg 的 custom_skills
    def _mutate(cfg):
        if isinstance(skills, list):
            # v1.0.303：合法 id = 内置 + 自定义（否则前端保存启用设置时自定义技能被静默过滤）
            valid = {s["id"] for s in SKILLS} | {s["id"] for s in _custom_skills(cfg)}
            cfg["enabled_skills"] = [x for x in skills if x in valid]
        else:
            cfg["enabled_skills"] = None          # None = 全部启用
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True})


# ---------- 技能专项优化（单技能查看/覆盖/agent 优化） ----------
def _skill_tool_names():
    return {t.get("function", {}).get("name") for t in TOOLS}

@bp.route("/skills/<sid>", methods=["GET"])
def api_skill_detail(sid):
    """单个技能详情：base=内置定义，current=生效定义（已应用覆盖）。
    v1.0.303：自定义技能（custom=True）base=current=存储定义，无覆盖概念。"""
    cfg = load_config()
    base = next((s for s in SKILLS if s["id"] == sid), None)
    is_custom = False
    if not base:
        base = next((s for s in _custom_skills(cfg) if s.get("id") == sid), None)
        if not base:
            return jsonify({"ok": False, "error": "技能不存在"}), 404
        is_custom = True
    cur = base if is_custom else _effective_skill(base, cfg)
    keys = ("id", "name", "desc", "icon", "prompt", "tools", "judge", "default")
    return jsonify({"ok": True, "id": sid, "base": {k: base.get(k) for k in keys},
                    "current": {k: cur.get(k) for k in keys},
                    "overridden": (not is_custom) and sid in (cfg.get("skill_overrides") or {}),
                    "custom": is_custom,
                    "tool_names": sorted(_skill_tool_names())})


@bp.route("/skills/<sid>/override", methods=["POST"])
def api_skill_override(sid):
    """保存单个技能的覆盖定义（持久化到 config.json 的 skill_overrides）。"""
    base = next((s for s in SKILLS if s["id"] == sid), None)
    if not base:
        return jsonify({"ok": False, "error": "技能不存在"}), 404
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "技能名称不能为空"}), 400
    tools = data.get("tools")
    if tools is None:
        tools = base.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        return jsonify({"ok": False, "error": "tools 必须是工具名数组"}), 400
    cfg = load_config()
    ov = cfg.get("skill_overrides") or {}
    valid = _skill_tool_names()
    # v1.0.349：变更前快照当前提示词（版本历史，供回滚）——record_version 是文件
    # 副作用（版本库），留在事务外执行一次；配置变更本身走 update_config 事务。
    from agent_tools.skill_versions import record_version
    record_version(sid, (ov.get(sid) or {}).get("prompt") or base.get("prompt", ""),
                   "manual_edit", "技能中心手动编辑前的快照")
    def _mutate(cfg):
        ov = cfg.get("skill_overrides") or {}
        ov[sid] = {
            "name": name,
            "desc": str(data.get("desc") or ""),
            "icon": str(data.get("icon") or base.get("icon") or ""),
            "prompt": str(data.get("prompt") or ""),
            "tools": [t for t in tools if isinstance(t, str) and t in valid],
        }
        cfg["skill_overrides"] = ov
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True})


@bp.route("/skills/<sid>/override", methods=["DELETE"])
def api_skill_override_delete(sid):
    """清除单个技能的覆盖，恢复内置定义。"""
    cfg = load_config()
    ov = cfg.get("skill_overrides") or {}
    if sid in ov:
        # v1.0.349：清除前快照当前覆盖提示词（可回滚）——副作用留事务外
        from agent_tools.skill_versions import record_version
        record_version(sid, (ov.get(sid) or {}).get("prompt", ""), "override_clear",
                       "清除覆盖（恢复内置定义）前的快照")
        def _mutate(cfg):
            ov = cfg.get("skill_overrides") or {}
            ov.pop(sid, None)
            cfg["skill_overrides"] = ov
            return True
        if not update_config(_mutate):
            return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True})

@bp.route("/skills/<sid>/optimize", methods=["POST"])
def api_skill_optimize(sid):
    """调用 LLM 基于当前技能定义 + 用户诉求生成优化后的技能定义，回填前端编辑界面（不落盘）。
    v1.0.303：自定义技能同样可优化（基于其存储定义）。"""
    data = request.get_json(force=True, silent=True) or {}
    req_text = str(data.get("request") or "").strip()
    cfg = load_config()
    base = next((s for s in SKILLS if s["id"] == sid), None)
    if not base:
        base = next((s for s in _custom_skills(cfg) if s.get("id") == sid), None)
    if not base:
        return jsonify({"ok": False, "error": "技能不存在"}), 404
    # 双模型分工（v1.0.236）：技能优化=判断/创作密集任务，配置了判断大模型就用它，否则退回默认模型
    model = judge_model(cfg) or resolve_model(cfg)
    if not model:
        return jsonify({"ok": False, "error": "未配置任何模型，请在「模型管理」添加。"}), 400
    cur = _effective_skill(base, cfg)
    tool_names = sorted(_skill_tool_names())
    prompt = (
        "你是「企微伴侣」的技能优化专家。下面是一个技能定义（JSON）与用户的优化诉求。\n"
        "请输出优化后的完整技能定义 JSON。硬性要求：\n"
        f"1. id 必须保持为 \"{sid}\" 不变；\n"
        "2. 字段：id, name, desc, icon, prompt, tools, judge, default；\n"
        "3. name/desc/icon 为展示信息，要语义准确、简洁；\n"
        "4. prompt 是注入给 agent 的能力说明/执行流程提示词，是本技能的灵魂，要针对诉求精炼、步骤化、可执行；\n"
        f"5. tools 只能从以下内置工具中选择（按需增删）：{json.dumps(tool_names, ensure_ascii=False)}；\n"
        "6. judge/default 保留原值不变；\n"
        "7. 只输出合法 JSON，不要任何解释性文字。\n"
        f"当前技能定义：\n{json.dumps({k: cur.get(k) for k in ('id','name','desc','icon','prompt','tools','judge','default')}, ensure_ascii=False, indent=2)}\n"
        f"用户优化诉求：\n{req_text or '（无，按通用最佳实践优化）'}"
    )
    rf = {"type": "json_schema", "json_schema": {"name": "skill_definition", "strict": False,
          "schema": {"type": "object",
                     "properties": {"name": {"type": "string"}, "desc": {"type": "string"},
                                    "icon": {"type": "string"}, "prompt": {"type": "string"},
                                    "tools": {"type": "array", "items": {"type": "string"}},
                                    "judge": {"type": "string"}, "default": {"type": "boolean"}},
                     "required": ["name", "prompt", "tools"]}}}
    text = _llm_complete(model, [{"role": "system", "content": prompt},
                                 {"role": "user", "content": "请生成优化后的技能定义 JSON。"}], response_format=rf)
    skill = extract_json(text) if text else None
    if not skill or not isinstance(skill, dict):
        return jsonify({"ok": False, "error": "模型未能生成有效的技能定义，请重试或改用手工编辑。"})
    skill["id"] = sid
    for k in ("judge", "default"):
        if k not in skill or skill[k] is None:
            skill[k] = cur.get(k)
    valid = _skill_tool_names()
    skill["tools"] = [t for t in (skill.get("tools") or []) if isinstance(t, str) and t in valid]
    return jsonify({"ok": True, "skill": skill})


# ---------- 自定义技能（v1.0.303）：技能中心用户自建，持久化到 config.json 的 custom_skills ----------
def _norm_custom_skill(data, existing=None):
    """校验并规范化自定义技能定义。返回 (skill_dict, err)。
    规则：name 必填；tools 必须是工具名字符串数组且取内置工具名的子集（非法工具名静默丢弃）；
    缺省字段回退 existing（更新时），judge 固定 none（自定义技能不支持双模型判断位）。"""
    name = str(data.get("name") or (existing or {}).get("name") or "").strip()
    if not name:
        return None, "技能名称不能为空"
    tools = data.get("tools")
    if tools is None:
        tools = (existing or {}).get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        return None, "tools 必须是工具名数组"
    valid = _skill_tool_names()
    return {
        "id": (existing or {}).get("id") or "",
        "name": name,
        "desc": str(data.get("desc") or (existing or {}).get("desc") or ""),
        "icon": str(data.get("icon") or (existing or {}).get("icon") or "🧩"),
        "prompt": str(data.get("prompt") or (existing or {}).get("prompt") or ""),
        "tools": [t for t in tools if t in valid],
        "judge": "none",
        "default": True,
    }, ""


@bp.route("/skills/custom", methods=["POST"])
def api_custom_skill_create():
    """创建自定义技能：id 自动生成（custom_<时间戳>_<4位hex>），立即生效。
    显式启用模式（enabled_skills 为 list）下自动把新 id 加入启用列表——
    否则默认全开语义下新建的技能会是关闭的，用户会以为没生效。"""
    data = request.get_json(force=True, silent=True) or {}
    skill, err = _norm_custom_skill(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    skill["id"] = "custom_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4]
    # v1.0.555（P0-10）：append + 启用入 update_config 事务（fresh cfg 上合并）
    def _mutate(cfg):
        skills = cfg.get("custom_skills") or []
        skills.append(skill)
        cfg["custom_skills"] = skills
        en = cfg.get("enabled_skills")
        if isinstance(en, list) and skill["id"] not in en:
            en.append(skill["id"])
            cfg["enabled_skills"] = en
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True, "id": skill["id"]})


@bp.route("/skills/custom/<sid>", methods=["PUT"])
def api_custom_skill_update(sid):
    """更新自定义技能（name/desc/icon/prompt/tools）。"""
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    skills = cfg.get("custom_skills") or []
    existing = next((s for s in skills if s.get("id") == sid), None)
    if not existing:
        return jsonify({"ok": False, "error": "自定义技能不存在"}), 404
    skill, err = _norm_custom_skill(data, existing)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    # v1.0.349：变更前快照（版本历史，供回滚）——副作用留事务外
    from agent_tools.skill_versions import record_version
    record_version(sid, existing.get("prompt", ""), "manual_edit", "自定义技能编辑前的快照")
    # v1.0.555（P0-10）：替换入 update_config 事务
    def _mutate(cfg):
        skills = cfg.get("custom_skills") or []
        for i, s in enumerate(skills):
            if s.get("id") == sid:
                skills[i] = skill
                break
        cfg["custom_skills"] = skills
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True})


@bp.route("/skills/custom/<sid>", methods=["DELETE"])
def api_custom_skill_delete(sid):
    """删除自定义技能，并同步清理 enabled_skills 中的残留 id。"""
    cfg = load_config()
    skills = cfg.get("custom_skills") or []
    existing = next((s for s in skills if s.get("id") == sid), None)
    new = [s for s in skills if s.get("id") != sid]
    if len(new) == len(skills):
        return jsonify({"ok": False, "error": "自定义技能不存在"}), 404
    # v1.0.349：删除前快照提示词（内容可从版本历史找回）——副作用留事务外
    from agent_tools.skill_versions import record_version
    record_version(sid, (existing or {}).get("prompt", ""), "delete", "删除自定义技能前的快照")
    # v1.0.555（P0-10）：删除 + enabled 清理入 update_config 事务
    def _mutate(cfg):
        skills = cfg.get("custom_skills") or []
        cfg["custom_skills"] = [s for s in skills if s.get("id") != sid]
        en = cfg.get("enabled_skills")
        if isinstance(en, list) and sid in en:
            cfg["enabled_skills"] = [x for x in en if x != sid]
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True})


# ---------- 公共技能库（v1.0.309）：内置快照 + 可配置远程源，一键装成自定义技能 ----------


def _hub_source_skills(cfg):
    """取当前技能库清单：下沉到 skill_hub_data.hub_skills_with_fallback（v1.0.317，
    供无 Flask 层复用）。返回 (skills, source, note)。"""
    from agent_tools.skill_hub_data import hub_skills_with_fallback
    return hub_skills_with_fallback(cfg)


@bp.route("/skills/hub", methods=["GET"])
def api_skill_hub():
    """公共技能库清单 + 已安装状态与版本对比（hub 技能装为 custom id 前缀 hub_）。
    v1.0.316：安装时记录源版本到 custom 条目 hub_version；这里对比源版本 vs 已装版本
    输出 update_available——前端只对 True 的技能显示「更新」按钮（已装且同版只显示徽标）。"""
    cfg = load_config()
    skills, source, note = _hub_source_skills(cfg)
    if skills is None:
        from agent_tools.skill_hub_data import HUB_SKILLS, HUB_VERSION
        skills = [dict(s) for s in HUB_SKILLS]
        version = HUB_VERSION
    else:
        version = ""
    installed = {}
    for s in _custom_skills(cfg):
        if str(s.get("id", "")).startswith("hub_"):
            installed[s["id"][4:]] = str(s.get("hub_version") or "")   # key=去前缀的源 id
    for s in skills:
        src_id = s["id"]
        src_ver = str(s.get("version") or version or "")
        s["version"] = src_ver
        s["installed"] = src_id in installed
        # 已装但无版本记录（旧数据）视为有更新——点一次「更新」即校准
        s["update_available"] = (src_id in installed) and (installed[src_id] != src_ver)
    return jsonify({"ok": True, "skills": skills, "source": source, "note": note,
                    "version": version, "url": str((cfg.get("skill_hub") or {}).get("url") or "")})


@bp.route("/skills/hub/install", methods=["POST"])
def api_skill_hub_install():
    """安装/更新一个公共技能：写入 custom_skills（id=hub_<原id>，幂等覆盖），自动启用。"""
    data = request.get_json(force=True, silent=True) or {}
    hid = str(data.get("id") or "").strip()
    if not hid:
        return jsonify({"ok": False, "error": "缺少技能 id"}), 400
    cfg = load_config()
    skills, source, _ = _hub_source_skills(cfg)
    if skills is None:
        from agent_tools.skill_hub_data import HUB_SKILLS, HUB_VERSION
        skills = HUB_SKILLS
        list_version = HUB_VERSION
    else:
        list_version = ""
    src = next((s for s in skills if s.get("id") == hid), None)
    if not src:
        return jsonify({"ok": False, "error": "技能库中不存在该技能"}), 404
    tools = src.get("tools")
    if not isinstance(tools, list):
        tools = []
    skill, err = _norm_custom_skill({"name": src.get("name"), "desc": src.get("desc", ""),
                                     "icon": src.get("icon") or "🧩", "prompt": src.get("prompt", ""),
                                     "tools": tools})
    if err:
        return jsonify({"ok": False, "error": err}), 400
    skill["id"] = "hub_" + hid          # 固定前缀：重装=更新，不产生重复
    # v1.0.316：记录安装时的源版本（技能级 version 优先，回退清单级 version），
    # 供 /skills/hub 对比 update_available——只在源更新后才显示「更新」按钮
    skill["hub_version"] = str(src.get("version") or list_version)
    # v1.0.555：合并入 update_config 事务
    # v1.0.555（借鉴 TencentCloud/Octop skill_packages 的 copy_policy 设计）：
    #   - snapshot：覆盖前用 record_version 快照旧提示词（技能中心可回滚）——
    #     原 hub 更新直接 cskills[i] = skill 覆盖，用户手工调整被静默冲掉；
    #   - lock：存量技能带 hub_locked: true 时跳过覆盖（远程清单更新不得
    #     覆盖用户锁定版本），结果里报 locked 供前端提示。
    _upd = {"flag": False, "locked": False}
    def _mutate(cfg):
        cskills = cfg.get("custom_skills") or []
        for i, s in enumerate(cskills):
            if s.get("id") == skill["id"]:
                if s.get("hub_locked"):
                    _upd["locked"] = True
                    break
                try:
                    from agent_tools.skill_versions import record_version
                    record_version(skill["id"], s.get("prompt", ""), "hub_update",
                                   "技能库更新覆盖前的快照（hub_version="
                                   + str(s.get("hub_version") or "") + "）")
                except Exception:  # noqa: BLE001
                    pass
                cskills[i] = skill          # 已装 → 覆盖更新
                _upd["flag"] = True
                break
        else:
            cskills.append(skill)
        cfg["custom_skills"] = cskills
        en = cfg.get("enabled_skills")
        if isinstance(en, list) and skill["id"] not in en:
            en.append(skill["id"])
            cfg["enabled_skills"] = en
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True, "id": skill["id"], "name": skill["name"],
                    "updated": _upd["flag"], "locked": _upd.get("locked", False)})


@bp.route("/skills/hub/url", methods=["POST"])
def api_skill_hub_url():
    """保存公共技能库远程源 URL（空字符串=恢复内置快照）。"""
    data = request.get_json(force=True, silent=True) or {}
    url = str(data.get("url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "URL 必须以 http(s):// 开头"}), 400
    # v1.0.555（P0-10）：update_config 事务
    if not update_config(lambda cfg: (cfg.__setitem__("skill_hub", {"url": url}), True)[1]):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True})


@bp.route("/skills/hub/import_source", methods=["POST"])
def api_skill_hub_import_source():
    """v1.0.318 技能源导入（agent 的 import_skill_source 弹窗确认后调用）：
    拉取 URL 校验为技能清单 → 设为远程源 → 清单内全部技能批量安装（hub_ 前缀幂等，
    与手动安装/更新共用一套机制）。任意错误不落任何配置变更。"""
    from agent_tools.skill_hub_data import fetch_skill_manifest
    data = request.get_json(force=True, silent=True) or {}
    url = str(data.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "URL 必须以 http(s):// 开头"}), 400
    skills, list_version = fetch_skill_manifest(url)
    if skills is None:
        return jsonify({"ok": False, "error": list_version}), 502
    # v1.0.555（P0-10）：整批合并入 update_config 事务——原 load→批量改→save 期间
    # 任何其它配置写都会被这批陈旧快照整体回滚。
    _normed = []
    for src in skills:
        tools = src.get("tools") if isinstance(src.get("tools"), list) else []
        skill, err = _norm_custom_skill({"name": src.get("name"), "desc": src.get("desc", ""),
                                         "icon": src.get("icon") or "🧩", "prompt": src.get("prompt", ""),
                                         "tools": tools})
        if err:
            continue
        skill["id"] = "hub_" + str(src["id"])
        skill["hub_version"] = str(src.get("version") or list_version)
        _normed.append(skill)
    _stat = {"created": 0, "updated": 0, "locked": 0}
    def _mutate(cfg):
        cskills = cfg.get("custom_skills") or []
        for skill in _normed:
            replaced = False
            for i, s in enumerate(cskills):
                if s.get("id") == skill["id"]:
                    # v1.0.555（借鉴 Octop copy_policy）：lock 拒改 / snapshot 覆盖前快照
                    if s.get("hub_locked"):
                        _stat["locked"] += 1
                        replaced = None      # 既不覆盖也不新增
                        break
                    try:
                        from agent_tools.skill_versions import record_version
                        record_version(skill["id"], s.get("prompt", ""), "hub_update",
                                       "技能源批量更新覆盖前的快照")
                    except Exception:  # noqa: BLE001
                        pass
                    cskills[i] = skill
                    replaced = True
                    break
            if replaced is None:
                continue
            if replaced:
                _stat["updated"] += 1
            else:
                cskills.append(skill)
                _stat["created"] += 1
            en = cfg.get("enabled_skills")
            if isinstance(en, list) and skill["id"] not in en:
                en.append(skill["id"])
                cfg["enabled_skills"] = en
        cfg["custom_skills"] = cskills
        cfg["skill_hub"] = {"url": url}
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    created, updated = _stat["created"], _stat["updated"]
    log_event(f"[skills] 技能源导入 {url}：清单 {len(skills)} 项（新装 {created} 更新 {updated}"
              + (f" 锁定跳过 {_stat['locked']}" if _stat["locked"] else "") + "）")
    return jsonify({"ok": True, "imported": len(skills), "created": created, "updated": updated,
                    "locked": _stat.get("locked", 0)})


# ---------- v1.0.349 技能版本历史与回滚 ----------
@bp.route("/skills/<sid>/versions", methods=["GET"])
def api_skill_versions(sid):
    """技能提示词版本历史（新→旧；每次编辑/自修复/清除覆盖/回滚前自动快照，留 20 份）。"""
    from agent_tools.skill_versions import list_versions
    return jsonify({"ok": True, "versions": list_versions(sid)})


@bp.route("/skills/<sid>/rollback", methods=["POST"])
def api_skill_rollback(sid):
    """回滚技能提示词到指定版本快照。
    内置技能：恢复进 skill_overrides（保留其它覆盖字段）；自定义技能：改其存储定义。
    回滚前先对当前状态做一次快照（rollback_pre）——回滚本身可再撤销。"""
    from agent_tools.skill_versions import get_version, record_version
    data = request.get_json(force=True, silent=True) or {}
    entry = get_version(sid, data.get("ts"))
    if not entry:
        return jsonify({"ok": False, "error": "版本不存在（可能已被清理）"}), 404
    cfg = load_config()
    base = next((s for s in SKILLS if s["id"] == sid), None)
    custom = next((s for s in (cfg.get("custom_skills") or []) if s.get("id") == sid), None)
    if base:
        ov = cfg.setdefault("skill_overrides", {})
        cur = ov.get(sid) or {}
        record_version(sid, cur.get("prompt") or base.get("prompt", ""),
                       "rollback_pre", "回滚前的当前状态")
    elif custom:
        record_version(sid, custom.get("prompt", ""), "rollback_pre", "回滚前的当前状态")
    else:
        return jsonify({"ok": False, "error": "技能不存在"}), 404
    # v1.0.555（P0-10）：回滚写入入 update_config 事务
    def _mutate(cfg):
        if base:
            ov = cfg.setdefault("skill_overrides", {})
            cur = ov.get(sid) or {}
            cur["prompt"] = entry.get("prompt", "")
            cur.setdefault("name", base.get("name", ""))
            cur.setdefault("desc", base.get("desc", ""))
            cur.setdefault("icon", base.get("icon", ""))
            cur.setdefault("tools", base.get("tools", []))
            ov[sid] = cur
            cfg["skill_overrides"] = ov
        else:
            customs = cfg.get("custom_skills") or []
            for s in customs:
                if s.get("id") == sid:
                    s["prompt"] = entry.get("prompt", "")
                    break
            cfg["custom_skills"] = customs
        return True
    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    log_event(f"[skills] 回滚技能 {sid} 提示词到版本 {entry.get('ts')}")
    return jsonify({"ok": True})

@bp.route("/skills/auto_create", methods=["POST"])
def api_skill_auto_create():
    """v1.0.582（用户需求）：相似任务自动沉淀——把检测到的相似任务固化为自定义技能。
    body: {prompt: 任务原文, title: 建议标题, count: 执行次数}
    写 cfg['custom_skills']（agent_tools._custom_skills 标准格式），技能中心立即可见；
    幂等：同 id 技能已存在时跳过（created=false）。"""
    try:
        from core.config_store import load_config, update_config
        import hashlib
        d = request.get_json(force=True, silent=True) or {}
        prompt = str(d.get("prompt") or "").strip()
        title = str(d.get("title") or "").strip()[:24] or "未命名技能"
        count = int(d.get("count") or 0)
        if len(prompt) < 6:
            return jsonify({"ok": False, "error": "任务内容过短，无法固化为技能"}), 400
        fid = "auto-" + hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8]

        def _mutate(cfg):
            lst = cfg.setdefault("custom_skills", [])
            if any(isinstance(s, dict) and s.get("id") == fid for s in lst):
                return False   # 已存在，幂等跳过
            lst.append({
                "id": fid, "name": title,
                "desc": f"由相似任务自动沉淀（已执行 {count} 次）。原文：{prompt[:80]}",
                "icon": "⚙️", "prompt": prompt, "tools": [], "judge": "none",
                "default": False,   # v1.0.652：布尔（旧字符串 "False" 恒真值，与其它技能的 True 语义混乱）
            })
            return True

        changed = update_config(_mutate)
        if not changed:
            return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
        log_event(f"[skills] 相似任务自动沉淀为技能：{title}（id={fid}）")
        return jsonify({"ok": True, "id": fid, "created": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:160]}), 500

# -*- coding: utf-8 -*-
"""routes.b_admin — 管理员/自启动/经验/连接器/状态/配置/用量/快捷链/通知/版本/规则
"""
# -*- coding: utf-8 -*-
import os
import sys
import time
import threading
import uuid

from flask import Blueprint, request, jsonify

from core_utils import log_event, workspace_root
from services import rule_check
from agent_tools import CONNECTORS, is_connector_configured
from core import safe_open  # v1.0.646：打开本机地址的唯一收口
from core.config_store import (load_config, update_config, _mask_connector_config,
                          _load_experiences, _save_experiences, _exp_rec, APP_PORT,
                          FEATURE_FLAG_WRITABLE_KEYS)   # v1.0.570 清理：_save_config_safe/save_config 未用摘除
from llm_client import resolve_model, judge_model
from agent.sys_prompts import _QUICK_AGENT_SYSTEM
from .wecom import bind_status
from .templates import _agent_gen, _run_agent_chat_endpoint

from version import get as _version_get  # v1.0.510（P2-13）：版本兜底块收敛到 version.get
VERSION = _version_get()


class _ConfigBadRequest(Exception):
    """v1.0.555（P0-10）：update_config 事务内的输入校验失败——由调用方转 400
    （update_config 对 mutate 抛出的异常只记日志返回 False，无法区分校验失败
    与写入失败，故用异常类型透传校验语义）。"""


from routes.b_models import mask_models  # status/config 脱敏复用

bp = Blueprint("mgmt_admin", __name__)

# v1.0.407 修复：v1.0.402 拆分时 GET 装饰器遗失（POST /experiences 在，GET 404）——
# 经验库列表/编辑弹窗拿不到数据。与 b_models.get_models 同款问题，AST 扫描确认仅此两处。
@bp.route("/experiences", methods=["GET"])
def api_experiences_list():
    return jsonify({"ok": True, "items": _load_experiences()})


@bp.route("/experiences", methods=["POST"])
def api_experiences_add():
    data = request.get_json(force=True, silent=True) or {}
    if not str(data.get("problem") or "").strip() and not str(data.get("solution") or "").strip():
        return jsonify({"ok": False, "error": "问题与解决方法不能同时为空"}), 400
    items = _load_experiences()
    rec = _exp_rec(data)
    items.append(rec)
    ok, err = _save_experiences(items)
    if not ok:
        return jsonify({"ok": False, "error": f"保存失败：{err}"}), 500
    return jsonify({"ok": True, "item": rec})


@bp.route("/experiences/<eid>", methods=["PUT"])
def api_experiences_update(eid):
    data = request.get_json(force=True, silent=True) or {}
    items = _load_experiences()
    for i, e in enumerate(items):
        if e.get("id") == eid:
            items[i] = _exp_rec(data, e)
            ok, err = _save_experiences(items)
            if not ok:
                return jsonify({"ok": False, "error": f"保存失败：{err}"}), 500
            return jsonify({"ok": True, "item": items[i]})
    return jsonify({"ok": False, "error": "经验不存在"}), 404


@bp.route("/experiences/<eid>", methods=["DELETE"])
def api_experiences_delete(eid):
    items = _load_experiences()
    new_items = [e for e in items if e.get("id") != eid]
    if len(new_items) == len(items):
        return jsonify({"ok": False, "error": "经验不存在"}), 404
    ok, err = _save_experiences(new_items)
    if not ok:
        return jsonify({"ok": False, "error": f"删除失败：{err}"}), 500
    return jsonify({"ok": True})

@bp.route("/connectors", methods=["GET"])
def get_connectors():
    cfg = load_config()
    conns = cfg.get("connectors") or {}
    out = []
    for c in CONNECTORS:
        cc = conns.get(c["id"]) or {}
        masked = _mask_connector_config(cc)
        out.append({
            "id": c["id"], "name": c["name"], "icon": c.get("icon", ""), "desc": c.get("desc", ""),
            "config_schema": c.get("config_schema", []),
            "enabled": bool(cc.get("enabled")), "configured": is_connector_configured(c, cc),
            "config": masked,
        })
    return jsonify({"connectors": out})


@bp.route("/connectors", methods=["POST"])
def post_connectors():
    data = request.get_json(force=True, silent=True) or {}
    incoming = data.get("connectors") or {}
    # v1.0.555（P0-10）：load→改→save 改 update_config 锁内事务——原无整体锁，
    # 连接器凭据（api_key 等）存在被并发写覆盖静默丢失的风险。
    def _mutate(cfg):
        existing = cfg.get("connectors") or {}
        for cid, val in incoming.items():
            defn = next((c for c in CONNECTORS if c["id"] == cid), None)
            if not defn or not isinstance(val, dict):
                continue
            store = existing.get(cid, {})
            store["enabled"] = bool(val.get("enabled"))
            for f in defn.get("config_schema", []):
                k = f["key"]
                if k not in val:
                    continue
                v = val[k]
                # v1.0.555（P1-13 修复）：前端对未改动的密码字段渲染成空输入框并送空串
                # （placeholder 承诺"留空=保留不变"），原逻辑 `if k in val: store[k]=val[k]`
                # 把已存 api_key 静默清空——每点一次保存且不重输 Key，凭据就被摧毁。
                # 敏感字段空串且原值非空 → 跳过写入，保留原值。
                if (isinstance(v, str) and v.strip() == ""
                        and str(f.get("type") or "") == "password"
                        and str(store.get(k) or "") != ""):
                    continue
                store[k] = v
            existing[cid] = store
        cfg["connectors"] = existing
        return True

    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True})

# 模型连通探测缓存（v1.0.528-2）：60s TTL + 后台线程刷新——/api/status 高频调用
# 绝不同步等网络（探测失败最坏 2.5s 会拖慢状态刷新），首查返回 None=「检测中」。
_MODEL_PROBE = {"ts": 0.0, "data": None}
_MODEL_PROBE_LOCK = threading.Lock()


def _model_probe_payload():
    with _MODEL_PROBE_LOCK:
        cached = dict(_MODEL_PROBE)
    fresh = time.time() - cached["ts"] < 60
    if cached["data"] is None or not fresh:
        def _job():
            try:
                from llm_client import resolve_model, probe_model_reachable
                cfg = load_config()
                m = resolve_model(cfg)
                r = probe_model_reachable(m) if m else {"state": "none", "detail": "未配置模型"}
            except Exception as e:  # noqa: BLE001
                r = {"state": "down", "detail": str(e)[:100]}
            with _MODEL_PROBE_LOCK:
                _MODEL_PROBE["ts"] = time.time()
                _MODEL_PROBE["data"] = r
        threading.Thread(target=_job, name="model-probe", daemon=True).start()
    return cached["data"]   # 旧缓存/None（首查）——前端显示「检测中」


@bp.route("/status")
def api_status():
    cfg = load_config()
    model = resolve_model(cfg)
    jm = judge_model(cfg)
    return jsonify({
        "ready": model is not None,
        "model_probe": _model_probe_payload(),
        "version": VERSION,
        # v1.0.603：打包清空历史对话时 +1；前端据此失效 localStorage 里的 hist_* 缓存
        "chat_epoch": cfg.get("chat_epoch") or 0,
        "models_count": len([m for m in cfg.get("models", []) if m.get("enabled", True)]),
        "default_model": model.get("name") if model else None,
        "judge_model": jm.get("name") if jm else None,
        "dual_model": jm is not None,
        "templates_count": len(cfg.get("templates") or []),
        "wecom": bind_status(cfg),
        "workspace": workspace_root(),
    })


@bp.route("/config", methods=["GET"])
def get_config():
    cfg = load_config()
    out = dict(cfg)
    out["wecom"] = dict(cfg.get("wecom", {}))
    out["wecom"].pop("bound_pid", None)
    out["wecom"].pop("bound_sig", None)
    # webhook 是含密钥的机器人地址，不向前端回传（P1-3）；但必须回一个"配没配"的布尔，
    # 否则输入框恒空 + POST 收空串 = 用户改任何一个企微开关都会把它清空（v1.0.616 P0）。
    out["wecom"]["webhook_set"] = bool(str(cfg.get("wecom", {}).get("webhook") or "").strip())
    out["wecom"].pop("webhook", None)
    out["models"] = [mask_models(m) for m in cfg.get("models", [])]
    if isinstance(out.get("connectors"), dict):
        out["connectors"] = {cid: _mask_connector_config(cc) for cid, cc in out["connectors"].items()}
    # v1.0.361 审查修复（P16）：dsh.api_key 与 models/connectors 同口径脱敏（原明文回传前端）
    if isinstance(out.get("dsh"), dict) and out["dsh"].get("api_key"):
        _k = str(out["dsh"]["api_key"])
        out["dsh"]["api_key"] = (_k[:4] + "****" + _k[-4:]) if len(_k) > 8 else "****"
    out["version"] = VERSION
    return jsonify(out)


@bp.route("/config", methods=["POST"])
def post_config():
    data = request.get_json(force=True, silent=True) or {}
    # v1.0.555（P0-10）：load→改→save 改 update_config 锁内事务——原无整体锁，
    # 自修复体检/启动绑定持 cfg 跨分钟级操作的场景下，用户在设置页保存的模型/
    # 连接器/功能开关会被陈旧快照静默回滚。
    # 判断模型 id 校验需要读 models——放事务内（fresh cfg），失败返回 400。
    def _mutate(cfg):
        w = cfg.setdefault("wecom", {})
        for k in ("cdp_port", "auto_launch", "auto_cdp", "silent_mode"):
            if k in data:
                w[k] = data[k]
        # v1.0.616（P0）：空串＝**不修改**。GET 不回传 webhook（密钥不外泄），输入框恒空，
        # 旧写法 `is not None` 会把"用户只是勾了个静默开关"提交上来的空串当成清空指令写回
        # config —— 已配置的群机器人地址就此静默失效，界面上还看不出来。
        # 真要清除走显式 clear_webhook（与 dsh 凭据同口径）。
        _wh = str(data.get("webhook") or "").strip()
        if _wh:
            w["webhook"] = _wh[:500]
        elif data.get("clear_webhook"):
            w["webhook"] = ""
        # 双模型分工（v1.0.234）：持久化「判断模型」配置（必须是已启用模型 id 或空串）
        if "judge_model_id" in data:
            jid = data.get("judge_model_id") or ""
            valid_ids = {m.get("id") for m in cfg.get("models", []) if m.get("enabled", True)}
            if jid and jid not in valid_ids:
                raise _ConfigBadRequest("判断模型不是有效模型 id")
            cfg["judge_model_id"] = jid
        # 功能管理总开关（v1.0.312）：只接受布尔键，逐键合并（防整段被脏数据覆盖）。
        # v1.0.617：白名单不再抄字面量，直接取 core.config_store.FEATURE_FLAG_WRITABLE_KEYS——
        # 以前加一个开关要在"默认值 / 路由白名单 / UI 数组"三处各写一遍，漏掉白名单
        # 就是"界面上勾了、保存成功、后端永远读不到"（permission_level 漏加即同型事故）。
        # 该白名单**故意不含 auto_save_dialogs**（另存为弹窗自动应答，用户口径弹窗零容忍，
        # 只允许手改 config.json），锁见 tests/test_v617_flags.py。
        if "feature_flags" in data and isinstance(data.get("feature_flags"), dict):
            ff = cfg.setdefault("feature_flags", {})
            for k in FEATURE_FLAG_WRITABLE_KEYS:
                if k in data["feature_flags"]:
                    ff[k] = bool(data["feature_flags"][k])
            cfg["feature_flags"] = ff
        # 用户固定规则（v1.0.313）：多行文本， cap 3000 字符
        if "user_rules" in data:
            cfg["user_rules"] = str(data.get("user_rules") or "")[:3000]
        return True

    try:
        ok = update_config(_mutate)
    except _ConfigBadRequest as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not ok:
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True})


@bp.route("/usage/summary")
def usage_summary():
    """Token 用量统计（v1.0.252）：聚合 dist/logs/usage_YYYYMM.jsonl 独立存档。"""
    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        days = 14
    from services import usage_stats
    return jsonify(usage_stats.summary(days))


@bp.route("/audit/progress")
def audit_progress():
    """后台批审进度（v1.0.523-6）：前端进度条轮询。"""
    from services import batch_flow
    return jsonify(batch_flow.background_progress())


@bp.route("/audit/abort", methods=["POST"])
def audit_abort():
    """中止后台批审（v1.0.537）：当前单据跑完后停止，已处理结果出报告，剩余可 resume。"""
    from services import batch_flow
    return jsonify(batch_flow.request_audit_abort())


@bp.route("/audit/resume", methods=["POST"])
def audit_resume():
    """续跑剩余批审（v1.0.540）：从断点接续（跳过已完成项），后台执行。"""
    from services import batch_flow
    return jsonify(batch_flow.start_background_audit(port=None, mode="resume"))


@bp.route("/asr/status")
def asr_status():
    """语音转写引擎状态（v1.0.536 组件化）：三态 installed/missing/False + 安装进度。"""
    from services import asr
    return jsonify({"available": asr.asr_available(),
                    "installed": asr.component_installed(),
                    # v1.0.611：把「装好了却加载失败」的真实原因透出去，界面不再只有一句转写失败
                    "last_error": asr.last_error(),
                    "progress": asr.install_progress()})


@bp.route("/asr/install", methods=["POST"])
def asr_install():
    """安装/重装语音组件（v1.0.545）：pypi wheel 下载解压到 产物库/组件/asr/。
    已安装时允许重装（修复旧组件缺依赖——v1.0.544 教训：手挑清单漏 tqdm 被标记
    「假成功」，重装 = staging 完整替换）。"""
    from services import asr
    return jsonify(asr.install_component())


@bp.route("/asr/transcribe", methods=["POST"])
def asr_transcribe():
    """语音转文字（v1.0.523-5）：multipart 音频 → 文本。
    录音在浏览器本地产生、模型本地推理——音频不外发。"""
    from services import asr
    f = request.files.get("audio")
    if f is None or not f.filename:
        return jsonify({"ok": False, "error": "未收到音频文件"}), 400
    ext = (f.filename.rsplit(".", 1)[-1] or "webm").lower()[:5]
    # v1.0.614：体积判定要在读之前。原来 `data = f.read()` 之后才在 transcribe_bytes 里
    # 判 20MB —— 超大文件会先整块进内存再被拒（Werkzeug 虽会落盘中转，但 read() 一把梭）。
    _cl = f.content_length or 0
    if _cl and _cl > 20 * 1048576:
        return jsonify({"ok": False, "error": "音频过大（>20MB），请说短一点"}), 413
    data = f.read(20 * 1048576 + 1)
    if len(data) > 20 * 1048576:
        return jsonify({"ok": False, "error": "音频过大（>20MB），请说短一点"}), 413
    text, info = asr.transcribe_bytes(data, ext=ext)
    if text is None:
        return jsonify({"ok": False, "error": str(info)}), 500
    return jsonify({"ok": True, "text": text, "info": info})


# ---------- 任务运行看板（v1.0.523-2） ----------
_DASH_CACHE = {"ts": 0.0, "data": None}
# v1.0.616（审查 P2）：读写都要锁。同文件的 _MODEL_PROBE 早就有锁，这里却是裸的——
# `jsonify(_DASH_CACHE["data"])` 序列化途中被另一个请求的写线程改写，能撞出
# "dictionary changed size during iteration" 的 500（Flask 是 threaded=True）。
_DASH_LOCK = threading.Lock()


@bp.route("/dashboard")
def dashboard():
    """任务运行看板聚合（v1.0.523-2）：定时任务/会话/知识库/今日用量四块。
    60s 缓存（前端打开工作台拉一次，不做实时轮询，数据半静态）。"""
    with _DASH_LOCK:
        _hit = _DASH_CACHE["data"]
        if _hit is not None and time.time() - _DASH_CACHE["ts"] < 60:
            return jsonify(_hit)
    out = {"schedules": {}, "conversations": {}, "kb": {}, "usage_today": {}}
    today = time.strftime("%Y-%m-%d")
    # 1) 定时任务：清单 + 今日运行成败计数（last_result 无结构化历史，取"最后一次"口径）
    try:
        from services import scheduler
        items = scheduler.list_schedules() or []
        s = {"total": len(items), "enabled": sum(1 for x in items if x.get("enabled")),
             "ran_today": 0, "failed_last": 0, "running_now": 0, "items": []}
        for x in items:
            lr = str(x.get("last_run") or "")
            if lr.startswith(today):
                s["ran_today"] += 1
            if x.get("running"):
                s["running_now"] += 1
            res = str(x.get("last_result") or "")
            # v1.0.614：判据落到结构化字段 last_status（scheduler._run_one 现在会回
            # ok/timeout/blocked/error）。原来只嗅"异常|失败"字样，超时中止的文案里
            # 根本没这两个词 → 超时被算成成功。老记录没这个字段时才回落字样判定。
            st = str(x.get("last_status") or "")
            if lr and (st in ("timeout", "blocked", "error")
                       or (not st and res and ("异常" in res or "失败" in res))):
                s["failed_last"] += 1
            if lr:
                s["items"].append({"name": x.get("name") or x.get("id", ""),
                                   "last_run": lr, "last_result": res[:60],
                                   "last_status": st or "unknown"})
        s["items"].sort(key=lambda x: x["last_run"], reverse=True)
        out["schedules"] = s
    except Exception as e:  # noqa: BLE001
        out["schedules"] = {"error": str(e)[:120]}
    # 2) 会话统计：总数 + 今日新增（按文件 mtime）
    try:
        from services.conversations import conv_dir
        d = conv_dir()
        total, today_new = 0, 0
        for n in os.listdir(d):
            if not n.endswith(".json"):
                continue
            total += 1
            try:
                if time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(os.path.join(d, n)))) == today:
                    today_new += 1
            except OSError:
                continue
        out["conversations"] = {"total": total, "today": today_new}
    except Exception as e:  # noqa: BLE001
        out["conversations"] = {"error": str(e)[:120]}
    # 3) 知识库规模：各分类 md 文件计数
    try:
        kb_root = os.path.join(workspace_root(), "知识库")
        by_dir, total = {}, 0
        if os.path.isdir(kb_root):
            for sub in os.listdir(kb_root):
                p = os.path.join(kb_root, sub)
                if not os.path.isdir(p) or sub.startswith("."):
                    continue
                cnt = 0
                for root, _dirs, files in os.walk(p):
                    cnt += sum(1 for f in files if f.lower().endswith((".md", ".txt")))
                if cnt:
                    by_dir[sub] = cnt
                    total += cnt
        out["kb"] = {"total": total, "by_dir": by_dir}
    except Exception as e:  # noqa: BLE001
        out["kb"] = {"error": str(e)[:120]}
    # 4) 今日用量（复用 usage_stats 聚合，days=1）
    try:
        from services import usage_stats
        out["usage_today"] = usage_stats.summary(1).get("today", {})
    except Exception as e:  # noqa: BLE001
        out["usage_today"] = {"error": str(e)[:120]}
    # 5) 批审历史趋势（v1.0.540：最近 5 轮判定摘要，台账/月报判定.json）
    try:
        from services import batch_flow
        out["audit_history"] = batch_flow.audit_history(5)
        out["audit_resumable"] = bool(batch_flow._load_progress())
    except Exception as e:  # noqa: BLE001
        out["audit_history"] = []
        out["audit_resumable"] = False
        # v1.0.619（审查③）：原实现 `except Exception as e` 里的 e 从不使用、也不记日志——
        # 界面只显示"没有批审历史"，分不清"真没跑过"还是"读挂了"（月报进度.json 坏了、
        # batch_flow 导入失败都会长这样）。错误原文进 payload + 日志各留一份。
        out["audit_history_error"] = type(e).__name__ + ": " + str(e)[:120]
        log_event(f"[dashboard] 批审历史/断点读取失败：{type(e).__name__}: {e}")
    with _DASH_LOCK:
        _DASH_CACHE["ts"] = time.time()
        _DASH_CACHE["data"] = out
    return jsonify(out)


@bp.route("/usage/clear", methods=["DELETE", "POST"])
def usage_clear():
    """清空 Token 用量统计（v1.0.270）：删除全部 dist/logs/usage_*.jsonl 存档。"""
    from services import usage_stats
    r = usage_stats.clear()
    log_event(f"[usage] 用量统计已清空：{r['records']} 条记录 / {r['removed']} 个月度文件")
    return jsonify({"ok": True, **r})


@bp.route("/quick_links", methods=["POST"])

def post_quick_links():
    """保存左侧栏「快捷入口」可编辑列表（持久化到 config.json 的 quick_links）。"""
    data = request.get_json(force=True, silent=True) or {}
    links = data.get("quick_links")
    if not isinstance(links, list):
        return jsonify({"ok": False, "error": "quick_links 必须是数组"}), 400
    cleaned = []
    for it in links:
        if not isinstance(it, dict):
            continue
        cleaned.append({
            "id": str(it.get("id", "")),
            "icon": str(it.get("icon", "🔗")),
            "name": str(it.get("name", "")),
            "prompt": str(it.get("prompt", "")),
            # v1.0.249：即发（点击直接执行）与自动续跑（auto_loop，考试未过自动开新会话）
            "auto_send": bool(it.get("auto_send", False)),
            "auto_loop": bool(it.get("auto_loop", False)),
        })
    # v1.0.555（P0-10）：update_config 事务——原 load→改→save 无整体锁，
    # 与其它设置项并发保存时互相覆盖。
    if not update_config(lambda cfg: (cfg.__setitem__("quick_links", cleaned), True)[1]):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    return jsonify({"ok": True, "quick_links": cleaned})

def _quick_agent_gen(msgs, cfg, model):
    """快捷入口 AI 生成流：快捷入口 result 清洗，其余走共用生成器（routes.templates._agent_gen）。"""
    def _on_result(res):
        if isinstance(res, list) and res:
            cleaned = []
            for it in res[:8]:
                if not isinstance(it, dict):
                    continue
                nm = str(it.get("name") or "").strip()[:40]
                if not nm:
                    continue
                cleaned.append({
                    "id": "q" + uuid.uuid4().hex[:10],
                    "icon": (str(it.get("icon") or "🔗").strip() or "🔗")[:8],
                    "name": nm,
                    "prompt": str(it.get("prompt") or ""),
                })
            if cleaned:
                yield {"type": "result", "links": cleaned}

    return _agent_gen(msgs, cfg, model, sys_prompt=_QUICK_AGENT_SYSTEM,
                      schema_name="quick_links_gen_reply",
                      result_schema={"type": ["array", "null"], "items": {"type": "object",
                          "properties": {"name": {"type": "string"}, "icon": {"type": "string"},
                                         "prompt": {"type": "string"}},
                          "required": ["name", "icon", "prompt"]}},
                      on_result=_on_result)


@bp.route("/quick_links/agent/chat", methods=["POST"])
def api_quick_links_agent_chat():
    """快捷入口 AI 生成：无状态聊天（前端每次全量传 messages），SSE 返回 token/options/result/error/done。"""
    data = request.get_json(force=True, silent=True) or {}
    return _run_agent_chat_endpoint(data, _quick_agent_gen)

# v1.0.407 修复：v1.0.402 拆分时此处遗留孤儿装饰器 @bp.route("/models") GET（函数体已
# 搬去 b_models），它把下方 api_rules 错误注册到 GET /api/models——模型管理页 GET /api/models
# 被规则模块列表遮蔽，前端 loadModels 拿到 {"modules":[...]}，模型列表/添加全部失效。
# 现删除孤儿装饰器，GET /api/models 回归 b_models.get_models（POST 本就正常）。
@bp.route("/rules/check", methods=["POST"])
@bp.route("/rules/list", methods=["GET"])
def api_rules():
    """脚本判定引擎直判接口（v1.0.94）：
      POST /api/rules/check  {module, data} → 确定性规则脚本判定结果（不耗 LLM）。
      GET  /api/rules/list   → 已注册的脚本判定模块清单。
    输入 data 为 read_wecom_fields 返回的完整结构（含 fields 数组）。"""
    if request.method == "GET":
        return jsonify({"ok": True, "modules": rule_check.list_rules()})
    data = request.get_json(force=True, silent=True) or {}
    module = str(data.get("module", "")).strip()
    fields = data.get("data") or {}
    return jsonify(rule_check.run_rule(module, fields))

# 管理员模式解锁的系统级能力（普通模式下这些操作会被 Windows 直接拒绝 Access Denied）：
#   ① 系统级命令与服务（sc 创建服务、修改 hosts、系统配置类命令）
#   ② 写入系统保护目录（Program Files / Windows 下自更新、部署文件；仍受安全中心审批约束）
#   ③ HKLM 全机注册表（全机自启、系统级配置）
#   ④ 系统级计划任务（schtasks /ru SYSTEM）
# 安全边界：管理员模式不改变安全中心审批流程——危险命令仍走 block/ask，fail-closed 不变。

def _is_admin_now():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


@bp.route("/admin/status", methods=["GET"])
def api_admin_status():
    """管理员模式状态：当前是否以管理员运行 + 是否可提权（frozen exe 才能自我提权重启）。"""
    return jsonify({"ok": True, "is_admin": _is_admin_now(),
                    "elevatable": os.name == "nt" and bool(getattr(sys, "frozen", False)),
                    "exe": sys.executable if getattr(sys, "frozen", False) else "",
                    # v1.0.655（B6）：并入原 /admin/task_status 的 armed（自举任务是否
                    # 已注册，前端决定重启走哪条链）——查询侧零成本，省一个端点。
                    "armed": _task_armed()})


@bp.route("/webui/open", methods=["POST"])
def api_webui_open():
    """v1.0.405（M6 死引用补齐）：用系统默认浏览器打开本机服务地址。
    背景：设置页「🌐 打开网页版」按钮调用的前端 openWebUI 与本端点此前都未实现
    （按钮点击 ReferenceError）。无头模式（WECOM_NO_GUI=1）下这也是标准入口。"""
    url = f"http://127.0.0.1:{APP_PORT}/"
    try:
        # v1.0.646：走 core/safe_open 收口（只放行 http(s) + 本机 host）
        ok, err = safe_open.open_url(url)
        if not ok:
            return jsonify({"ok": False, "error": err, "url": url}), 400
        return jsonify({"ok": True, "url": url})
    except Exception as e:  # noqa: BLE001
        log_event(f"[webui] 打开浏览器失败：{e}")
        return jsonify({"ok": False, "error": f"打开失败：{e}", "url": url}), 500


# ---------- v1.0.542 管理员重启·计划任务方案 ----------
_ADMIN_TASK = "WeComCompanionAdminBootstrap"   # 预授权自举任务（/sc once 占位，仅 /run 手动触发）


def _admin_flag_path():
    return os.path.join(os.path.dirname(sys.executable), "产物库", "系统", "admin_respawn.flag")


def _schtasks(args, timeout=15):
    """schtasks 同步执行（普通权限），返回 (rc, stdout)。"""
    import subprocess as _sp
    r = _sp.run(["schtasks"] + args, capture_output=True, timeout=timeout,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    return r.returncode, (r.stdout or b"").decode("gbk", "ignore")


def _task_armed():
    try:
        rc, _ = _schtasks(["/query", "/tn", _ADMIN_TASK])
        return rc == 0
    except Exception:  # noqa: BLE001
        return False


@bp.route("/admin/arm_task", methods=["POST"])
def api_admin_arm_task():
    """注册管理员重启自举任务（v1.0.542，**一次性 UAC**）。
    创建 /rl HIGHEST 的占位计划任务（仅 schtasks /run 手动触发，不自动跑）；
    之后「管理员模式重启」走 schtasks /run——预授权最高权限启动，**零 UAC、
    不经 runas（安全软件无从拦截）**。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return jsonify({"ok": False, "error": "仅打包版支持"}), 400
    exe = sys.executable
    # v1.0.652（审计 P1-16）：/tr 引号按 CommandLineToArgvW 语义转义。旧写法
    # `tr = '"exe" --admin-respawn'`（真实引号）再被 join 的 `"{}"` 包一层 →
    # `""C:\Program Files\...\exe" --admin-respawn"`——第二个引号即关闭引号态，
    # 空格处断词，/tr 只拿到 `C:\Program`（exe 在含空格路径时管理员重启静默失效；
    # 本机路径无空格从未暴露）。改与下方 autostart 同款 `\"` 转义：外层引号包裹时
    # \" 是字面引号，argv 值完整保留 `"exe 路径" --admin-respawn`。
    tr = '\\"{}\\" --admin-respawn'.format(exe)
    args = ["/create", "/f", "/tn", _ADMIN_TASK, "/tr", tr,
            "/sc", "once", "/st", "23:59", "/rl", "HIGHEST"]
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteExW.restype = ctypes.c_int
        # SEE_MASK_NOASYNC 同步等 UAC；runas schtasks 创建最高权限任务（需管理员）
        rc = shell32.ShellExecuteW(None, "runas", "schtasks.exe",
                                   " ".join('"{}"'.format(a) if " " in a else a for a in args),
                                   None, 0)   # SW_HIDE
        if rc is None or int(rc) <= 32:
            return jsonify({"ok": False, "error": "UAC 被取消或创建失败"}), 400
        time.sleep(1.0)
        armed = _task_armed()
        if armed:
            log_event("[admin] 管理员重启自举任务已注册（此后重启零 UAC）")
            return jsonify({"ok": True, "hint": "已启用——之后「管理员模式重启」不再弹 UAC"})
        return jsonify({"ok": False, "error": "任务注册后未查询到（可能被安全软件拦截）"}), 500
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"注册失败：{e}"}), 500


@bp.route("/admin/restart", methods=["POST"])
def api_admin_restart():
    """以管理员模式重启软件（v1.0.542 计划任务方案）。
    前置：设置页一次性「启用管理员重启支持」（UAC 注册 /rl HIGHEST 自举任务，
    仅 schtasks /run 手动触发、不自动跑）。之后重启 = 写标志 + schtasks /run——
    预授权最高权限启动，**零 UAC、不经 runas**（安全软件无从拦截——v1.0.532/533
    两代 runas 链在用户机器上三次失败：cmd 中转被拦、直接 runas 也无痕迹）。
    看门狗保留：respawn 实例 20s 未起 → 托盘气泡归因。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return jsonify({"ok": False, "error": "仅打包版支持管理员模式重启；开发模式请以管理员身份运行 IDE/终端"}), 400
    if _is_admin_now():
        return jsonify({"ok": False, "error": "当前已经以管理员模式运行"}), 400
    if not _task_armed():
        return jsonify({"ok": False, "need_arm": True,
                        "error": "尚未启用管理员重启支持（需一次性 UAC 注册计划任务，之后零 UAC）"}), 400
    try:
        flag = _admin_flag_path()
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        with open(flag, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        rc, out = _schtasks(["/run", "/tn", _ADMIN_TASK])
        if rc != 0:
            if os.path.isfile(flag):
                os.remove(flag)
            return jsonify({"ok": False, "error": f"计划任务触发失败（rc={rc}）：{out[:120]}"}), 500
        log_event("[admin] 管理员重启：计划任务已触发（预授权，无 UAC）；当前进程即将退出")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"计划任务触发异常：{e}"}), 500

    def _quit_soon():
        time.sleep(0.8)   # 计划任务启动 onefile 解包需数秒，标志文件已核销（respawn 侧自删）
        try:
            from desktop import close_guard
            close_guard.remove_tray_icon()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    threading.Timer(0.1, _quit_soon).start()
    return jsonify({"ok": True, "hint": "已触发管理员模式重启（计划任务预授权，无 UAC）；若 20 秒内未出现会弹窗告知原因"})


# ---- 开机自启（v1.0.369）：HKCU Run 注册表开关，仅打包版（frozen exe）支持 ----
# PyInstaller onefile 下 sys.executable 指向用户真实运行的 exe（dist/WeComCompanion.exe），
# 注册表值带引号包裹防空格路径被截断；开发模式（python backend.py）不支持——注册 python.exe 无意义。
# v1.0.373：支持「启动后隐藏主窗口」——注册表值追加 --minimized 启动参数，backend 启动时
# 检测该参数并 window.hide()（托盘常驻，从托盘菜单恢复）。
AUTOSTART_NAME = "WeComCompanion"
_AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_HIDE_ARG = "--minimized"
_AUTOSTART_TASK = "WeComCompanionAdmin"   # v1.0.373：管理员模式自启动（计划任务，最高权限）


def _admin_task_exists():
    """管理员自启动计划任务是否已创建（schtasks /query，普通权限可查）。"""
    if os.name != "nt":
        return False
    try:
        import subprocess
        r = subprocess.run(["schtasks", "/query", "/tn", _AUTOSTART_TASK],
                           capture_output=True, timeout=15,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _runas_schtasks(args):
    """UAC 提权执行 schtasks（创建/删除最高权限任务）。返回是否成功派发（rc>32）。"""
    import ctypes
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    rc = shell32.ShellExecuteW(None, "runas", "schtasks.exe", args, None, 0)
    return rc is not None and int(rc) > 32


def _parse_autostart_value(val):
    """解析 HKCU Run 值 → (enabled, hide_main)。"""
    v = str(val or "").strip()
    return bool(v), (_AUTOSTART_HIDE_ARG in v)


def _autostart_cmdline(hide_main):
    return '"%s"%s' % (sys.executable, (" " + _AUTOSTART_HIDE_ARG) if hide_main else "")


def _autostart_status():
    """返回 (supported, enabled, hide_main)。enabled = HKCU Run 或管理员计划任务任一存在。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False, False, False
    hkcu_on = False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _t = winreg.QueryValueEx(k, AUTOSTART_NAME)
            hkcu_on, _hide = _parse_autostart_value(val)
    except FileNotFoundError:
        pass
    except OSError as e:
        log_event(f"[autostart] 注册表读取异常（按关闭处理）：{e}")
    admin_task = _admin_task_exists()
    return True, (hkcu_on or admin_task), hkcu_on and _hide


@bp.route("/autostart", methods=["GET", "POST"])
def api_autostart():
    """开机自启：GET 查状态；POST {"enabled": bool, "hide_main": bool, "admin": bool|null}。
    v1.0.373（需求2）：admin=true 时用「计划任务（/rl highest，最高权限）」实现管理员
    模式自启动——登录后免 UAC 直接提权运行；创建/删除需 UAC 确认（ShellExecuteW runas
    弹窗，等待任务出现/消失最多 30s）。与 HKCU Run 普通自启互斥（切换时自动清理对方）。"""
    if request.method == "GET":
        supported, enabled, hide_main = _autostart_status()
        admin_task = supported and _admin_task_exists()
        mode = "admin" if admin_task else ("normal" if enabled else None)
        cfg = load_config()
        admin_hide = bool((cfg.get("ui") or {}).get("autostart_admin_hide"))
        return jsonify({"ok": True, "supported": supported, "enabled": enabled,
                        "hide_main": hide_main, "mode": mode,
                        "admin_hide": admin_hide if admin_task else hide_main})
    data = request.get_json(force=True, silent=True) or {}
    enable = bool(data.get("enabled"))
    hide_main = bool(data.get("hide_main")) and enable
    admin = data.get("admin")
    if admin is None:
        admin = _admin_task_exists()   # 未指定 → 保持现有方式（hide 切换时重建）
    admin = bool(admin) and enable
    supported = _autostart_status()[0]
    if not supported:
        return jsonify({"ok": False, "error": "仅打包版（exe）支持开机自启，开发模式请手动配置"}), 400

    import winreg
    def _hkcu_write():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_cmdline(hide_main))
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass  # silent-ok: 注册表值不存在＝本来就没设自启，删除即达成目标

    # hide 记忆（管理员任务重建 /tr 时需要）
    try:
        # v1.0.555（P0-10）：小 delta 走 update_config 锁内合并——原 load→save
        # 会与其它配置写并发竞争（陈旧快照整体覆盖）。
        update_config(lambda cfg: (cfg.setdefault("ui", {}).__setitem__(
            "autostart_admin_hide", hide_main), True)[1])
    except Exception:  # noqa: BLE001
        pass

    if enable and admin:
        # 管理员模式：删 HKCU（互斥）→ UAC 创建最高权限计划任务 → 轮询确认
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        except OSError as e:
            log_event(f"[autostart] HKCU 清理失败（忽略）：{e}")
        tr = '\\"%s\\"' % _autostart_cmdline(hide_main)
        args = '/create /f /tn "%s" /sc onlogon /rl highest /tr %s' % (_AUTOSTART_TASK, tr)
        if not _runas_schtasks(args):
            return jsonify({"ok": False, "error": "提权失败（UAC 被取消）"}), 400
        import time as _t
        deadline = _t.time() + 30
        while _t.time() < deadline:
            if _admin_task_exists():
                log_event("[autostart] 管理员模式自启动已开启（计划任务最高权限，启动后%s）"
                          % ("隐藏主窗口" if hide_main else "正常显示主窗口"))
                _s, enabled, _h = _autostart_status()
                return jsonify({"ok": True, "enabled": True, "mode": "admin", "hide_main": hide_main})
            _t.sleep(0.5)
        return jsonify({"ok": False, "error": "30 秒内未检测到计划任务（UAC 未确认或创建失败）"}), 400

    # 普通模式 / 关闭：若计划任务存在 → UAC 删除（切回普通或全关都要清）
    if _admin_task_exists():
        args = '/delete /f /tn "%s"' % _AUTOSTART_TASK
        if _runas_schtasks(args):
            import time as _t
            deadline = _t.time() + 25
            while _t.time() < deadline:
                if not _admin_task_exists():
                    break
                _t.sleep(0.5)
            if _admin_task_exists():
                return jsonify({"ok": False, "error": "管理员计划任务删除失败（UAC 未确认）"}), 400
        else:
            return jsonify({"ok": False, "error": "删除管理员计划任务失败（UAC 被取消）"}), 400
    try:
        _hkcu_write()
    except OSError as e:
        return jsonify({"ok": False, "error": "注册表写入失败：%s" % e}), 500
    log_event("[system] 开机自启已" + ("开启（普通权限%s）" % ("，启动后隐藏主窗口" if hide_main else ""))
              if enable else "[system] 开机自启已关闭")
    _s, enabled, hide = _autostart_status()
    return jsonify({"ok": True, "enabled": enabled, "hide_main": hide, "mode": ("normal" if enable else None)})


# ---------------- v1.0.647：定时任务 CRUD（自 routes/schedule.py 并入） ----------------
# 原文件 45 行、5 个端点，全部委托 services.scheduler；与设置页域同源故并入本蓝图。
# scheduler 不在模块级 import：保持 b_admin 原有的惰性口径（见上方 _dashboard 聚合）。
def _sched():
    from services import scheduler
    return scheduler


@bp.route("/schedules", methods=["GET"])
def api_schedules():
    return jsonify({"ok": True, "schedules": _sched().list_schedules()})


@bp.route("/schedules", methods=["POST"])
def api_schedule_upsert():
    data = request.get_json(force=True, silent=True) or {}
    ok, r = _sched().upsert(data)
    if not ok:
        return jsonify({"ok": False, "error": r}), 400
    log_event(f"[sched] 任务已保存：{data.get('name')}（{data.get('cron')}）")
    return jsonify({"ok": True, "id": r})


@bp.route("/schedules/<sid>", methods=["DELETE"])
def api_schedule_delete(sid):
    if not _sched().delete_schedule(sid):
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True})


@bp.route("/schedules/<sid>/enabled", methods=["POST"])
def api_schedule_toggle(sid):
    data = request.get_json(force=True, silent=True) or {}
    if not _sched().set_enabled(sid, bool(data.get("enabled"))):
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True})


@bp.route("/schedules/<sid>/run", methods=["POST"])
def api_schedule_run(sid):
    if not _sched().run_now(sid, load_config()):
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True, "msg": "已在后台执行，结果稍后见 产物库/产出/ 与任务列表"})

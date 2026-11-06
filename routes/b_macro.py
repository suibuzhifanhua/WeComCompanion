# -*- coding: utf-8 -*-
"""routes.b_macro — 任务宏 API（存档/重放/删除）
"""
# -*- coding: utf-8 -*-
import json
import os
import time

from flask import Blueprint, request, Response, jsonify, stream_with_context

from core_utils import log_event, workspace_root
from core.config_store import load_config
from core.result import is_err, err_msg   # v1.0.655 B5

from version import get as _version_get  # v1.0.510（P2-13）：版本兜底块收敛到 version.get
VERSION = _version_get()


bp = Blueprint("mgmt_macro", __name__)

# ---- v1.0.373 任务宏（体验C9）：成功任务的工具调用序列存档/重放 ----
def _macro_dir():
    d = os.path.join(workspace_root(), "宏")
    os.makedirs(d, exist_ok=True)
    return d


@bp.route("/macro/list", methods=["GET"])
def api_macro_list():
    items = []
    try:
        for fn in sorted(os.listdir(_macro_dir()), reverse=True):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(_macro_dir(), fn), encoding="utf-8") as f:
                    d = json.load(f)
                items.append({"id": fn[:-5], "name": d.get("name", fn[:-5]),
                              "created": d.get("created", ""), "steps": len(d.get("calls") or [])})
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": True, "macros": items})


@bp.route("/macro/save", methods=["POST"])
def api_macro_save():
    from agent_tools import dispatch_tool   # noqa: F401  (确保执行环境一致)
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name") or "").strip()[:60]
    calls = data.get("calls") or []
    if not name:
        return jsonify({"ok": False, "error": "缺少宏名称"}), 400
    if not isinstance(calls, list) or not calls:
        return jsonify({"ok": False, "error": "没有可保存的工具调用序列（任务需成功完成）"}), 400
    clean = [{"name": str(c.get("name") or "")[:60],
              "args": c.get("args") if isinstance(c.get("args"), dict) else {}}
             for c in calls[:200] if isinstance(c, dict) and c.get("name")]
    clean = [c for c in clean if c["name"] not in ("finish", "ask_user")]
    if not clean:
        return jsonify({"ok": False, "error": "序列里没有可重放的工具调用"}), 400
    import datetime
    mid = "macro_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    rec = {"id": mid, "name": name, "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "calls": clean}
    with open(os.path.join(_macro_dir(), mid + ".json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    log_event(f"[macro] 已保存任务宏「{name}」（{len(clean)} 步）")
    return jsonify({"ok": True, "id": mid, "steps": len(clean)})


@bp.route("/macro/del", methods=["POST"])
def api_macro_del():
    data = request.get_json(force=True, silent=True) or {}
    mid = str(data.get("id") or "")
    if not mid or "/" in mid or "\\" in mid or not mid.startswith("macro_"):
        return jsonify({"ok": False, "error": "非法 id"}), 400
    p = os.path.join(_macro_dir(), mid + ".json")
    if os.path.isfile(p):
        os.remove(p)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "宏不存在"}), 404


@bp.route("/macro/run", methods=["POST"])
def api_macro_run():
    """重放宏：按序列逐个 dispatch（无 LLM 规划）。SSE 流式返回每步结果。

    v1.0.616（审查 P0）：重放原先**完全绕过权限闸门**——录好的写操作（点通过/提交/
    发消息）被逐个直接 dispatch，「每次询问」档与 `_ALWAYS_ASK`（不可撤销的真实业务
    提交）在这条通道上全部失效，等于给安全策略开了第二条不带闸的门。现在每一步与
    agent 走同一个 `gate_write_tool`：需要确认时推 `approval_request` + `macro_paused(i)`
    并停流；前端批准后带 `{"id":…, "start": i}` 从**未执行的那一步**续跑——不是从头
    重放（从头重放会把已经点过的「通过」再点一次）。"""
    from agent_tools import dispatch_tool
    from agent.loop_guard import is_read_tool
    from security_ops import gate_write_tool, begin_approval
    from core_utils import ocr_ready_or_install  # noqa: F401  预热提示：工具内部自会处理
    data = request.get_json(force=True, silent=True) or {}
    mid = str(data.get("id") or "")
    if not mid or "/" in mid or "\\" in mid or not mid.startswith("macro_"):
        return jsonify({"ok": False, "error": "非法 id"}), 400
    try:
        start = max(0, min(int(data.get("start") or 0), 100000))
    except (TypeError, ValueError):
        start = 0
    p = os.path.join(_macro_dir(), mid + ".json")
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"宏读取失败：{e}"}), 404
    calls = rec.get("calls") or []
    cfg = load_config()

    def _gen():
        ok_n, fail_n = 0, 0
        yield f"data: {json.dumps({'type': 'macro_start', 'total': len(calls), 'from': start, 'name': rec.get('name', '')}, ensure_ascii=False)}\n\n"
        for i, c in enumerate(calls):
            if i < start:
                continue          # 续跑：已执行过的前缀不再执行（防重复点击）
            try:
                _gate = gate_write_tool(c.get("name"), c.get("args") or {}, cfg,
                                        None, is_read=is_read_tool(c.get("name") or ""))
                if isinstance(_gate, dict) and _gate.get("__approval_needed__"):
                    log_event(f"[macro] 第 {i + 1} 步 {c.get('name')} 需用户确认，重放停在此处")
                    # v1.0.652（审计 P0-1）：nonce 只进 SSE（弹窗回传），grant 双因子校验
                    _gate_nonce = begin_approval(_gate.get('hash', ''))
                    yield f"data: {json.dumps({'type': 'approval_request', 'kind': _gate.get('kind'), 'tool': _gate.get('tool', ''), 'hash': _gate.get('hash', ''), 'nonce': _gate_nonce, 'reason': _gate.get('reason', ''), 'command': _gate.get('command', ''), 'path': _gate.get('path', '')}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'macro_paused', 'i': i, 'total': len(calls), 'name': c.get('name', '')}, ensure_ascii=False)}\n\n"
                    return
                if is_err(_gate):
                    fail_n += 1
                    yield f"data: {json.dumps({'type': 'macro_step', 'i': i + 1, 'total': len(calls), 'name': c.get('name', ''), 'summary': '被拒绝：' + err_msg(_gate)[:220], 'ok': False}, ensure_ascii=False)}\n\n"
                    continue
                out = dispatch_tool(c["name"], c.get("args") or {}, cfg)
                bad = is_err(out)   # v1.0.655 B5 单谓词
                summary = ("失败：" + err_msg(out)) if bad \
                    else json.dumps(out, ensure_ascii=False)[:200]
                if bad:
                    fail_n += 1
                else:
                    ok_n += 1
                yield f"data: {json.dumps({'type': 'macro_step', 'i': i + 1, 'total': len(calls), 'name': c['name'], 'summary': summary[:300], 'ok': not bad}, ensure_ascii=False)}\n\n"
            except Exception as e:  # noqa: BLE001
                fail_n += 1
                yield f"data: {json.dumps({'type': 'macro_step', 'i': i + 1, 'total': len(calls), 'name': c.get('name', ''), 'summary': f'执行异常：{e}'[:300], 'ok': False}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'macro_done', 'ok': ok_n, 'fail': fail_n}, ensure_ascii=False)}\n\n"

    return Response(stream_with_context(_gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- v1.0.478 修复：产物库「本月自动化月报」数据源 ----------------
# 背景：02-nav-quicklinks.js 切 workspace 视图时调用 loadMonthlyStats()，但该前端函数
# 从未落地（v1.0.405 M6 补齐时漏掉）→ 同步 ReferenceError 连坐 loadMacroList，
# 「本月自动化月报」与「任务宏」两卡片永远停在「加载中…」。
# ---------------- v1.0.650（F3，用户"全部优化"）：不通过清单推群 ----------------
# 缺口本来不是"没数据、没通道"：月报判定逐机构明细在 台账/月报判定.json 里，
# webhook 通道也在（设置里填群机器人地址），缺的是**从结果到发出去那一步**——
# 提示词跑到 finish 就停，界面上没有任何"把这张清单发出去"的入口，用户只能手抄。
# 口径（用户未指定推哪个群/什么阈值，所以一律走显式）：
#   · 只推**用户点出来**的这一轮，绝不自动推、不进定时任务、不看阈值触发；
#   · 点按钮先看后发（confirm 里给全文预览），确认才发；
#   · 只用群机器人 webhook 发消息，一次企微页面都不碰（零前台抢占、零点击）。
_REJECT_CHUNK_CHARS = 1200      # 企微 text 消息上限 2048 字节，中文按 ~3 字节/字留足余量
_REJECT_MAX_MSGS = 8            # 单次最多发 8 条，超了如实报"还有多少条没推"


def _monthly_runs():
    """台账/月报判定.json 里的 runs（结构与 monthly_stats 同一份数据源）。"""
    hp = os.path.join(workspace_root(), "台账", "月报判定.json")
    if not os.path.isfile(hp):
        return []
    from core.files import read_json   # v1.0.654 A8：句柄样板收敛
    d = read_json(hp)
    runs = d.get("runs") if isinstance(d, dict) else None
    return runs if isinstance(runs, list) else []


def reject_lines(results):
    """把一轮判定结果里 verdict=red 的条目排成可发群的行（纯函数，便于测试）。
    机构名取 title，原因取 reason，附带 checks 里未通过的项——与月报正文同一口径。"""
    lines = []
    for it in (results or []):
        if str(it.get("verdict") or "").lower() != "red":
            continue
        title = str(it.get("title") or "").strip() or "（无标题）"
        reason = str(it.get("reason") or "").strip()
        bad = [str(c.get("item") or "").strip()
               for c in (it.get("checks") or [])
               if isinstance(c, dict) and c.get("pass") is False]
        bad = [b for b in bad if b]
        line = "· " + title
        if reason:
            line += "｜" + reason
        if bad:
            line += "｜未通过项：" + "、".join(bad[:4])
        lines.append(line[:300])
    return lines


def chunk_lines(lines, size=_REJECT_CHUNK_CHARS):
    """按字符数把行拼成若干条消息（不拆行——一条机构记录被截半比不推更糟）。"""
    out, cur, cur_len = [], [], 0
    for ln in lines:
        if cur and cur_len + len(ln) + 1 > size:
            out.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


@bp.route("/audit/reject_list", methods=["GET"])
def api_audit_reject_list():
    """预览：最近一轮（或指定 ts）判为不通过的机构清单。"""
    runs = _monthly_runs()
    want_ts = str(request.args.get("ts") or "").strip()
    run = None
    for r in reversed(runs):
        if not want_ts or str(r.get("ts", "")) == want_ts:
            run = r
            break
    if not run:
        return jsonify({"ok": False, "error": "没有找到对应的月报判定记录（本月还没跑过？）"}), 404
    lines = reject_lines(run.get("results"))
    return jsonify({"ok": True, "ts": str(run.get("ts") or ""), "total": len(run.get("results") or []),
                    "red": len(lines), "lines": lines})


@bp.route("/audit/push_rejects", methods=["POST"])
def api_audit_push_rejects():
    """把不通过清单推到群里（用户点一次推一次；发送前后都留痕）。"""
    data = request.get_json(force=True, silent=True) or {}
    want_ts = str(data.get("ts") or "").strip()
    runs = _monthly_runs()
    run = None
    for r in reversed(runs):
        if not want_ts or str(r.get("ts", "")) == want_ts:
            run = r
            break
    if not run:
        return jsonify({"ok": False, "error": "没有找到对应的月报判定记录"}), 404
    lines = reject_lines(run.get("results"))
    if not lines:
        return jsonify({"ok": False, "error": "这一轮没有判定为不通过的机构，无需推送"}), 400
    cfg = load_config()
    hook = str(((cfg.get("wecom") or {}).get("webhook")) or "").strip()
    if not hook:
        return jsonify({"ok": False, "error": "还没配置群机器人地址（设置 → 企业微信 → 群机器人 webhook）"}), 400
    groups = chunk_lines(lines)
    if len(groups) > _REJECT_MAX_MSGS:
        return jsonify({"ok": False, "error": f"清单要分 {len(groups)} 条才发得完，超过单次上限 "
                                             f"{_REJECT_MAX_MSGS} 条——请先在月报正文里按机构筛选后再推"}), 400
    import wecom_bridge as wb
    head = f"【月报自查·不通过清单】{run.get('ts') or ''}（共 {len(lines)} 家）"
    sent = 0
    for i, g in enumerate(groups, 1):
        body = head + (f"｜{i}/{len(groups)}" if len(groups) > 1 else "") + "\n" + g
        r = wb.send_webhook(hook, body)
        if is_err(r):
            log_event(f"[audit] 不通过清单推送失败（第 {i} 条）：{err_msg(r)}")
            return jsonify({"ok": sent > 0, "sent": sent, "total_msgs": len(groups),
                            "error": f"第 {i} 条发送失败：{err_msg(r)}"
                                      + ("（前面的已发出，重试会重复推送，请先到群里确认）" if sent else "")}), 502
        sent += 1
    log_event(f"[audit] 不通过清单已推群：{sent} 条 / {len(lines)} 家")
    return jsonify({"ok": True, "sent": sent, "items": len(lines), "ts": str(run.get("ts") or "")})


@bp.route("/audit/monthly_stats", methods=["GET"])
def api_audit_monthly_stats():
    """本月自动化月报统计：从 产物库/台账/月报判定.json 聚合当月 runs。"""
    try:
        hp = os.path.join(workspace_root(), "台账", "月报判定.json")
        runs = []
        if os.path.isfile(hp):
            from core.files import read_json   # v1.0.654 A8：句柄样板收敛
            d = read_json(hp)
            if isinstance(d, dict) and isinstance(d.get("runs"), list):
                runs = d["runs"]
        month_prefix = time.strftime("%Y-%m")
        month_runs = [r for r in runs if str(r.get("ts", "")).startswith(month_prefix)]
        green = red = other = 0
        for r in month_runs:
            for it in (r.get("results") or []):
                v = (it.get("verdict") or "").lower()
                if v == "green":
                    green += 1
                elif v == "red":
                    red += 1
                else:
                    other += 1
        last = runs[-1] if runs else None
        return jsonify({
            "ok": True,
            "month": month_prefix,
            "runs": len(month_runs),
            "audited": green + red + other,
            "green": green, "red": red, "other": other,
            "last_ts": (last or {}).get("ts", ""),
            "last_total": len((last or {}).get("results") or []),
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:120]})

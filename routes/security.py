"""routes/security — 安全中心（v1.0.320）：配置/审计/审批令牌。蓝图 url_prefix=/api。"""

from flask import Blueprint, request, jsonify

from core.config_store import load_config, update_config, _save_config_safe  # noqa: F401（_save_config_safe 兼容旧导入）
from core_utils import log_event
import security_ops

bp = Blueprint("security", __name__, url_prefix="/api")



@bp.route("/security/config", methods=["GET"])
def api_sec_config():
    cfg = load_config()
    sec = security_ops.sec_cfg(cfg)
    return jsonify({"ok": True, "security": sec,
                    "audit": security_ops.audit_read(20),
                    "audit_path": security_ops.audit_jsonl()})


@bp.route("/security/config", methods=["POST"])
def api_sec_config_post():
    data = request.get_json(force=True, silent=True) or {}
    sec_in = data.get("security")
    if not isinstance(sec_in, dict):
        return jsonify({"ok": False, "error": "缺少 security 对象"}), 400
    # v1.0.555（P0-10）：输入校验纯函数化（不依赖 cfg）→ 通过后进 update_config
    # 锁内读-改-写事务。原 load→改→save 无整体锁：两个并发写（或一个慢写与一个
    # 用户写交叉）即后写覆盖先写——安全策略/模型/连接器凭据静默丢失
    # （config.json 是凭据与安全策略的唯一事实源，此类丢失属于数据事故）。
    if "command_mode" in sec_in and sec_in["command_mode"] not in ("block", "ask", "pass"):
        return jsonify({"ok": False, "error": "command_mode 只能是 block/ask/pass"}), 400
    # v1.0.590 权限级别（询问范围轴）：必须校验后落盘——本路由按白名单逐键写入，
    # 漏加就会「保存成功但设置被静默丢弃」（v1.0.555 P0-10 同型坑）
    if "permission_level" in sec_in and sec_in["permission_level"] not in security_ops.PERMISSION_LEVELS:
        return jsonify({"ok": False, "error": "permission_level 只能是 ask_every/auto/full"}), 400
    for k in ("cmd_blacklist", "cmd_whitelist", "protected_paths"):
        if k in sec_in:
            v = sec_in[k]
            if isinstance(v, str):
                v = [l.strip() for l in v.splitlines() if l.strip()]
            if not isinstance(v, list):
                return jsonify({"ok": False, "error": f"{k} 应为字符串数组"}), 400
            # v1.0.616（审查 P1）：黑白名单是**正则**，但命中处写的是
            # `try: re.compile(pat) except re.error: continue`（security_ops.py:346/372）
            # —— 一字写错的规则从不生效、也从不报错，安全策略呈现"保存成功"假象
            # （与 kb_auto 写错键名同型）。保存前逐条试编译，指出第几行。
            if k != "protected_paths":
                import re as _re
                for _i, _p in enumerate(v, 1):
                    try:
                        _re.compile(str(_p), _re.I)
                    except _re.error as e:
                        return jsonify({"ok": False,
                                        "error": f"{k} 第 {_i} 行不是合法正则：{str(_p)[:60]}（{e}）"}), 400
    if "delete_confirm_limit" in sec_in:
        try:
            int(sec_in["delete_confirm_limit"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "删除审批阈值应为数字"}), 400
    if "backup_limit_mb" in sec_in:
        try:
            int(sec_in["backup_limit_mb"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "备份上限应为数字（MB）"}), 400

    def _mutate(cfg):
        sec = security_ops.sec_cfg(cfg)
        if "command_mode" in sec_in:
            sec["command_mode"] = sec_in["command_mode"]
        if "permission_level" in sec_in:
            sec["permission_level"] = sec_in["permission_level"]
        for k in ("cmd_blacklist", "cmd_whitelist", "protected_paths"):
            if k in sec_in:
                v = sec_in[k]
                if isinstance(v, str):
                    v = [l.strip() for l in v.splitlines() if l.strip()]
                sec[k] = [str(x)[:200] for x in v[:50]]
        if "delete_confirm_limit" in sec_in:
            sec["delete_confirm_limit"] = max(1, min(100, int(sec_in["delete_confirm_limit"])))
        if "auto_backup" in sec_in:
            sec["auto_backup"] = bool(sec_in["auto_backup"])
        if "backup_limit_mb" in sec_in:
            sec["backup_limit_mb"] = max(50, min(10240, int(sec_in["backup_limit_mb"])))
        cfg["security"] = sec
        return True

    if not update_config(_mutate):
        return jsonify({"ok": False, "error": "保存失败：配置写入异常（详见日志）"}), 500
    log_event("[security] 配置已更新（update_config 事务）")
    return jsonify({"ok": True})


@bp.route("/security/grant", methods=["POST"])
def api_security_grant():
    """审批批准：发放一次性令牌（agent 原样重试时安全层放行）。
    v1.0.652（审计 P0-1）：必须出示弹窗 SSE 事件里的 nonce——hash 是可自算的，
    单凭 hash 发令牌等于允许模型/本机进程自铸通行证绕过审批闸。"""
    data = request.get_json(force=True, silent=True) or {}
    h = str(data.get("hash") or "").strip()
    nonce = str(data.get("nonce") or "").strip()
    if not h:
        return jsonify({"ok": False, "error": "缺少 hash"}), 400
    if not security_ops.grant(h, nonce):
        return jsonify({"ok": False,
                        "error": "审批凭据无效或已过期（需从审批弹窗「批准」按钮提交）"}), 400
    log_event(f"[security] 用户批准操作 {h}")
    return jsonify({"ok": True})


@bp.route("/security/audit/clear", methods=["POST"])
def api_sec_audit_clear():
    security_ops.audit_clear()
    return jsonify({"ok": True})


@bp.route("/security/audit/export", methods=["POST"])
def api_sec_audit_export():
    p = security_ops.audit_export()
    return jsonify({"ok": True, "path": p})

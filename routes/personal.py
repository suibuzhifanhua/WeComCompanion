"""routes/personal — 个人信息与后台任务开关（v1.0.614）

补的是优化审查报告里"有配置没 UI"那一类（㉙㉚㉛）：
  · 报销人姓名 / 银行账号 / 开户行 只存在于 config.json，而快捷入口的提示词写着
    "个人信息在设置里"——用户找不到，只能手编 JSON（且这些值是 DPAPI 加密的，手编极易写坏）；
  · dsh（PC 接管）装完 800MB 之后仍缺 base_url/api_key，组件体检还指向一个不存在的界面；
  · digests.daily / kb_auto / todo_watch 三个会**自己花钱**的后台任务没有开关，用户既看不出
    是谁在耗额度也关不掉。

加密不用在这里做：`save_config` 落盘前统一走 `security._walk_secrets`——
`api_key` 命中键名名单、`bank_account/bank_name` 命中键名名单、`reimburse.name`
命中路径名单（security.py:32），所以只要写进这几个键就自动 DPAPI 加密。

字段名必须与消费方一致：kb_auto 读的是 organize/digest 两个开关
（services/knowledge_ops.py:438 起），不是 enabled——按 enabled 写等于写了个没人读
的键，保存成功但后台照旧跑。
"""

from flask import Blueprint, request, jsonify

from core.config_store import load_config, update_config, AGENT_SOFT_BUDGET_DEFAULTS
from core_utils import log_event

bp = Blueprint("personal", __name__, url_prefix="/api")

# 每个字段都限长：这些值会被拼进发给大模型的提示词（rule_check 的报销默认值）
_MAX = {"name": 40, "bank_account": 48, "bank_name": 60, "base_url": 200, "api_key": 200,
        "cwd": 260, "extra_args": 200}


def _still_cipher(v):
    """运行期仍以 enc: 开头 = DPAPI 没解开（配置来自别的 Windows 用户/机器，
    实测错误码 0x8009002B）。这种值既不能给用户看，也不能被"限长截断"或"留空覆盖"
    毁掉——它是唯一的一份密文（security._unprotect_secret 的保留密文原则，v1.0.590）。"""
    return str(v or "").startswith("enc:")


def _budget_num(v, default):
    """软预算取值：0 是合法值（=该项关闭，v1.0.622 定下的口径，不能被 `or 默认` 吞掉），
    负数按 0，非数字/缺省回落到 AGENT_SOFT_BUDGET_DEFAULTS，上限 10 亿防手滑。"""
    if v is None:
        return int(default)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return int(default)
    return max(0, min(1_000_000_000, n))


@bp.route("/personal/config", methods=["GET"])
def api_personal_get():
    cfg = load_config()
    r = cfg.get("reimburse") if isinstance(cfg.get("reimburse"), dict) else {}
    d = cfg.get("dsh") if isinstance(cfg.get("dsh"), dict) else {}
    dg = (cfg.get("digests") or {}).get("daily") or {}
    ka = cfg.get("kb_auto") if isinstance(cfg.get("kb_auto"), dict) else {}
    tw = cfg.get("todo_watch") if isinstance(cfg.get("todo_watch"), dict) else {}
    ag = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    rb_out, enc_fields = {}, []
    for k in ("name", "bank_account", "bank_name"):
        v = str(r.get(k) or "")
        if _still_cipher(v):
            enc_fields.append(k)
            rb_out[k] = ""            # 密文不进 DOM（进了一次就等着被 48 字截断写回去）
        else:
            rb_out[k] = v
    rb_out["mask_for_llm"] = bool(r.get("mask_for_llm", False))
    rb_out["encrypted_fields"] = enc_fields
    key = str(d.get("api_key") or "")
    return jsonify({
        "ok": True,
        "reimburse": rb_out,
        # 凭据只回"有没有 + 尾巴几位"，不把明文/密文再往外抄一遍；解不开时尾号也不给
        # （密文末 4 位既无辨识意义又泄露密文片段）
        "dsh": {"base_url": ("" if _still_cipher(d.get("base_url")) else str(d.get("base_url") or "")),
                "api_key_set": bool(key.strip()),
                "api_key_tail": ("" if _still_cipher(key) else key[-4:]),
                "api_key_unreadable": _still_cipher(key),
                # v1.0.616：cwd/extra_args 也是"有配置没 UI"——dsh 的工作目录与附加参数
                # 此前只能手编 JSON（而 JSON 里同段还有加密值，手编极易写坏）
                "cwd": str(d.get("cwd") or ""),
                "extra_args": str(d.get("extra_args") or "")},
        "background": {"digests_daily": bool(dg.get("enabled", True)),
                       "digests_time": str(dg.get("time") or "18:30"),
                       "kb_organize": bool(ka.get("organize", True)),
                       "kb_digest": bool(ka.get("digest", True)),
                       "todo_watch": bool(tw.get("enabled", False)),
                       "todo_interval": _interval(tw.get("interval_min"))},
        # v1.0.650（F4）：任务资源软预算——后端一直在读 cfg["agent"]，界面此前没有入口。
        # 下发的是**生效值**（缺失/坏值已回落到代码默认），所见即 agent 实际用的数字。
        "budget": {k: _budget_num(ag.get(k), v) for k, v in AGENT_SOFT_BUDGET_DEFAULTS.items()},
    })


def _hhmm(v):
    s = str(v or "").strip()
    a, _, b = s.partition(":")
    return s if a.isdigit() and b.isdigit() and 0 <= int(a) < 24 and 0 <= int(b) < 60 else None


def _interval(v):
    """与消费方 services/todo_watch.py:_cfg 同口径（max(2, int(... or 10))）——
    两边算出不同的话，界面显示 10 分钟而实际按别的值在轮询。"""
    try:
        return max(2, int(v or 10))
    except (TypeError, ValueError):
        return 10


@bp.route("/personal/config", methods=["POST"])
def api_personal_post():
    """逐键合并写入。空串表示"清空该字段"；api_key 缺省不覆盖（避免只改名字时把凭据抹掉——
    与 v1.0.588 P1-13 连接器密码同一口径）。"""
    data = request.get_json(force=True, silent=True) or {}
    rb = data.get("reimburse") if isinstance(data.get("reimburse"), dict) else None
    ds = data.get("dsh") if isinstance(data.get("dsh"), dict) else None
    bg = data.get("background") if isinstance(data.get("background"), dict) else None
    bd = data.get("budget") if isinstance(data.get("budget"), dict) else None
    if rb is None and ds is None and bg is None and bd is None:
        return jsonify({"ok": False, "error": "没有可保存的内容"}), 400
    if bg and bg.get("digests_time") is not None and _hhmm(bg["digests_time"]) is None:
        return jsonify({"ok": False, "error": "小结时间应为 HH:MM"}), 400

    def _mutate(cfg):
        if rb is not None:
            r = cfg.setdefault("reimburse", {})
            for k in ("name", "bank_account", "bank_name"):
                if k not in rb:
                    continue
                new = str(rb.get(k) or "").strip()[:_MAX[k]]
                if not new and _still_cipher(r.get(k)):
                    # 空值 + 存的是本机解不开的密文 → 不动。前端这时输入框本就是空的
                    # （密文不下发），按普通"清空"处理会把唯一一份密文截成空串（v1.0.590 同型事故）。
                    log_event(f"[personal] {k} 存的是本机解不开的密文，空值提交已忽略（要换新值直接填）")
                    continue
                r[k] = new
            if "mask_for_llm" in rb:
                r["mask_for_llm"] = bool(rb["mask_for_llm"])
        if ds is not None:
            d = cfg.setdefault("dsh", {})
            if "base_url" in ds:
                d["base_url"] = str(ds.get("base_url") or "").strip()[:_MAX["base_url"]]
            for _k in ("cwd", "extra_args"):
                if _k in ds:
                    d[_k] = str(ds.get(_k) or "").strip()[:_MAX[_k]]
            if str(ds.get("api_key") or "").strip():        # 只有真给了新值才覆盖
                d["api_key"] = str(ds["api_key"]).strip()[:_MAX["api_key"]]
            elif ds.get("clear_key"):
                d.pop("api_key", None)
        if bg is not None:
            if "digests_daily" in bg or "digests_time" in bg:
                dg = cfg.setdefault("digests", {}).setdefault("daily", {})
                if "digests_daily" in bg:
                    dg["enabled"] = bool(bg["digests_daily"])
                t = _hhmm(bg.get("digests_time"))
                if t:
                    dg["time"] = t
            if "kb_organize" in bg or "kb_digest" in bg:
                ka = cfg.setdefault("kb_auto", {})
                if "kb_organize" in bg:
                    ka["organize"] = bool(bg["kb_organize"])
                if "kb_digest" in bg:
                    ka["digest"] = bool(bg["kb_digest"])
            if "todo_watch" in bg:
                cfg.setdefault("todo_watch", {})["enabled"] = bool(bg["todo_watch"])
            if "todo_interval" in bg:
                # 与消费方同口径钳位（≥2 分钟），界面文案就不再会与实际轮询间隔不一致
                cfg.setdefault("todo_watch", {})["interval_min"] = _interval(bg["todo_interval"])
        if bd is not None:
            # v1.0.650（F4）：只认 AGENT_SOFT_BUDGET_DEFAULTS 里的键（白名单，逐键合并），
            # 与 feature_flags 那条"三处各写一遍必漏一处"的教训同口径——不然界面能写进
            # 任意 cfg["agent"] 键，而 agent 一个都不读，存的只是幻觉。
            a = cfg.setdefault("agent", {})
            for k, default in AGENT_SOFT_BUDGET_DEFAULTS.items():
                if k in bd:
                    a[k] = _budget_num(bd.get(k), default)
        return True

    try:
        changed = update_config(_mutate)
    except Exception as e:  # noqa: BLE001
        log_event(f"[personal] 保存失败：{e}")
        return jsonify({"ok": False, "error": str(e)[:160]}), 500
    if not changed:
        return jsonify({"ok": False, "error": "配置写入异常（详见日志）"}), 500
    log_event("[personal] 个人信息/后台开关已更新（银行账号与凭据按 DPAPI 加密落盘）")
    return jsonify({"ok": True})

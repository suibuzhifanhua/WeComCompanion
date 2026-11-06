"""
config_store — 配置持久化（config.json 加解密/加载/保存 + 经验库 + 连接器脱敏）
====================================================================
从 backend.py 抽离（T03）：APP_PORT / DEFAULT_CONFIG / _config_lock /
_encrypt_secrets / _decrypt_secrets / load_config / _has_plaintext_secret /
save_config / 经验库四函数 / _mask_connector_config / _save_config_safe。
"""
import json
import os
import copy
import threading
import datetime
import uuid

from core_utils import config_path, log_event
from core.secrets import (_is_sensitive_key, _walk_secrets,
                      _protect_secret, _unprotect_secret, _SENSITIVE_PATHS)

# 本地服务端口
APP_PORT = 8787

# 功能管理总闸：键名的唯一事实源（v1.0.617）。
# 过去这三份名单各写各的字面量——路由白名单（routes/b_admin.py）、UI 数组
# （js/11-workspace-kb-study.js::_FF_KEYS）、本文件的默认值段，加一个开关要改三处，
# 漏一处就是"保存成功但静默不生效"（本项目复发率最高的一类：kb_auto 键名写错、
# permission_level 漏进白名单，都是同一形状的病）。现在白名单与默认值都从这里取，
# UI 那份由 tests/test_v617_flags.py 做集合相等断言锁住（改键名不改锁 → 测试变红）。
# 每行 = (键名, 默认值, 是否进设置页 UI)。默认值一律取该键**消费方原本的兜底值**，
# 不做任何行为改变：self_repair.py:916 读 self_repair_daily 兜 True、:82/:853 读
# self_auto_fix 兜 False、backend.py:748 读 auto_save_dialogs 兜 False。
FEATURE_FLAGS = (
    ("skills", True, True),
    ("connectors", True, True),
    ("team", True, True),
    ("memory", True, True),
    ("evolve", True, True),
    ("self_repair_daily", True, True),
    # 危险开关（会自动改源码），UI 默认不勾选、只有显式 True 才算开
    ("self_auto_fix", False, True),
    # 另存为对话框自动应答：用户口径是「弹窗零容忍」，故意**不给复选框**，只留 config.json
    # 手改的后门。以前它连默认值都没有，属于"消费方在读、名单里查无此键"的暗键。
    ("auto_save_dialogs", False, False),
)
FEATURE_FLAG_KEYS = tuple(k for k, _d, _ui in FEATURE_FLAGS)
FEATURE_FLAG_DEFAULTS = {k: d for k, d, _ui in FEATURE_FLAGS}
FEATURE_FLAG_UI_KEYS = tuple(k for k, _d, ui in FEATURE_FLAGS if ui)
# 允许被 POST /api/config 写入的键 = 进 UI 的那 7 个。**故意不含 auto_save_dialogs**：
# 它开的是"另存为对话框自动应答"（替用户点系统弹窗里的按钮），用户口径是弹窗零容忍，
# 只能自己手改 config.json 才生效——留进白名单等于给任何持 token 的调用方（含 agent 通路）
# 一个"让软件去点弹窗"的开关。
FEATURE_FLAG_WRITABLE_KEYS = FEATURE_FLAG_UI_KEYS
# 需要"缺省即关"语义的键（UI 勾选逻辑用；其余键保持"缺省即开"的历史语义）
FEATURE_FLAG_OFF_BY_DEFAULT = tuple(k for k, d, _ui in FEATURE_FLAGS if not d)

# 任务资源软预算默认值（v1.0.622 起的口径，原先住在 agent/loop.py 里）：
# 提到这里是为了让「设置」页与 agent 读同一份数字——以前只有代码知道默认值，
# 界面上看不见、也改不了（用户要调只能手编 config.json，而那份 JSON 里连 `agent` 段
# 都没声明，按本文件规矩"顶层段不声明会被 save 抹掉"，手改还会静默丢失）。
# 0 = 该项不参与判定（不是"用默认值"，v1.0.622 修的就是这个吞 0 的坑）。
AGENT_SOFT_BUDGET_DEFAULTS = {"soft_budget_tokens": 400000, "soft_budget_minutes": 120,
                              "soft_budget_tokens_per_item": 200000,
                              "soft_budget_minutes_per_item": 15}

# 默认配置：字段缺失/类型错误时由 load_config 合并兜底
DEFAULT_CONFIG = {
    "wecom_archive": {"enabled": True, "consent": False},   # v1.0.477 静默分析；v1.0.588 加 consent 授权闸（未授权不解析聊天文件）
    "schema_version": 2,
    "wecom": {
        "exe_path": "",
        "cdp_port": 9222,
        "auto_launch": True,
        "auto_cdp": True,
        # v1.0.523（用户需求）：静默后台执行——任务全流程不抢前台焦点。
        # 覆盖：tucker 收纳扩到企微名下全部新窗口（1.2s 快速收纳 + CDP 停摆
        # Win32 兜底）、截图不前置（走 DOM 降级）、OS 级发消息等用户空闲、
        # run_js 弹窗 API 拦截、定时任务完成不弹通知、企微重启后自动最小化。
        "silent_mode": True,
        "bound_pid": None,
        "bound_sig": None,
    },
    "models": [],
    # v1.0.303：原为 None——磁盘 config "templates": null 时合并后仍为 None，
    # /api/templates 返回 JSON null，前端 templates.length 直接抛
    # "Cannot read properties of null (reading 'length')"（任务模板页加载失败 P0）。
    # 改 [] 后：list 默认值分支在磁盘为 null（非 list）时保留默认 []，自动修复脏数据。
    "templates": [],
    # None = 全部技能默认启用（load_config 合并时保持 None 即全开）；
    # 仅当用户显式保存启用列表（或保存空数组 = 全部关闭）时才为 list。
    "enabled_skills": None,
    "skill_overrides": {},
    # 自定义技能（v1.0.303）：技能中心用户自建 [{id,name,desc,icon,prompt,tools,judge,default}]。
    # 必须在此声明——同 dsh/judge_model_id 的坑：顶层段不声明，磁盘上的段会在任意
    # save_config（含明文自愈重写）时被 load_config 合并逻辑静默抹掉（v1.0.300 实测教训）。
    "custom_skills": [],
    "connectors": {},
    "quick_links": [],
    # 双模型分工（v1.0.234）：判断大模型 id；空串=纯单模型。
    # 必须在此声明，否则 load_config 合并兜底只遍历 DEFAULT_CONFIG 的 key，
    # 磁盘里这个新顶层字段会被静默丢弃，导致 /api/status 永远 dual_model:false。
    "judge_model_id": "",
    # dsh 接管通道（v1.0.298 引入 / v1.0.300 补声明）：模型端点/凭据/工作目录/附加参数。
    # 必须在此声明——同 judge_model_id 的坑：不在 DEFAULT_CONFIG 里，磁盘上的 dsh 段
    # 每次 load_config 后 save_config 都会被静默抹掉（2026-09-10 实测：用户填过的
    # dist config dsh 段被运行时保存抹掉，凭据永远存不住）。api_key 由 _walk_secrets
    # 按 key 名自动走 DPAPI 加密落盘。
    "dsh": {
        "base_url": "",
        "api_key": "",
        "cwd": "",
        "extra_args": "",
    },
    # 知识库后台维护（v1.0.301）：空闲整理（INDEX+查重，保守不动文件）+ 每日 AI 消化。
    # 必须在此声明（顶层段不声明会被 load/save 抹掉——v1.0.300 实测教训）。
    "kb_auto": {
        "organize": True,
        "digest": True,
    },
    # 公共技能库（v1.0.309）：url=远程清单地址（gitee/github raw 等可 GET 的 JSON），
    # 空=只用内置快照（skill_hub_data.HUB_SKILLS，离线可用）。必须声明，否则被 save_config 抹段。
    "skill_hub": {
        "url": "",
    },
    # 功能管理总开关（v1.0.312）：键名与默认值都来自本文件顶部的 FEATURE_FLAGS（唯一事实源）。
    # skills=技能总闸 / connectors=连接器 / team=👥 智能体团队 / memory=记忆沉淀 / evolve=技能自进化 /
    # self_repair_daily=每日体检 / self_auto_fix=自动改源码（默认关）/ auto_save_dialogs=另存为自动应答（默认关、不进 UI）
    "feature_flags": dict(FEATURE_FLAG_DEFAULTS),
    # v1.0.523-3：每日工作小结（产物库/日报/工作小结_YYYY-MM-DD.md，静默生成）
    "digests": {
        "daily": {"enabled": True, "time": "18:30"},
    },
    # v1.0.523-4：待办变更轻提醒（托盘气泡；默认关——需企微已绑定且开着待办页）
    "todo_watch": {
        "enabled": False,
        "interval_min": 10,
    },
    # v1.0.650（F4）：任务资源软预算。以前这段在本文件里**根本不存在**——agent/loop.py
    # 只认代码里的默认值，用户想调只能手编 config.json；而按本文件的规矩"顶层段不声明
    # 会被 save 抹掉"，手改出来的 `agent` 段还会在一次普通保存后静默消失。现在显式声明。
    "agent": dict(AGENT_SOFT_BUDGET_DEFAULTS),
    # 安全中心（v1.0.320）：命令黑白名单与三模式/保护路径/删除审批阈值/自动备份/备份上限。
    # 必须声明，否则被 save_config 抹段。
    "security": {
        "command_mode": "block",   # 危险命令命中后的【严重度】：block 拦截 / ask 弹窗审批 / pass 放行
        # v1.0.590 权限级别：【询问范围】轴，与 command_mode 的严重度轴正交。
        #   ask_every 每次询问——所有写类操作（点页面/写文件/跑命令/发消息）都先确认
        #   auto    自动审批——只在命中风险规则时确认（默认，等同历史行为）
        #   full    完全访问——不再确认；但 command_mode=block 的地板仍生效（致命命令、
        #           保护路径写入依然直接拒绝，不会因为选了完全访问就失去兜底）
        "permission_level": "auto",
        "cmd_blacklist": [],
        "cmd_whitelist": [],
        "protected_paths": [],
        "delete_confirm_limit": 10,
        "auto_backup": True,
        "backup_limit_mb": 500,
    },
    # 用户固定规则（v1.0.313，三层记忆之「用户级规则记忆」）：设置页手编多行文本，
    # 每次对话注入 system 最前（如「始终用中文回复」「输出一律带表格」「不要动注册表」）。
    "user_rules": "",
}

_config_lock = threading.RLock()   # v1.0.361：可重入（update_config 持锁调 load_config，其自愈路径也取锁）

def _encrypt_secrets(cfg):
    """深拷贝并对所有敏感字段加密（落盘前调用），不改动原 cfg。"""
    return _walk_secrets(cfg, _protect_secret)


def _decrypt_secrets(cfg):
    """就地解密敏感字段（load_config 后调用），保留明文供运行期使用。
    v1.0.510 修复（P1-3）：原只认 enc:v1: 前缀——DPAPI 偶发失败降级写入的
    enc:weak:（security._protect_secret 非 Windows/失败路径）永远不会被解密，
    运行时拿到字面量 "enc:weak:..." 当 api_key 用，凭据静默失效且不自愈。
    现按 enc: 前缀统一交给 _unprotect_secret（它同时支持 v1/weak 两种格式）；
    解密出的明文在下次 save_config 时会自动重新加密为 enc:v1:（自愈）。"""
    def _do(o):
        if isinstance(o, dict):
            for k, v in list(o.items()):
                if isinstance(v, (dict, list)):
                    _do(v)
                elif isinstance(v, str) and v.startswith("enc:"):
                    # v1.0.588：解密改由 enc: 前缀驱动，不再要求 key 命中敏感词——
                    # 加密侧已支持路径级敏感（reimburse.name，key 本身是 "name"），
                    # 若这里仍按 key 名过滤，那段密文会永远解不开、运行时拿到密文当明文用。
                    o[k] = _unprotect_secret(v)
        elif isinstance(o, list):
            for x in o:
                _do(x)
    _do(cfg)
    return cfg


# v1.0.364 审查修复（H7）：配置进程内缓存——每个 HTTP 请求原本都全量读盘+DPAPI
# 解密+多重深拷贝（/api/status 每 8s、/wecom/windows 每 5s 都触发）。按 mtime 失效：
# 磁盘变化（本进程 save / 外部编辑）自动重新加载。缓存只加速读，写路径不变。
# v1.0.631：缓存键**必须含路径**。只有 mtime 时，两份不同文件落在同一个时间戳刻度
# 上就会互相冒领——读到另一份配置的内容。测试里每用例一份 tmp config（NTFS 时间戳
# 粒度 ~10ms，两个用例撞进同一刻度是常态级事件）；生产里 relocate / 备份回填
# （shutil.copy2 会保留原 mtime）同样能造出来。
# v1.0.634：光补路径还不够——**同一文件被连续改写时 mtime 可以完全不变**。
# 本机实测（200 次背靠背改写）：float mtime 与 st_mtime_ns 各有 152/200 次落在同一
# 刻度里（这块盘的 mtime 粒度远粗于 ns），所以换 ns 计数并不能救；st_size 则 200/200
# 都不同。现键 = (路径, mtime_ns, 字节数)，并且**本进程每次写盘后主动作废缓存**
# （等 mtime 变化是不可靠的：save_config 写完紧接着 load，同一刻度内就会读回旧配置）。
_CFG_CACHE = {"path": None, "stamp": None, "cfg": None}


def _cfg_stamp(cp):
    """配置文件的"内容版本"指纹：(mtime_ns, size)。粗粒度 mtime + 长度，两者同时
    不变才认为没变——单靠 mtime 在本机 76% 的背靠背改写里会漏。"""
    try:
        st = os.stat(cp)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (-1, -1)


def _invalidate_cfg_cache():
    """写盘之后必须调用：让下一次 load_config 真去读盘。"""
    _CFG_CACHE["path"] = None
    _CFG_CACHE["stamp"] = None
    _CFG_CACHE["cfg"] = None


def load_config():
    """v1.0.652（审计 P2-1）：缓存读-改-写全程进 _config_lock（RLock 可重入，
    update_config 持锁调本函数不死锁）。旧实现读盘→解密→写缓存全程无锁，与
    save/update 并发时：A 读到旧内容 → B 写盘并作废缓存 → A 把"旧内容+新指纹"
    写回缓存 → 陈旧配置被永久当作新鲜（用户保存的设置静默丢失）。"""
    with _config_lock:
        return _load_config_locked()


def _load_config_locked():
    _cp = config_path()
    _st = _cfg_stamp(_cp)
    if (_CFG_CACHE["cfg"] is not None and _CFG_CACHE.get("path") == _cp
            and _CFG_CACHE["stamp"] == _st):
        # v1.0.470 修复（P1-2）：缓存命中必须返回深拷贝——原实现把同一个 dict 对象
        # 返回给所有调用方，不走 update_config 的调用方（如 backend.ensure_binding_on_start
        # 直接 cfg.setdefault 改缓存）会原地污染共享状态：其他线程读到半成品配置、
        # save 失败后缓存与磁盘永久漂移。配置量小（KB 级），深拷贝开销可忽略。
        return copy.deepcopy(_CFG_CACHE["cfg"])
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        # P3-6：配置读取失败不能静默吞掉，记日志后降级为默认配置
        log_event(f"[config] 读取配置失败，已降级为默认配置：{e}")
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    # 合并默认值，保证字段齐全：wecom/connectors 深合并；models/templates 等 list 缺省兜底；
    # 脏类型（如 wecom 误存为 list）自动忽略，避免抛未捕获异常
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, default_v in merged.items():
        v = cfg.get(k)
        if isinstance(default_v, dict):
            if isinstance(v, dict):
                # 深拷贝 v 再合并，避免 merged 与 cfg 共享内嵌引用：
                # 否则 _decrypt_secrets(merged) 就地解密时会把 cfg 也污染成明文，
                # 导致下方 _has_plaintext_secret(cfg) 对原始磁盘配置永远命中、每次读取都刷日志重写
                merged[k].update(json.loads(json.dumps(v)))
        elif isinstance(default_v, list):
            if isinstance(v, list):
                merged[k] = json.loads(json.dumps(v))
        else:
            if v is not None:
                merged[k] = v
    # v1.0.405（审查 H2 修复）：未知顶层段保留合并——原实现只按 DEFAULT_CONFIG 的
    # key 合并，磁盘上未声明的顶层段被静默丢弃，随后任意一次 save_config（含明文
    # 自愈重写）就把它永久抹掉。历史上三次事故（judge_model_id v1.0.234 / dsh
    # v1.0.300 用户凭据存不住 / custom_skills v1.0.303）全是同一根因，防御靠
    # "记得在 DEFAULT_CONFIG 声明"的人记忆。现反转默认：新顶层段无需声明即可存活，
    # DEFAULT_CONFIG 仍负责已知键的缺省值与脏类型兜底（类型校验语义不变）。
    for _k in cfg.keys():
        if _k in merged or _k in ("provider", "llm_base_url", "llm_api_key",
                                   "llm_model", "wecom_cdp_url", "wecom_webhook"):
            continue
        try:
            merged[_k] = json.loads(json.dumps(cfg[_k]))   # 深拷贝，防与磁盘 cfg 共享引用
        except Exception as e:  # noqa: BLE001
            log_event(f"[config] 未知顶层段 {_k} 深拷贝失败（跳过保留）：{e}")
    # 丢弃旧版遗留字段，避免混乱
    for legacy in ("provider", "llm_base_url", "llm_api_key", "llm_model", "wecom_cdp_url", "wecom_webhook"):
        merged.pop(legacy, None)
    # 解密敏感字段（DPAPI），供运行期使用
    _decrypt_secrets(merged)

    # P1-自愈（v1.0.107）：本机配置若写于 v1.0.63 加密特性之前，敏感字段会明文落盘。
    # 这里检测明文敏感字段，若命中则就地加密并原子重写，杜绝明文持久化。
    # 判定基于磁盘原始 cfg（未解密）：_decrypt_secrets 已把 merged 中敏感字段解密为明文，
    # 若用 merged 判定必然每次命中，导致每次读取都重写配置并刷日志；改对 cfg 判定，
    # 仅在磁盘确为明文（无 enc: 前缀）时才触发自愈，幂等加密后下次不再命中。
    # v1.0.360 审查修复（S7）：自愈重写纳入 _config_lock，防止用旧快照覆盖并发保存的新字段。
    if _has_plaintext_secret(cfg):
        try:
            with _config_lock:
                # 锁内重读一次磁盘，把并发保存的新字段合并进来，避免旧快照覆盖
                try:
                    with open(config_path(), "r", encoding="utf-8") as f:
                        latest = json.load(f)
                    if isinstance(latest, dict):
                        for k2, v2 in latest.items():
                            if k2 not in merged:
                                merged[k2] = v2
                except Exception as e:  # noqa: BLE001
                    # v1.0.510 优化（P1-8）：原静默 pass——自愈重写若在此丢失并发保存的
                    # 新字段将无任何痕迹，至少补一条日志
                    log_event(f"[config] 自愈重写前锁内重读失败（并发新字段可能丢失）：{e}")
                tmp = config_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(_encrypt_secrets(merged), f, ensure_ascii=False, indent=2)
                os.replace(tmp, config_path())
            log_event("[config] 检测到明文敏感字段，已就地加密重写配置文件")
        except Exception as e:  # noqa: BLE001
            log_event(f"[config] 明文敏感字段自动加密失败（已降级继续运行）：{e}")
    # v1.0.364（H7）：写入进程内缓存
    # v1.0.470（P1-2）：缓存存深拷贝、返回原件——调用方对返回值就地修改不再污染缓存
    try:
        # 重新 stat：本函数中途可能因"检测到明文敏感字段"就地重写过配置（stamp 已变），
        # 用入口处的旧 stamp 记账会让下一次读取永远命不中缓存。
        _CFG_CACHE["path"] = _cp
        _CFG_CACHE["stamp"] = _cfg_stamp(_cp)
        _CFG_CACHE["cfg"] = copy.deepcopy(merged)
    except OSError:
        pass
    return merged


def silent_mode(default=True):
    """v1.0.523：静默后台执行总开关（wecom.silent_mode，默认 True）。
    高频调用（tucker 每轮）走 load_config 的 mtime 缓存，开销可忽略；
    读取失败保守返回 default（静默优先——宁可多收纳也不抢用户前台）。"""
    try:
        w = (load_config() or {}).get("wecom")
        if isinstance(w, dict) and "silent_mode" in w:
            return bool(w.get("silent_mode"))
    except Exception:  # noqa: BLE001
        pass
    return default


def _has_plaintext_secret(obj, _path=()):
    """递归扫描：是否存在敏感项（key 命中词 或 路径级 _SENSITIVE_PATHS）且值为明文
    （无 enc: 前缀）字符串。与 security._walk_secrets 的判定口径保持一致，否则
    reimburse.name 这类路径级 PII 明文会被判为「无需再加密」而长期裸存。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = _path + (str(k).lower(),)
            if isinstance(v, str) and v and not v.startswith("enc:") \
                    and (_is_sensitive_key(k) or p in _SENSITIVE_PATHS):
                return True
            if isinstance(v, (dict, list)) and _has_plaintext_secret(v, p):
                return True
    elif isinstance(obj, list):
        for x in obj:
            if isinstance(x, (dict, list)) and _has_plaintext_secret(x, _path):
                return True
    return False


def cdp_port(cfg=None):
    """企微 CDP 端口的**全项目唯一口径**（v1.0.588 收敛）。

    历史上这段逻辑被手抄了 8 遍，且写法不一致：`or 9222` / `.get("cdp_port", 9222)` /
    带范围校验三种并存。`.get(k, 9222)` 那几版在配置里 `cdp_port` 存在但为空串或 None 时
    会把空值原样返回（默认值只在键缺失时生效），端口为空 → CDP 全部调用失败。
    cfg 传入时直接用（工具执行链上已有配置，避免重复读盘），否则走 load_config 缓存。
    非法/越界一律回退 9222。"""
    try:
        c = cfg if isinstance(cfg, dict) else load_config()
        wecom = (c or {}).get("wecom") or {}
        p = int(wecom.get("cdp_port") or 0)
        return p if 1 <= p <= 65535 else 9222
    except Exception:  # noqa: BLE001
        return 9222


def save_config(cfg):
    with _config_lock:
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_encrypt_secrets(cfg), f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path())               # 原子写
        # v1.0.634：写完必须作废缓存。本机 mtime 粒度粗（背靠背改写 76% 落在同一刻度），
        # 靠"下次 load 时 mtime 变了"来失效等于没失效——save 后紧接着 load 会读回旧配置。
        _invalidate_cfg_cache()


def update_config(mutate):
    """v1.0.360 审查修复（S7）：加锁的「读-改-写」事务原语。
    mutate(cfg) 就地修改 cfg 并返回 True 表示有变更需要落盘（返回 False/None 跳过写）。
    修复原竞态：各路由 load→改→save 序列无整体锁，并发后写覆盖先写（models/flags/security 段丢失）。"""
    with _config_lock:
        cfg = load_config()
        try:
            changed = mutate(cfg)
        except Exception as e:  # noqa: BLE001
            log_event(f"[config] 事务修改失败（已放弃保存）：{e}")
            return False
        if changed:
            tmp = config_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_encrypt_secrets(cfg), f, ensure_ascii=False, indent=2)
            os.replace(tmp, config_path())
            _invalidate_cfg_cache()                  # v1.0.634，同 save_config
    return True


_BIND_DELTA_KEYS = ("exe_path", "bound_pid", "bound_sig", "bound_started_ts", "cdp_port")


def make_bind_merger(cfg_snapshot):
    """构造 update_config 用的 mutate：把 ensure_binding 写进 cfg_snapshot['wecom'] 的
    绑定键（exe_path/绑定四元组/cdp_port）**按键合并**进事务内的 fresh cfg。
    v1.0.652（P1-17）语义：绝不整段覆盖 wecom 段——绑定流程含拉进程+CDP 等待可达
    数十秒，期间用户改 silent_mode 等设置不得被陈旧快照回滚。
    v1.0.654 A9：backend 与 routes/wecom 两处逐字克隆收敛于此。"""
    delta = {k: v for k, v in (cfg_snapshot.get("wecom") or {}).items()
             if k in _BIND_DELTA_KEYS}

    def _merge_bind(c):
        w = dict(c.get("wecom") or {})
        w.update(delta)
        c["wecom"] = w
        return True

    return _merge_bind


def _experiences_path():
    # v1.0.275：经验库并入 产物库/（原在应用根目录，与 config.json 同级）
    return os.path.join(os.path.dirname(config_path()), "产物库", "experiences.json")


_exp_lock = threading.Lock()   # v1.0.360 审查修复（S6）：经验库读改写与原子写


def _load_experiences():
    try:
        with _exp_lock:
            with open(_experiences_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict) and e.get("id")]
    except FileNotFoundError:
        pass  # silent-ok: 经验库文件不存在＝还没写过，等价空列表
    except Exception as e:  # noqa: BLE001
        log_event(f"[experiences] 读取失败，返回空列表：{e}")
    return []



def _save_experiences(items):
    """保留旧接口（self_repair 等调用方）；内部改为锁 + 原子替换写。"""
    with _exp_lock:
        try:
            tmp = _experiences_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _experiences_path())
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, str(e)


def _exp_rec(data, existing=None):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return {
        "id": (existing or {}).get("id") or ("exp_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4]),
        "module": str(data.get("module") or "").strip(),
        "problem": str(data.get("problem") or "").strip(),
        "solution": str(data.get("solution") or "").strip(),
        "created": (existing or {}).get("created") or now,
        "updated": now,
    }

def _mask_connector_config(cc):
    """脱敏连接器配置：api_key 打码；headers 中的 Authorization/Token 类敏感头打码（P1-3）。"""
    masked = dict(cc)
    if masked.get("api_key"):
        masked["api_key"] = "******"
    hdrs = masked.get("headers")
    if isinstance(hdrs, dict):
        out_hdrs = {}
        for k, v in hdrs.items():
            if str(k).lower() in ("authorization", "x-api-key", "token", "apikey", "api-key"):
                out_hdrs[k] = "******"
            else:
                out_hdrs[k] = v
        masked["headers"] = out_hdrs
    return masked

def _save_config_safe(cfg):
    try:
        save_config(cfg)
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)

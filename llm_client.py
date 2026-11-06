"""
llm_client — LLM HTTP 客户端（全软件唯一的大模型调用入口）
====================================================================
所有对话/批审/定时任务/judge 升级的 LLM 请求都经本模块；12 个下游消费者
（agent/loop、routes/chat、routes/b_models、services/scheduler 等）。

【结构导览】（v1.0.573 重写导览，代码分区自上而下）
  ① 常量区           超时/重试/并发闸（29-77 行注释含各熔断取值的实测依据）
  ② 会话与硬停止     _HTTP_SESSION 惰性会话、_ACTIVE_RESPONSES 硬停止注册表、
                     abort_active_responses（closesocket 中断 + 连接池清理）
  ③ 模型解析         resolve_model / judge_model（双模型分工）/ probe_model_reachable
  ④ 请求构造         _apply_reasoning_params（思考开关 vLLM 双姿势）/
                     _model_timeouts（特性自适应熔断）/ _chat_url / _norm_system_first
  ⑤ 流式主链         stream_llm_events（中断重试外层）→ _stream_llm_events_once
                     （全局并发闸）→ _stream_body（核心：请求/熔断泵/流解析）
  ⑥ Ollama 快速通道  _ollama_think_off_complete（原生 /api/chat，think=false）
  ⑦ 完整生成与探测   _llm_complete（聚合流式）/ _probe_local_models /
                     guess_model_caps / probe_remote_models / probe_reasoning_effort

【公共 API（签名冻结——下游 12 处引用，改动需全量回归）】
  resolve_model / judge_model / stream_llm_events / _llm_complete /
  abort_active_responses / llm_active_count / probe_model_reachable /
  probe_remote_models / probe_reasoning_effort / _probe_local_models /
  _chat_url / build_headers / _apply_reasoning_params

【事件契约】stream_llm_events yield：
  ('token', text) | ('reasoning', text) | ('tool', {...}) |
  ('done', {text, tool_calls, usage}) | ('error', msg, code)
  error code：stream_interrupt(可自动重试) / aborted / queue_timeout /
  server_error / http_request / think_truncated(思考耗尽预算截断)

【熔断层次】（各取值实测依据见常量区注释）
  连接 30s / read 90s → idle 120s（无 data 行）→ no-progress 240s（data 活跃零产出）
  → no-content 300s（纯思考零正文）→ 总时长 600s（reasoning 模型 900s）
  → 全局限流 3 并发 / 排队 90s
"""
# v1.0.324 启动提速 + v1.0.364 M8 收敛：requests 懒加载代理实现收敛到 core_utils
from core_utils import _lazy_requests as requests
import json
import queue
import threading
import time

_ACTIVE_LOCK = threading.Lock()

from core.logging import log_event  # v1.0.647 直连定义处（壳转发保留给存量用法）
# v1.0.621（用户实测：从别处复制的接口地址带 U+200B，报 InvalidSchema 而其他软件能用）
# 使用侧也必须清——只清写入侧的话，config.json 里已存的脏值会一直报错。
from core.strings import clean_url

# ---------- HTTP 会话与并发工具白名单 ----------
# LLM HTTP 请求超时（连接/读取，秒）：连接 30s；read 置 None——vLLM 共享服务排队期
# 可能长时间不吐首字节，固定 read 超时（如 120s）会误杀排队中的请求；读取阶段改由
# LLM_IDLE_TIMEOUT（有效 data 行间隔）与 LLM_STREAM_MAX_SECONDS（总时长）双重熔断管控。
# v1.0.472 修复（任务卡死）：read timeout 原 None（无限）——中转层/上游挂起时响应头
# 永不返回，请求永久挂死（2026-10-07 14:50 实测：远端服务 /v1/models 秒回但
# chat/completions 挂 7 分钟无响应）。read timeout 语义是【每次 socket read 独立计时】：
# 90s 覆盖响应头等待（含上游预填充受理）；流式生成期间由更宽松的 idle 熔断
#（LLM_IDLE_TIMEOUT=120s）负责，两者不冲突。
LLM_HTTP_TIMEOUT = (30, 90)
# LLM 请求失败重试次数（网络抖动/超时容忍）
LLM_MAX_RETRIES = 2
# v1.0.345 全局 LLM 并发闸：多 pane 同时对话/评测并发打模型时，本地推理服务过载
# 会表现为"空转 0 产出"（v1.0.287 空转事故诱因之一）。用信号量限制同时在飞的
# LLM 流式请求数（默认 3，可用环境变量 WECOM_LLM_MAX_CONCURRENCY 调整；≤0 = 不限）。
# 信号量只在请求存活期持有（finally 释放），队列等待不占线程池。
import os as _os_llm
try:
    _LLM_MAX_CONC = int(_os_llm.environ.get("WECOM_LLM_MAX_CONCURRENCY", "3"))
except Exception:  # noqa: BLE001
    _LLM_MAX_CONC = 3
LLM_GLOBAL_SEMAPHORE = threading.BoundedSemaphore(_LLM_MAX_CONC) if _LLM_MAX_CONC > 0 else None
# v1.0.555（P0-7 修复）：全局并发闸排队超时。原 acquire() 无超时——3 个在途请求
# 撞上挂起服务端（最坏 3×900s 熔断）时，后续请求全部堆在 acquire 上静默排队约
# 45 分钟：排队中的请求尚未进入 _stream_body，无 socket、无注册表条目，
# abort_active_responses 关不到它，poll_abort 也还没开始轮询——多 pane 场景下
# 一次服务端挂起即系统性静默停摆（v1.0.472"停止无反应"同源事故的队列路径）。
LLM_QUEUE_TIMEOUT = 90
# v1.0.271：流式中断（done 事件前断流：900s 总时长熔断/空闲熔断/网络断开）的自动重试次数。
# 现场实测 2026-09-02→03：11 次「LLM 流式中断」全部已收 0 字符——大上下文（40+ 条消息）请求
# 在 DaoPro/vLLM 共享服务上空转（流持续吐 data 行骗过 idle 熔断但永远不出内容），同一请求
# 重发立即成功。旧行为：抛错后整个任务停住，等用户手动点「继续」。
LLM_STREAM_EMPTY_RETRIES = 2
# 流式输出空闲检测：120s 无有效 data 行判定模型卡死/失联（防"思考中"无限等待；
# 取值需容忍 vLLM 共享服务排队期——45s 曾把排队中的长上下文请求误杀，2026-08-27）
LLM_IDLE_TIMEOUT = 120
# 单轮 LLM 流式响应总时长熔断：无论流是否活跃，超过即中断报错（防服务端半死状态
# 靠慢速滴流/空行绕过 idle 检测，导致前端无限"思考中"——2026-08-27 实际发生过 9min+ 挂起）
# v1.0.244：300→600。本地慢推理模型（DaoPro/9b 级）在「读台账+读页面」的整合规划轮
# 实测 450s 仍在正常产出思考 token 却被 reasoning 1.5x 熔断(=450s)误杀。提到 600s 后
# reasoning 模型上限 900s，给真思考留够窗口；idle(120s) 仍兜底真正失联/卡死的流。
LLM_STREAM_MAX_SECONDS = 600
# 零产出空转专项熔断（v1.0.287）：流持续吐 data 行（骗过 idle 检测——vLLM 空转时
# 吐的是空 delta chunk，有 data 行无任何字段）但 content/reasoning/tool 参数
# 累计 0 字符持续超过该秒数 → 强制中断走自动重试。
# 与 idle 的区别：idle 管「连接静默」（排队/prefill 期无 data 行，180s 判死）；
# 本项管「data 行活跃却零产出」（服务端半死空转）——2026-09-05/06 实测 DaoPro
# 大上下文请求连续 3 次空转满 900s 总时长熔断（每次白等 15 分钟），空转特征
# 极明确（连 reasoning token 都没有），正常推理不可能 240s 零 token，取 240s 安全。
LLM_NO_PROGRESS_TIMEOUT = 240
# 无内容推理循环专项熔断（v1.0.293）：流持续吐 reasoning 思考 token（思考也算实质
# 产出，no-progress 熔断不触发）但 content/工具调用累计 0 字符超过该秒数 =
# 无限推理/重复生成循环。现场实测 2026-09-07 博易云测试（34KB 素材 + 18 条消息
# 大上下文）：DaoPro 连吐 15 分钟纯 reasoning、0 content 0 工具调用，只有 900s
# 总时长熔断才杀掉（用户全程看到"一直在推理没有动作"）。与 no-progress 的区别：
# 那项管「零产出」（连 reasoning 都没有），本项管「无限思考、零可执行产出」。
# 当前上下文口径（KEEP_RECENT=8、压缩后 <50K）下真思考实测 9~58s，300s 足够安全。
LLM_NO_CONTENT_TIMEOUT = 300
# v1.0.628：「无限思考」熔断文案的机器可读标记（raise 侧与重试侧共用同一常量，
# 改字面量不会只改一处把重试策略静默失效）。这一类中断**原样重发必然复现**
# （同一上下文 + 同一思考档位 → 同一条死循环），所以外层重试要换参数而不是换运气。
_THINK_LOOP_MARK = "无可执行产出"

# 复用连接池，避免每个流式请求重复 TCP/TLS 握手
_HTTP_SESSION = None

# ---------- 活动响应注册表（v1.0.326 硬停止） ----------
# abort 时主动 close 进行中的 LLM 连接——预填充慢（read timeout=None 永不触发）时
# 不必等模型吐字。
# v1.0.361 审查修复（S2）：原 WeakSet 不持有强引用，_ActiveLLMReq 在 _post 返回后
# 立即被 GC 出集——abort 时集合恒空，硬停止从未生效。改为普通 set 强引用持有 +
# 流结束（done/error/GeneratorExit）时显式注销。
_ACTIVE_RESPONSES = set()   # items: _ActiveLLMReq（强引用，勿改回 WeakSet）


class _ActiveLLMReq:
    def __init__(self, resp, sock):
        self.resp = resp
        self.sock = sock


def llm_active_count():
    """v1.0.482：在途 LLM 请求数（流式硬停止池）。
    后台杂务（kb-ops 每日消化等）据此给用户对话让路：有对话在跑就不抢同一个模型。"""
    with _ACTIVE_LOCK:
        return len(_ACTIVE_RESPONSES)


def abort_active_responses():
    """硬停当前生成：close 进行中 LLM 连接的底层 socket（closesocket）。
    【Windows 坑】shutdown(SHUT_RDWR) 不唤醒另一线程阻塞中的 recv（Linux 会），
    必须 closesocket 才能让泵线程立即报错。也不走 resp.close()——那会与读线程
    的 urllib3 内部锁互斥死锁（close 等读线程放锁、读线程等模型吐字，实测阻塞
    23s）。closesocket 绕过 Python 层锁直接中断。返回关闭数。
    注：多 pane 并发生成会一并中断，单用户场景可接受。"""
    with _ACTIVE_LOCK:
        items = list(_ACTIVE_RESPONSES)
    n = 0
    for item in items:
        sock = getattr(item, "sock", None)
        try:
            if sock is not None:
                sock.close()
                n += 1
        except OSError:
            pass  # 已断开
        except Exception as _e:# noqa: BLE001
            log_event(f"[warn] llm_client.py:105 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志
    if n:
        # v1.0.561：closesocket 绕过 urllib3 连接池强关 socket → 池内残留 fd=-1
        # 死连接（每轮首请求 select(-1) 报错重试）。清池断根。
        _clear_http_pool()
        log_event(f"[llm] 硬停止：已关闭 {n} 个进行中的 LLM 连接")
    return n


def _clear_http_pool():
    """v1.0.561：清空 keep-alive 连接池（丢弃全部空闲连接）。
    硬停止 closesocket 绕过 Python 层强关 socket——urllib3 连接池不知情，池内
    残留 fd=-1 的死连接；后续每轮请求复用死连接时 urllib3 的 is_connection_dropped
    对其 select(-1) → 「file descriptor cannot be a negative integer (-1)」→ 每轮
    首请求失败、重试第 2 次才新建成功（用户实测：全程每轮多等数秒、日志刷失败）。
    清池强制后续请求全部新建连接。幂等、无副作用（空闲连接本可随时丢弃）。"""
    s = _HTTP_SESSION
    if s is None:
        return
    try:
        for adapter in getattr(s, "adapters", {}).values():
            adapter.poolmanager.clear()
    except Exception as e:  # noqa: BLE001
        log_event(f"[warn] 清空 LLM 连接池失败（忽略）：{e}")


def _http_session():
    """惰性会话（v1.0.324）：requests 懒加载后不能在模块级建 Session。
    v1.0.361 审查修复（P3）：加锁防首次并发创建多个 Session。"""
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        with _ACTIVE_LOCK:
            if _HTTP_SESSION is None:
                _HTTP_SESSION = requests.Session()
    return _HTTP_SESSION


def is_stream_interrupt(code=None, text=None):
    """stream_interrupt（可自动重试的流式中断）判定——**单一事实来源**（v1.0.654 A3）。
    此前"code == 'stream_interrupt' or '流式中断' in text"双轨判定在 7 处手抄
    （llm_client×2 / loop / auto_loop / self_repair×2 / chat），各自漏改过一轮。
    入参两种形态：is_stream_interrupt(ev) 直接传 ("error", text, code) 元组；
    或 is_stream_interrupt(code, text) 传已拆开的结构化 code 与错误文本。
    字符串兜底保留为历史事件（无 code 的第三方桩/旧日志）兼容。"""
    if isinstance(code, (tuple, list)):
        ev = code
        code = ev[2] if len(ev) > 2 else None
        text = ev[1] if len(ev) > 1 else ""
    return code == "stream_interrupt" or (isinstance(text, str)
                                          and ("流式中断" in text or "流异常中断" in text))


def resolve_model(cfg, model_id=None):
    """按 id 解析模型配置。优先级：显式 model_id → is_default → 列表首个启用项。
    v1.0.573：model_id 未命中时记日志（此前静默回落默认——"选模型没用"类问题
    的排查盲区；常见原因是前端缓存的模型 id 与配置不一致/模型已删除）。"""
    models = [m for m in cfg.get("models", []) if m.get("enabled", True)]
    if not models:
        return None
    if model_id:
        for m in models:
            if m.get("id") == model_id:
                return m
        log_event(f"[llm] 指定 model_id={model_id!r} 不在启用模型列表中，回落默认"
                  f"（模型已删除/未启用/前端缓存过期？）")
    for m in models:
        if m.get("is_default"):
            return m
    return models[0]


def judge_model(cfg):
    """双模型分工：返回配置的判断模型（大模型），未配置 judge_model_id 返回 None。
    小模型跑工具主循环，大模型（judge）只在判断点/卡住时介入。留空=纯单模型。"""
    if not cfg:
        return None
    jid = cfg.get("judge_model_id")
    if not jid:
        return None
    return resolve_model(cfg, jid)


def probe_model_reachable(model, timeout=2.5):
    """真连通探测（v1.0.528-2）：GET {base_url}/models 元数据端点。
    **零 token 消耗**——/models 是模型清单元信息接口，不经过推理不计费
    （OpenAI/Ollama/LM Studio/中转站均免费），仅几百字节流量。
    返回三态：{"state": "ok"|"auth"|"down", "detail": str}
      ok   = HTTP 有响应（服务在线；200 最理想）
      auth = 服务可达但凭证被拒（401/403——云端 key 失效场景）
      down = 连接失败/超时（服务停了/网络不通）"""
    base = clean_url((model or {}).get("base_url") or "")
    if not base:
        return {"state": "down", "detail": "模型未配置 base_url"}
    if base.lower().startswith(("http://", "https://")) is False:
        # 不带协议头 requests 会抛 InvalidSchema（"No connection adapters…"），
        # 那句对用户毫无意义——直接说清要什么。
        return {"state": "down", "detail": "接口地址需以 http:// 或 https:// 开头：" + base[:60]}
    headers = {}
    key = (model or {}).get("api_key") or ""
    if key and not str(key).startswith("enc:"):   # 运行期已解密；异常态（仍加密）不带
        headers["Authorization"] = "Bearer " + str(key)
    try:
        # 函数级 import 是**测试 patch 点**（test_model_probe monkeypatch
        # core_utils._lazy_requests）——勿上提为模块级，否则 mock 失效打真网络。
        from core_utils import _lazy_requests as requests
        r = requests.get(base + "/models", headers=headers, timeout=(1.5, timeout))
        if r.status_code in (401, 403):
            return {"state": "auth", "detail": f"服务可达但凭证被拒（HTTP {r.status_code}）"}
        return {"state": "ok", "detail": f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"state": "down", "detail": str(e)[:100] or type(e).__name__}


# ---------- 推理参数注入（v1.0.186） ----------
# 适配主流推理模型/服务商的思考强度控制参数：
#   reasoning_effort  → OpenAI o1/o3/gpt-5 系（low/medium/high）
#   enable_thinking   → DeepSeek-R1 / Qwen thinking 模式（true/false）
#   thinking_budget   → Gemini thinking_config.thinking_budget（思考预算 token 数）
#   max_input_tokens  → 单轮请求的上下文窗口上限（OpenAI/Anthropic 都用 max_tokens 控输出，
#                       上下文窗口一般服务商默认；显式传 max_tokens 即限制单次输出 token 数）
#   extra_params      → 任意自定义扩展参数（dict，直接合并进请求体，覆盖上面默认值）
# 所有字段可选；未配置（None）时不发送对应参数，服务商走自己的默认——旧配置零行为变化。

def _apply_reasoning_params(payload, model):
    """把模型配置里的推理参数注入 OpenAI 兼容请求体 payload（就地修改并返回）。
    v1.0.573：enable_thinking 同时注入 chat_template_kwargs（vLLM/SGLang 的 Qwen3 系
    思考开关只认模板参数，顶层字段会被忽略——用户反馈"配了禁思考仍卡思考"的根因）。"""
    effort = model.get("reasoning_effort")
    if effort:
        payload["reasoning_effort"] = effort
    thinking = model.get("enable_thinking")
    if thinking is not None:
        payload["enable_thinking"] = bool(thinking)
        if thinking is False:
            # vLLM/SGLang（Qwen3 系）的思考关闭只认 chat_template_kwargs，顶层字段被忽略
            # （用户反馈"配了禁思考仍卡思考"的根因）。仅在关思考时注入——避免
            # OpenAI 官方 API 对未知顶层参数报 400 影响正常模型。
            ctk = payload.setdefault("chat_template_kwargs", {})
            ctk["enable_thinking"] = False
    budget = model.get("thinking_budget")
    if budget:
        payload["thinking_config"] = {"thinking_budget": int(budget)}
    # 单次输出 token 上限（OpenAI/Anthropic 通用）；max_input_tokens 是上下文窗口，
    # 一般不显式传（服务端默认通常已够），仅在需要强制约束时由 extra_params 覆盖。
    out_tokens = model.get("max_output_tokens")
    if out_tokens:
        payload["max_tokens"] = int(out_tokens)
    extra = model.get("extra_params")
    if isinstance(extra, dict) and extra:
        payload.update(extra)
    return payload


# v1.0.601 会话级思考档位（聊天输入行的「🧠 思考」下拉）。
# 思考参数原本只有**模型配置**一个来源（模型管理页里逐模型设 enable_thinking /
# reasoning_effort），用户在对话里想临时"这次别想了"只能去改配置——改完还影响别的任务。
# 这里给一个**请求级覆盖**：在解析出的模型 dict 上做浅拷贝再改字段，
# 全局配置一个字节都不动；下游 _apply_reasoning_params / _model_timeouts
# 读的还是同一个 dict，所以档位同时决定请求参数与超时放宽，不需要两处改。
_THINK_OFF = {"off", "none", "false", "0", "no"}
_THINK_LEVELS = {"minimal", "low", "medium", "high"}


def with_thinking_override(model, thinking):
    """把会话级思考选择套到模型配置上，返回**新 dict**（原 model 不被修改）。

    thinking 取值："" / None → 跟随模型配置（原行为）；
    "off" → 关思考；"low|medium|high|minimal" → 思考强度档位。
    未知值一律按"跟随模型"处理（宁可不生效，也不要把乱码塞进请求体）。
    """
    if not thinking or not isinstance(model, dict):
        return model
    t = str(thinking).strip().lower()
    if t in _THINK_OFF:
        m = dict(model)
        m["enable_thinking"] = False
        m["reasoning_effort"] = None
        return m
    if t in _THINK_LEVELS:
        m = dict(model)
        m["reasoning_effort"] = t
        m["enable_thinking"] = True
        return m
    return model


def _model_timeouts(model):
    """v1.0.206 模型特性自适应：按模型特性返回 (idle_timeout, stream_max_seconds)。
    - reasoning 模型（enable_thinking / thinking_supported_levels / reasoning_effort）：
      工具调用前要写长思考，响应窗口放宽 1.5 倍（ornith-1.5:9b 实测 5 分钟熔断的根因之一）
    - 大输出模型（max_output_tokens>4096）：生成长，总时长再放宽 1.3 倍
    非 reasoning 小模型保持默认，熔断更快不误等。"""
    idle, mx = LLM_IDLE_TIMEOUT, LLM_STREAM_MAX_SECONDS
    m = model or {}
    reasoning = (bool(m.get("enable_thinking")) or bool(m.get("thinking_supported_levels"))
                 or bool(m.get("reasoning_effort")))
    if reasoning:
        idle = int(idle * 1.5)
        mx = int(mx * 1.5)
    out_t = m.get("max_output_tokens")
    if out_t and int(out_t) > 4096:
        mx = int(mx * 1.3)
    return idle, mx


# 协议 → 路径后缀（v1.0.187）。自定义协议时 base_url 已含完整路径，不追加 /chat/completions。
def _chat_url(model):
    base = clean_url(model.get("base_url") or "")
    proto = model.get("protocol") or "openai"
    if proto == "custom":
        return base  # 自定义协议假定 base_url 已是完整 endpoint
    # 兼容两种填法：base 地址（.../v1）自动补 /chat/completions；完整路径直接用，避免重复拼接 404
    if not base.endswith("/chat/completions"):
        base += "/chat/completions"
    return base


# ---------- LLM 流式（OpenAI 兼容） ----------

def _norm_system_first(msgs):
    """sglang 等严格服务端要求 system 消息至多一条且必须位于 index 0——
    实测多条 system 即使都在开头也会报 'System message must be at the beginning.'(400)。
    把散落在对话各处的 system 消息合并为一条（按原顺序用空行连接）放在列表开头，
    非 system 消息保持原顺序。对所有 OpenAI 兼容服务语义无损。"""
    if not msgs:
        return msgs
    sys_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"]
    other_msgs = [m for m in msgs if not (isinstance(m, dict) and m.get("role") == "system")]
    if len(sys_msgs) <= 1:
        return msgs  # 0 或 1 条 system，顺序天然合规，无需处理
    merged = {"role": "system",
              "content": "\n\n".join((m.get("content") or "").strip() for m in sys_msgs)}
    return [merged] + other_msgs


def stream_llm_events(model, messages, tools=None, response_format=None, poll_abort=None):
    """v1.0.271：流式中断自动重试外层（对调用方透明，yield 契约不变）。
    流在 done 事件前中断（900s 总时长熔断/空闲熔断/网络断开——均为瞬态故障）时，
    自动重发同一请求至多 LLM_STREAM_EMPTY_RETRIES 次，全部失败才把 error 抛给前端。
    现场实测 2026-09-02→03：11 次中断全部「已收 0 字符」（大上下文请求在 DaoPro/vLLM
    共享服务上空转），重发立即成功——旧行为是停住等用户手动点「继续」。
    安全性：工具调用只在 done 事件一次性发出，中途重试不会重复执行工具；已吐出的
    token/reasoning 会留在 UI 显示（思考块重复），但上下文不受影响（round_text 只取
    done）。非「流式中断」的 error（HTTP 500/请求失败等）有各自的连接层重试，不在此处。
    v1.0.628：「无限思考循环」（no-content 熔断，思考在流但正文/工具恒 0）不再原样
    重发——同一上下文同一思考档位必然复现同一循环，重试只是把 300s 白烧重复 N 次；
    这类中断的后续重试自动关思考（模型 caps 声明可关才关）。
    """
    for attempt in range(1, LLM_STREAM_EMPTY_RETRIES + 2):
        retryable = False
        think_loop = False
        for ev in _stream_llm_events_once(model, messages, tools, response_format, poll_abort):
            kind = ev[0]
            yield ev
            if kind == "done":
                return
            if kind == "error":
                # v1.0.344：优先按结构化 code 判断（ev[2]），字符串匹配仅兜底旧消费者/测试桩
                _code = ev[2] if len(ev) > 2 else None
                if is_stream_interrupt(_code, ev[1] if len(ev) > 1 else ""):
                    retryable = True
                    # v1.0.628：区分「瞬态断流」与「无限思考循环」——后者重发同一请求
                    # 必然再卡（同上下文同思考档位），必须换参数重试，否则只是把
                    # 300s 白烧重复 LLM_STREAM_EMPTY_RETRIES 次（现场：10-27 生成 PPT
                    # 三次 300s 纯思考 0 正文，共 905s 无产出）。
                    if isinstance(ev[1], str) and _THINK_LOOP_MARK in ev[1]:
                        think_loop = True
                break
        if not retryable or attempt > LLM_STREAM_EMPTY_RETRIES:
            return
        # v1.0.472：用户已点停止 → 不再自动重试（原实现中断重试不感知 abort，
        # 服务端挂起场景下点停止后每次重试还要再挂 90s+）
        if poll_abort is not None and poll_abort():
            yield ("error", "已停止（用户中断）", "aborted")
            return
        # v1.0.628：思考循环 → 后续重试一律关思考。两个前提缺一不可：模型自己声明
        # 可关（不可关的绝不塞参数去砸服务端），且本轮确实开着思考
        # （enable_thinking is True——从 False/None 改成 False 会给 OpenAI 官方 API
        #  多塞一个未知顶层参数报 400，只关"我们打开过的那个开关"）。
        if think_loop and model.get("enable_thinking") is True \
                and (model.get("caps") or {}).get("can_disable_thinking", True):
            model = with_thinking_override(model, "off")
            log_event(f"[llm] 上一轮判定无限思考循环 → 自动关思考重试 {attempt}/"
                      f"{LLM_STREAM_EMPTY_RETRIES}：{model.get('name')}")
        else:
            log_event(f"[llm] 流式中断未收尾 → 自动重试 {attempt}/{LLM_STREAM_EMPTY_RETRIES}"
                      f"（免人工点「继续」）：{model.get('name')}")
        import random as _rnd
        time.sleep(2 + _rnd.random())  # v1.0.345：加抖动，避免多 pane 同节奏重试挤兑


def _stream_llm_events_once(model, messages, tools=None, response_format=None, poll_abort=None):
    """v1.0.345 薄壳：持有全局 LLM 并发闸（信号量）跑一次流式请求。
    try/finally + yield from——消费端正常耗尽、提前 break（GeneratorExit）、异常
    三种路径都可靠释放；队列等待方不占线程资源。"""
    _sem = LLM_GLOBAL_SEMAPHORE
    if _sem:
        # v1.0.555（P0-7）：排队也必须有上界——超时走显式错误事件（loop 对未知
        # code 按 terminal error 推送前端），服务端过载从"无限假死"变为一分钟内
        # 明确报错，且停止按钮在排队态立即生效（无需等队列排到）。
        if not _sem.acquire(timeout=LLM_QUEUE_TIMEOUT):
            yield ("error", f"模型服务繁忙（{llm_active_count()} 个请求在飞），"
                            f"排队超过 {LLM_QUEUE_TIMEOUT}s 仍未轮到，请稍后重试", "queue_timeout")
            return
    try:
        yield from _stream_body(model, messages, tools, response_format, poll_abort)
    finally:
        if _sem:
            try:
                _sem.release()
            except Exception as _e:# noqa: BLE001
                log_event(f"[warn] llm_client.py:271 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志


def build_headers(model):
    """v1.0.355：OpenAI/Anthropic 请求头构造收敛到一处（此前 llm_client 与
    routes/management 的用量查询各写一份，协议字段改两处会漏）。"""
    headers = {"Content-Type": "application/json"}
    proto = model.get("protocol") or "openai"
    if proto == "anthropic":
        # Anthropic 协议用 x-api-key + anthropic-version，不走 Bearer；当前 _chat_url 默认补
        # /chat/completions 但 Anthropic 的真实端点是 /v1/messages——后面 v1.0.188 接入真协议时
        # 切换（本次只先做协议字段与服务端 schema/UI，留口子不直接走通）
        headers["x-api-key"] = model.get("api_key", "")
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {model.get('api_key','')}"
    return headers


def _stream_body(model, messages, tools=None, response_format=None, poll_abort=None):
    """
    yield ('token', text) | ('tool', {name,args}) | ('done', {text, tool_calls}) | ('error', msg, code)
    """
    messages = _norm_system_first(messages)
    url = _chat_url(model)
    # v1.0.200：请求开始日志——卡住时能立刻区分「请求已发出等在响应（vLLM 排队/模型慢）」
    # 与「请求根本没发出（网络/配置问题）」。之前无开始日志，卡 3 分钟只能猜。
    _model_name = model.get("name") or model.get("model") or "?"
    log_event(f"[llm] 请求开始：{_model_name} → {url}，messages={len(messages)} 条，tools={'有' if tools else '无'}")
    headers = build_headers(model)
    payload = {"model": model.get("model"), "messages": messages, "stream": True}
    # v1.0.253：流式也要 usage——OpenAI 标准 stream_options.include_usage（vLLM/Ollama/LM
    # Studio/OpenAI 均支持），否则 vLLM 等服务的流式末尾不吐 usage chunk，用量统计会永久为 0。
    payload["stream_options"] = {"include_usage": True}
    # 推理参数注入：思考强度/思考开关/思考预算/自定义扩展（v1.0.186）
    _apply_reasoning_params(payload, model)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        # 效率/准确度优化：工具调用场景用低温度提升工具选择与参数生成的确定性；
        # 开启 parallel_tool_calls，让模型一轮可同时发起多个只读调用，配合 agent_loop 的
        # _PARALLEL_SAFE_TOOLS 并行预取显著减少往返轮次。服务端不支持时自动降级（见下方 400 处理）。
        payload["temperature"] = 0.2
        payload["parallel_tool_calls"] = True
    if response_format:
        payload["response_format"] = response_format

    _req_ref = None   # v1.0.361 当前活动请求引用（流结束/异常时从注册表注销）

    def _post(rf):
        # 复用 _HTTP_SESSION（keep-alive），避免每个流式请求重复 TCP/TLS 握手
        nonlocal _req_ref
        r = _http_session().post(url, headers=headers,
                                 json={**payload, **({"response_format": rf} if rf else {})},
                                 stream=True, timeout=LLM_HTTP_TIMEOUT)
        try:
            _sock = r.raw._fp.fp.raw._sock
        except Exception:  # noqa: BLE001
            _sock = None
        with _ACTIVE_LOCK:
            _req_ref = _ActiveLLMReq(r, _sock)
            _ACTIVE_RESPONSES.add(_req_ref)   # v1.0.326 硬停止注册（v1.0.361 改强引用）
        return r

    def _release_req():
        nonlocal _req_ref
        with _ACTIVE_LOCK:
            if _req_ref is not None:
                _ACTIVE_RESPONSES.discard(_req_ref)
            _req_ref = None

    # ---------- 【段 2/5】发送与连接层重试 ----------
    # 双层重试语义：
    #   本层（LLM_MAX_RETRIES=2）——HTTP 请求建立失败（连接/超时/5xx）的连接级重试；
    #   外层（stream_llm_events 的 LLM_STREAM_EMPTY_RETRIES=2）——流已建立但中途
    #   断流（done 前熔断/网络断开）的任务级重试。两层独立计数、独立退避。
    resp = None
    last_exc = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            resp = _post(response_format)
            last_exc = None
            break
        except Exception as e:  # noqa: BLE001
            last_exc = e
            log_event(f"[llm] 请求失败(第{attempt}/{LLM_MAX_RETRIES}次)：{model.get('name')} -> {e}")
            # v1.0.561：连接池可能残留被外部强关的死连接（fd=-1）——清池再重试，
            # 避免重试又复用同一死连接继续失败（"file descriptor cannot be a
            # negative integer" 的现场即池内死连接）。
            _clear_http_pool()
            if attempt < LLM_MAX_RETRIES:
                # v1.0.472：用户已点停止 → 不再重试（原实现失败重试循环不感知 abort，
                # 服务端挂起场景下点停止后仍会 90s×N 次空转）
                if poll_abort is not None and poll_abort():
                    yield ("error", "已停止（用户中断）", "aborted")
                    return
                # v1.0.345 指数退避+抖动：1s → 2s+（服务端过载时同节奏重试只会继续撞墙）
                import random as _rnd
                # v1.0.592：池内死连接（fd=-1）不是远端过载——上一行刚清过池，重试必然
                # 新建连接，指数退避只是白等（实测 21 轮每轮多花 1.2s）。单独给短停顿。
                if "negative integer" in str(e):
                    time.sleep(0.2)
                else:
                    time.sleep(min(8.0, 2.0 ** (attempt - 1)) + _rnd.random() * 0.5)
    if resp is None:
        # v1.0.344：error 事件追加机器可读 code（第三元素），消费方按码分支，字符串匹配仅作旧格式兜底
        yield ("error", f"请求失败：{last_exc}", "http_request")
        return
    # 强制用 UTF-8 解码响应，避免中文被 requests 默认 Latin-1 解成乱码
    resp.encoding = "utf-8"
    if resp.status_code != 200:
        body = resp.text[:400]
        lower = body.lower()
        # v1.0.355：三个「服务端不支持 → 去参数重试」分支收敛为规则表（原逐字重复 ×3）
        _drop = _rf = None
        if response_format and ("response_format" in body or "json_schema" in body or "unsupported" in lower):
            _drop, _rf = "response_format", None   # response_format 是调用级参数，重试直接不带
        elif "stream_options" in body or "include_usage" in body:
            _drop, _rf = "stream_options", response_format
        elif "parallel_tool_calls" in body or ("parallel" in lower and "unsupported" in lower):
            _drop, _rf = "parallel_tool_calls", response_format
        if _drop:
            log_event(f"[llm] 服务端不支持 {_drop}，去掉后重试：{model.get('name')}")
            if _drop != "response_format":
                payload.pop(_drop, None)
            # v1.0.515 修复（审计 P1-2）：旧响应已 close，必须先注销旧 _req_ref 再发
            # 降级请求——否则 _post 里 nonlocal 重新赋值后，旧注册项永久残留
            # _ACTIVE_RESPONSES（对不支持该参数的服务端每个请求泄漏一条）。
            _release_req()
            try:
                resp.close()   # v1.0.361 审查修复（P4）：旧错误响应先归还连接池
                resp = _post(_rf)
                resp.encoding = "utf-8"
                if resp.status_code != 200:
                    # v1.0.515：先清理再 yield——原顺序 yield 在前，消费方若在
                    # yield 点断开（GeneratorExit）清理行永不执行
                    resp.close()
                    _release_req()   # v1.0.470（P2-1）：错误路径注销强引用
                    yield ("error", f"LLM HTTP {resp.status_code}: {resp.text[:400]}", "http_error")
                    return
                # 200 → 继续走下方的 iter_lines 解析这次 fallback 响应
            except Exception as e2:  # noqa: BLE001
                _release_req()   # v1.0.470（P2-1）——v1.0.515：先清理再 yield
                yield ("error", f"LLM 请求失败：{e2}", "http_request")
                return
        else:
            log_event(f"[llm] HTTP 错误 {resp.status_code}：{model.get('name')} -> {body}")
            resp.close()   # v1.0.361（P4）
            _release_req()   # v1.0.470（P2-1）——v1.0.515：先清理再 yield
            yield ("error", f"LLM HTTP {resp.status_code}: {body}", "http_error")
            return

    # ---------- 【段 3/5】服务端兼容降级（400 参数不接受 → 摘参重发） ----------
    tool_acc = {}
    _tc_seq = 0        # v1.0.652（审计 P2-3）：无 index 时按出现顺序分配槽位
    _last_idx = 0
    content_buf = []
    finish_reason = None   # v1.0.573：末 chunk 的 finish_reason（length=截断检测用）
    usage = None
    # —— LLM 卡死兜底（P4-0）：流式响应空闲检测 ——
    # requests 的 read timeout 对「服务端建立连接后缓慢/停止发送」的挂起不灵敏
    #（尤其流式长任务 + 慢速推理），此前会导致前端永远"思考中"无任何输出。
    # ---------- 【段 4/5】流式消费与四重熔断（idle / no-progress / no-content / 总时长） ----------
    # 这里用后台线程泵读响应行，主循环经 queue.get(timeout=idle) 检测：
    # 超过阈值无任何新数据即判定模型卡死/失联，抛超时由 agent_loop 兜底转显式
    # error 事件并保存断点，用户最坏 idle 秒内收到明确反馈。
    # v1.0.206：idle/max 按模型特性缩放（reasoning/大输出模型放宽）
    _idle, _mx = _model_timeouts(model)
    _q = queue.Queue()
    _deadline = time.monotonic() + _mx   # v1.0.652：monotonic 防 NTP 回拨延长熔断（与 loop.py 口径一致）
    # 空闲检测基准时刻必须初始化：否则服务端连接建立后迟迟不吐首个 data 行
    #（vLLM 排队/本地模型冷启动等正常慢响应）时，首个 5s 轮询超时就会因
    # _last_data_ts 未定义抛 NameError，把正常慢响应误判为「LLM 流式中断」
    #（与 README 记录的「45s 误杀排队请求」同源问题）。
    _last_data_ts = time.time()
    # v1.0.287：实质产出追踪——空转流特征是「data 行活跃但 content/reasoning/
    # tool 参数累计 0 字符」（空 delta chunk 骗过 idle 检测），用最近一次实质
    # 产出时刻做零产出专项熔断（LLM_NO_PROGRESS_TIMEOUT）。
    _received = 0
    _last_progress_ts = time.time()
    # v1.0.628：思考字数单独计数——中断路径原来只报「已收 N 字符」（正文），
    # 思考型模型空转 5 分钟时日志显示「已收 0 字符」，把「只思考不落地」和
    # 「全静默」两种完全不同的故障写成同一句话，事后无法分诊。
    _reasoning_chars = 0
    # v1.0.293：最近一次「可执行产出」（content/工具参数，不含 reasoning）时刻。
    # reasoning 会持续刷新 _last_progress_ts（思考也算产出），无限推理循环靠
    # 本戳 + LLM_NO_CONTENT_TIMEOUT 捕获（思考不停但永远不落 content/工具）。
    _last_content_ts = time.time()

    def _pump_lines():
        try:
            for line in resp.iter_lines(decode_unicode=True):
                _q.put(line)
        except Exception as e:  # noqa: BLE001
            _q.put(("__exc__", e))
        finally:
            _q.put(None)

    threading.Thread(target=_pump_lines, daemon=True).start()
    # —— 超时/异常时主动关闭连接，终止 _pump_lines 线程的 socket.readinto 阻塞（防 CLOSE_WAIT 堆积）
    # v1.0.326：【必须关底层 socket 而非 resp.close()】——跨线程 resp.close() 与泵线程
    # 的 urllib3 内部锁互斥（close 等锁、泵线程等模型吐字，实测死锁 21s 直到模型响应）。
    # 直接 closesocket 会唤醒 Windows 上另一线程的阻塞 recv，立即解锁。
    try:
        _kill_sock = resp.raw._fp.fp.raw._sock
    except Exception:  # noqa: BLE001
        _kill_sock = None
    _resp_holder = {"resp": resp, "sock": _kill_sock}

    def _kill_conn():
        """硬断底层连接（closesocket 唤醒泵线程）。resp 交给 GC。"""
        try:
            if _resp_holder.get("sock") is not None:
                _resp_holder["sock"].close()
                # v1.0.592：硬关之后就地清池。closesocket 绕过 urllib3，池里会留下
                # fd=-1 的死连接，下一轮请求复用它即抛「file descriptor cannot be a
                # negative integer (-1)」，要等 except 里的清池+退避重试才救回来。
                # 本函数只在「用户中断 / 空闲超时 / 零产出熔断 / 无产出熔断 / 总时长
                # 熔断」这 5 条中止路径上被调用——都是确定不会再复用这条连接的场景，
                # 所以在此清最稳妥。幂等，只丢空闲连接，无副作用。
                _clear_http_pool()
        except Exception as _e:# noqa: BLE001
            log_event(f"[warn] llm_client.py:449 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志
    try:
        while True:
            try:
                line = _q.get(timeout=5.0)
                _eof = (line is None)
            except queue.Empty:
                line, _eof = None, False
            # v1.0.326：停止轮询——预填充/慢模型期间每 5s 有一次中断机会
            #（若 sock.close() 硬停已生效则走不到这里；本检查是双保险）
            if poll_abort is not None and poll_abort():
                _kill_conn()
                _release_req()   # v1.0.515 修复（审计 P1-1）：中止路径注销注册项——
                                 # 原实现直接 return 泄漏 _req_ref，llm_active_count 永久虚高
                yield ("error", "已停止（用户中断，断点已保存可点「继续」）", "aborted")
                return
            if isinstance(line, tuple) and line and line[0] == "__exc__":
                raise line[1]
            if isinstance(line, str) and line.startswith("data:"):
                _last_data_ts = time.time()
            if time.time() - _last_data_ts > _idle:
                _kill_conn()  # 主动关闭 socket，终止 _pump_lines
                raise TimeoutError(
                    f"LLM 流式响应超过 {_idle}s 无有效数据（data 行），判定模型卡死或失联，"
                    f"请检查远端 {model.get('base_url')} 的模型服务状态")
            # v1.0.287：零产出空转专项熔断——data 行活跃（idle 不触发）但长时间
            # 无任何实质 token（content/reasoning/tool 参数均 0 字符）= 服务端
            # 半死空转，不必等满 _mx 总时长（900s）才熔断，提前止损走自动重试。
            _since_progress = time.time() - _last_progress_ts
            if _since_progress > LLM_NO_PROGRESS_TIMEOUT:
                _kill_conn()  # 同理解放 socket
                raise TimeoutError(
                    f"LLM 流式响应已 {_since_progress:.0f}s 无任何实质产出"
                    f"（已收 {len(''.join(content_buf))} 字符，data 行仍活跃）——"
                    f"判定服务端空转（重复生成/半死状态），强制中断走自动重试")
            # v1.0.293：无内容推理循环专项熔断——reasoning 思考流会持续刷新
            # _last_progress_ts（零产出熔断不触发），但长时间无任何可执行产出
            # （content/工具调用）= 无限推理/重复生成循环，不必等满 _mx 总时长
            # （900s）才熔断，提前止损走自动重试（免人工点「继续」）。
            _since_content = time.time() - _last_content_ts
            if _since_content > LLM_NO_CONTENT_TIMEOUT:
                _kill_conn()  # 同理解放 socket
                raise TimeoutError(
                    f"LLM 流式响应已 {_since_content:.0f}s {_THINK_LOOP_MARK}（content/工具调用）"
                    f"（思考已收 {_reasoning_chars} 字仍在流、正文 0 字——判定无限推理/"
                    f"重复生成循环），强制中断走自动重试")
            if time.monotonic() > _deadline:   # v1.0.652：与 _deadline 同用 monotonic
                _kill_conn()  # 同理解放 socket
                raise TimeoutError(
                    f"LLM 单轮流式响应总时长超过 {_mx}s 仍未完成"
                    f"（流仍活跃但迟迟不收尾，可能陷入重复生成或服务端异常），已强制中断，"
                    f"可点击「继续」重试")
            if _eof:
                break
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if "error" in obj:
                resp.close()   # v1.0.361 审查修复（P4）：错误路径归还连接
                _release_req()   # v1.0.470（P2-1）：注销强引用——v1.0.515：先清理再 yield
                yield ("error", str(obj["error"]), "server_error")
                return
            # 流式末尾/独立 chunk 可能带 usage（LM Studio/Ollama/OpenAI 兼容）
            if obj.get("usage"):
                usage = obj["usage"]
            # v1.0.254：include_usage 的 usage chunk 里 choices 为空数组——
            # 不能对空列表取 [0]（会 IndexError 崩掉整个流），无 choices 时跳过 delta 解析
            _choices = obj.get("choices") or []
            if not _choices:
                continue
            # v1.0.573：采集 finish_reason——思考型模型思考耗尽 completion 预算被截断
            # （finish_reason=length）时 content 全空，此前无感知、静默当"完成"。
            _fr = _choices[0].get("finish_reason")
            if _fr:
                finish_reason = _fr
            delta = _choices[0].get("delta", {})
            if delta.get("content"):
                content_buf.append(delta["content"])
                _received += len(delta["content"])
                _last_progress_ts = time.time()
                _last_content_ts = time.time()  # v1.0.293：可执行产出刷新无内容熔断基准
                yield ("token", delta["content"])
            # 推理模型（如 Qwen3.8-27B via vLLM）先吐 delta.reasoning 思考流，
            # 可能持续数分钟才进 content。不处理会导致前端全程"思考中"无任何反馈，
            # 且用户无法区分"正在推理"与"卡死"（2026-08-27 现场）。
            # reasoning 不进 content_buf——它不是最终回答，不污染 round_text/上下文。
            if delta.get("reasoning"):
                _received += len(delta["reasoning"])
                _reasoning_chars += len(delta["reasoning"])  # v1.0.628：分诊用，见上文
                _last_progress_ts = time.time()  # 思考 token 也算实质产出（v1.0.287）
                yield ("reasoning", delta["reasoning"])
            for tc in delta.get("tool_calls", []) or []:
                # v1.0.652（审计 P2-3）：index 缺省不再恒 0——多工具调用且服务端不带
                # index 时旧实现全部并进 slot 0，args 串成非法 JSON 静默变 {}。
                # 现按出现顺序分配；无 index 且带 id 视为新调用，否则视为上一调用的续块。
                if tc.get("index") is not None:
                    idx = tc["index"]
                    _last_idx = idx
                    try:
                        _tc_seq = max(_tc_seq, int(idx) + 1)
                    except (TypeError, ValueError):
                        pass
                elif tc.get("id"):
                    idx = _tc_seq
                    _tc_seq += 1
                    _last_idx = idx
                else:
                    idx = _last_idx
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("function", {}).get("name"):
                    # name 首个非空即锁定——部分网关每个 delta 重发全量 name，
                    # += 会拼成 click_wecom_elementclick_wecom_element（unknown tool 空耗轮次）
                    slot["name"] = slot["name"] or tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    slot["args"] += tc["function"]["arguments"]
                    _received += len(tc["function"]["arguments"])
                    _last_progress_ts = time.time()  # 工具参数流也算实质产出（v1.0.287）
                    _last_content_ts = time.time()  # v1.0.293：工具参数=可执行产出
    except Exception as e:  # noqa: BLE001
        # 兜底：流式中途网络断开/响应异常时，不向上抛异常导致 SSE 裸断流，
        # 转成显式 error 事件（丢弃不完整数据，避免半截工具调用污染上下文）。
        partial = "".join(content_buf)
        log_event(f"[llm] 流式中断（正文 {len(partial)} 字 / 思考 {_reasoning_chars} 字）："
                  f"{model.get('name')} -> {e}")
        resp.close()   # v1.0.361 审查修复（P4）：中断路径归还连接
        _release_req()
        yield ("error", f"LLM 流式中断：{e}" + (f"。已生成内容：{partial[-120:]}" if partial else ""), "stream_interrupt")
        return
    finally:
        # v1.0.515 修复（审计 P1-3）：GeneratorExit（客户端断开/生成器链被 GC close）
        # 是 BaseException，不被上面的 except Exception 捕获，会直接穿透 yield 点
        # 跳过 resp.close() 与 _release_req()——连接与硬停止注册项双泄漏。
        # 兜底：仅当 _req_ref 仍注册着（= 正常/错误路径已清理的不会进来）才清理；
        # 用 closesocket 而非 resp.close()——跨线程 close 与泵线程互斥可能死锁
        #（见 _kill_conn 注释），且此场景连接本就无法复用。
        if _req_ref is not None:
            _kill_conn()
            _release_req()
    # ---------- 【段 5/5】收尾：工具参数装配 / 用量统计 / 截断检测 / done ----------
    calls = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        try:
            args = json.loads(slot["args"] or "{}")
        except Exception as e:  # noqa: BLE001
            log_event(f"[agent] 工具参数 JSON 解析失败，按空参数处理：{e}")
            args = {}
        calls.append({"id": slot["id"], "name": slot["name"], "args": args})
    # v1.0.252：Token 用量落盘（独立存档 dist/logs/usage_YYYYMM.jsonl），统计失败不影响主流程
    if usage:
        try:
            from services.usage_stats import record_usage
            record_usage(_model_name, usage)
        except Exception as _e:# noqa: BLE001
            log_event(f"[warn] llm_client.py:573 异常已降级继续: {_e}")  # v1.0.405（H3）：原静默 pass 补日志
    resp.close()   # v1.0.361 审查修复（P4）：流结束归还连接池
    _release_req()
    # v1.0.573：流正常收尾但零正文零工具 + 有思考产出 + finish_reason=length
    # ——思考耗尽 completion 预算被截断（用户截图实锤：17K 字思考 0 回复，
    # 通知却报「完成」）。转显式 error 走前端可见提示，不再静默当完成。
    if not content_buf and not calls and finish_reason == "length":
        yield ("error",
               "模型思考耗尽输出预算被截断（只有思考内容没有回复）。"
               "建议：关闭/降低思考强度，或更换模型后重试。", "think_truncated")
        return
    yield ("done", {"text": "".join(content_buf), "tool_calls": calls, "usage": usage,
                    "finish_reason": finish_reason})


def _ollama_native_base(model):
    """从 /v1 兼容 base_url 推导 Ollama 原生根地址（http://host:11434/v1 → http://host:11434）。
    非 Ollama 风格返回 None。"""
    base = clean_url(model.get("base_url") or "")
    if base.endswith("/v1"):
        return base[:-3]
    return None


def _ollama_think_off_complete(model, messages, timeout=180, poll_abort=None):
    """Ollama 原生 /api/chat 快速通道：think=false 直接关闭思考型模型的思考输出，
    判断类短问答提速数倍（/v1 兼容端会静默忽略 think 参数，必须走原生端，v1.0.239 实测）。
    任何失败返回 None，由调用方回退通用 /v1 路径（兼容 LM Studio / 云端 API 等非 Ollama）。
    v1.0.555 修复（审计 P1-9）：原 docstring 声称"注册 _ACTIVE_RESPONSES 可 closesocket
    打断"，实现却用 requests.post——执行期间没有任何 socket 句柄、无中断手段，
    judge 挂起时点停止/看门狗 abort 只能等满 180s（且标量 timeout 对每次 socket read
    独立计时，慢速滴流可无限续）。现改用 http.client 直连：request() 完成 connect 后
    立即把 socket 注册进硬停止池（覆盖 getresponse 的阻塞读），超时拆为连接 10s /
    读取 timeout 两段；排队同样受 LLM_QUEUE_TIMEOUT 保护（P0-7）。"""
    native = _ollama_native_base(model)
    if not native:
        return None
    if poll_abort is not None and poll_abort():
        return None
    _sem = LLM_GLOBAL_SEMAPHORE
    if _sem:
        if not _sem.acquire(timeout=LLM_QUEUE_TIMEOUT):
            log_event("[llm] ollama 原生通道排队超时（全局并发闸占满），回退通用路径")
            return None
    try:
        import http.client as _hcl
        from urllib.parse import urlparse as _up
        _u = _up(native)
        # v1.0.652（审计 P1-18）：timeout 改单值——http.client.HTTPConnection 的
        # timeout 最终进 socket.settimeout()，只收 float/None；旧传 (10, timeout) 元组
        # （那是 requests 的二元组语法）触发 TypeError 被外层 except 吞掉后恒回退
        # 通用路径，think:false 快速通道自 v1.0.555 起从未成功执行过。
        _conn = _hcl.HTTPConnection(_u.hostname, _u.port or 80, timeout=float(timeout or 30))
        _payload = json.dumps({"model": model.get("model"), "messages": messages,
                               "think": False, "stream": False,
                               "options": {"temperature": 0.2, "top_p": 0.8}})
        _conn.request("POST", "/api/chat", body=_payload,
                      headers={"Content-Type": "application/json"})
        # request() 内部已完成 connect——此刻注册硬停止，覆盖 getresponse/read 的阻塞
        _ref = _ActiveLLMReq(None, getattr(_conn, "sock", None))
        with _ACTIVE_LOCK:
            _ACTIVE_RESPONSES.add(_ref)
        try:
            _resp = _conn.getresponse()
            _raw = _resp.read()
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_RESPONSES.discard(_ref)
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        if _resp.status != 200:
            log_event(f"[llm] ollama 原生通道 HTTP {_resp.status}，回退通用路径")
            return None
        data = json.loads(_raw.decode("utf-8", "replace"))
        content = (data.get("message") or {}).get("content")
        # v1.0.252：原生通道同样埋点（Ollama 字段 prompt_eval_count/eval_count），统计失败不影响主流程
        try:
            if data.get("prompt_eval_count") or data.get("eval_count"):
                from services.usage_stats import record_usage
                record_usage(model.get("model"), data)
        except Exception as _e:# noqa: BLE001
            log_event(f"[warn] llm_client.py 原生通道埋点异常已降级继续: {_e}")  # v1.0.405（H3）
        return content or None
    except Exception as e:  # noqa: BLE001
        log_event(f"[llm] ollama 原生通道异常：{e}，回退通用路径")
        return None
    finally:
        if _sem:
            try:
                _sem.release()
            except Exception:  # noqa: BLE001
                pass


def _llm_complete(model, messages, response_format=None, disable_thinking=False,
                  poll_abort=None, deadline=None):
    """非流式 LLM 完整生成：复用 stream_llm_events（含 response_format 兼容降级），
    收集全部 token 返回纯文本；失败返回 None。供技能优化 / 模板 AI 生成等结构化任务复用。
    disable_thinking=True 时（判断大模型短问答场景）优先走 Ollama 原生 think:false 快速通道。
    v1.0.515（审计 P1-9/P2-11）新增：
      - poll_abort：外部取消钩子（如 judge 升级传 _is_aborted）——每个事件边界检查，
        命中立即放弃（生成器被 GC 走 _stream_body 的 finally 清理）；
      - deadline：整次调用的墙钟上限（秒，基于 time.monotonic）——原实现最坏
        3 次流尝试 ×(600~1170s) ≈ 1 小时占住模型，后台任务无法取消；
      - 半截文本修复：流重试耗尽后原实现返回各次失败尝试拼接的 buf（垃圾文本，
        还会被 ask_judge 缓存固化）——现凡流以 error 收尾一律返回 None。"""
    if disable_thinking and not response_format:
        fast = _ollama_think_off_complete(model, messages, poll_abort=poll_abort)
        if fast:
            return fast
    buf = []
    _interrupt_retries = 0
    _t0 = time.monotonic()
    _had_error = False
    try:
        for ev in stream_llm_events(model, messages, response_format=response_format,
                                    poll_abort=poll_abort):
            if poll_abort is not None and poll_abort():
                log_event("[llm] 完整生成被外部取消（poll_abort）")
                return None
            if deadline is not None and (time.monotonic() - _t0) >= deadline:
                log_event(f"[llm] 完整生成超过 deadline={deadline}s，放弃（防后台任务无限占模型）")
                return None
            if ev[0] == "token":
                buf.append(ev[1])
            elif ev[0] == "error":
                # v1.0.361 审查修复（P2）：流式中断时 continue 消费
                # 后续重试事件——原实现 return None 使生成器被 GC，stream_llm_events 的
                # 透明重试永不到达（agent/loop.py 已修过同款，此处漏改）。
                if is_stream_interrupt(ev):   # v1.0.654 A3：判定单源化
                    _interrupt_retries += 1
                    _had_error = True
                    if _interrupt_retries <= LLM_STREAM_EMPTY_RETRIES + 1:
                        log_event(f"[llm] 完整生成遇流式中断，等待自动重试（第 {_interrupt_retries} 次）")
                        continue
                log_event(f"[llm] 完整生成失败：{ev[1]}")
                return None
            elif ev[0] == "done":
                txt = ev[1].get("text") or ""
                return txt
    except Exception as e:  # noqa: BLE001
        log_event(f"[llm] 完整生成异常：{e}")
        return None
    # v1.0.515（P2-11）：流以 error 收尾（重试耗尽）——buf 是各次失败尝试的拼接
    # 半截文本，不能当有效结果返回（ask_judge 会把它缓存固化）。仅"流正常结束
    # 但 done 事件缺失"的罕见情形才回退 buf，且要求期间零错误。
    if _had_error:
        return None
    text = "".join(buf)
    return text or None

# ---------- 本地模型 API 自动扫描（LM Studio / Ollama） ----------
# 每项：(provider, 显示名, OpenAI 兼容 base_url, 探测路径[(相对路径, 列表字段, id 字段),...])
# 探测路径按顺序尝试，第一个成功即返回；base_url 统一为 /v1 兼容端，供 chat/completions 直接使用。
_LOCAL_MODEL_ENDPOINTS = [
    ("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1",
     [("models", "data", "id")]),
    ("lmstudio", "LM Studio", "http://localhost:1234/v1",
     [("models", "data", "id")]),
    ("ollama", "Ollama", "http://127.0.0.1:11434/v1",
     [("models", "data", "id"), ("api/tags", "models", "name")]),
    ("ollama", "Ollama", "http://localhost:11434/v1",
     [("models", "data", "id"), ("api/tags", "models", "name")]),
]


def _probe_local_models(api_base, probes, timeout=2.5):
    """按顺序探测本地模型 API，返回 (models, error)；models 为 None 表示不可达。"""
    for path, list_key, id_key in probes:
        url = api_base.rstrip("/") + "/" + path
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 401:
                return None, "服务在线但需要 API Key 鉴权（可在「添加模型」中手动填写 API Key）"
            if r.status_code != 200:
                continue
            j = r.json()
            if not isinstance(j, dict):
                continue
            items = j.get(list_key)
            if not isinstance(items, list):
                continue
            models = []
            for m in items:
                if not isinstance(m, dict):
                    continue
                mid = m.get(id_key)
                if mid:
                    models.append({"id": str(mid), "name": str(mid)})
            if models:
                return models, None
        except Exception:  # noqa: BLE001
            continue
    return None, "未探测到可用的本地模型服务"


# ---------- 远程模型列表探测 + 能力推断（v1.0.188） ----------
# OpenAI 兼容服务普遍提供 GET {base}/models 返回 {"data":[{"id":...}]}。
# 思考强度/能力支持没有标准 API 可查，用两层推断：
#   1) 模型名启发式（可靠、零成本）——o1/o3/gpt-5 → reasoning_effort；
#      qwen/deepseek-r1 → thinking；gemini → thinking_config；vision 等
#   2) 可选真实试探：对首个模型发 max_tokens=2 请求带 reasoning_effort=high，
#      200=接受该参数；400 且错误含 unknown/unexpected/extra/parameter → 明确不支持。

_MODEL_CAPS_HINTS = [
    # (关键词列表, caps 键, 值)
    (["o1", "o3", "gpt-5", "gpt-5.1", "x1"], "reasoning_effort", True),
    (["qwen", "qwq"], "thinking", True),
    (["deepseek-r1", "deepseek-reasoner"], "thinking_only", True),
    (["deepseek"], "thinking", True),
    (["gemini"], "thinking", True),
    (["kimi", "moonshot"], "thinking", True),
    (["glm", "zhipu"], "thinking", True),
    (["claude"], "thinking", True),
    (["vision", "vl", "omni", "multimodal", "4o", "gemini", "qwen-vl", "gpt-4.1"], "vision", True),
]
# reasoning_effort 明确支持的模型（低/中/高三档，OpenAI 系）
_REASONING_EFFORT_LEVELS = ("low", "medium", "high")
# thinking 开关类模型（qwen/deepseek/kimi/glm）常见档位语义：低=少思考，高=多思考
_THINKING_LEVELS = ("low", "medium", "high", "xhigh")


def guess_model_caps(model_id):
    """按模型名启发式推断能力位与思考强度档位。返回 (caps, levels, reasoning_effort_supported)。"""
    mid = (model_id or "").lower()
    caps = {"tool_call": True}  # 现代模型默认支持工具调用
    for kws, key, val in _MODEL_CAPS_HINTS:
        if any(kw in mid for kw in kws):
            caps[key] = val
    # 互斥修正：thinking_only 时也认为具备 thinking
    if caps.get("thinking_only"):
        caps["thinking"] = True
        caps["can_disable_thinking"] = False
    else:
        caps.setdefault("can_disable_thinking", True)
    # 思考强度档位：
    effort_supported = any(kw in mid for kw in ("o1", "o3", "gpt-5", "x1"))
    if caps.get("thinking") and not effort_supported:
        levels = list(_THINKING_LEVELS)
    elif effort_supported:
        levels = list(_REASONING_EFFORT_LEVELS)
    else:
        levels = []
    return caps, levels, effort_supported


def probe_remote_models(base_url, api_key="", timeout=6):
    """探测远程 OpenAI 兼容服务的可用模型列表。
    返回 (models, error)；models=[{id, caps, levels, effort}] 或 None。"""
    base = clean_url(base_url or "")
    if not base:
        return None, "接口地址为空"
    if not base.lower().startswith(("http://", "https://")):
        # 不传下去让 requests 抛 InvalidSchema（"No connection adapters were found for …"），
        # 那句话对用户没有可操作性——用户只会反复重贴同一个地址。
        return None, ("接口地址要以 http:// 或 https:// 开头（当前是：" + base[:60] +
                      "）。已自动剔除粘贴带入的零宽字符/BOM；若仍不对，请补上协议头。")
    # 若用户填的是完整 /chat/completions 路径，退一级到 /models
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    url = base + "/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return None, f"连接失败：{e.__class__.__name__}: {e}"
    if r.status_code == 401 or r.status_code == 403:
        return None, "需要 API Key 或 Key 无效（HTTP " + str(r.status_code) + "）"
    if r.status_code == 404:
        return None, "该服务未提供 /models 列表接口（HTTP 404），仍可手动填写模型名"
    if r.status_code != 200:
        return None, "HTTP " + str(r.status_code)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        return None, "响应不是 JSON"
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, list) or not data:
        return None, "模型列表为空"
    out = []
    for m in data:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name") or ""
        if not mid:
            continue
        caps, levels, effort = guess_model_caps(str(mid))
        out.append({"id": str(mid), "name": str(mid),
                    "caps": caps, "levels": levels, "effort": effort})
    return out, None


def probe_reasoning_effort(base_url, api_key, model_id, timeout=6):
    """真实试探：对指定模型发 max_tokens=2 请求带 reasoning_effort=high。
    返回 "supported" | "rejected" | "unknown" | "error:xxx"。"""
    base = clean_url(base_url or "")
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model_id, "messages": [{"role": "user", "content": "hi"}],
               "max_tokens": 2, "stream": False, "reasoning_effort": "high"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return "error:" + e.__class__.__name__
    if r.status_code == 200:
        return "supported"
    # 400/422 且错误信息提到未知参数 → 明确不支持
    body = r.text or ""
    low = body.lower()
    if r.status_code in (400, 422) and any(k in low for k in
            ("unknown", "unexpected", "extra field", "not supported", "invalid parameter",
             "unsupported", "does not support", "additional properties")):
        return "rejected"
    if r.status_code in (400, 422):
        return "unknown"
    return "error:HTTP " + str(r.status_code)

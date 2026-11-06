# 企微伴侣（WeComCompanion）流程分块重构方案

> 作者：架构师（software-architect）　日期：2026-08-27
> 性质：**纯结构性重构**（只分块、不改行为）。所有 API 路由、SSE 事件类型/格式、工具名、
> 数据格式、打包产物必须保持兼容。
> 本文档是工程师的执行依据：每份新文件都标注了「从哪个文件的哪些行号搬移」，可直接照做。

---

## 一、现状盘点

### 1.1 文件清单（行数 + 职责 + 问题标注）

| 文件 | 行数 | 职责 | 问题标注 |
|---|---|---|---|
| `backend.py` | 2677 | Flask app + 28 个路由 + agent 主循环 + LLM 流式客户端 + 上下文压缩 + 死循环熔断 + 配置存取/密钥自愈 + 经验库 CRUD + 模板/快捷入口 AI 生成 + SSE 保活包装 + 启动器（webview/托盘/单实例） | **巨型文件，10+ 种职责混杂**（详见 1.3）；巨型函数 `agent_loop`（L772–1204，433 行）、`stream_llm_events`（L348–531，184 行）；**疑似 NameError：`save_workspace_file`/`workspace_size_info` 在 L2150/L2186 使用但未在 L60–64 导入**；25+ 个模块级全局单例散落 |
| `wecom_bridge.py` | 2097 | 企微 exe 探测/进程检测/幂等绑定/CDP WebSocket 通信内核/窗口枚举/页面读取/交互操作/截图/下载/上传 + 8 块内联 JS | 巨型函数 `ensure_binding`（L247–350，104 行）、`read_fields`（L1465–1611，147 行，含函数内魔法常量）、`download_attachment`（L1841–1942，102 行）；内联 JS 与 Python 函数交错（JS 占约 600 行） |
| `agent_tools.py` | 758 | TOOLS（25 个工具 schema 定义）+ SKILLS（19 个技能）+ CONNECTORS + build_agent_tools + 连接器执行器 + 8 个内置工具执行函数 + `_TOOL_HANDLERS` 注册表 | 工具 schema 描述文本 ~300 行与运行时逻辑混在一个文件；`backend` 反向 import 其内部符号 `_cdp_port`/`_effective_skill`（1.4）；`_run_list_workspace_files` 与 backend `_ws_tree` 重复枚举逻辑 |
| `core_utils.py` | 568 | 路径/产物库/日志轮转/脱敏/多格式文件解析/JSON 抽取与校验 | `_parse_file_content`（L305–521，217 行）一个函数 10 种格式分支；其余职责清晰，**本次不拆** |
| `rule_check.py` | 541 | 脚本判定引擎（6 模块 + RULES 注册表 + run_rule） | 结构良好（注册表模式），**不动** |
| `security.py` | 114 | DPAPI 密钥保护 + 非 Windows 弱保护降级 | 良好，**不动** |
| `index.html` | 2876 | 单文件前端：内联 CSS（L7–414）+ HTML（~420–751）+ JS（L752–末，~2100 行） | **本次不动**（前端契约冻结）；`/api/` 调用 50+ 处、SSE 流解析（`streamChat`/`runStream`）为契约边界 |
| `build.py` | 192 | 一键打包（版本号 +1 + PyInstaller） | 良好，不动（版本号逻辑保持） |
| `WeComCompanion.spec` | 62 | PyInstaller 配置，入口 `backend.py` | `hiddenimports` 硬编码模块名清单（L6–12）——新模块必须补登，见风险 R3 |
| `tray.py` | 91 | pystray 托盘 | 良好，不动 |
| `version.py` / `version_info.txt` / `config.json` | 13/—/— | 版本号 / exe 资源 / 运行配置 | 不动 |
| `patch_p0.py` | 56 | 一次性补丁脚本（历史遗留） | 不影响运行，不动（可后续清理，不在本次范围） |

### 1.2 巨型函数清单（重构优先目标）

| 函数 | 位置（行号） | 行数 | 问题 |
|---|---|---|---|
| `agent_loop` | backend.py L772–1204 | 433 | 主循环 + 3 组死循环熔断 + 并行预取 + 完成确认 + 收尾续跑 + 断点 + SSE 组装 + 保存聊天记录，全部在一个生成器里 |
| `stream_llm_events` | backend.py L348–531 | 184 | HTTP 重试 + response_format/parallel_tool_calls 双重降级 + 后台泵线程 + 双超时熔断 + SSE delta 解析 + tool_calls 累积 |
| `_compress_loop_msgs` | backend.py L664–769 | 106 | 上下文压缩（依赖 `_msg_chars`/`_high_value_summary` 两个辅助） |
| `read_fields` | wecom_bridge.py L1465–1611 | 147 | 关键词命中重试循环 + 3 路增强字段合并 + 函数内魔法常量 `WAIT_RETRIES=4`/`WAIT_STEP=1.5`（L1484–1485） |
| `ensure_binding` | wecom_bridge.py L247–350 | 104 | 8 个分支的绑定状态机 |
| `download_attachment` | wecom_bridge.py L1841–1942 | 102 | fetch 下载 + 浏览器下载回退 + 轮询落盘 |
| `_parse_file_content` | core_utils.py L305–521 | 217 | 10 种文件格式分支（**不拆**，core_utils 保持现状） |
| `api_chat` | backend.py L2345–2424 | 80 | 路由函数内直接执行 7 个 CDP 调用构造上下文（应下沉到 agent 包） |
| `_tpl_agent_gen` / `_quick_agent_gen` | backend.py L2020–2083 / L1700–1764 | 64/65 | 高度同构（90% 重复），随 routes 迁移时**保持两个函数原样**（禁止顺手合并，避免行为漂移） |

### 1.3 职责混杂点（backend.py 为例）

1. **`agent_loop`（L772–1204）**：循环推进、LLM 事件消费、工具并行执行、3 组死循环熔断
   （写 L1036–1114 / 读 L1115–1160 / 报错 L1042–1079）、断点保存、SSE 事件字符串组装、
   聊天记录落盘——6 种职责。
2. **`stream_llm_events`（L348–531）**：HTTP 客户端 + 重试/降级 + 流解析 + 卡死检测，
   4 种职责。
3. **`api_chat`（L2345–2424）**：HTTP 路由 + 企微窗口上下文注入（capture/window_tree/
   pick_window/get_interactive_elements 等 7 处 CDP 调用）+ system prompt 拼装。
4. **模块顶层（L28–213）**：Flask app 实例、DEFAULT_CONFIG、密钥加密/明文自愈
   （L219–299）、LLM 超时常量（L146–157）、熔断阈值（L99–124）、压缩阈值（L118–124）、
   端口 8787、单例互斥——配置/常量/状态/框架对象混在一个文件头部。
5. **`__main__`（L2597–2677）**：启动器（单实例/后台绑定/产物库初始化/Flask 线程/
   webview/托盘/日志配置）约 80 行。

### 1.4 跨文件重复 / 反向依赖

**重复代码：**
1. `wecom_bridge.click_element`（L1047–1126）与 `select_option`（L1640–1722）：
   "文字→TreeWalker 模糊匹配→向上找可点祖先→scrollIntoView→click" 的 JS 块
   （各约 60 行）几乎逐字重复。**本次只随函数原样搬移，不合并**（合并属行为优化，超范围）。
2. `backend._tpl_agent_gen`（L2020）与 `_quick_agent_gen`（L1700）：
   `_sse` 辅助 / extract_json 降级 / token+options+result 事件序列 90% 重复。原样搬移。
3. `backend._ws_tree`（L2164–2180）与 `agent_tools._run_list_workspace_files`（L592–616）：
   产物库子目录枚举逻辑重复。原样保留（两处返回结构不同）。
4. `wecom_bridge` 的 5 处 WebSocket 关闭收尾块（L468–475 / L690–698 / L825–833 /
   L1801–1810 / L2081–2089）完全相同。随搬移保留原样。
5. 绑定降级 status dict 在 `wecom_bridge.ensure_binding` 与 `backend.ensure_binding_on_start`
   各构造一份。保留原样。

**反向 / 交叉依赖：**
1. `backend`（L65–66）反向 import `agent_tools` 的**内部符号** `_cdp_port`、`_effective_skill`，
   被 `api_windows`/`api_capture`/`api_screenshot`/`api_interactive`/`api_chat`（L2126–2145、
   L2351）与技能路由（L1215、L1251）使用 → 路由层依赖工具层内部实现。
2. `wecom_bridge`/`security` 依赖 `backend` 运行时 `set_logger(log_event)` 注入日志
   （backend L311–313），属于对注入时序的隐式耦合（见风险 R6）。
3. `backend._quick_agent_gen`（L1744）使用定义在其后 255 行的 `_tpl_agent_questions`（L1999）
   ——跨功能块私有函数借用。

**魔法数字 / 常量散落（迁移时收拢到模块常量区，值不变）：**
- LLM：`LLM_HTTP_TIMEOUT=(30,None)`、`LLM_MAX_RETRIES=2`、`LLM_IDLE_TIMEOUT=120`、
  `LLM_STREAM_MAX_SECONDS=300`（L149–157）→ `llm_client.py` 常量区。
- 熔断/轮次：`MAX_ROUNDS=100`、`TAIL_FINISH_ROUNDS=10`、`LOOP_WARN_AT=4`、`LOOP_KILL_AT=8`、
  `LOOP_READ_WARN_AT=5`、`LOOP_READ_KILL_AT=9`、`LOOP_ERR_WARN_AT=6`、`LOOP_ERR_KILL_AT=10`、
  `CONTEXT_COMPRESS_THRESHOLD=80000`、`KEEP_LEADING_MSGS=6`、`KEEP_RECENT_MSGS=20`、
  `CHAT_STATES_MAX=10`（L93–132）→ `agent/session.py` + `agent/messages.py` 常量区。
- SSE：`_HEARTBEAT=8.0`（L2284，函数内）、`_q` maxsize 64（L2287）、tool_result summary
  截断 `[:400]`（L1018）、`"data: "... + "\n\n"` 拼接格式（全文 40+ 处）。
- 窗口上下文：正文注入 `[:6000]`（L2378、L1518）、元素清单 `elements[:100]`/`text[:40]`
  （L2413–2414）→ `agent/context.py` 常量区。
- wecom：`wait_page_load` 内 `min(float(timeout), 8.0)`（L569）、`read_fields` 的
  `WAIT_RETRIES=4`/`WAIT_STEP=1.5`（L1484–1485）、`_wait_body_stable` 预算 4.0/0.4/3
  （L511）。
- 其他：`APP_PORT=8787`（L193）→ `config_store.py`；workspace 预览 `limit = 5*1024*1024`
  （L2238）、`over_limit >= 500`（L2187）、上传预览 `[:3000]`（L2216）。
- 工具层：下载 body 截断 `[:6000]`、requests `timeout=30`（agent_tools L482）、
  `timeout=20`/`max_chars 8000`（L688–680）→ 保留在 `agent_tools/executors.py` 常量区。

### 1.5 路由 API 契约（30 个端点，路径/方法/状态码必须原样保持）

| 方法 路径 | 所在函数（backend.py 行号） | 迁移去向 |
|---|---|---|
| GET `/` | index（L1591） | backend.py（保留） |
| GET `/api/status` | api_status（L1596） | routes/management.py |
| GET/POST `/api/config` | get_config/post_config（L1611/L1626） | routes/management.py |
| GET/POST `/api/models` | get_models/post_models（L1785/L1891） | routes/management.py |
| POST `/api/models/test` | api_models_test（L1791） | routes/management.py |
| POST `/api/models/scan` | api_models_scan（L1866） | routes/management.py |
| GET/POST `/api/templates` | get_templates/post_templates（L1927/L1933） | routes/templates.py |
| POST `/api/template/run`(SSE) | api_template_run（L2464） | routes/templates.py |
| POST `/api/template/agent/chat`(SSE) | api_template_agent_chat（L2086） | routes/templates.py |
| POST `/api/quick_links` | post_quick_links（L1650） | routes/management.py |
| POST `/api/quick_links/agent/chat`(SSE) | api_quick_links_agent_chat（L1767） | routes/management.py |
| GET/POST `/api/skills` | get_skills/post_skills（L1208/L1223） | routes/skills.py |
| GET `/api/skills/<sid>` | api_skill_detail（L1244） | routes/skills.py |
| POST/DELETE `/api/skills/<sid>/override` | api_skill_override/delete（L1259/L1291） | routes/skills.py |
| POST `/api/skills/<sid>/optimize` | api_skill_optimize（L1392） | routes/skills.py |
| GET/POST `/api/experiences`、PUT/DELETE `/api/experiences/<eid>` | 4 个（L1347–1389） | routes/skills.py |
| GET/POST `/api/connectors` | get_connectors/post_connectors（L1457/L1474） | routes/skills.py |
| GET `/api/wecom/status`、POST `/api/wecom/bind` | api_wecom_status/api_wecom_bind（L2105/L2110） | routes/wecom.py |
| GET `/api/wecom/windows`、`/api/wecom/capture`、`/api/wecom/interactive`、`/api/wecom/screenshot`、POST `/api/wecom/send` | 5 个（L2124–2160） | routes/wecom.py |
| GET/POST `/api/chat`(SSE)、POST `/api/chat/abort`、POST `/api/chat/continue`(SSE) | api_chat/api_chat_abort/api_chat_continue（L2345/L2427/L2449） | routes/chat.py |
| GET `/api/workspace`、GET `/api/workspace/size`、POST `/api/workspace/upload`、GET `/api/workspace/file`、POST `/api/workspace/open` | 5 个（L2191–2267） | routes/workspace.py |
| POST `/api/rules/check` + GET `/api/rules/list`（同一函数） | api_rules（L2478） | routes/workspace.py |
| 全局 `@app.errorhandler(Exception)` | handle_exception（L1580） | backend.py（保留在 app 上） |

### 1.6 核心函数跨文件引用清单（迁移时 import 必须覆盖）

- `_cdp_port`（agent_tools L621）← backend 5 处路由 + agent_tools 内部 handlers
- `build_agent_tools` ← `agent_loop`（L790）、`api_chat`（L2372）
- `_effective_skill` ← 技能路由 4 处；`SKILLS`/`TOOLS`/`CONNECTORS` ← 路由 + `_skill_tool_names`
- `stream_llm_events` ← `agent_loop`（L894）、`_llm_complete`（L539）、`template_run`（L1531）
- `_llm_complete` ← 技能优化（L1426）、quick/template agent 生成（L1729/L2048）
- `resolve_model` ← agent_loop/status/模板/技能/quick/模型测试 8 处
- `load_config`/`save_config`/`_save_config_safe` ← 几乎全部路由
- `mask_models`（L2516）← 模型路由 4 处；`bind_status`（L2494）← status + wecom 状态
- `_sse_stream` ← 6 个 SSE 路由（chat/chat continue/template run/quick/template agent）
- `SYSTEM_PROMPT`（L555）← api_chat（L2373）
- `chat_states`/`_set_abort`/`_is_aborted`/`_save_chat_state`/`_chat_state_lock` ←
  agent_loop + api_chat/abort/continue + `_sse_stream`
- `wb.*`：ensure_binding/capture/window_list_summary/cdp_up/windows_signature/
  list_targets/pick_window/get_interactive_elements/detect_wecom_exe/find_wecom_pids/
  window_tree_text/send_webhook/capture_screenshot ← backend 路由 + agent_tools handlers

---

## 二、目标文件结构（10 个逻辑模块 / 24 个 Python 文件）

分块原则：**wecom_bridge 与 agent_tools 就地升级为包**（`xxx.py` → `xxx/`，门面
`__init__.py` re-export 全部既有公开符号 → 现有 `import wecom_bridge as wb` /
`from agent_tools import ...` 全部零改动）；**backend.py 只拆不删入口**（仍是 PyInstaller
唯一入口）；每个新文件都对应现状一个可独立描述的职责。

```
企微伴侣/
├── backend.py                 # 【重构后 ≤300 行】Flask app 创建 + 蓝图注册 + 全局异常处理
│                              #   + 日志注入 + __main__ 启动器（单实例/绑定/webview/托盘）
├── config_store.py            # 新增：DEFAULT_CONFIG / load_config / save_config / 密钥加密
│                              #   深合并 / 明文自愈合 / 经验库 experiences.json CRUD /
│                              #   _mask_connector_config / _save_config_safe / APP_PORT
├── llm_client.py              # 新增：resolve_model / _norm_system_first / stream_llm_events /
│                              #   _llm_complete / _HTTP_SESSION / LLM_* 常量 /
│                              #   _LOCAL_MODEL_ENDPOINTS + _probe_local_models
├── tray.py                    # 不动
├── version.py / version_info.txt / build.py / app.ico / index.html / config.json
│                              # 不动
├── WeComCompanion.spec        # 改：仅 hiddenimports 补登新顶层模块/包名
├── core_utils.py              # 不动（保持现有 568 行）
├── security.py                # 不动
├── rule_check.py              # 不动
│
├── wecom_bridge/              # 【wecom_bridge.py 就地升级】CDP 桥接 + 企微进程/绑定
│   ├── __init__.py            # 门面：from .cdp_client import * 等，re-export 原模块全部
│   │                          #   公开符号（DEFAULT_PORT/set_logger/detect_wecom_exe/
│   │                          #   ensure_binding/capture/... 一个不少）
│   ├── cdp_client.py          # CDP 通信内核：CDP_* 常量 + CDP_FLAGS + set_logger/_log/_logw
│   │                          #   + _run_hidden + cdp_up + _cdp_ws_ok + _open_ws + _recv_until
│   │                          #   + _cdp_eval + _cdp_eval_retry + _cdp_command +
│   │                          #   _wait_dom_ready + _BODY_LEN_JS + _wait_body_stable
│   ├── wecom_proc.py          # 进程与绑定：detect_wecom_exe + find_wecom_pids(+winapi/tasklist)
│   │                          #   + _relaunch_with_cdp + ensure_binding + windows_signature
│   ├── windows.py             # 窗口枚举：list_targets + _pages + pick_window +
│   │                          #   _fetch_target_parents + window_list_summary +
│   │                          #   window_tree_text + _page_ws
│   ├── read.py                # 页面读取：wait_page_load + _CAPTURE_JS/capture +
│   │                          #   capture_screenshot + _DOM_RENDER_JS/_dom_render_screenshot
│   │                          #   + _INTERACTIVE_SEL/_BLOCK_SCAN_JS/_INTERACTIVE_JS/
│   │                          #   get_interactive_elements + _READ_PAGE_JS/read_page +
│   │                          #   _READ_FIELDS_JS/read_fields + send_webhook +
│   │                          #   WEBHOOK_MSGTYPES
│   ├── act.py                 # 页面交互：click_element + type_text + scroll_window +
│   │                          #   select_option + _LINKS_JS/list_links + type_document
│   └── download.py            # 下载与上传：DOWNLOAD_* 常量 + set_download_behavior +
│                              #   download_attachment + _fetch_download + upload_file
│
├── agent_tools/               # 【agent_tools.py 就地升级】工具注册表 + 技能 + 连接器
│   ├── __init__.py            # 门面：re-export TOOLS/SKILLS/CONNECTORS/build_agent_tools/
│   │                          #   dispatch_tool/run_tool/is_connector_configured/
│   │                          #   _effective_skill/_cdp_port/_tool_by_name（含私有别名，
│   │                          #   供 backend 现有 import 保持可用）
│   ├── registry.py            # TOOLS 定义（25 个工具 schema，原 L27–221 逐字搬移）
│   ├── skills.py              # SKILLS 定义（19 个技能，原 L236–374 逐字搬移）
│   ├── connectors.py          # CONNECTORS 定义 + is_connector_configured + run_connector
│   ├── executors.py           # 内置工具执行器：DEFAULT_* 常量 + _run_create_skill/
│   │                          #   _run_save_file/_run_download_file/_run_read_workspace_file/
│   │                          #   _run_list_workspace_files/_run_web_search/_run_fetch_webpage
│   │                          #   + _TOOL_HANDLERS + run_tool + _cdp_port + _tool_by_name
│   └── dispatch.py            # build_agent_tools + _effective_skill + dispatch_tool
│
├── agent/                     # 新增包：agent 核心（从 backend.py 拆出）
│   ├── __init__.py            # 门面：agent_loop / _sse_stream / SYSTEM_PROMPT / chat_states
│   ├── messages.py            # _msg_chars + _high_value_summary + _compress_loop_msgs +
│   │                          #   CONTEXT_COMPRESS_THRESHOLD/KEEP_LEADING_MSGS/KEEP_RECENT_MSGS
│   ├── session.py             # 会话状态：_abort_lock/_abort_flags/_set_abort/_is_aborted +
│   │                          #   chat_states/_chat_state_lock/_save_chat_state/CHAT_STATES_MAX +
│   │                          #   MAX_ROUNDS/TAIL_FINISH_ROUNDS/LOOP_* 全部熔断阈值常量
│   ├── context.py             # 窗口上下文注入：build_chat_messages（从 api_chat L2361–2419
│   │                          #   下沉，7 处 CDP 调用原样保留）+ 6000/100/40 截断常量
│   ├── sys_prompts.py         # SYSTEM_PROMPT（L555–589 逐字）+ _QUICK_AGENT_SYSTEM +
│   │                          #   _TPL_AGENT_SYSTEM 三大提示词
│   ├── tools_call.py          # 并行预取：_PARALLEL_SAFE_TOOLS/_CDP_READ_TOOLS +
│   │                          #   execute_calls（从 agent_loop L977–1024 抽出的执行+回灌段）
│   ├── sse_stream.py          # _sse_stream 保活包装（L2270–2342 原样）+ _HEARTBEAT
│   └── loop.py                # agent_loop（L772–1204）：循环体 + 3 组死循环熔断 +
│                              #   完成确认 + 收尾续跑 + SSE 事件组装 + 断点/收尾——
│                              #   整函数逐行搬移，只改 import 与模块内私有名引用
│
└── routes/                    # 新增包：Flask 蓝图（每蓝图 url_prefix="/api"，
    ├── __init__.py            #   路由字符串相应去掉 "/api" 前缀，最终 URL 不变）
    │                          #   提供 register_all(app) 统一注册
    ├── management.py          # 蓝图 management：`/` 除外——/api/status、/api/config×2、
    │                          #   /api/models×2、/api/models/test、/api/models/scan、
    │                          #   /api/quick_links、/api/quick_links/agent/chat +
    │                          #   _quick_agent_gen + mask_models
    ├── skills.py              # 蓝图 skills：/api/skills 全部 7 个端点 + 经验库 4 个端点 +
    │                          #   /api/connectors×2 + _skill_tool_names + _mask_connector_config
    ├── templates.py           # 蓝图 templates：/api/templates×2 + /api/template/run +
    │                          #   /api/template/agent/chat + template_run + _tpl_agent_*
    ├── chat.py                # 蓝图 chat：/api/chat、/api/chat/abort、/api/chat/continue
    ├── wecom.py               # 蓝图 wecom：/api/wecom/status、bind、windows、capture、
    │                          #   interactive、screenshot、send + bind_status（L2494）
    └── workspace.py           # 蓝图 workspace：/api/workspace×5 + /api/rules×2 + _ws_tree
```

**模块数说明**：10 个逻辑模块（core_utils / security / rule_check / config_store /
llm_client / wecom_bridge 包 / agent_tools 包 / agent 包 / routes 包 / backend 入口），
其中 3 个保持现状、7 个新增或升级，符合 8–12 的克制目标。

---

## 三、依赖方向图（无循环依赖）

```mermaid
graph TD
    subgraph L1[基础层（不依赖项目内任何模块）]
        SEC[security.py]
    end
    subgraph L2[纯工具层]
        CU[core_utils.py<br/>依赖 security]
        RC[rule_check.py<br/>零依赖]
    end
    subgraph L3[能力层]
        WB[wecom_bridge 包<br/>门面 __init__ + cdp_client/windows/read/act/download/wecom_proc]
        AT[agent_tools 包<br/>registry/skills/connectors/executors/dispatch<br/>依赖 core_utils + wecom_bridge + rule_check]
        LC[llm_client.py<br/>依赖 core_utils]
        CS[config_store.py<br/>依赖 core_utils + security]
    end
    subgraph L4[Agent 核心]
        AG[agent 包<br/>loop/sse_stream/session/tools_call/context/messages/sys_prompts<br/>依赖 llm_client + agent_tools + core_utils + wecom_bridge]
    end
    subgraph L5[Web 层]
        RT[routes 包<br/>management/skills/templates/chat/wecom/workspace<br/>依赖 agent + llm_client + config_store + wecom_bridge + agent_tools + rule_check]
    end
    subgraph L6[入口]
        BE[backend.py<br/>Flask app + 蓝图注册 + errorhandler + 启动器<br/>依赖 routes + config_store + wecom_bridge + agent + tray(懒)]
    end

    SEC --> CU
    CU --> WB
    CU --> AT
    CU --> LC
    CU --> CS
    AT --> WB
    AT --> RC
    LC --> AG
    AT --> AG
    WB --> AG
    CU --> AG
    AG --> RT
    LC --> RT
    CS --> RT
    WB --> RT
    AT --> RT
    RC --> RT
    RT --> BE
    WB --> BE
    CS --> BE
    AG --> BE
```

规则：只允许上层 import 下层；`agent` 包内部依赖为
`loop → (messages, session, tools_call, sse_stream, context, sys_prompts)`，
`tools_call → agent_tools`，`sse_stream → session`，无环。
**禁止** routes 直接 import `agent.loop` 的私有函数（只走 `agent` 门面）。

---

## 四、共享约定（模块间接口契约）

工程师迁移时必须遵守的跨模块约定：

1. **日志**：唯一日志函数是 `core_utils.log_event`（自带 1MB×5 轮转）。
   下层模块（wecom_bridge、security）不直接 import 它，继续走
   `set_logger(log_event)` 注入 + `_logw` 包装（现有机制不动）。
   日志前缀约定保持：`[agent]` `[llm]` `[config]` `[wecom]` `[bind]` `[workspace]`
   `[tool-error]` `[connector]` `[model-test]` `[error]`（便于 companion.log grep）。
2. **配置对象**：`cfg` 始终是 `load_config()` 返回的 dict；`cfg["wecom"]`/`["models"]`/
   `["templates"]`/`["connectors"]`/`["enabled_skills"]`/`["skill_overrides"]`/
   `["quick_links"]` 键名不变；敏感字段读写全部经 config_store（落盘前 `_encrypt_secrets`、
   读取后 `_decrypt_secrets` 的现有机制逐行保留）。路由保存配置一律走
   `_save_config_safe` → `(ok, err)` 二元组。
3. **LLM 客户端**：
   - `stream_llm_events(model, messages, tools=None, response_format=None)` →
     依次 yield `('token', str) | ('reasoning', str) | ('tool', dict) | ('done', dict) | ('error', str)`，
     事件元组结构不变（`'done'` 载荷 `{text, tool_calls, usage}`）。
   - `_llm_complete(model, messages, response_format=None) -> str | None`。
   - `resolve_model(cfg, model_id=None) -> dict | None`。
   - `_HTTP_SESSION`（requests.Session 复用连接池）必须仍是**单一模块级单例**，
     不能每请求新建。
4. **agent 核心**：
   - `agent_loop(messages=None, cfg=None, max_rounds=None, pane_id=None)`：
     生成器，yield **完整 SSE 数据行字符串**（`"data: {...}\n\n"`），与现状完全一致。
   - `_sse_stream(gen, pane_id=None)`：保活 8s 发 `": keepalive\n\n"` 注释行；
     GeneratorExit/异常时置 abort + 清该 pane 断点；正常走完保留断点。语义不变。
   - `chat_states`（dict，key=paneId，value=`{loop_msgs, rounds}`，LRU 上限 10）与
     `_abort_flags`/锁 都是**模块级单例**，全部集中在 `agent/session.py`，
     其他模块通过 `from agent.session import ...` 使用，不得各自持有副本。
   - 熔断阈值常量（`LOOP_*` 6 个）放 `agent/session.py` 常量区（原 L99–116 注释整段随迁）。
     读类工具判定规则保持：`name.startswith(("capture_","list_","read_","search_","query_","get_")) or name=="finish"`。
   - 工具执行统一走 `agent_tools.dispatch_tool(name, args, cfg)`；返回值必须是 dict，
     非 dict 一律包 `{"ok": True, "result": out}`；错误 `{"__error__": str}`；
     完成 `{"__finish__": True, "final_answer": str}`。工具结果回灌上下文统一截断
     `TOOL_RESULT_MAX_LEN=20000`（core_utils），SSE 展示 summary 截断 `[:400]`。
5. **SSE 契约（冻结）**：事件类型仅
   `meta | token | reasoning | tool | tool_result | usage | error | aborted |
   need_continue | done`（模板流另有 `options | result`）；
   JSON 一律 `json.dumps(..., ensure_ascii=False)`；帧格式 `"data: " + json + "\n\n"`；
   响应头 `mimetype="text/event-stream; charset=utf-8"` + `Cache-Control: no-cache` +
   `X-Accel-Buffering: no`；全部经 `stream_with_context(_sse_stream(...))` 包装。
   **前端 index.html 的 SSE 解析逐行依赖上述格式，任何字节级差异都会破坏功能。**
6. **路由契约（冻结）**：1.5 节 30 个端点的路径/方法/成功状态码/错误状态码（400/404/500）/
   响应 JSON 结构全部不变。蓝图用 `url_prefix="/api"`，函数内路由字符串去掉前缀，
   拼接结果必须等于原路径；`GET /` 保留在 backend.py 的 app 上（`send_file(resource_path("index.html"))`）。
   `@app.errorhandler(Exception)` 保留注册在 backend.py 的 app 上（蓝图级不注册）。
7. **wecom_bridge 门面**：`__init__.py` 必须 re-export 原模块全部公开名（含
   `DEFAULT_PORT`、`set_logger`、全部公开函数）；包内子模块间用
   `from .cdp_client import _cdp_eval, ...` 相对导入。`import wecom_bridge as wb`
   与 `wb.xxx` 调用点在 agent_tools/executors、agent/context、routes 中**零改动**。
8. **PyInstaller**：入口仍是 `['backend.py']`；`datas=[('index.html','.'), ('app.ico','.')]`
   不变；每个**新顶层模块/包名**（`config_store`、`llm_client`、`agent`、`routes`、
   以及包内子模块名）追加进 `hiddenimports`；模块内懒加载 import（`from ddgs import DDGS`、
   `from bs4 import BeautifulSoup`、OCR/pypdfium2/docx/openpyxl/pptx/xlrd/olefile/striprtf
   等）保持懒加载形式不动（spec 已 collect_all，勿改）。
9. **冻结检测**：路径逻辑继续走 `core_utils.resource_path`/`_app_dir`
   （`sys._MEIPASS`/`sys.frozen`），各新模块不得自行判断冻结态。
10. **单例清单（迁移后必须仍是全局唯一实例）**：`llm_client._HTTP_SESSION`、
    `config_store._config_lock`、`wecom._BIND_CACHE`（backend 顶层，保留在 backend.py）、
    `_SINGLE_MUTEX`（backend.py）、`agent.session.chat_states/_abort_flags`、
    `core_utils._OCR_ENGINE`。

---

## 五、有序任务列表（5 个任务，按依赖顺序）

> 通用验收（每个任务完成后都要做）：
> ① `python -m py_compile` 全部 .py 通过；
> ② 源码直跑 `python backend.py`，启动日志出现 `[ok] 伴侣已启动`；
> ③ 打开前端完成一轮最小冒烟：状态刷新（/api/status）+ 一次 /api/chat 对话（SSE token/done
>    正常）+ 一次 read_wecom_page 工具执行；
> ④ git diff 审查：所有搬移代码**逐字**，无逻辑/文案/阈值改动（import 与文件位置除外）。

### T01 — wecom_bridge 升级包骨架 + CDP 通信内核（cdp_client）
- **目标文件**：删除 `wecom_bridge.py`，新建 `wecom_bridge/`：
  `__init__.py`、`cdp_client.py`；原 `wecom_bridge.py` 内容（除 cdp_client 部分外）
  **暂存于 `wecom_bridge/legacy.py`**（T02 拆分用，T02 完成后删除）。
- **具体动作**：
  1. `cdp_client.py` 承接原 L26–35（`DEFAULT_PORT`/`CDP_CONNECT_TIMEOUT`/`CDP_EVAL_TIMEOUT`/
     `CDP_UPLOAD_TIMEOUT`/`CDP_SCREENSHOT_TIMEOUT`/`CDP_SCREENSHOT_TRY_TIMEOUT`/`DEVNULL`/
     `CDP_FLAGS`）、L37–78（`_log`/`set_logger`/`_logw`/`CREATE_NO_WINDOW`/
     `_hide_startupinfo`/`_run_hidden`）、L202–221（`cdp_up`/`_cdp_ws_ok`）、
     L401–440（`_open_ws`）、L423–435（`_recv_until`）、L437–488（`_cdp_eval`/
     `_cdp_eval_retry`）、L491–543（`_wait_dom_ready`/`_BODY_LEN_JS`/`_wait_body_stable`）、
     L1787–1810（`_cdp_command`）。逐字搬移，函数体零改动。
  2. `legacy.py` = 原文件剩余部分（进程/绑定/枚举/读取/交互/下载/上传），内部改
     `from .cdp_client import _cdp_eval, _cdp_eval_retry, _open_ws, _recv_until, _run_hidden,
     CREATE_NO_WINDOW, CDP_*, _logw, _wait_dom_ready, _wait_body_stable, _BODY_LEN_JS,
     _cdp_command`。
  3. `__init__.py`：`from .legacy import *` + 显式 `from .cdp_client import set_logger,
     DEFAULT_PORT, ...`（把原模块 `dir()` 里所有公开名全部导出，保证
     `import wecom_bridge as wb` 后 `wb.<任意旧名>` 可用）。
- **验收标准**：`python -c "import wecom_bridge as wb; assert wb.ensure_binding and wb.capture"`；
  通用验收 ①–④ 通过；`wb` 对外符号集合与重构前一致（可用 `set(dir(wb))` 对比）。

### T02 — wecom_bridge 包内拆分（wecom_proc / windows / read / act / download）
- **目标文件**：`wecom_bridge/wecom_proc.py`、`windows.py`、`read.py`、`act.py`、
  `download.py`；删除 `wecom_bridge/legacy.py`；更新 `wecom_bridge/__init__.py`。
- **具体动作**（源行号指原 wecom_bridge.py，内容逐字搬移）：
  1. `wecom_proc.py`：L82–199（`detect_wecom_exe`/`find_wecom_pids`/`_find_wecom_pids_winapi`/
     `_find_wecom_pids_tasklist`）+ L224–350（`windows_signature`/`_relaunch_with_cdp`/
     `ensure_binding`）。
  2. `windows.py`：L353–441 中 `list_targets`/`_pages`/`pick_window` + L795–941
     （`_fetch_target_parents`/`window_list_summary`/`window_tree_text`/`_page_ws`）+
     L928–929（`_INTERACTIVE_SEL`，被 act.py 使用的选择器常量）。
  3. `read.py`：L546–792（`wait_page_load`/`_CAPTURE_JS`/`capture`/`capture_screenshot`/
     `_DOM_RENDER_JS`/`_dom_render_screenshot`/`WEBHOOK_MSGTYPES`/`send_webhook`）+
     L949–1044（`_BLOCK_SCAN_JS`/`_INTERACTIVE_JS`/`get_interactive_elements`）+
     L1185–1611（`_READ_PAGE_JS`/`read_page`/`_READ_FIELDS_JS`/`read_fields`）。
     注意 `read_fields` 内 `WAIT_RETRIES=4`/`WAIT_STEP=1.5` 保留为**函数内局部常量**
     （或提至 read.py 模块常量区，值不变）。
  4. `act.py`：L1047–1181（`click_element`/`type_text`）+ L1613–1784
     （`scroll_window`/`select_option`/`_LINKS_JS`/`list_links`/`type_document`）。
     click 与 select 中的重复 JS 块**不合并**，原样保留。
  5. `download.py`：L1813–1815（`DOWNLOAD_WAIT_TIMEOUT`/`DOWNLOAD_POLL_STEP`）+
     L1818–2089（`set_download_behavior`/`download_attachment`/`_fetch_download`/
     `upload_file`）。
  6. 各子模块 `from .cdp_client import ...` / `from .windows import _page_ws, list_targets, ...`，
     相对导入；`__init__.py` 改为从 5 个子模块汇总 re-export（公开符号集与 T01 后一致）。
- **验收标准**：`dir(wecom_bridge)` 公开符号集与重构前一致；通用验收 ①–④ 通过；
  额外冒烟：前端「企微」页窗口列表刷新 + 手动截图（/api/wecom/screenshot）正常。

### T03 — agent_tools 升级包 + config_store.py + llm_client.py
- **目标文件**：删除 `agent_tools.py`，新建 `agent_tools/`（`__init__.py`、`registry.py`、
  `skills.py`、`connectors.py`、`executors.py`、`dispatch.py`）；新建 `config_store.py`、
  `llm_client.py`；`backend.py` 中对应段落删除并改 import（本任务 backend 允许被改，
  但只动 import 行与已搬走函数的删除，路由与 agent_loop 暂留原处）。
- **具体动作**：
  1. `agent_tools/registry.py` ← 原 L27–221（TOOLS 25 项，逐字）；
     `agent_tools/skills.py` ← 原 L236–374（SKILLS 19 项 + 文件头设计注释）；
     `agent_tools/connectors.py` ← 原 L378–419（CONNECTORS + `is_connector_configured`）
     + L464–491（`run_connector`）；
     `agent_tools/executors.py` ← 原 L20–23 常量（DEFAULT_SCROLL_AMOUNT/DEFAULT_MSG_TYPE/
     DOWNLOAD_TIMEOUT）+ L514–710（8 个 `_run_*` 执行函数）+ L621–627（`_cdp_port`）+
     L712–742（`_TOOL_HANDLERS`）+ L745–758（`run_tool`）+ L403–411（`_tool_by_name`）；
     `agent_tools/dispatch.py` ← 原 L422–461（`_effective_skill`/`build_agent_tools`）+
     L494–511（`dispatch_tool`）；`agent_tools/__init__.py` re-export：
     `TOOLS, SKILLS, CONNECTORS, build_agent_tools, dispatch_tool, run_tool,
     is_connector_configured, _effective_skill, _cdp_port, _tool_by_name, _finish_tool`
     （与 backend L65–66 现有 import 完全对齐）。
  2. `config_store.py` ← backend L193（`APP_PORT`）、L195–213（`DEFAULT_CONFIG`）、
     L215–216（`_config_lock`）、L219–309（`_encrypt_secrets`/`_decrypt_secrets`/
     `load_config`/`_has_plaintext_secret`/`save_config`）、L1309–1344（经验库
     `_experiences_path`/`_load_experiences`/`_save_experiences`/`_exp_rec`）、
     L1440–1454（`_mask_connector_config`）、L1642–1647（`_save_config_safe`）。
     注意：`_security_set_logger`/`wb.set_logger` 注入**仍留在 backend.py 顶层**（时序要求，见 R6）。
  3. `llm_client.py` ← backend L145–169（LLM 超时常量 + `_PARALLEL_SAFE_TOOLS` 除外——
     该两个 frozenset 属于 agent，放 `agent/tools_call.py`，L158–167 留给 T04）+
     L169（`_HTTP_SESSION`）+ L316–328（`resolve_model`）+ L332–345（`_norm_system_first`）+
     L348–552（`stream_llm_events`/`_llm_complete`）+ L1821–1863（`_LOCAL_MODEL_ENDPOINTS`/
     `_probe_local_models`）+ L1799–1818 中 `api_models_test` 内联的 URL 拼接逻辑**不动**
     （留在路由函数里，属业务代码）。
  4. `backend.py` 顶部 import 区改为：
     `from config_store import (load_config, save_config, _save_config_safe, DEFAULT_CONFIG,
     APP_PORT, _mask_connector_config, _load_experiences, _save_experiences, _exp_rec)`；
     `from llm_client import (resolve_model, stream_llm_events, _llm_complete,
     _probe_local_models, _LOCAL_MODEL_ENDPOINTS)`；`from agent_tools import (...)` 名称不变
     （现在来自包门面）。
- **验收标准**：`python -c "from agent_tools import TOOLS, SKILLS, CONNECTORS, build_agent_tools, dispatch_tool, run_tool, is_connector_configured, _effective_skill, _cdp_port"` 通过；
  通用验收 ①–④ 通过；额外冒烟：/api/models/test、/api/models/scan、保存一次技能开关
  （/api/skills POST）、一次 run_rule_check 工具调用（/api/rules/check POST）。

### T04 — agent 包 + sys_prompts（backend.py 首次大瘦身）
- **目标文件**：新建 `agent/`（`__init__.py`、`messages.py`、`session.py`、`context.py`、
  `sys_prompts.py`、`tools_call.py`、`sse_stream.py`、`loop.py`）；backend.py 删除对应段落。
- **具体动作**（源行号指重构前 backend.py）：
  1. `agent/session.py`：L69–88（abort 标志）、L93–124（MAX_ROUNDS/TAIL_FINISH_ROUNDS/
     熔断阈值/压缩阈值——压缩 3 常量随 `messages.py` 走，这里保留 L93–116 + L126–142
     的 `chat_states`/`_chat_state_lock`/`CHAT_STATES_MAX`/`_save_chat_state`）。
     所有注释（含 P1-3/P1-5 等修复说明）**整段随迁**。
  2. `agent/messages.py`：L592–769（`_msg_chars`/`_high_value_summary`/`_compress_loop_msgs`
     + `CONTEXT_COMPRESS_THRESHOLD`/`KEEP_LEADING_MSGS`/`KEEP_RECENT_MSGS`）。
  3. `agent/sys_prompts.py`：L555–589（SYSTEM_PROMPT）+ L1676–1697（`_QUICK_AGENT_SYSTEM`）
     + L1971–1996（`_TPL_AGENT_SYSTEM`）三大提示词逐字搬移（**提示词是行为本体，一个字都不能改**）。
  4. `agent/tools_call.py`：L158–167（`_PARALLEL_SAFE_TOOLS`/`_CDP_READ_TOOLS`）+
     从 agent_loop L977–1024 抽出工具执行段为
     `execute_calls(calls, cfg, pane_id)`：并行预取 + 逐个 `dispatch_tool` + dict 防御 +
     `tool`/`tool_result` SSE 事件 yield + `__finish__` 识别 + loop_msgs 回灌与
     `_msgs_chars` 增量返回。
     **实现约束**：为保持 agent_loop 逐行等价，`execute_calls` 设计为
     「返回 (events 列表或生成器, 更新后的 _msgs_chars, finished, final_answer,
     asst_idx 信息)」的纯函数形态；若等价性无法 100% 保证，**允许退化为不抽
     execute_calls，tools_call.py 只放两个 frozenset 常量**，工具执行段留在 loop.py。
     该取舍以「逐行等价可验证」为唯一标准。
  5. `agent/sse_stream.py`：L2270–2342（`_sse_stream`，原样）+ 内部 `_HEARTBEAT=8.0` 提为
     模块常量（值不变）；import 自 `agent.session`。
  6. `agent/context.py`：从 `api_chat` L2361–2419 抽出
     `build_chat_messages(messages, cfg, port, pick, skill_prompt) -> list`
     （含 `wb.capture`/`window_tree_text`/`pick_window`/`list_targets`/
     `get_interactive_elements` 7 处调用与 4 个 system 消息拼装，异常兜底文案逐字保留）；
     常量 `CTX_BODY_MAX=6000`/`CTX_ELEM_MAX=100`/`CTX_ELEM_TEXT_MAX=40`。
  7. `agent/loop.py`：L772–1204（`agent_loop`）整函数逐行搬移；import 改为
     `from .session import ...`、`from .messages import _compress_loop_msgs, _msg_chars,
     CONTEXT_COMPRESS_THRESHOLD`、`from llm_client import stream_llm_events, resolve_model`、
     `from agent_tools import build_agent_tools, dispatch_tool`、`from core_utils import
     save_chat_log, _mask_args, TOOL_RESULT_MAX_LEN`。函数体内 3 组熔断状态
     （`loop_last_sig`/`loop_same_streak`/`loop_dup_warned`/`loop_read_*`/`loop_err_*`）
     与 `asst_idx` 回退逻辑**保持函数内局部变量原样**，禁止提为跨文件类状态。
  8. `agent/__init__.py`：`from .loop import agent_loop`；`from .sse_stream import _sse_stream`；
     `from .session import chat_states, _set_abort, _is_aborted, _save_chat_state, _chat_state_lock`；
     `from .sys_prompts import SYSTEM_PROMPT`；`from .context import build_chat_messages`。
  9. `backend.py`：删除上述已搬段落（约 -900 行）；`api_chat` 改用
     `build_chat_messages(...)`（行为等价）；其余路由/模板生成函数暂留 backend（T05 迁走）。
- **验收标准**：`python -c "from agent import agent_loop, _sse_stream, SYSTEM_PROMPT, chat_states"`；
  通用验收 ①–④ 通过；**重点冒烟**：一轮长对话触发 meta/rounds 计数正常；
  手动构造重复工具调用验证熔断干预 token 事件仍能出现（可在测试对话中让模型连点同一无效
  元素）；「继续」断点恢复（/api/chat/continue）可用；「停止」（/api/chat/abort）立即生效。

### T05 — routes 包 + backend.py 最终瘦身 + spec 更新 + 全量验收
- **目标文件**：新建 `routes/`（`__init__.py`、`management.py`、`skills.py`、`templates.py`、
  `chat.py`、`wecom.py`、`workspace.py`）；`backend.py` 最终 ≤300 行；
  `WeComCompanion.spec` hiddenimports 更新。
- **具体动作**：
  1. 按 1.5 节「迁移去向」列把 28 个路由函数 + 配套私有函数搬入对应蓝图文件
     （`_quick_agent_gen`→management、`_tpl_agent_questions`/`_tpl_agent_gen`/
     `template_run`→templates、`bind_status`→wecom、`_ws_tree`→workspace、
     `mask_models`→management）。蓝图统一 `Blueprint("xxx", __name__, url_prefix="/api")`，
     路由字符串去 `/api` 前缀（`/api/skills/<sid>` → `"/skills/<sid>"` 等）。
     经验库 4 个端点从 config_store 调 `_load_experiences`/`_save_experiences`/`_exp_rec`。
  2. `routes/__init__.py` 提供 `register_all(app)`：注册 6 个蓝图。
  3. `backend.py` 收敛为：模块头注释（更新路由清单）→ import（flask、routes、
     config_store、llm_client、agent、wecom_bridge、core_utils、security、tray/version）→
     `app = Flask(__name__)` → `register_all(app)` → `@app.route("/")` →
     `@app.errorhandler(Exception)` → `_BIND_CACHE={}` / `_SINGLE_MUTEX=None` /
     `ensure_single_instance` / `_exit_app` → 日志注入
     （`_security_set_logger(log_event)` + `wb.set_logger(log_event)`，**必须位于所有
     首次日志调用之前**）→ `_serve`/`ensure_binding_on_start`/`_startup_bind`/`_tray_loop`
     （L2524–2594 原样保留在 backend.py）→ `__main__`（L2597–2677 原样）。
     **顺带修复（见 R7）**：确认 `save_workspace_file`/`workspace_size_info` 来自
     core_utils 的正确 import（现状疑似缺失导致 /api/workspace/size 500）。
  4. `WeComCompanion.spec`：`hiddenimports` 追加
     `'config_store', 'llm_client', 'agent', 'agent.loop', 'agent.session',
     'agent.messages', 'agent.sse_stream', 'agent.tools_call', 'agent.context',
     'agent.sys_prompts', 'routes', 'routes.management', 'routes.skills',
     'routes.templates', 'routes.chat', 'routes.wecom', 'routes.workspace',
     'wecom_bridge', 'wecom_bridge.cdp_client', 'wecom_bridge.wecom_proc',
     'wecom_bridge.windows', 'wecom_bridge.read', 'wecom_bridge.act',
     'wecom_bridge.download', 'agent_tools', 'agent_tools.registry',
     'agent_tools.skills', 'agent_tools.connectors', 'agent_tools.executors',
     'agent_tools.dispatch'`（`core_utils/rule_check/security/tray/version` 已在原清单，
     保留；原清单中 `'core_utils', 'agent_tools', 'rule_check', 'security'` 4 项保留不删）。
  5. 全量验收：
     ① 源码直跑全流程：启动 → 企微绑定 → 常规对话 → 快捷任务（5 轮约束）→ 继续/停止 →
     模板运行（/api/template/run）→ 技能优化（/api/skills/<sid>/optimize）→
     模板 AI 生成 → 快捷入口 AI 生成 → 经验库 CRUD → 产物库上传/浏览/打开 →
     本地模型扫描 → 模型测试 → 截图/窗口列表。
     ② SSE 契约核对：浏览器 Network 面板确认 chat 流含 `: keepalive` 注释行、
     事件序列 meta→(reasoning)→token→tool→tool_result→…→done 与重构前一致。
     ③ 打包验收：`python build.py --yes` 产出 dist/WeComCompanion.exe；exe 启动后
     重复 ① 的核心链路（绑定 + 对话 + 一次财务审核类工具链：read_wecom_page →
     read_wecom_fields → run_rule_check）；确认 `resource_path("index.html")` 在冻结态
     可读到、config.json/产物库路径在 exe 同级。
     ④ 回归对照：`git stash` 切回重构前版本，任选一条财务审核完整任务流，
     对比重构前后 companion.log 中 `[agent]` 事件序列（工具名/顺序/熔断日志）一致。
- **验收标准**：以上 ①–④ 全部通过；backend.py ≤300 行；`grep -rn "import backend"` 结果为空
  （无任何模块 import backend）；`dir(wecom_bridge)`、`dir(agent_tools)` 公开符号集与
  重构前一致。

**依赖关系**：T01 → T02 → T03 → T04 → T05 严格串行（T02 依赖 T01 包骨架；
T03 的 backend import 改动依赖 T02 门面稳定；T04 的 agent 包依赖 T03 的
llm_client/agent_tools 就位；T05 依赖 T04 的 agent 门面）。每完成一个任务做一次 git commit
（commit message 标注 T0x），便于二分回滚。

---

## 六、风险清单（按破坏功能的可能性排序）

| # | 风险 | 说明 | 缓解措施 |
|---|---|---|---|
| R1 | **agent_loop 拆分**（最高） | 433 行生成器内 9 个熔断状态变量跨轮共享 + `loop_msgs[asst_idx]` 原地改写 + `del loop_msgs[asst_idx+1:]` 回退 + `_msgs_chars` 增量计数，任何跨文件化都可能破坏「回退本轮未执行工具」语义 | 熔断三组状态 + 主循环**必须同文件**（loop.py 函数内局部变量）；`execute_calls` 抽取以「逐行等价可验证」为唯一标准，不能保证就不抽；T04 验收含熔断专项冒烟 |
| R2 | **SSE 字节契约** | 前端逐字节解析 `data: ` 帧与 `: keepalive` 注释行；`ensure_ascii=False`、`\n\n` 结尾、事件类型拼写都是硬契约 | 所有 yield 语句**逐字搬移不重写**；T04/T05 验收含 Network 面板 SSE 序列对照 |
| R3 | **PyInstaller 打包** | spec `hiddenimports` 硬编码模块名；新包/新顶层模块漏登 → 冻结态 ModuleNotFoundError（tray/version 历史上就是这么保的）；`datas` 缺 index.html → 首页 404 | T05 第 4 步显式补登全清单；打包后 exe 必测（T05 验收 ③）；禁止把懒加载 import 改成顶层 import（会引入新收集依赖） |
| R4 | **Flask app/蓝图/上下文** | `@app.errorhandler` 必须在最终 app 上注册；`stream_with_context` 包装不能丢；`send_file(resource_path(...))` 依赖 spec datas；`_BIND_CACHE.clear()` 引用的对象必须与 ensure_binding_on_start 更新的是**同一个 dict** | errorhandler/`/`/app 创建全部留在 backend.py；`_BIND_CACHE` 不迁移（留 backend.py 顶层）；蓝图只挂路由不建 app |
| R5 | **模块级单例分裂** | `_HTTP_SESSION`/`chat_states`/`_abort_flags`/`_SINGLE_MUTEX`/`_OCR_ENGINE` 若因 `__init__` 与子模块重复加载出现两份实例 → 断点丢失/连接池失效/双开进程 | 单例只定义在唯一叶子模块（约定第 10 条清单）；`__init__.py` 只做 import re-export，不定义状态；验收时 `python -c "import agent, agent.loop; assert agent.chat_states is agent.loop.chat_states"` 类断言 |
| R6 | **启动时序** | `set_logger(log_event)` 注入（backend 顶层）必须先于 wb/security 的首次日志；`ensure_single_instance` 必须先于任何子线程创建；`_startup_bind` 后台线程先于 `webview.start`；config 明文自愈合写盘发生在每次 `load_config` | 日志注入与 `__main__` 整体**留在 backend.py**，顺序逐行不动；T05 验收含 exe 冷启动日志核对 |
| R7 | **/api/workspace/size NameError（现存 bug）** | backend L2186 调用 `workspace_size_info()`、L2150 调用 `save_workspace_file()`，但两者未从 core_utils import → size 接口 500、截图落盘静默失效（被裸 `except: pass` 吞掉）。疑似 v1.0.65 拆分遗漏 | T05 第 3 步补 import 修复（纯结构性，实现原设计意图）；**行为变化需主理人确认**：size 接口从 500 变 200、截图开始落盘到产物库/下载。若确认不修，则本项从任务中剔除并在报告中保留记录 |
| R8 | **提示词/文案漂移** | SYSTEM_PROMPT、19 个技能 prompt、3 个 AI 生成系统提示词、10 条熔断干预文案、SSE 用户可见文案（如 need_continue 文本）都是「行为本体」 | 统一规则：字符串**逐字搬移**，禁止"顺手润色"；git diff 时字符串改动应为 0 |
| R9 | **wecom_bridge 门面完整性** | 包化后若 `__init__` 漏导出某个符号（包括被 backend 使用的 `window_tree_text`/`list_targets`/`pick_window` 等"半公开"函数）→ 运行时 AttributeError | T01/T02 用 `set(dir(...))` 新旧对比做符号集断言；1.6 节引用清单逐项核对 |
| R10 | **内联 JS 常量搬迁** | 8 块内联 JS（_CAPTURE_JS/_DOM_RENDER_JS/_BLOCK_SCAN_JS/_INTERACTIVE_JS/_READ_PAGE_JS/_READ_FIELDS_JS/_LINKS_JS 及函数内 expr）是 CDP 行为本体 | 只改"所在文件"，JS 字符串内容字节级不动；特别注意 `_INTERACTIVE_JS` 由 `_BLOCK_SCAN_JS.replace(...)` 在**模块导入时**生成——两个常量必须搬进**同一文件**（read.py），保持该生成语句原样 |

---

## 七、明确不做什么（范围冻结）

1. 不改任何功能逻辑：熔断/压缩阈值、重试次数、超时值、截断长度**一个数都不动**。
2. 不升级/新增/删除任何依赖；不改 `build.py` 版本号与打包参数（仅 spec hiddenimports 增补）。
3. 不动 wecom_bridge 内联 JS 的业务逻辑（只允许纯结构性移动，JS 内容字节级不变）；
   `click_element`/`select_option` 的重复 JS 块、`_tpl_agent_gen`/`_quick_agent_gen` 的同构逻辑
   **都不合并**。
4. 不改 API 路由路径/方法/状态码、SSE 事件类型/格式、工具名/参数 schema、
   config.json 磁盘格式、experiences.json 格式、产物库目录结构。
5. 不动 index.html 前端（CSS/JS 一律不改）。
6. 不重构 core_utils.py / rule_check.py / security.py / tray.py / build.py。
7. 不引入新框架、新设计模式、类型注解改造、测试框架（本次零新增依赖）；
   不重命名任何对外符号（含下划线"私有"符号中已被跨模块使用的：
   `_cdp_port`、`_effective_skill`、`_sse_stream`、`_llm_complete`、`_save_config_safe` 等）。
8. `patch_p0.py` 历史补丁脚本不处理。

---

*报告完。执行入口：按 T01→T05 串行推进，每任务一个 commit，验收不过不进入下一任务。*

# 企微伴侣（WeComCompanion）

Windows 桌面应用（pywebview + Python 3.13），面向企业微信场景的 AI 工作伴侣：
月报/财务批审自动化、学习中心、自我修复体检、多模型 LLM、工作台、知识库等。

## 快速开始

**下载即用**：从 [Releases](https://github.com/suibuzhifanhua/WeComCompanion/releases) 下载
`WeComCompanion-vX.Y.Z-win64.zip`，解压后直接运行 `WeComCompanion.exe`，无需安装。
首次运行自动生成默认配置与产物库目录。

**源码运行**：Python 3.13 环境 `pip install -r requirements.txt` 后 `python backend.py`
直接启动（弹出 GUI 窗口，后端监听 8787 端口）；或 `python build.py` 打包为单文件 exe。
详见 [开发环境与构建](#开发环境与构建)。

## 功能亮点

- **月报/财务批审自动化**：分支机构月报批量处理、Excel 交叉校验、断点续跑、后台队列
- **学习中心**：课件采集（SVG 直连 / OCR 双路径）、考试助手（断点续考、提分策略）、无人值守跑课
- **自我修复体检**：LLM 体检 + 白名单自动修复 + 每日守护
- **多模型 LLM**：本地 Ollama / OpenAI 兼容接口，流式输出 + 判断大模型兜底切换
- **工作台 / 知识库 / 任务宏 / 语音转写 / 智能体团队 / 三工作模式 / 三档权限审批**

## 目录
1. [功能总览](#功能总览)
2. [技术架构](#技术架构)
3. [开发环境与构建](#开发环境与构建)
4. [开发习惯（本项目的约定）](#开发习惯本项目的约定)
5. [常用操作速查](#常用操作速查)

## 功能总览

| 模块 | 说明 |
|---|---|
| 工作台 | 企微实时窗口列表 + 截图预览 + 可操作元素 + Agent 聊天；三工作模式（🤖 直接干 / 📋 先出方案 / 💬 只问答，重启记忆）；**三档权限级别**（🔒 每次询问 / 🛡 自动审批 / ⚡ 完全访问，按任务记忆，v1.0.590）；👥 智能体团队开关；**多任务并行**（会话切换不断流，列表 ⏳ 标记）+ **每任务独立模型**（下拉选择按会话记忆）|
| 助手对话 | 不绑定窗口的通用对话；未启动企微也能对话并操控电脑（pc_* / 文件 / 联网） |
| 学习中心 | 无人值守逐门完成学习平台课程（视频拖尾/文档/考试自动试错提分），断点续跑 |
| 模型管理 | 多模型配置（OpenAI 兼容 API + Ollama 原生快速通道），默认/启用/双模型分工（judge 判断大模型，卡点自动介入）/Token 用量统计/一键跑分（速度+质量+工具调用）/本地模型扫描/多模型自动兜底 |
| 模板 | 结构化输出模板（输入字段 → 结果 schema）+ AI 生成模板 |
| 技能中心 | 28 个内置技能 + 公共技能库（内置 12 精选 + 远程清单 URL）+ 自定义技能 CRUD + **技能版本历史与一键回滚**（⏱ 版本按钮，变更前自动快照，保留 20 份）+ 总闸 |
| 连接器 | 通用 HTTP 连接器（配置 Base URL/Key 后 Agent 可调外部 API）+ 功能管理总闸 |
| 自我修复 | 🩺 LLM 体检：错误日志+经验库 → 结构化发现 → 白名单自动修复（技能教训/经验条目），代码/配置类只出报告；实时进度（阶段+已接收字符数）、随时取消、每日自动体检、失败自动换备用模型；实验性自动代码修复闭环（补丁→验证→提交/回滚）；报告落盘 产物库/自修复/ |
| 版本管理 | 软件内版本库：保留最近 3 个版本（源码快照 + exe 备份 + 自动变更清单），设置页一键切换回退（切换前自动快照当前版，重启生效） |
| 产物库 | Agent 产出的统一存放处（对话记录/下载/产出/技能/上传/知识库三分类/记忆/日志/自修复报告/版本库） |
| 经验库 | 软件内置经验沉淀区：模块 → 问题 → 解决方法，可增删改查（`experiences.json` 独立存储） |
| 知识库 | 课件/题库/制度三分类自动归档 + 空闲自动整理（INDEX）与消化摘要 + **企微聊天文件静默分析**（附件/企业网盘明文文档入库，agent 可检索；进度/空消化状态可视。**v1.0.588 起需先在知识库页明确授权**——该功能会把聊天附件正文复制进本机知识库，未授权时定时与手动都不读取任何文件，可随时撤回）|
| 设置 | 界面偏好 / 功能管理（四总闸）/ 用户固定规则 / 定时任务 / 企微绑定 / **🧩 运行组件体检**（OCR/语音/Git/Node/打包环境/dsh/企微 七类状态 + 一键安装/升级/国内源指引）/ OCR 组件在线安装与更新 / 系统信息 |
| 帮助 | 软件内使用速查（与本 README 功能总览同步更新） |

**内置技能（28 个，均可开关；另可从「公共技能库」一键安装 12 个精选技能、或自建自定义技能）**：窗口感知、窗口操作、文件产出、企微消息、答题助手、数据下载、数据整理、流程分析、流程总结、财务审核、财务单据核查、报销单填写、联网搜索、会议纪要、文档审阅、周报月报、数据报表分析、公文通知起草、资料调研、待办审批处理、归档整理。

**核心能力**：
- 窗口感知：读取企微窗口正文、列出可交互元素（含必填项检测、label[for] 关联、同名 label 消歧）
- 窗口操作：点击、输入、选择、滚动、上传文件、编辑在线文档（对 MUI/自定义 div 组件做了文字定位回退）；输入/选择后**自动回读验证**（verify:false 让模型重试，防 React 受控组件吞输入）
- 表单处理：`read_wecom_page` 返回必填进度 `form_progress` + 表单快照 diff `changed_fields`（未变化字段不重复占上下文）+ 字段必填标记 `required:true`
- 多格式文件：上传文件给 Agent 分析，支持 txt/csv/md/json/pdf/docx/xlsx/pptx 自动解析
- 联网搜索：内置搜索/抓网页工具，技能中心可扩展资讯检索类任务
- 专项技能：网页答题（含两层交卷确认、存疑题交卷前列出）、财务单据批量核查、报销付款单自动填写（含金额一致性校验）、流程合理性分析（描述 vs 附件交叉验证，硬约束 ≤5 次工具调用、5 轮内完成，禁止逐个下载全部附件）、流程总结（结构化提取步骤/角色/材料/审批规则）、会议纪要/文档审阅/周报月报/数据报表分析/公文起草/资料调研/待办审批/归档整理
- 判定策略（脚本优先）：财务审核/财务单据核查/数据整理/数据报表分析/归档整理/报销单填写 6 个模块的确定性规则由 `rule_check.py` 脚本判定（<1ms、零 LLM 成本），模型仅兜底；报销/核查支持**金额一致性校验**（明细合计 vs 总额，差>0.01 判 red）；答题、文档审阅等理解型模块保持模型判定
- 经验沉淀：`save_experience` 工具把答题规律/审批约定/流程陷阱写入经验库（跨任务记忆），后续同类任务自动遵循
- 工具参数 schema 校验：内置工具调用前校验必填/枚举/类型，缺参返回结构化错误让模型自纠正
- **自我修复（v1.0.348+）**：设置页「🩺 自我修复」——每日/手动 LLM 体检（错误日志+经验库），实时进度（阶段+已接收字符数+随时取消），白名单自动落地技能教训与经验条目（均可回滚），代码/配置类只出报告；多模型自动兜底 + 重试链修复；实验性自动代码修复（补丁→验证→提交/回滚）；失败/成功均落盘可查
- **软件版本管理（v1.0.359+）**：每次构建自动登记（源码 hash + exe 备份 + 自动变更清单），保留最近 3 版；设置页「📦 版本管理」一键切换回退，切换前自动快照当前版，重启生效
- **技能版本回滚（v1.0.349+）**：技能中心「⏱ 版本」——编辑/自修复/清除覆盖前自动快照完整提示词（20 份/技能），一键回滚且回滚本身可逆
- **卡住自救（v1.0.347+）**：agent 反复失败触发熔断干预时，随附未安装公共技能清单并引导 request_skill 请求安装——先找技能再硬试

---

## 技术架构

```
WeComCompanion.exe (PyInstaller onefile, --noconsole)
├── backend.py          Flask app 骨架 + 启动器（鉴权/启动绑定/看门狗/webview，~880 行）
├── core/               核心基础层（v1.0.402 自 core_utils 按域拆分，转发壳保兼容）
│   ├── paths.py        路径与产物库/知识库常量（workspace_root/ws_sub/kb_dir）
│   ├── logging.py      日志轮转/事件记录/agent 活动时间戳
│   ├── config.py       配置路径/脱敏/参数拾取/JSON Schema 校验/原子写
│   ├── config_store.py 配置读写/加解密/事务 update_config/DEFAULT_CONFIG
│   ├── workspace_io.py 产物库写入/旧布局迁移/台账/拖拽白名单（DND）
│   ├── ocr.py          OCR 引擎/在线安装（wheel 直解压）/版本更新检查
│   ├── instance_lock.py 单实例 mutex（含管理员重启竞态修复，v1.0.646 自 core/security 改名）
│   ├── secrets.py      DPAPI 密钥保护（v1.0.647 自顶层 security.py 归位）
│   ├── notify.py       任务通知 + Windows 原生 Toast
│   ├── files.py        文件内容解析（pdf/docx/xls/ppt/OCR/HTML，v1.0.647 起含老格式 Office 转换）
│   ├── daemon.py       守护线程/看门狗两原语（v1.0.654：6 套守护骨架收敛于此）
│   ├── result.py       错误契约单源（v1.0.655 B5：err/is_err/err_msg，收读方双读）
│   ├── frontend_check.py 前端结构/JS 语法校验实现（冻结包内可复用）
│   ├── idle.py / portable.py / relocate.py / safe_open.py / smoke.py
│   │                   用户占用检测 / 便携运行时 / 目录归位 / 安全打开 / 打包冒烟
│   ├── strings.py / study_js.py / wecom_state.py
│   └── ─（历史合并：json_utils→config、office→files、security→instance_lock）
├── services/           功能服务层
│   ├── batch_flow.py   批审流水线（v1.0.656 三层化：FlowTransport/ReportExtractor/AuditOrchestrator）
│   ├── rule_check.py   规则判定引擎（确定性规则优先，<1ms 零成本）
│   ├── self_repair.py  自我修复：LLM 体检/白名单自动修复/报告落盘/每日守护
│   ├── scheduler.py    定时任务调度（30s tick + 串行 worker + 看门狗）
│   ├── study_engine.py 学习中心引擎 / learn_capture.py 课件采集
│   ├── knowledge_ops.py 知识库空闲整理/每日 AI 消化 / wecom_archive.py 企微文件归档
│   ├── asr.py          语音转写组件（按需安装）/ model_bench.py 模型评测
│   ├── daily_digest.py 每日工作小结 / todo_watch.py 待办监视（v1.0.654 骨架收敛）
│   ├── conversations.py 会话存档/恢复 / memory_ops.py 三层记忆 / skill_evolve.py 技能自演化
│   ├── version_store.py 版本登记/切换 / usage_stats.py 用量统计 / task_fingerprint.py 指纹
│   └── skill_versions.py 等（详见目录）
├── desktop/            桌面集成
│   ├── close_guard.py  X 关闭拦截（自绘 Win32 对话框 + web 确认层）/全局热键
│   ├── save_dialog_auto.py 另存为弹框自动应答 / tray.py 托盘 / shell.py 壳层装配
├── routes/             HTTP 路由蓝图（统一 url_prefix=/api，18 模块）
│   ├── management.py   组装壳 + 嵌套注册 6 个子蓝图
│   ├── b_models.py     模型管理 / b_repair.py 自我修复 / b_ocr_admin.py OCR 组件
│   ├── b_memory.py     记忆管理 / b_macro.py 任务宏 / b_admin.py 管理/配置/看板/快捷链
│   ├── chat.py         /chat /chat/abort /chat/continue（SSE + 断点续跑）
│   ├── wecom.py        企微 bind/windows/capture/screenshot / conversations.py 会话
│   ├── knowledge.py / workspace.py / templates.py（含 _agent_gen 公共 AI 生成器）
│   ├── skills.py / study.py / personal.py / security.py（安全中心/审批双因子）
├── agent/              对话运行时层
│   ├── loop.py         agent_loop 主循环（ReAct + 无限续跑 + LoopGuard 熔断）
│   ├── loop_ops.py     主循环辅助（配对修复/save_state_safe/judge 升级）
│   ├── loop_guard.py   死循环/交替循环检测 / messages.py 上下文压缩
│   ├── sse_parse.py    【完成】滚动缓冲抽取（v1.0.654 单源）/ sse_stream.py SSE 心跳包装
│   ├── session.py      abort/断点存储 / context.py 上下文拼装 / sys_prompts.py 系统提示词
│   └── auto_loop.py（考试续跑）/ team.py（规划-执行-审查）
├── wecom_bridge/       CDP 桥接层（v1.0.655 按域拆分，windows.py 兼容再导出全量名）
│   ├── cdp_client.py   CDP 内核（连接池/求值/总超时；_json_result 解析单源）
│   ├── wecom_proc.py   进程探测/绑定/端口避让 / frames.py iframe 上下文
│   ├── windows.py      窗口交互域（_page_ws/_require_page）+ 再导出保 monkeypatch 语义
│   ├── targets.py / screenshot.py / os_windows.py / tucker.py
│   │                   枚举选择 / 截图 / OS 窗口与收纳授权 / 新窗口后台化
│   ├── read.py / act.py / download.py / win_send.py（页面读取/操作/下载/键击发消息）
│   ├── js_loader.py + js/  read 内嵌 JS 外置（match/mui_candidates/read_page/read_fields）
├── agent_tools/        工具注册表 + 执行器（registry→dispatch→executors 表驱动）
├── llm_client.py       LLM 客户端：流式事件/重试/多模型兜底/is_stream_interrupt 单源
├── security_ops.py     安全中心：命令黑白名单/路径保护/审批双因子/审计 JSONL
├── css/style.css       前端样式（设计令牌化，深浅双主题一套变量）
├── index.html          前端骨架（单页全视图）
├── js/                 前端 16 个功能域文件（core/nav/chat/windows/usage/templates/dashboard…）
├── build.py            打包编排：版本 +1 + 六道门禁 _run_gate + --release（职责拆入 buildlib/）
├── buildlib/           打包域实现（v1.0.655）：gates/check/verify/backup/aux/release/ship/trace 等 13 模块
├── tools/              开发期脚本（check_frontend 打包门禁 / scan_silent_except 等）
├── requirements.txt    运行时依赖固化（v1.0.405 起，52 项）
├── requirements-dev.txt 开发依赖（pyinstaller + pytest + pyflakes）
└── WeComCompanion.spec 打包配置（hiddenimports 构建时自动刷新 + app.ico + version_info）
```

> **运行时数据目录 `产物库/`**（dist 下，exe 实际使用；源码根同名目录为 venv 直跑用）：
> 对话记录/下载/产出/技能/上传 五个基础目录 + 日志（companion.log + 用量/安全审计）+
> 版本（versions.json 近 3 版回退库）+ 断点（会话断点落盘，重启可「继续」，v1.0.405）+
> 系统/api_token.txt（本机 API 鉴权令牌，HttpOnly cookie 校验，v1.0.405）+
> 备份（agent 覆盖/删除前的自动备份）+ 宏（任务宏）/知识库/自修复/自进化/记忆。

> **v1.0.401/402 分模块化改造**（按 `docs/archive/重构计划.md` 执行前两阶段，计划已归档）：
> 阶段一目录归位（services//desktop//core + CSS 外置）；
> 阶段二 core_utils 拆入 core/ 十域 + management 拆 6 蓝图（转发壳保兼容，
> 路径函数属性查找——patch 定义处即可全局生效）。URL 与功能零变化，
> 197 项回归全绿。阶段三（模块热更新）按计划暂缓。
> **v1.0.162 结构重构说明**（历史）：wecom_bridge/agent_tools 升级为包，
> 门面 re-export 保持 import 兼容，路由表 44 条前后一致。

### 关键链路

1. **CDP 接管**：企微需以 `--remote-debugging-port=9222 --remote-allow-origins=*` 启动。
   软件通过 `http://127.0.0.1:9222/json` 枚举窗口，经 websocket（`websocket-client`）向页面注入 JS 执行读取/操作。
2. **Agent 循环**：ReAct 模式——LLM 流式输出（`stream_llm_events`）→ 解析工具调用 → `dispatch_tool` 执行 →
   结果回填上下文 → 继续。常规对话**无限续跑**（v1.0.129）：不设轮次上限，上下文超
   `CONTEXT_COMPRESS_THRESHOLD=80000` 字符自动压缩中间历史后继续推进，不再触顶中断；
   任务级 `max_rounds`（如流程分析快捷任务 = 5 轮）保留「主预算 + 收尾 10 轮」约束（`TAIL_FINISH_ROUNDS`）；
   `finish` 工具结束并输出最终结论；死循环熔断（写/读/持续报错三通道）与断点续跑（`/api/chat/continue`）兜底。
3. **SSE 流式**：`text/event-stream; charset=utf-8`，事件类型：`token` / `tool` / `tool_result` / `error` /
   `need_continue` / `done` / `aborted`。
4. **技能体系**（对标 WorkBuddy）：`SKILLS` 声明式注册表（id/name/icon/desc/prompt/tools/default，v1.0.95 起增 `judge` 字段：
   `script`/`model`/`none`），`build_agent_tools(cfg)` 按 `enabled_skills` 动态装配工具与提示词；
   `_TOOL_HANDLERS` 注册表 + `dispatch_tool` 路由。
5. **脚本优先判定**：`rule_check.py` 的 `RULES` 注册表按模块实现确定性规则（财务审核/财务单据核查/数据整理/
   数据报表分析/归档整理/报销单填写）；script 模块 prompt 优先调 `run_rule_check`（`data` 结构按模块约定，
   返回 `__error__` 才退回模型），<1ms、不消耗 LLM；理解型模块（答题/审阅/会议纪要等，judge=model）保持模型判定。
6. **产物库**：`_app_dir()/产物库/`，5 个可写段：对话记录 / 下载 / 产出 / 技能 / 上传；
   其余顶层目录（台账/日志/日报/备份/自修复/记忆/宏/会话/版本…）由 `paths.tree_subs()`
   **动态**作只读段展示，新增目录不用回来补文档（v1.0.616 口径）；
   v1.0.619 起首启**延后**建立产物库（闸门：开机延时 / 任何一次写操作 / 目录本已存在）；
   `ws_rel_path()` 防路径穿越；`save_file/download_file/read_workspace_file/list_workspace_files/create_skill` 全套工具。

### 运行时

- Python venv：`%USERPROFILE%\.workbuddy\binaries\python\envs\wecom\Scripts\python.exe`
  （2026-09-17 迁机重建；路径按当前用户自适应。build.py 的 PYINSTALLER 探测顺序：当前用户 wecom → 当前用户 default → 历史机器固定路径 → PATH）
- Node：`%USERPROFILE%\.workbuddy\binaries\node\versions\22.22.2-3\node.exe`
- 前端界面由 pywebview 6.2.1 包裹（WinForms / WebView2 渲染）
- 日志：`dist/产物库/日志/companion.log`（排错第一入口；v1.0.275 起，dist 顶层那份已废弃）

---

## 开发环境与构建

### 环境初始化（新机器/重建环境）

```bash
PY="$HOME/.workbuddy/binaries/python/envs/wecom/Scripts/python.exe"
"$PY" -m pip install -r requirements.txt -r requirements-dev.txt
# requirements.txt = 运行时依赖（52 项，v1.0.405 自打包 venv freeze 固化）
# requirements-dev.txt = pyinstaller + pytest（测试/打包）
```

### 每次改代码后的标准验证栈（必须全过）

```bash
# 0. 全程带 PYTHONUTF8=1（Windows 默认 cp936，中文断言/输出会假红）
export PYTHONUTF8=1
PY="$HOME/.workbuddy/binaries/python/envs/wecom/Scripts/python.exe"

# 1. Python 语法（全模块；v1.0.401/402 归位后的新路径）
"$PY" -m compileall -q core services desktop routes agent agent_tools wecom_bridge tools tests

# 1.5 未定义名门禁（v1.0.588 起 build.py 内置，F821 命中即拒打包；开发时也先跑）
"$PY" -c "import build,sys; sys.exit(1 if build.check_undefined_names() else 0)"

# 1.6 开发期目录引用门禁（v1.0.619：生产代码 import tools/tests 这类冻结包里不存在的
#     目录 → 运行期静默失效，--smoke 只导包内已有的模块、发现不了，只能打包前静态判）
"$PY" -c "import build,sys; sys.exit(1 if build.check_dev_only_imports() else 0)"

# 2. 前端结构一键校验：div 配对 / 重复 id / CSS 花括号 /（有 node 时）JS 语法
#    实现已上移到 core.frontend_check（随包发布），tools/check_frontend.py 只是 CLI 壳
"$PY" tools/check_frontend.py .

# 3. 全量回归（test_dual_model 为局域网 Ollama 环境依赖，离线机器 --ignore）
#    预期 **0 failed**；本机没装 faster-whisper 时 tests/test_asr.py 有 2 条 skip
#    （缺环境依赖走 skip 不走 fail——v1.0.619 前这里长期挂着 3 条"环境性红"，
#     真回归混在固定噪音里就看不出来）
"$PY" -m pytest tests/ -q --ignore=tests/test_dual_model.py

# 4. 杀旧进程 → 重新打包（自动版本 +1，内置前端校验 + except-pass 计数 + dist 备份还原）
taskkill /f /im WeComCompanion.exe
python build.py            # 常规升级包（保留用户 config/产物库）
python build.py --release  # 对外分发包（剥离开发机 config.json/产物库/日志，防内网拓扑泄露）

# 5. 实跑验证（v1.0.405 起 /api/* 需鉴权 cookie：先访问 / 拿 Set-Cookie）
TOK=$(cat "dist/产物库/系统/api_token.txt")
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/api/status   # 无 cookie → 401（正确）
curl -s -H "Cookie: __wc_token=$TOK" http://127.0.0.1:8787/api/status       # 200
curl -s -H "Cookie: __wc_token=$TOK" http://127.0.0.1:8787/api/skills       # 200
curl -s -X POST http://127.0.0.1:8787/api/chat -H "Cookie: __wc_token=$TOK" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"回一个字：好"}],"use_wecom_context":false}'
```

### 构建注意

- `build.py` 每次自动 `version.py` 版本号 +1；spec 内嵌全模块 hiddenimports
  （v1.0.555 起由 `build._autogen_hiddenimports()` 在构建前**自动重写** spec——
  新增模块不用再手工补登，手工双写反而会漏；历史上 tray/version 就是漏登导致冻结态
  ModuleNotFoundError 的。真要排查用 `python -m PyInstaller --log-level WARN` 看缺模块）。
- 打包前务必 `Stop-Process -Name "WeComCompanion" -Force`（或 `taskkill /f /im WeComCompanion.exe`），
  否则 PyInstaller 可能因文件占用失败；**绝不手动 `rm -rf dist`**（产物库/config.json/companion.log
  会被清掉，build.py 有备份-还原逻辑，交给它处理）。
- 产物库有两个位置：源码 `企微伴侣/产物库`（venv 直跑）vs `dist/产物库`（exe 实际用）。
  **测试/恢复目录/验证文件都要落 dist**，否则 exe 里看不到。
- `--release` 模式会把开发机的 config.json/产物库/日志移到 `build/_release_excluded/<时间戳>/`
  （不是删除，可取回）；发布产物首次运行自动生成默认 config。
  **每次剥离都新增一份**，v1.0.619 起剥离完会报出累计份数与体积（那里面是 PII，别整目录外发）；
  确认不再回取时清旧份：`python build.py --prune-release --yes`（始终保留最新一份；
  `_dist_backup/_rollback/_chat_archive` 三处属回滚/存档，这个命令一律不碰）。

## 开发习惯（本项目的约定）

这些是长期迭代中形成的工作方式，改代码时默认遵守：

1. **验证栈固定**：任何改动都必须过「py_compile + node --check + 重打包 + 实跑 curl」四步，缺一不可。
2. **根因优先**：问题先查 `dist/产物库/日志/companion.log` 和真实 DOM/CDP，不靠猜；用日志证据链定位（对比时间戳）。
3. **工具选择器要覆盖现代组件**：`querySelectorAll` 的选择器要包含 `[role="button"]/[role="textbox"]/label` 等，
   不能只依赖原生 tag（input/select）——MUI/React 组件大量用 div+role。
4. **拼接 JS 禁止 `//` 注释**：Python 字符串拼出的单行 JS 里 `//` 会把后续所有代码注释掉，只能用 `/* */` 或不写。
5. **交互类操作默认"不自动提交"**：agent 填完表单不点提交/保存，必须用户确认——不可逆操作边界。
6. **技能 prompt 要"硬约束"**：把实测跑通的步骤、异常兜底（重读/跳过/如实报告）、禁止行为（如"未答 0 前不许交卷"）写死。
7. **长任务防呆**：SSE 加看门狗、LLM 加流式读超时、工具加重试——任何单点挂起都不该让前端白等或断连。
8. **前端消息顺序**：工具块要 insertBefore 到 agent 消息之前，最终总结保持在底部（滚动必见）。
9. **多轮文本隔离**：非首轮 LLM 输出前插分隔线，最终汇报前插【完成】分隔，避免文本粘连。
10. **产物落地**：报告/下载/技能等一切产出必须 save_file 落产物库，并在 final_answer 里给出路径。

11. **清理只碰可再生物，且默认只预览**：`python tools/junk_clean.py` 列清单（不删），加 `--apply` 才执行。
    白名单 = 字节码/测试缓存、台子的 headless Chrome profile、TEMP 里台子与测试自己 `mkdtemp` 留下的工作区、台子截图；
    `build/_pack_*.log`（历次打包取证）要 `--include-logs` 才动。**黑名单写死不可删**：`dist/`、`产物库/`、`docs/`、
    `build/_rollback/`（无 git 时唯一回滚手段）、`build/_dist_backup/`（普通打包靠它还原）、`build/_chat_archive/`（归档对话）；
    判据与"删错就红"的锁在 `tests/test_tools_junk_clean.py`（本机实测一次清出 334.4 MB + 3390 个测试泄漏的合成工作区）。

---

## 常用操作速查

```bash
# 启动（企微需带调试参数）
"C:\Program Files (x86)\WXWork\WXWork.exe" --remote-debugging-port=9222 --remote-allow-origins=*

# 重打包
taskkill /f /im WeComCompanion.exe
python build.py

# 课件采集回归测试
python tests/regress_learn_capture_oopif.py   # OOPIF SVG 直连路径（无外部依赖）
python tests/regress_learn_capture.py          # iframe 截图 OCR 路径（无头浏览器）

# 看日志（v1.0.275 起在产物库/日志/，dist 顶层已废弃）
tail -50 "dist/产物库/日志/companion.log"

# 探后端（v1.0.405 起 /api/* 需鉴权：先从 / 拿 token 文件换 cookie）
TOK=$(cat "dist/产物库/系统/api_token.txt")
curl -s -H "Cookie: __wc_token=$TOK" http://127.0.0.1:8787/api/status
curl -s -H "Cookie: __wc_token=$TOK" http://127.0.0.1:8787/api/skills

# 连企微 CDP
curl -s http://127.0.0.1:9222/json

# 上传文件给 agent
curl -s -F "file=@xxx.pdf" -H "Cookie: __wc_token=$TOK" http://127.0.0.1:8787/api/workspace/upload

# 产物库
# 源码侧：企微伴侣/产物库    exe 侧：企微伴侣/dist/产物库（实际使用）
```

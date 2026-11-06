# -*- coding: utf-8 -*-
"""update_readme.py — 阶段二后更新 README 技术架构章节。一次性工具。"""
import io
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)

p = 'README.md'
s = io.open(p, encoding='utf-8').read()
k = s.find('## 技术架构')
j = s.find('### 关键链路')
assert k > 0 and j > k

new_arch = '''## 技术架构（v1.0.402 分模块化后）

```
WeComCompanion.exe (PyInstaller onefile, --noconsole)
├── backend.py          Flask app 骨架 + 启动器（单实例/webview/启动绑定，~490 行）
├── core/               核心基础层（v1.0.402 自 core_utils 按域拆分，转发壳保兼容）
│   ├── paths.py        路径与产物库/知识库常量（BASE/workspace_root/ws_sub/kb_dir）
│   ├── logging.py      日志轮转/事件记录/agent 活动时间戳/_LazyRequests
│   ├── config.py       配置路径/脱敏/参数拾取/JSON Schema 校验
│   ├── workspace_io.py 产物库写入/旧布局迁移/台账/拖拽白名单（DND）
│   ├── ocr.py          OCR 引擎/在线安装（wheel 直解压）/版本更新检查
│   ├── security.py     单实例 mutex（含管理员重启竞态修复）
│   ├── notify.py       任务通知中心 + Windows 原生 Toast
│   ├── office.py       老版本 Office 格式转换（PS COM 脚本）
│   ├── files.py        文件内容解析（pdf/docx/xls/ppt/图片 OCR/HTML）
│   ├── json_utils.py   LLM 输出 JSON 抽取/Schema 校验
│   └── config_store.py 配置读写/加解密/经验库 CRUD/DEFAULT_CONFIG
├── services/           功能服务层（v1.0.401 顶层归位）
│   ├── self_repair.py  自我修复：LLM 体检/白名单自动修复/报告落盘/每日守护
│   ├── scheduler.py    定时任务调度（30s tick）
│   ├── version_store.py 版本登记/一键切换（近 8 版）
│   ├── conversations.py 会话存档/恢复
│   ├── knowledge_ops.py 知识库空闲整理/每日 AI 消化
│   ├── study_engine.py  学习中心引擎 / learn_capture.py 课件采集
│   ├── batch_flow.py    批量对话流 / rule_check.py 规则判定引擎
│   └── memory_ops.py    三层记忆 / skill_evolve.py 技能自演化
├── desktop/            桌面集成（v1.0.401 归位）
│   ├── close_guard.py  X 关闭拦截（自绘 Win32 对话框）/全局热键
│   └── tray.py         系统托盘（pystray：打开/退出/双击还原）
├── routes/             HTTP 路由蓝图（统一 url_prefix=/api）
│   ├── management.py   组装壳：bp 定义 + 嵌套注册 6 个子蓝图（54 行）
│   ├── b_models.py     模型管理 API（列表/评测 bench7/扫描/探针/导入导出）
│   ├── b_repair.py     自我修复 API（体检/报告/取消/一键打包并重启）
│   ├── b_ocr_admin.py  OCR 组件管理 API（状态/安装/更新）
│   ├── b_memory.py     记忆管理 API（列表/删行/查看/清空）
│   ├── b_macro.py      任务宏 API（存档/重放/删除）
│   ├── b_admin.py      管理员/自启动/经验/连接器/状态/配置/用量/快捷链/通知/版本/规则
│   ├── skills.py       技能列表/详情/覆盖/LLM 优化
│   ├── templates.py    模板 CRUD / AI 生成 / 结构化运行（SSE）
│   ├── chat.py         /chat /chat/abort /chat/continue（SSE + 断点续跑）
│   ├── wecom.py        企微 bind/windows/capture/interactive/screenshot/send
│   └── workspace.py    产物库树/上传/读取/打开/大小
├── agent/              对话运行时层
│   ├── loop.py         agent_loop 主循环（ReAct + 无限续跑 + 死循环熔断三通道）
│   ├── session.py      abort 标志 / 轮次与熔断常量 / 断点存储
│   ├── context.py      对话上下文拼装（含企微窗口上下文）
│   ├── messages.py     消息压缩（字符统计/高价值摘要）
│   ├── sse_stream.py   SSE 包装器（8s 心跳防 WebView2 断连）
│   ├── sys_prompts.py  三大系统提示词
│   ├── tools_call.py   工具执行白名单（并行安全/CDP 只读）
│   └── auto_loop.py / team.py / loop_guard.py
├── wecom_bridge/       CDP 桥接层
│   ├── cdp_client.py   CDP 通信内核（ws 连接/求值/超时/重试）
│   ├── wecom_proc.py   进程探测/拉起/带 CDP flag 重启
│   ├── windows.py      窗口枚举/窗口树/截图/webhook
│   ├── read.py         正文/字段/元素读取（含 MUI 文字回退）
│   ├── act.py          点击/输入/选择/滚动/上传
│   └── download.py     附件下载
├── agent_tools/        工具注册表 + 执行器
│   ├── registry.py     TOOLS 声明
│   ├── skills.py       SKILLS 内置技能
│   ├── connectors.py   CONNECTORS 通用 HTTP 连接器
│   ├── executors.py    工具执行器 + dispatch + 参数 schema 校验
│   ├── dispatch.py     build_agent_tools 动态装配
│   ├── dsh_bridge.py   外部 CLI 桥（run/超时杀树）
│   ├── os_ops.py       系统命令/文件操作（安全边界）
│   └── office_gen.py   文档生成（Excel/Word/PPT）
├── llm_client.py       LLM HTTP 客户端：流式事件/重试/多模型兜底/本地探测
├── security.py         DPAPI 加密配置敏感字段
├── rule_check.py       规则判定引擎（确定性规则优先，<1ms 零成本）
├── css/style.css       前端样式（v1.0.401 外置，63K）
├── index.html          前端骨架（CSS 外置后 ~1.1K 行）
├── js/                 前端 13 个功能域文件（boot/nav/chat/windows/usage/templates...）
├── build.py            版本 +1 打包（前端结构校验关卡 + 自动登记版本库）
├── tools/check_frontend.py  前端结构校验（div/id/CSS/JS 五项，打包前强制）
└── WeComCompanion.spec 打包配置（hiddenimports 全清单 + app.ico + version_info）
```

> **v1.0.401/402 分模块化改造**（按 `docs/重构计划.md` 三阶段执行前两阶段）：
> 阶段一目录归位（services//desktop//core + CSS 外置）；
> 阶段二 core_utils 拆入 core/ 十域 + management 拆 6 蓝图（转发壳保兼容，
> 路径函数属性查找——patch 定义处即可全局生效）。URL 与功能零变化，
> 197 项回归全绿。阶段三（模块热更新）按计划暂缓。
> **v1.0.162 结构重构说明**（历史）：wecom_bridge/agent_tools 升级为包，
> 门面 re-export 保持 import 兼容，路由表 44 条前后一致。

'''
s = s[:k] + new_arch + s[j:]
io.open(p, 'w', encoding='utf-8').write(s)
print('README arch updated, total lines:', s.count('\n'))

# 企微伴侣 死代码与无效交互审查报告（2026-10-08）

> 审查方式：AST 扫描器（tools/tmp_deadscan.py，词频交叉验证）+ 3 路并行深度探查（Python 后端 / JS 前端 / 按钮交互层）+ 逐项人工 grep 复核。
> 结论标注：✅ 可安全删 ｜ ⚠️ 可删但需注意 ｜ 🔧 不建议删（应重新接线修复）。
> 排除项：dist/、build/、产物库/、docs/、__pycache__；tests/ 内的 test_* 函数由 pytest 收集，不算死代码。

---

## 一、P0 级发现：不是死代码，是断链功能（🔴 重点）

### A1. 安全审批弹窗整条前端链路断裂（孤儿功能）
- **位置**：`js/10-chat.js:1139 showApprovalModal()`（内部 :1164 POST /api/security/grant + :1168 continueChat 重试）
- **现状**：
  - 后端链路完好：`agent/loop.py:760-767` 检测 `__approval_needed__` → yield SSE 事件 `type:'approval_request'`（含 kind/hash/reason/command/path）→ 跟发 `need_continue`「请在弹窗中选择批准/拒绝」；
  - 前端 SSE 事件分发表（10-chat.js:120-250）处理 aborted/ask_user/done/error/meta/need_continue/path/reasoning/skill_request/skill_source_request/task_calls/task_progress/token/tool/tool_result/usage 共 16 种——**唯独没有 `approval_request` 分支**；
  - `showApprovalModal` 全库零调用。
- **后果**：危险命令/批量删除触发审批挂起时，用户只看到文字提示"请在弹窗中选择"，**弹窗永远不出现**，任务卡在 need_continue；设置页的「危险命令弹窗审批」选项（index.html:549）与「删除审批阈值」（index.html:553）配置了永远无法生效的流程。
- **判定**：🔧 **不可删除，应重新接线**——在 SSE 分发处补 `else if(type==="approval_request"){ showApprovalModal(paneId, d); }`（约 1 行），Modal 本体、grant API、后端事件全是现成的。
- **注意**：修复后需真跑一次"60 秒内连删超阈值"场景验证弹窗弹出→批准→重试放行闭环。

---

## 二、确认的死代码（Python）

### B1. 零引用函数（✅ 可安全删）
| 位置 | 符号 | 判定依据 | 注意事项 |
|---|---|---|---|
| services/learn_capture.py:70 | `_save_shot()` | 全库词边界搜索仅定义行，无调用 | 截图管线改版遗留；删除前确认 learn_capture 截图走其他路径 |
| desktop/close_guard.py:735 | `_tray_install()` | 仅定义行，无调用 | 托盘功能若计划恢复则保留；当前 close_guard 无托盘入口 |
| desktop/close_guard.py:775 | `_tray_remove()` | 同上 | 与 _tray_install 成对删除 |
| core/config_store.py:290 | `mutate_experiences()` | 仅定义行，无调用 | 经验库读写已由 services/memory_ops 接管 |
| desktop/save_dialog_auto.py:327 | `stop_daemon()` | 仅定义行，无调用 | daemon 由 start_daemon 启动但无停止入口；如需优雅停机则改造而非删除 |
| core/paths.py:98 | `experiences_path()` | 经 core_utils 转发后全库 0 消费（config_store 用自己的 `_experiences_path`） | ✅ 连同 core_utils.py:41 转发一起删 |
| tests/test_wecom_bind.py:56 | `cfg_wecom_port()`（fixture） | 非 autouse、无任何测试请求该 fixture | 死 fixture，可删 |

### B2. 未使用 import（约 250 处，✅ 可批量删）
> 外观层文件 **core_utils.py（全文件转发）、core/workspace_io.py:14-15、agent_tools/__init__.py、wecom_bridge/__init__.py** 的 import 是刻意 re-export，**不是死代码，勿动**。

| 文件 | 数量 | 代表例子 | 备注 |
|---|---|---|---|
| routes/management.py | 44 | json/os/re/sys/time/uuid/threading/judge_model/resolve_model/_sse_stream… | 路由文件从同一模板复制头部导入，拆分后遗留；每文件实际只用到少数 |
| routes/b_ocr_admin.py | 31 | re/requests/threading/uuid/log_event/mask_key… | 同上 |
| routes/b_repair.py | 17 | Response/stream_with_context/extract_json… | 同上 |
| routes/b_memory.py | 15 | APP_PORT/judge_model/load_config… | 同上 |
| routes/b_macro.py | 15 | 同上 | — |
| routes/b_models.py | 9 | 同上 | — |
| core/security.py | 7 | os/json/re/sys/shutil/datetime/threading | — |
| core/office.py | 7 | sys/re/json/shutil/datetime/threading/time | — |
| core/files.py | 7 | sys/re/shutil/datetime/threading/time/ocr_ready_or_install | — |
| core/ocr.py | 6 | time/shutil/datetime/_LazyRequests/resource_path/workspace_root | — |
| core/json_utils.py | 6 | os/sys/shutil/datetime/threading/time | — |
| core/config.py | 6 | sys/re/shutil/time/datetime/threading | — |
| agent/loop.py | 6 | queue/SYSTEM_PROMPT/TOOL_RESULT_MAX_LEN/_CDP_READ_TOOLS/AUTO_LOOP_MAX_RESTARTS/AUTO_LOOP_STATES | auto_loop 常量已由 agent/auto_loop.py 自管 |
| 其余（单条） | ~20 | backend.py `import ctypes as _ct`、llm_client.py `weakref`、desktop/tray.py `threading`、wecom_bridge/{download.quote, frames.websocket, read.pick_window}、windows.py {CDP_SCREENSHOT_TIMEOUT, _cdp_eval_retry}、routes/chat.py `os as _os`、routes/conversations.py `log_event`、services/wecom_archive.py `config_path`、agent/sse_stream.py {chat_states, _chat_state_lock} | 逐条可删 |

**删除注意事项**：① 一次只清一类（先 routes/ 再 core/），每清一批跑 `pytest tests/ -q`（223 用例）+ 打包冒烟；② `noqa: F401` 标注的保留（如 skill_versions.py:18 paths 是给测试 patch 用的）；③ tests/ 与 tools/ 里的未用 import 不影响生产，可顺手清但优先级最低。

### B3. 传递性死代码簇与恒假代码（⚠️ 需确认后处理）
| 位置 | 内容 | 判定依据 | 建议 |
|---|---|---|---|
| desktop/close_guard.py:735/775/724/676/688/615 | **托盘残留簇**：`_tray_install/_tray_remove/_tray_dbg` + `NOTIFYICONDATAW` 类 + `NIM_*/NIF_*` 常量 + `WM_APP_TRAY` 及 wndproc:615 的 `if msg==WM_APP_TRAY` 分支 | remove_tray_icon docstring（:788）明言"ctypes 自注册托盘已移除（v1.0.373 双图标根治）"；无任何窗口以 WM_APP_TRAY 注册回调 | ⚠️ 成簇删除；**`_tray_user32`(:705) 仍被 register_hotkeys(:854→:628) 使用，绝不能连带删** |
| services/memory_ops.py:189 | `parts.append("用户：" if False else "")` | 恒假三元（"用户："字面量永不可达），且 :190 立即覆写该元素——双重死写入 | ✅ 两行合并为 `parts.append(f"{role}：…")` |
| core/notify.py:52/66 | `list_notifications`/`mark_notifications_read` | 经 core_utils 转发后全库 0 消费——**通知中心只写不读**（push_notification 写文件，无路由/前端读取） | 🔧 与 ⭐ 收藏同类的断链功能：要么补通知列表 UI，要么整簇删（连 core_utils 转发） |
| wecom_bridge/download.py:509/475 | `fetch_via_http` + 唯一调用方 `_get_page_cookies` | v1.0.468 直下通道摘除（cookie 分区会话 100% 失败），但注释明言"**保留（非企微场景可用）**" | ❌ 有意保留的对外能力，不删；仅标注 |

### B4. 疑似冗余模块（⚠️ 需确认后合并）
| 位置 | 说明 | 建议 |
|---|---|---|
| skill_versions.py（根目录） vs services/version_store.py | 名字相近但功能不同：前者=技能提示词版本历史（routes/skills.py、services/self_repair.py 在用），后者=exe 版本回滚（routes/b_repair.py 在用）。**都活着，不是重复** | 不删；建议 README 注明两者区别，防未来误合并 |

---

## 三、确认的死代码（JS）

### C1. 零引用函数与只写不读状态（✅ 可安全删 / ⚠️ 注意）
| 位置 | 符号 | 判定依据 | 注意事项 |
|---|---|---|---|
| js/11-workspace-kb-study.js:300 | `_repairLatestName()` | v1.0.350 自我修复遗留，全库零调用 | ✅ 删；v1.0.354 体检进度渲染已替代其调用场景 |
| js/02-nav-quicklinks.js:148 | `toggleOpAutoLoop()` | 全库零调用；其按钮 `#opAutoLoopBtn` 在 index.html 不存在（随 v1.0.492 work-chat-row 拆除） | ✅ 删函数；**注意** auto_loop 功能本身活着（快捷入口 auto_loop:true → 10-chat.js:1056/1066 随请求发送） |
| js/02-nav-quicklinks.js:139 | `setOpAutoLoop()` 内 `$("opAutoLoopBtn")` 分支 | 按钮不存在，`if(b)` 永假 | ⚠️ 显示反馈永久 no-op——续跑开启状态用户不可见（小 UX 缺陷）；建议在 chips 区补一个状态徽标而非删逻辑 |
| js/01-core.js:2 + js/06-windows.js:320 | `lastCapture` 全局变量 | 只写不读（仅定义 + 赋值两处） | ✅ 两行一起删 |
| js/09-chat-render.js:295,297 | `__workTab` 变量 | 只写不读；`switchWorkTab()` 函数本身存活（classList 切换正常），仅状态变量无人消费 | ✅ 删赋值行，保留函数 |
| js/07-usage-models.js:156 | `$("modelCount")` 引用 | DOM id 不存在，`&&` 守卫下永久 no-op | ✅ 删该行（模型计数无处显示；如需恢复在模型页加 `<span id="modelCount">`） |
| js/08-templates.js:7 | `$("tplCount")` 引用 | 同上 | ✅ 删该行 |
| js/10-chat.js switchConv/openScratchConv 内 `$("convList")?.classList.remove("open")` ×2 | 已拆 conv-dd 的残留引用 | optional chaining 下永久 no-op（v1.0.497 拆除遗留） | ✅ 删两行 |

### C2. 孤儿入口函数（⚠️ 不建议直接删，先决定语义）
| 位置 | 符号 | 现状 | 建议 |
|---|---|---|---|
| js/11-workspace-kb-study.js:1 | `openWorkspace()` | `closeWsFile()+loadWorkspace()`；openView('workspace') 只调 loadWorkspace → **closeWsFile 永不执行**（切走再切回，文件预览残留） | 二选一：openView 改调 openWorkspace（修复残留预览）或删函数接受现状 |
| js/11-workspace-kb-study.js:300 附近 | `openKnowledge()` | `closeKbFile()+loadKnowledge()+loadWecomArchive()`；openView('knowledge') 只调 loadKnowledge → 企微文件分析状态不刷新 | 同上：openView 改调 openKnowledge 更优（顺带修复） |

### C3. 误报澄清（看起来死但其实活，勿删）
| 符号 | 存活原因 |
|---|---|
| js/01-core.js:1 `const $` | 词边界正则无法匹配 `$` 符号，实际全项目使用 |
| js/10-chat.js:1467 `patchSendWithConv` / 02-nav:366 `restoreNavMore` / 10-chat:1139 附近 `restoreToolbarFold` | IIFE 自执行函数 `(function X(){…})()`，加载即运行 |
| 10-chat.js `LONG_TEXT_FLAT/PANE_MSG_MAX/__chipState/__modelsCache/__trCtx` 等常量、11-workspace `_FF_KEYS/_PHASE_IDX/_REPAIR_PHASES`、06-windows `_BENCH7_COLS`、10-chat `_DND_MAX_FILES`、02-nav `QUICK_LINKS_DEFAULT` | 扫描器常量段统计 bug（前缀切片偏移），人工复核全部在用 |

---

## 四、按钮与交互层（专项深查结论：无死按钮）

### D1. 静态按钮（index.html，~90 个）
- 全部 `<button>` 均有 onclick，且 170 个 on* 内联处理函数（含 onkeydown/onchange/oninput）经宽模式（function/const/window.赋值/async 箭头）交叉验证**全部有定义**；
- 26 个 display:none 元素逐一验证显示路径（navMore→toggleNavMore、trBatchOps→__convBatchMode、parentBtn→父窗口时显示、probePanel→probeModels 等），**无永久隐藏死按钮**；
- 51 个带 title 的按钮全量比对 onclick 与 title 语义，0 错配。

### D2. 动态按钮（JS 注入，16 个无内联 onclick 的逐一验证）
- 唯一事件委托点：07-usage-models.js:188 `#modelTable td.ops [data-act]`（编辑/评测/默认/判断/删 5 按钮 → 5 个 handler 均存在）；
- skReqSkip/skReqInstall、skSrcSkip/skSrcGo、apprDeny/apprGrant、askSkip/askSubmit、.ask-opt 批量、data-ck/data-cl/data-cc——全部在创建后 1-8 行内挂事件；
- createElement("button") 15 处均在创建后立即挂 onclick/addEventListener；
- v1.0.497 conv-bar 迁移核查干净：HTML 无残留节点，renderConvList/syncConvBar 为命名残留 shim 但功能正确（8 处/3 处调用有效），新建/归档/删除入口唯一化。

### D3. 交互层问题清单
| 位置 | 问题 | 级别 | 建议 |
|---|---|---|---|
| js/10-chat.js:1139-1171 `showApprovalModal` + 内部 apprGrant/apprDeny 按钮 | **审批弹窗本体按钮接线完好，但 Modal 自身无人调起**（后端 approval_request 事件无前端分支，见 A1）——按钮活着却永远没有出场机会 | P0 | 随 A1 补 SSE 分支即整体复活 |
| js/09-chat-render.js:154 `msgFavMsg()` | **⭐ 收藏按钮断链**：点击写入 localStorage `__favs`（只写不读，全库无任何读取方），toast 提示「收藏箱在会话下拉里」——收藏箱 UI 从未存在，且会话下拉 v1.0.497 已拆。用户收藏后无处查看 | P1 | 二选一：补收藏箱 UI（如设置页或任务栏 ⋯ 菜单）；或删 ⭐ 按钮与 msgFavMsg |
| 02-nav-quicklinks.js:141 | `#opAutoLoopBtn` 不存在 → 续跑开关状态不可见（功能仍生效，仅无 UI 反馈） | P2 | chips 区补状态徽标 |
| index.html:549/553 | 「危险命令弹窗审批」选项 + 阈值配置，对应弹窗链路断裂（见 A1） | P0（随 A1 修复即恢复） | 修 A1 |
| index.html:521 vs :539 | 两处「📦 打包并重启」同调 rebuildNow——**有意重复**（帮助页有说明），非死按钮 | — | 不动 |
| setChatPane「🗑 清空」按钮 | 缺 title（opPane 侧同名按钮有） | P3 | 顺手补 |
| 全部纯图标按钮（↻/📎/⤢/↑/👥/▾/＋/☑/▼/⋯） | 无 aria-label，读屏不可知（可访问性短板，非死代码） | P3 | 择机补 |

---

## 五、不可达代码与孤儿模块扫描结果

- **AST 级不可达语句**（return/raise/continue/break 后同层残留、恒假分支）：**0 处**；
- **成段注释死代码**（≥25 行且含代码特征的注释块）：**0 处**（现有注释均为版本说明，保留合理）；
- **孤儿模块**：**0 个**——全部 .py 均被 import/打包 spec/入口引用（model_bench.py 由 routes/b_models.py 与测试引用=开发工具+基准 API；security_ops.py/skill_hub_data.py/usage_stats.py/skill_versions.py 均有生产调用方）；
- **同名重复定义甄别**：生产代码中的同名函数均为跨模块同名不同义（如 `_sse`×3 各文件私有工具 8 处自用、`resource_path`×2 各自实现、skill_versions.py 与 services/version_store.py 功能完全不同），无同命名空间遮蔽，**无需处理**；
- 裸 `except-pass` 186 处（check_frontend 门禁在跟踪，基线 194 单调递减中）——属于健壮性债，不属死代码，不在本报告展开。

---

## 六、处置优先级汇总

| 优先级 | 事项 | 动作 |
|---|---|---|
| P0 | A1 审批弹窗断链 | 补 SSE 分支 1 行重新接线 + 端到端验证 |
| P1 | ⭐ 收藏断链（msgFavMsg/__favs 只写不读） | 补收藏箱 UI 或删按钮 |
| P1 | 通知中心只写不读（notify.py list/mark 零消费） | 补通知 UI 或整簇删 |
| P1 | B2 routes/*.py 模板遗留导入（~170 条） | 分批清理，每批 pytest |
| P1 | C2 openWorkspace/openKnowledge 孤儿入口 | openView 改调（顺带修 UX） |
| P2 | B1 零引用函数 ×6 + 死 fixture | 直接删 |
| P2 | B3 托盘残留簇 + memory_ops:189 恒假三元 | 成簇删 / 改写（保住 _tray_user32） |
| P2 | C1 toggleOpAutoLoop + opAutoLoopBtn 残留 | 删函数；考虑补续跑状态可见性 |
| P3 | core/ 层零散未用 import（~60 条） | 顺手清（noqa 标注的保留） |

---
*扫描工具：tools/tmp_deadscan.py（AST+词频，审查完成后可删）；复核方式：逐项 grep -rwn 全库验证。*

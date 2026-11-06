# 并发锁层级图（v1.0.646）

本软件是**纯线程**模型：Flask `threaded`（现优先 waitress，threads=16）+ 十几个
后台 daemon + 每个对话一条 SSE 泵线程。全局锁 30 余把，此前没有任何顺序约定——
`agent/session.py` 内部就有一句注释在躲自己的嵌套顺序（`_save_chat_state` 锁内不敢
碰权限侧表）。死锁的特点是"界面卡死、日志无异常"，事后极难归因，所以把约定写下来。

## 一、分层（约定：只允许上层锁 → 下层锁，**禁止反向**）

| 层 | 锁 | 位置 | 保护对象 |
|---|---|---|---|
| L4 编排层 | `_abort_lock` | agent/session.py:17 | abort 标志与全停代次 |
| L4 | `_chat_state_lock` | agent/session.py:142 | 内存 chat_states（断点） |
| L4 | `_tick_queue_lock` | services/scheduler.py:28 | 定时任务队列 |
| L3 领域层 | `_CONV_LOCK`(R) | services/conversations.py:17 | 会话 JSON 读改写 |
| L3 | `_mem_lock` / `_inflight_lock` | services/memory_ops.py:21,23 | 记忆库、check-and-set |
| L3 | `_EV_LOCK` / `_EV_INFLIGHT_LOCK` | services/skill_evolve.py:33,38 | 技能自进化 |
| L3 | `_exp_lock` | core/config_store.py:394 | 经验库 |
| L3 | `_AUTO_FIX_LOCK` / `_RUN_LOCK` | services/self_repair.py:61,62 | 自愈重入 |
| L3 | `_state_lock` | services/wecom_archive.py:37 | 归档状态 |
| L2 基础设施层 | `_config_lock`(R) | core/config_store.py:149 | config.json 读改写 |
| L2 | `_LEDGER_LOCK` | core/workspace_io.py:132 | 产物台账追加 |
| L2 | `_LOG_LOCK` / `_EARLY_LOCK` | core/logging.py:29,32 | 日志追加与轮转 |
| L2 | `_DND_ALLOWED_LOCK` | core/workspace_io.py:343 | 拖拽放行表 |
| L2 | `_LOCK`(skill_versions) | skill_versions.py:23 | 技能版本 |
| L1 设备/IO 层 | `_WS_POOL_LOCK` | wecom_bridge/cdp_client.py:53 | CDP 连接池 |
| L1 | `_AUTO_PICK_LOCK` | wecom_bridge/windows.py:145 | 跟随窗口作用域 |
| L1 | `_tuck_lock` | wecom_bridge/windows.py:1330 | 窗口收纳 |
| L1 | `_lock`(idle) | core/idle.py:22 | 界面空闲时间戳 |
| L1 | `_ACTIVE_LOCK` | llm_client.py:44 | LLM 并发闸 |
| L1 | `_OCR_*_LOCK` | core/ocr.py:16,108,481 | OCR 引擎/下载/自检 |
| L1 | `_STATE_LOCK` | core/portable.py:27 | 便携运行时状态 |
| L0 独立（不与任何锁嵌套） | `_ASK_JUDGE_CACHE_LOCK` | agent_tools/executors.py:604 | judge 结果缓存 |
| L0 | `_MODEL_PROBE_LOCK` / `_DASH_LOCK` | routes/b_admin.py:143,350 | 模型探测/面板 |
| L0 | `_lock`(security_ops) | security_ops.py:19 | 审批状态 |
| L0 | `_lock` / `_INSTALL_LOCK`(R) | services/asr.py:41,190 | ASR |
| L0 | `_lock`(scheduler) | services/scheduler.py:23 | 调度器自身 |
| L0 | `_lock`(usage_stats) | usage_stats.py:32 | 用量统计 |

## 二、硬规则

1. **L1 设备锁内绝不调 L2+**：CDP 池锁内不许 `log_event`、不许读配置——日志/配置
   卡住会让所有页面操作一起停摆（`_pool_trim_locked` 的"关 socket 必须放到锁外"
   注释就是这条规则的实例）。
2. **`_chat_state_lock` 内不取 `_perm_lock`**：`agent/session.py` 已按此实现
   （侧表写移到锁外）。新增代码若要在断点锁内动权限侧表，先把锁顺序改对。
3. **可重入锁（R）不参与排序**：`_config_lock` / `_CONV_LOCK` / `_INSTALL_LOCK`
   允许同线程重入，但**不得**在持它们时去取别的锁的第一次（重入会掩盖顺序问题）。
4. **永不持锁做 IO 等待**：WS 收发、subprocess、文件大块读写一律放到锁外；
   这是 `_WS_POOL` 与台账写入已经踩过的坑。
5. **新增锁必须登记本表**：写明层级、保护对象、是否会与谁嵌套。

## 三、待办（未消除的风险）

- `_mem_lock` 与 `_inflight_lock`（memory_ops）目前按"先 inflight 后 mem"使用，
  无反向调用，但无测试钉住；建议补一个并发用例。
- `services/scheduler.py` 的 `_lock` 与 `_tick_queue_lock` 同处一文件、语义不同，
  易被后人混用；建议重命名或合并。
- 本图为**约定**而非强制：若要强制执行，可加调试期 `assert` 或线程锁顺序探针
  （成本高于收益，暂不做）。

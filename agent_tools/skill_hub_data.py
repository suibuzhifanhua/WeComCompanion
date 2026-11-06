# -*- coding: utf-8 -*-
"""技能中心·公共技能库（v1.0.309）
=================================
内置快照：离线即用的精选公共技能清单。技能中心「📚 公共技能库」从此渲染，
一键安装 = 复制进 config.custom_skills（id 前缀 hub_，与用户自建技能同机制生效）。

远程源：config.skill_hub.url 非空时尝试拉取同结构 JSON（{"skills":[...]}），
失败自动回退内置快照并注明。发布自己的公共技能：把本文件的 HUB_SKILLS dump 成
JSON 托管到任意可 GET 的 URL（gitee/github raw 均可）即可共享。

纪律（写新技能必守）：
- tools 只能用 agent_tools.TOOLS 里已有的工具名（当前 42 个，见 registry）；
- prompt 参照既有 SKILLS 风格：场景→步骤→纪律，编号化、可执行、不空谈；
- judge 固定 "none"（公共技能与自定义技能同机制，不支持双模型判断位）。
"""

HUB_VERSION = "2026-09-14"

HUB_SKILLS = [
    # ---------- 📊 办公效率 ----------
    {"id": "weekly-report", "name": "周报生成器", "icon": "📋", "category": "办公效率",
     "desc": "把本周零散的工作记录/聊天要点整理成结构化周报（做了什么/进展/风险/下周计划），落盘可发群。",
     "tools": ["web_search", "save_file", "read_workspace_file", "send_wecom_message"],
     "prompt": "【周报生成】用户给一周的工作素材（口述要点/记录文件/上一份周报），产出结构化周报：\n"
               "1. 读素材（read_workspace_file / 用户消息），按主题归并列出「本周完成」「进行中」「风险与阻塞」「下周计划」四节；\n"
               "2. 每条写结果与状态（完成/进行中 %），不写过程流水账；量化优先（条数、金额、进度）；\n"
               "3. 有上一份周报时沿用其格式与粒度，保持口径连续；\n"
               "4. 产出 Markdown 存 产物库/产出/（save_file，文件名含日期区间）；\n"
               "5. 用户要求发群时经 send_wecom_message 发送，发出前把全文展示给用户确认。\n"
               "纪律：不编造数据；素材里没有的进展宁缺毋滥；风险条目必须带「需要谁支持」。"},

    {"id": "meeting-notes", "name": "会议纪要", "icon": "📝", "category": "办公效率",
     "desc": "把会议记录/录音转写/聊天讨论整理成决议导向纪要：结论、待办（负责人+期限）、分歧点。",
     "tools": ["save_file", "read_workspace_file", "send_wecom_message"],
     "prompt": "【会议纪要】用户给会议素材（转写文本/聊天记录/要点口述），产出决议导向纪要：\n"
               "1. 头部：会议主题/时间/参与人（素材有则写，没有就留占位让用户补）；\n"
               "2. 「结论与决议」逐条列出（编号，动词开头）；\n"
               "3. 「待办事项」表格式：事项/负责人/期限/交付物——素材没给负责人期限的写「待定」并标注；\n"
               "4. 「未达成一致」单独列（分歧双方观点各一句），不替会议下结论；\n"
               "5. 存 产物库/产出/（save_file）；用户要求时可 send_wecom_message 发给指定群。\n"
               "纪律：忠实于素材，不脑补承诺；转写口语先归一再入稿；待办必须可验证（有交付物）。"},

    {"id": "email-drafter", "name": "邮件/回复草拟", "icon": "✉️", "category": "办公效率",
     "desc": "按场景与语气草拟商务邮件/回复（催办、致歉、报价跟进、跨部门协作），给出 2 版供选。",
     "tools": ["save_file", "read_workspace_file"],
     "prompt": "【邮件草拟】用户给场景（发给谁/什么事/期望结果/语气要求），产出邮件草稿：\n"
               "1. 先确认四要素：收件人与关系、核心诉求、期望的行动与时限、语气（正式/中性/委婉）；缺项用一句话向用户确认；\n"
               "2. 给出 2 版：A 直接版（诉求前置）、B 委婉版（铺垫后置），各 ≤300 字；\n"
               "3. 结构：一句背景 → 核心诉求（编号）→ 需要对方做什么+期限 → 收尾；\n"
               "4. 敏感内容（金额/承诺/期限）草稿里用【请确认：××】标记，让用户逐项拍板；\n"
               "5. 用户选定后可存 产物库/产出/ 供复制。\n"
               "纪律：不替用户承诺任何未经确认的条件；催办类不带情绪词。"},

    {"id": "doc-summarizer", "name": "长文档摘要", "icon": "📄", "category": "办公效率",
     "desc": "读报告/制度/论文等长文档，按需产出摘要（一页速览/分章要点/执行清单三种模式）。",
     "tools": ["read_workspace_file", "save_file", "list_workspace_files"],
     "prompt": "【长文档摘要】用户给文档（路径或上传），按指定模式产出摘要：\n"
               "1. read_workspace_file 读全文（超长分段读，先 list 目录确认文件名）；\n"
               "2. 三种模式按用户选择：①一页速览（≤10 句：解决什么问题/核心结论/关键数字/建议动作）\n"
               "   ②分章要点（每章 ≤5 条，保留术语原文）③执行清单（只提取「要做的事」，按责任人/期限归位）；\n"
               "3. 数字/日期/条款号必须与原文一致，引用处标注章节；\n"
               "4. 摘要存 产物库/产出/ 并在结尾附「原文未覆盖的部分」清单（防止用户误以为全了）。\n"
               "纪律：不加自己的解读进摘要正文；评述类内容放「备注」并标明是 AI 观点。"},

    # ---------- 💻 开发效率 ----------
    {"id": "code-reviewer", "name": "代码审查", "icon": "🔍", "category": "开发",
     "desc": "审查本地代码文件：正确性/边界/安全/可维护性四维，按 P0-P2 分级给修复建议，可现场验证。",
     "tools": ["pc_files", "pc_run", "save_file", "pc_takeover", "dsh_status"],
     "prompt": "【代码审查】用户给文件/目录路径，做四维审查：\n"
               "1. pc_files 读目标代码（目录先 list 再挑重点文件，一次别超 5 个）；\n"
               "2. 四维过一遍：正确性（逻辑错/边界条件）、安全（注入/路径穿越/敏感信息）、健壮性（异常/资源泄漏）、可维护性（重复/命名/超长函数）；\n"
               "3. 按严重度分级输出：P0 必修（会出错/丢数据）/ P1 应修（隐患）/ P2 建议（质量），每条带文件:行号+最小修复代码；\n"
               "4. 对 P0 修复建议，征得用户同意后 pc_run 跑测试/语法检查验证，再出最终报告；\n"
               "5. 审查报告 save_file 存 产物库/产出/。\n"
               "纪律：只审不改（改码须用户逐条确认）；不确定的问题标「待验证」而不是断言。"},

    {"id": "sql-helper", "name": "SQL 助手", "icon": "🗄️", "category": "开发",
     "desc": "把自然语言需求转成 SQL（含建表建议/索引提示），能连本地环境时先 EXPLAIN 验证再交付。",
     "tools": ["pc_run", "read_workspace_file", "save_file"],
     "prompt": "【SQL 助手】用户给表结构/需求描述，产出 SQL：\n"
               "1. 先问清/从上下文推断：库类型（MySQL/SQLite/PG）、表结构（没有就让用户贴 DDL 或样例数据）；\n"
               "2. 写 SQL：复杂查询拆 CTE 分层注释；聚合查询注明 NULL/空集语义；\n"
               "3. 指出性能注意点：全表扫描风险、建议索引（给具体 CREATE INDEX 语句）；\n"
               "4. 用户本机有 sqlite/mysql 客户端且同意时，pc_run 用 EXPLAIN/EXPLAIN QUERY PLAN 验证执行计划再交付；\n"
               "5. 交付含：SQL 全文 + 每段一句用途说明 + 验证输出（如有）。\n"
               "纪律：绝不对生产库执行写操作（DROP/UPDATE/DELETE 必须【请确认】标记且只给语句不代跑）；字符串拼接类需求提示注入风险。"},

    {"id": "git-helper", "name": "Git 操作助手", "icon": "🌿", "category": "开发",
     "desc": "分支/合并/回滚/冲突处理/提交信息规范化，破坏性命令前强制确认并先备份。",
     "tools": ["pc_run", "pc_files", "save_file"],
     "prompt": "【Git 助手】用户描述 git 需求或贴报错，按步骤处理：\n"
               "1. 先 pc_run 跑 `git status` + `git log --oneline -5` + `git branch --show-current` 摸清现场（在用户给的 cwd）；\n"
               "2. 日常操作（add/commit/branch/merge/checkout）：给出并执行命令，提交信息按约定式（feat/fix/docs: 中文描述）；\n"
               "3. 冲突处理：列出冲突文件，逐文件给出「保留哪边/如何融合」的建议，等用户拍板再改；\n"
               "4. 【破坏性红线】reset --hard / push --force / clean / rebase 执行前必须：①说明会丢什么 ②先 `git branch backup-<date>` 建备份分支 ③等用户明确回复确认；\n"
               "5. 操作完 `git status` 复核并汇报结果。\n"
               "纪律：绝不跳过确认执行破坏性命令；不明状态先查再动，不猜。"},

    # ---------- 🌐 信息获取 ----------
    {"id": "topic-brief", "name": "主题简报", "icon": "🌐", "category": "信息",
     "desc": "给一个主题（技术/产品/公司/政策），联网检索后产出带来源的结构化简报。",
     "tools": ["web_search", "fetch_webpage", "save_file"],
     "prompt": "【主题简报】用户给主题与关注角度，联网产出简报：\n"
               "1. web_search 多角度检索（主题本身/最新动态/不同立场各一组关键词，共 3-5 组）；\n"
               "2. 对可信来源（官网/一线媒体/原始文档）fetch_webpage 读原文，不用 snippet 当结论；\n"
               "3. 简报结构：TL;DR（≤3 句）→ 分主题要点（每条带来源链接与日期）→ 时间线（如适用）→ 信息缺口（没查到什么）；\n"
               "4. 冲突信息并列呈现（A 说/B 说），不擅自裁决；\n"
               "5. 存 产物库/产出/，文末列检索关键词供用户复查。\n"
               "纪律：每条事实必须有来源；超过 6 个月的信息标注「时效性待确认」。"},

    {"id": "web-monitor", "name": "页面/数据监控", "icon": "📡", "category": "信息",
     "desc": "定期检查指定网页变化（价格/公告/库存/版本号），变化时汇总结论；可配合电脑任务自动执行。",
     "tools": ["fetch_webpage", "web_search", "save_file", "read_workspace_file", "pc_run"],
     "prompt": "【页面监控】用户给要盯的网页 URL 与关注点：\n"
               "1. 首次：fetch_webpage 取当前内容，提取关注字段的基线值（如价格/状态/最新版本），存基线文件到 产物库/产出/监控_<名>/baseline.md；\n"
               "2. 每次被要求检查时：重新 fetch → 与基线 diff → 有变化时输出「字段 旧→新 + 判断（利好/利空/中性，仅对明确场景）」；无变化一句话带过；\n"
               "3. 变化后更新基线并在同目录追加 history.md（时间戳+变化记录）；\n"
               "4. 用户要求自动化时：说明本软件「自动任务」如何定时触发本技能（建议间隔≥30 分钟），不诱导过频请求；\n"
               "5. 目标页是 JS 动态渲染导致 fetch 拿不到内容时，改用 pc_screenshot 截屏描述可见状态，并说明此限制。\n"
               "纪律：只读不写，不登录、不提交任何表单；监控结果不作投资/采购建议。"},

    # ---------- 🖥 系统与文件 ----------
    {"id": "disk-cleaner", "name": "磁盘空间体检", "icon": "💽", "category": "系统",
     "desc": "扫描指定盘/目录找出大文件与垃圾（缓存/临时/重复下载），出报告；删除前先备份并逐批确认。",
     "tools": ["pc_run", "pc_files", "save_file"],
     "prompt": "【磁盘体检】用户给盘符或目录：\n"
               "1. pc_run 跑 PowerShell/du 类命令统计Top 大目录与大文件（只读，不改任何东西）；\n"
               "2. 识别可清理项并分级：🟢 安全（浏览器缓存/临时文件/回收站——注明会丢什么）/🟡 需确认（旧下载/重复文件——列清单）/🔴 别动（系统目录/未知用途——说明原因）；\n"
               "3. 输出报告：总量/Top 20 大文件/各类可释放空间估算，save_file 存档；\n"
               "4. 【删除纪律】用户逐项确认后才执行，且：每批 ≤10 个、先用 pc_files 复制到备份目录、删除走回收站、每批后复核；\n"
               "5. 结束汇报实际释放空间。\n"
               "纪律：系统目录（Windows/Program Files/AppData 核心区）永不建议删；用户犹豫的项目一律归入「保留」。"},

    {"id": "batch-rename", "name": "批量文件整理", "icon": "🗂️", "category": "系统",
     "desc": "按规则批量重命名/归类文件（照片按日期、下载按类型、报销单按月份），先预览再执行、可回滚。",
     "tools": ["pc_files", "pc_run", "save_file"],
     "prompt": "【批量整理】用户给目录与整理规则：\n"
               "1. pc_files 列出目标目录全部文件（含子目录计数），先给「现状清单」；\n"
               "2. 和用户确认规则（命名模板/归类维度/冲突策略——同名怎么办必须问清）；\n"
               "3. 生成「预览表」：原路径 → 新路径 全量列出，等用户确认后才执行；\n"
               "4. 执行：每批 ≤50 个，操作前把清单 save_file 存为 manifest（产物库/产出/整理_<日期>/）作为回滚依据；\n"
               "5. 完成后复核（抽样 5 个验证路径正确）并汇报；用户要撤销时按 manifest 逆操作。\n"
               "纪律：绝不移动/重命名系统目录与不明文件；预览未确认不执行第一步。"},

    {"id": "screenshot-reader", "name": "屏幕内容解读", "icon": "🖥️", "category": "系统",
     "desc": "截取当前屏幕/指定窗口，结合 OCR 与页面读取解读内容，辅助「帮我看下屏幕上是什么」类请求。",
     "tools": ["pc_screenshot", "pc_system_info", "list_wecom_windows", "read_wecom_page"],
     "prompt": "【屏幕解读】用户问屏幕上有什么/某个窗口什么状态：\n"
               "1. pc_screenshot 截全屏（用户指明窗口时用 pc_system_info 查窗口后截取对应区域，说明截了什么）；\n"
               "2. 结合上下文解读：用户在企微场景时优先 list_wecom_windows + read_wecom_page 读结构化内容（比看图准）；\n"
               "3. 输出：屏幕/窗口里有什么（按区域描述）、关键信息提取（数字/状态/报错）、与用户问题的关联；\n"
               "4. 看不清/OCR 结果矛盾时如实说明，请用户放大或改用文字描述，不硬猜。\n"
               "纪律：截屏仅存产物库不出网；解读不含主观猜测性结论（标明「推测」）。"},
]


_HUB_CACHE = {"url": None, "ts": 0.0, "result": None}
_HUB_CACHE_TTL = 60.0


def hub_skills_with_fallback(cfg):
    """取公共技能库清单（v1.0.317 下沉自 routes/skills，供 build_agent_tools 等
    无 Flask 层使用）：url 非空尝试远程（6s 超时），失败回退内置快照。
    返回 (skills, source, note)；内置路径为每项补 version=HUB_VERSION。"""
    import json as _json
    import time as _time_mod
    import urllib.request as _req
    url = str((cfg.get("skill_hub") or {}).get("url") or "").strip()
    # v1.0.327 速度优化：远程清单 60s 缓存（此前每个对话请求都拉一次，最多慢 6s）
    if url and _HUB_CACHE["url"] == url and _time_mod.time() - _HUB_CACHE["ts"] < _HUB_CACHE_TTL:
        cached = _HUB_CACHE["result"]
        if cached:
            return cached
        # v1.0.470（P2-9）：负缓存期内直接回退内置快照——远程源宕机时不逐请求阻塞 6s
        if _time_mod.time() - _HUB_CACHE["ts"] < 30.0:
            return None, "builtin", "远程源暂不可达（30s 负缓存内），已回退内置快照"
    if url:
        try:
            req = _req.Request(url, headers={"User-Agent": "WeComCompanion"})
            with _req.urlopen(req, timeout=6) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            raw = data.get("skills") if isinstance(data, dict) else data
            if isinstance(raw, list) and raw:
                skills = []
                for it in raw:
                    if isinstance(it, dict) and it.get("id") and it.get("name"):
                        skills.append({k: it[k] for k in
                                       ("id", "name", "desc", "icon", "category", "prompt", "tools", "version")
                                       if it.get(k) is not None})
                if skills:
                    _HUB_CACHE.update({"url": url, "ts": _time_mod.time(), "result": (skills, "remote", f"远程源：{url}")})
                    return skills, "remote", f"远程源：{url}"
            # v1.0.470（P2-9）：失败负缓存 30s——远程源配置了但格式无效/宕机时，原实现
            # 每个对话请求都同步等满 6s 超时；负缓存后 30s 内直接回退内置快照
            _HUB_CACHE.update({"url": url, "ts": _time_mod.time() - (_HUB_CACHE_TTL - 30.0),
                               "result": None})
            return None, "builtin", "远程清单格式无效，已回退内置快照"
        except Exception:  # noqa: BLE001
            _HUB_CACHE.update({"url": url, "ts": _time_mod.time() - (_HUB_CACHE_TTL - 30.0),
                               "result": None})
            return None, "builtin", "远程源拉取失败，已回退内置快照"
    skills = [dict(s) for s in HUB_SKILLS]
    for s in skills:
        s.setdefault("version", HUB_VERSION)
    return skills, "builtin", ""


def validate_skill_manifest(data):
    """校验并规范化一份技能清单（v1.0.318 import_skill_source 用）。
    data 为 {"skills":[...]} 或直接数组；每项必须含 id/name/prompt。
    返回规范化 skills 列表（至少 1 项），无效返回 None。"""
    import json as _json
    raw = data.get("skills") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        return None
    skills = []
    for it in raw:
        if isinstance(it, dict) and it.get("id") and it.get("name") and it.get("prompt"):
            skills.append({k: it[k] for k in
                           ("id", "name", "desc", "icon", "category", "prompt", "tools", "version")
                           if it.get(k) is not None})
    return skills or None


def fetch_skill_manifest(url):
    """拉取 URL 并校验为技能清单。成功返回 (skills, list_version)，失败返回 (None, 错误信息)。"""
    import json as _json
    import urllib.request as _req
    try:
        req = _req.Request(url, headers={"User-Agent": "WeComCompanion"})
        with _req.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return None, f"拉取失败：{e}"
    skills = validate_skill_manifest(data)
    if not skills:
        return None, '内容不是有效的技能清单（需要 JSON：{"skills":[{"id","name","prompt","tools"}...]}）'
    lv = str(data.get("version") or "") if isinstance(data, dict) else ""
    return skills, lv


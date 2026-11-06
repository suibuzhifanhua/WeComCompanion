"""agent_tools.registry — 内置工具定义（TOOLS，逐字来自原 agent_tools.py）。
条数不写在这里：它是会过期的数字，要准确值用 len(TOOLS)（tests/test_v588_fixes.py 已锁
_TOOLS_INDEX 与 TOOLS 同数，漏注册的工具会在那里红）。"""

# ---------- 工具 ----------
TOOLS = [
    {"type": "function", "function": {
        "name": "capture_wecom_window",
        "description": "轻量读取企业微信当前网页类窗口的正文(DOM 文本)，不含表单字段/按钮清单。仅需快速看正文内容时用它；需要字段、按钮、表单状态或答题/填表/OA 场景时改用 read_wecom_page（更全面）。pick 为窗口序号，不传则自动选正文最长的窗口。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），来自 list_wecom_windows。可省略。"}
        }}
    }},
    {"type": "function", "function": {
        "name": "list_wecom_windows",
        "description": "列出企业微信当前打开的所有可访问窗口(标题+URL+序号)，并附父子关系：parent 为该窗口由哪个窗口(序号)触发打开(根窗口为 -1)，children 为其子窗口序号。用于跨页流程：进入详情/附件等子窗口后，处理完直接切回父窗口序号继续(比在页面内找返回按钮更高效)。",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        # v1.0.470 修复（P0-2）：原名 send_wecom_message 与文件末尾 OS 级版本同名——
        # _TOOL_HANDLERS 字典里同名 key 后者覆盖前者，模型按本 schema 传 content 必报
        # 「需要 target 与 text」，首轮调用必失败且 webhook 通路成死代码。改名区分。
        "name": "send_wecom_webhook",
        "description": "经群机器人 webhook 把一段文字发送到企业微信群。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "要发送的消息正文"},
            "msgtype": {"type": "string", "enum": ["text", "markdown"], "description": "消息类型，默认 text"}
        }, "required": ["content"]}
    }},
    {"type": "function", "function": {
        "name": "list_interactive_elements",
        "description": "列出企业微信某窗口内可点击/可输入的元素(按钮/链接/输入框/可点击项)，含序号与可见文字。仅当需要精确元素序号、或 read_wecom_page 返回的元素不足以定位时才调用；常规点击/输入优先用可见文字直接操作，不必先列元素。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），来自 list_wecom_windows。多窗口时必填；省略则自动选当前窗口。"}
        }}
    }},
    {"type": "function", "function": {
        "name": "click_wecom_element",
        "description": "点击窗口内某元素。target 为元素序号(来自 list_interactive_elements)或可见文字(模糊匹配)；多窗口时传 pick。⚠️ 返回 ok=true 仅代表**点击事件已派发**，不代表页面已响应（SPA 按钮静默失败很常见）。点击后必须用 wait_wecom_page_load + list_wecom_windows / read_wecom_page 验证是否真的进入预期页面；页面没变化时改用真实鼠标坐标点击（返回值里的 x/y 可直接给 os_click）或换文字定位。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "target": {"description": "元素序号(整数)或可见文字"}
        }, "required": ["target"]}
    }},
    {"type": "function", "function": {
        "name": "type_wecom_text",
        "description": "向窗口内输入框写入文本，兼容 React 受控组件。target 同 click_wecom_element；多窗口时传 pick。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "target": {"description": "输入框序号(整数)或 placeholder/name/可见文字"},
            "text": {"type": "string", "description": "要写入的文本"}
        }, "required": ["target", "text"]}
    }},
    {"type": "function", "function": {
        "name": "read_wecom_page",
        "description": "深度读取企微当前窗口：正文全文 + 所有表单字段(输入框/单选/多选/下拉/文件)的当前值与勾选状态 + 可点击按钮 + links（页面上带真实 href 的链接入口清单，如单据/月报/详情链接）。做网页答题/填表/OA 表单前先调用它掌握页面现状。任务要求逐个处理列表项（如审核全部单据/月报）时，必须从 links 逐一点击进入每个项目处理，处理完回到列表继续下一个，不能只分析列表页就结束。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"}
        }}
    }},
    {"type": "function", "function": {
        "name": "read_wecom_fields",
        "description": "聚焦读取：只返回指定关键词附近的正文片段(每词最多 3 段)，返回体小处理快。用于只需检查少数字段/数值/附件名的场景(如财务审核的支出类别/专管部门/业务类型/预算执行单/附件)。返回 {ok, title, url, body_len, fields:[{key, count, snippets}]}，count=0 表示页面无该字段。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "要读取的关键词列表，如 [\"支出类别\",\"附件\"]"}
        }, "required": ["fields"]}
    }},
    {"type": "function", "function": {
        "name": "run_rule_check",
        "description": "脚本判定引擎：把结构化数据交给确定性规则脚本判定(不耗 LLM、<1ms)。已注册模块：finance-check(财务审核 5 项规则，data=read_wecom_fields 完整 data)、finance-audit(筛已付款单据，data={rows:列表行|body:列表正文})、data-organizer(去重+分类汇总，data={headers,rows})、data-analysis(数值统计+异常识别，data={headers,rows})、file-organizer(归档分类+重复检测，data={files})、reimburse-filler(表单默认值，data={fields:字段名})。返回 {result: green|red|ok, verdict, checks:逐项判定, fails, reason}。若返回 __error__ 表示模块未注册，才改用模型判定。",
        "parameters": {"type": "object", "properties": {
            "module": {"type": "string", "enum": ["finance-check", "finance-audit", "data-organizer", "data-analysis", "file-organizer", "reimburse-filler"],
                       "description": "判定规则模块名"},
            "data": {"type": "object",
                     "description": "结构化数据，结构按所选模块（见 description 中各模块 data 要求）"}
        }, "required": ["module", "data"]}
    }},
    {"type": "function", "function": {
        "name": "run_audit_flow",
        "description": "脚本流水线执行器（v1.0.220）：整条确定性流程（滚动加载列表→解析全部月报链接→逐个后台静默点开→读字段→静默取附件→规则判定→记录→关闭详情页）由 Python 脚本跑完，零 LLM 消耗。**全程不点下载按钮、不弹任何对话框**（v1.0.604：判定口径=运维天数一致 + 设备清单是否上传，在用台数仅参考、不再比对数量）。**脚本会自动生成报告文件**（返回 report_path），模型只负责：①读取/引用 report_path 里的真实数据汇报；②对 issues 疑问项做智能处理（all_labels 找近义字段/确认列名）。**禁止编造数字**——报告里没有的机构绝不写待定/通过/不通过，只汇报脚本实际处理的数量。⚠️ 强制纪律（v1.0.413）：凡用户要求检查/审核/核对/跑一遍月报、待办单据类批量任务，**第一步就必须调用本工具**——不要先手动 click/read_page 试探（实测逐张手动点击会因列表索引错位全部失败，且浪费大量 token）；也不要用 pc_takeover/dsh/浏览器打开 OA（脱离 CDP 控制必失败）。脚本执行失败时按返回 summary 的指引处理（通常=直接重跑一次 run_audit_flow）。",
        "parameters": {"type": "object", "properties": {
            "flow": {"type": "string", "description": "流水线名：monthly-report（月报审核）/ finance（财务单据核查：逐张静默开页→read_fields→rule_check 判定→关页，**全程只读**，没有任何审批点击路径）", "enum": ["monthly-report", "finance"]},
            "auto_approve": {"type": "boolean", "description": "判定通过的月报自动点击「通过」按钮提交审批。真实且不可撤销：软件会先弹独立确认（v1.0.614 起为代码级闸门，任何权限档都要确认，「完全访问」除外；定时/后台会话不能确认、会直接拒绝）。仅用户明确要求时传 true，默认 false"},
            "mode": {"type": "string", "description": "all=全量（默认）；retry=只重跑上一轮非通过的项（分支机构重传附件后复查用，30 秒级）；resume=从上次中断的断点继续（已完成项不重跑）", "enum": ["all", "retry", "resume"]}
        }, "required": ["flow"]}
    }},
    {"type": "function", "function": {
        "name": "learn_capture",
        "description": "在线课程内容采集器（v1.0.230）：学习平台的课件是 canvas 渲染的 PDF + 视频，read_wecom_page 拿不到文字（DOM 只有页码），但考试题直接出自课件——不采集就只能靠模型瞎猜（实测 60 分，及格 90）。本工具用「截图 + 本地 OCR」把课件转成可检索文本：自动定位课件所在的 iframe（主页面点不到翻页控件，v1.0.230 已解决），PDF 模式逐页「截图→OCR→iframe 内翻页」，视频模式按间隔抽帧 OCR；结果存为 产物库/知识库/课件/<课程名>.md，考试时 exam-solver 会检索它作答（准确率 90%+）。参数：name=课程名（用于命名）、mode=pdf/video/auto（auto 先试 PDF 无内容再试视频）、pick=窗口序号。若页面停在课程任务页（没有画布），会自动点课程卡片进入课件（绝不点『已完成』）。视频模式需视频处于播放状态。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "课程名（用作文件名，如『协助执法业务管理指引』）"},
            "mode": {"type": "string", "description": "采集模式", "enum": ["auto", "pdf", "video"]},
            "pick": {"type": "integer", "description": "窗口序号（list_wecom_windows 查看），缺省 0"}
        }, "required": ["name"]}
    }},
    {"type": "function", "function": {
        "name": "select_wecom_option",
        "description": "设置下拉框(select)/单选(radio)/勾选框(checkbox)的值。target 为序号(整数)或文字；value：下拉填选项文字或值，单选填要选的 value，勾选填 'true'/'false'。target 与 value 都必须提供。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "target": {"description": "元素序号(整数)或 placeholder/name/标签文字"},
            "value": {"type": "string", "description": "要设置的值（下拉填选项文字/值；勾选填 true/false）"}
        }, "required": ["target", "value"]}
    }},
    {"type": "function", "function": {
        "name": "scroll_wecom_window",
        "description": "滚动窗口以露出更多内容（长试卷/长文档/懒加载）。direction: up/down/top/bottom；amount 为像素(默认800)。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"], "description": "滚动方向，默认 down"},
            "amount": {"type": "integer", "description": "滚动像素，direction 为 up/down 时生效"}
        }}
    }},
    {"type": "function", "function": {
        "name": "wait_wecom_page_load",
        "description": "等待当前选中企微页面渲染落定（document.readyState==='complete' 且正文长度连续采样稳定）。点击进入预览页/详情页/新页面、或刚打开列表页后，先调用本工具再 read_wecom_page / read_wecom_fields / click_wecom_element，避免读到空白或半渲染内容。行为与实现一致：内部预算封顶 8 秒；**不会刷新页面**（历史版本超时后 reload 会把刚进入的表单刷回首页，造成来回跳，故取消）；超时不报错，返回 ok+warning，仍需再用 read 确认实际内容。pick 为窗口序号或标题（推荐标题如「待办事宜」），可省略。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），来自 list_wecom_windows。可省略。"},
            "timeout": {"type": "integer", "description": "最大等待秒数，默认 8（实现封顶 8 秒，传更大值也按 8 秒处理）。"}
        }}
    }},
    {"type": "function", "function": {
        "name": "wecom_run_js",
        "description": "在指定企微窗口执行一段 JavaScript（v1.0.260）并返回结果。用于 click/read 工具覆盖不到的页面操作：①学习视频直接拖到结尾——expression: const v=document.querySelector('video'); if(!v) return 'no video'; v.currentTime = v.duration - 3; return JSON.stringify({cur:Math.round(v.currentTime), dur:Math.round(v.duration)}); ②读取视频播放状态（paused/currentTime/duration）；③检测页面是否存在某按钮/弹窗/文本。expression 传函数体语句（会自动包成 (()=>{...})() 执行），最后用 return 返回结果（返回对象/数组会自动 JSON 序列化）。多窗口时传 pick。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "expression": {"type": "string", "description": "JS 函数体语句，用 return 返回结果。示例：const v=document.querySelector('video'); if(!v) return 'no video'; return JSON.stringify({cur:Math.round(v.currentTime), dur:Math.round(v.duration)});"}
        }, "required": ["expression"]}
    }},
    {"type": "function", "function": {
        "name": "list_wecom_links",
        "description": "提取当前窗口内所有链接，含可下载文件(pdf/xlsx/doc/zip 等)的直链。用于 OA 文件下载场景。返回 links:[{text,href}]。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"}
        }}
    }},
    {"type": "function", "function": {
        "name": "upload_wecom_file",
        "description": "把本地文件上传到窗口内的 <input type=file> 文件框，实现 OA 附件上传。filepath 应是「产物库」里的本地绝对路径（可由 save_file 先生成）。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "target": {"description": "文件输入框序号(整数)或 name/aria-label 文字"},
            "filepath": {"type": "string", "description": "要上传的本地文件绝对路径"}
        }, "required": ["target", "filepath"]}
    }},
    {"type": "function", "function": {
        "name": "type_wecom_document",
        "description": "向窗口内最大的可编辑区域(contenteditable，如腾讯文档/在线文档正文)末尾插入文本，用于编辑在线文档。多窗口时传 pick。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "text": {"type": "string", "description": "要写入文档正文的文本"}
        }, "required": ["text"]}
    }},
    {"type": "function", "function": {
        "name": "save_file",
        "description": "把一段文本/代码/JSON 等产出保存到本地「产物库」。subdir 可选：产出/对话记录/下载/技能/知识库/课件/知识库/题库/知识库/制度，默认 产出。filename 含扩展名(如 report.md/result.json/skill.md)。",
        "parameters": {"type": "object", "properties": {
            "subdir": {"type": "string", "description": "子目录：产出 | 对话记录 | 下载 | 技能 | 知识库/课件 | 知识库/题库 | 知识库/制度（嵌套，正/反斜杠均可）", "enum": ["产出", "对话记录", "下载", "技能", "知识库/课件", "知识库/题库", "知识库/制度"]},
            "filename": {"type": "string", "description": "文件名，含扩展名，如 会议纪要.md、result.json"},
            "content": {"type": "string", "description": "要保存的文本内容"}
        },         "required": ["filename", "content"]}
    }},
    {"type": "function", "function": {
        "name": "save_workspace_file",
        "description": "把内容写入产物库，支持【追加】模式（v1.0.230）：答题记录/进度流水这类「逐条累积」的内容用 mode=append 一条条追加，不必每次重写全量（省 token、历史不丢）。subdir 默认 产出。",
        "parameters": {"type": "object", "properties": {
            "subdir": {"type": "string", "description": "子目录：产出 | 对话记录 | 下载 | 技能 | 知识库/课件 | 知识库/题库 | 知识库/制度（知识库为嵌套子目录，正/反斜杠均可），默认 产出。考试答题记录写 知识库/题库。"},
            "filename": {"type": "string", "description": "文件名，含扩展名，如 答题记录_协助执法制度_2026-08-31.md"},
            "content": {"type": "string", "description": "要写入的内容（追加模式下接在文件末尾）"},
            "mode": {"type": "string", "description": "append=追加到末尾（默认）；overwrite=覆盖整个文件", "enum": ["append", "overwrite"]}
        }, "required": ["filename", "content"]}
    }},
    {"type": "function", "function": {
        "name": "append_progress",
        "description": "把一条「单项处理结果」追加到进度文件（产物库/产出/<key>_进度.md），返回已累计条目数。批量任务（逐项审核/逐张处理）每处理完【一项】立即调用一次记录结果，最后全部处理完再 read_workspace_file 读进度文件合并输出总结——中间结果不丢、单轮上下文不用背负全部历史（系统会在每次调用后自动精简上下文，下一个单据从干净上下文开始）。",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "任务标识，如 月报审核 / 单据核查（用于生成进度文件名）"},
            "content": {"type": "string", "description": "本条处理结果的内容（第几个/哪一项/结论/关键数据，Markdown 格式）"}
        }, "required": ["key", "content"]}
    }},
    {"type": "function", "function": {
        "name": "new_progress_round",
        "description": "给某个批量任务【开新一轮】：把当前的进度台账（产物库/产出/<key>_进度.md）改名归档到同目录（轮次归档_日期_时间_…），本轮从空文件重新开始。用途：进度台账是纯追加的，而快捷入口会先读它、把里面记过的条目当作“已处理”跳过——用户说“重新跑一轮/这次不算之前记过的/开新一轮”时必须先调它，否则上一轮的记录会把这一轮的跳过判定喂错。归档文件仍在产物库/产出里可查可点，不会被删。",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "任务标识，与 append_progress 用的 key 一致（如 月报审核 / 答题记录 / 学习进度）"}
        }, "required": ["key"]}
    }},
    {"type": "function", "function": {
        "name": "close_wecom_page",
        "description": "关闭企微里的指定窗口/标签页（pick 为窗口序号，缺省关闭当前跟随窗口）。批量审核流程【必须】：审完一项保存结果后调用它关闭详情页，再回列表页点下一项——详情页不关会标签堆积（列表链接每次点击都开新标签，实测堆出一串重复窗口）。关闭成功后窗口列表变化，用 list_wecom_windows 重新确认。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": "integer", "description": "要关闭的窗口序号（list_wecom_windows 查看）；缺省关闭当前跟随窗口"}
        }}
    }},
    {"type": "function", "function": {
        "name": "save_experience",
        "description": "把一条「问题+解决方法」沉淀到经验库（experiences.json，独立存储），让后续同类任务可复用（Claw memory 思想）。agent 在答题/审批/流程分析中发现新规则、踩坑经验、专用字段含义时主动调用：module=相关模块名（exam-solver/finance-check/oa-approval 等），problem=一句话问题，solution=可执行解决方法。",
        "parameters": {"type": "object", "properties": {
            "module": {"type": "string", "description": "所属模块名（如 exam-solver / finance-check / oa-approval / process-summary），便于经验库 UI 分类筛选"},
            "problem": {"type": "string", "description": "问题描述：一句话写清楚哪个模块、什么场景、出了什么问题"},
            "solution": {"type": "string", "description": "解决方法：具体怎么做的、调整了什么参数/步骤，下次如何避免"}
        }, "required": ["module", "problem", "solution"]}
    }},
    {"type": "function", "function": {
        "name": "download_file",
        "description": "从给定 URL 下载文件到本地「产物库」(默认 产物库/下载 子目录)。仅支持 http/https，非 200 报错；文件名优先取 Content-Disposition，其次 URL 末段。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "要下载的文件 URL（仅 http/https）"},
            "filename": {"type": "string", "description": "保存的文件名(可省略，自动从响应头/URL 取)"},
            "subdir": {"type": "string", "description": "产物库子目录，默认 下载"}
        }, "required": ["url"]}
    }},
    {"type": "function", "function": {
        "name": "download_wecom_attachment",
        "description": "下载企微窗口内 OA 附件（doc/pdf/xlsx 等）到「产物库/下载」：**只走页内 fetch 静默直传**——带登录态在页面里取附件下载接口，二进制流直接落盘本地，不点任何按钮、不弹下载框（v1.0.604 起停用「点击下载」回退：企微 Electron 必弹原生「另存为」，会打断用户前台）。target 为附件可见文字（如「合同.pdf」）。成功返回本地路径，可再用 read_workspace_file 读取内容；返回 silent_only=true 表示该页取不到直链——改用 read_wecom_page 的 links/body 找附件真实 href 再 download_file，或请用户手动下载，**不要**尝试点击页面下载按钮。",
        "parameters": {"type": "object", "properties": {
            "pick": {"type": ["integer", "string"], "description": "企微窗口：序号或标题模糊匹配（推荐标题如「待办事宜」——窗口开关后序号漂移，标题稳定），多窗口时必填"},
            "target": {"description": "附件的可见文字（如「合同.pdf」）"},
            "timeout": {"type": "integer", "description": "等待静默直传完成的秒数，默认 90"}
        }, "required": ["target"]}
    }},
    {"type": "function", "function": {
        "name": "list_workspace_files",
        "description": "列出「产物库」内文件：传 subdir 列出某子目录(产出/下载/对话记录/技能/知识库/课件/知识库/题库/知识库/制度)，不传则列出全部子目录与文件。用于数据整理前掌握已有材料、考试前查知识库素材。v1.0.369：传 path=完整目录路径 可列出用户拖入聊天框的本地文件夹（仅限拖拽白名单内目录），读其中文件用 read_workspace_file。",
        "parameters": {"type": "object", "properties": {
            "subdir": {"type": "string", "enum": ["产出", "下载", "对话记录", "技能", "知识库/课件", "知识库/题库", "知识库/制度"], "description": "子目录名（知识库为嵌套子目录，正/反斜杠均可），省略则列全部"},
            "path": {"type": "string", "description": "可选。用户拖入聊天框的本地文件夹完整路径（须已在拖拽白名单）——列出该目录内容，与 subdir 二选一"}
        }}
    }},
    {"type": "function", "function": {
        "name": "read_workspace_file",
        "description": "读取某文件并自动解析为文本：txt/csv/md/json/py 等直接读；pdf(含扫描件自动OCR)/doc/docx/xlsx/xls/pptx/rtf/图片 自动提取文本内容（大文件自动截断并注明，扫描件PDF默认OCR前8页）。路径须在产物库内（防目录穿越），或用户拖入聊天框的本地文件（拖拽白名单只读放行，v1.0.369——用户拖入的文件/文件夹可直接按原路径分析，无需先上传）。适合分析用户上传/下载/拖入的文件。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "产物库内相对/绝对路径（如 下载/报表.xlsx、产出/summary.md），或用户拖入的本地完整路径（如 C:\\Users\\xx\\Desktop\\报表.xlsx）"},
            "max_chars": {"type": "integer", "description": "可选。只读文件开头 N 字符的「头部概览」——超大素材(如多份视频OCR可达80KB)先读概览定位相关段、保持上下文精简，确需某段全文再去掉 max_chars 重读。不传=默认上限(50KB)。"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "analyze_monthly_attachment",
        "description": "读设备清单 Excel（产物库/下载 下的附件），脚本统计在用的台式电脑+笔记本电脑数量（自动识别 设备状态/设备类型 列，状态含'在用'且类型含'台式/笔记本'）。模型下载附件后直接调用拿数字，不需要自己逐行读 Excel 统计。返回 pcs_in_use（在用设备数）、total_rows、header。未识别到列时返回 error_hint+表头。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "产物库内文件相对路径，如 下载/甲分公司信息设备清单202607.xlsx"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "create_skill",
        "description": "生成一个新的 WorkBuddy 技能文件(SKILL.md)保存到本地「产物库/技能」目录，让用户可以拿到可复用的自动化流程。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能英文名(作为文件夹名)，如 pdf_extract"},
            "description": {"type": "string", "description": "技能一句话说明(给用户的触发场景)"},
            "content": {"type": "string", "description": "SKILL.md 的完整正文(markdown)"}
        }, "required": ["name", "content"]}
    }},
    {"type": "function", "function": {
        "name": "finish",
        "description": "当你认为任务已经完成（例如考试题全部答完、报告已生成、操作已结束），调用此工具结束工作并向用户给出最终结论。必须附上一段 final_answer 总结结果。",
        "parameters": {"type": "object", "properties": {
            "final_answer": {"type": "string", "description": "给用户的最终答案/总结/结果。用中文。"}
        }, "required": ["final_answer"]}
    }},
    {"type": "function", "function": {
        "name": "ask_user",
        "description": "【需要用户抉择时必用】任务执行中需要用户做选择/提供偏好/补充信息时（如 PPT 风格与主题、方案二选一、缺失的关键参数、是否继续某步），调用本工具弹窗向用户提问——用户会看到选项按钮并点选/输入，回答以【用户决定】回传给你后继续执行。铁律：需要用户决策时必须用本工具弹窗，禁止在最终回复里提问再等用户下一轮打字（那会中断任务流）；每个关键决策点最多问一次，能给的选项都给上（2-6 个），选项要具体可点。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "要问用户的问题，具体、可直接回答，如「PPT 想要什么视觉风格？」"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "给用户点的选项（2-6 个，每个 ≤30 字），如 [\"简约商务蓝\",\"活泼多彩\",\"科技暗黑\"]；纯开放问题可不传"},
            "allow_text": {"type": "boolean", "description": "默认 true——除点选项外允许用户自由输入"}
        }, "required": ["question"]}
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "通过 DuckDuckGo 进行联网搜索（开源免费、无需 API Key），按关键词返回标题/链接/摘要列表，供模型获取公网实时信息。依赖库：ddgs。联网请求独立访问公网，与本地模型配置无关。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词，尽量简洁具体，如「2026 世界人工智能大会 时间地点」"},
            "max_results": {"type": "integer", "description": "返回结果条数，1-10，默认 5"}
        }, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "fetch_webpage",
        "description": "抓取指定网页 URL 的正文文本（自动处理编码，自动截断到约 8000 字符），用于深度阅读搜索结果对应的页面内容。依赖库：requests + beautifulsoup4(bs4)。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "要抓取的完整网页地址（http/https）"},
            "max_chars": {"type": "integer", "description": "正文最大字符数，默认 8000"}
        }, "required": ["url"]}
    }},
    {"type": "function", "function": {
        "name": "ask_judge",
        "description": "向「判断大模型」请求判断/决策建议（双模型分工用）：当你遇到需要推理判断的步骤（答题选哪个答案、审批是否通过、存疑项判定、复杂决策），把题干/选项/上下文交给大模型，按其返回结论执行。机械操作（读页/翻页/点已知目标/滚动）不要调用本工具。未配置判断模型时本工具不可用。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "要明确判断的问题，如『这道题选哪个：A.xxx B.yyy C.zzz』"},
            "context": {"type": "string", "description": "补充上下文（题目全文/页面关键内容/已知约束），可选但建议提供以提高判断准确率"}
        }, "required": ["question"]}
    }},
    {"type": "function", "function": {
        "name": "pc_screenshot",
        "description": "截屏（v1.0.297 PC 操控）：全屏或指定窗口（按标题关键词定位）截图，存 产物库/产出/截图/ 并返回路径/尺寸。window 可选，传窗口标题关键词只截该窗口，找不到则截全屏并注明。截图是本地文件，模型不能直接看图——需要屏幕信息时优先用企微窗口工具或请用户描述。",
        "parameters": {"type": "object", "properties": {
            "window": {"type": "string", "description": "可选。窗口标题关键词，如「学习中心」；不传截全屏"}
        }}
    }},
    {"type": "function", "function": {
        "name": "pc_files",
        "description": "本机文件操作（v1.0.297 PC 操控）：op=list 列目录（最多200条）/ read 读文本（utf-8/gbk 自动识别，上限2万字符）/ write 写文本（可覆盖，覆盖前必须告知用户）/ copy / move / delete（**只进回收站可还原，永不硬删**）。path 支持绝对路径或 ~ 开头，相对路径基于用户主目录。破坏性操作（write 覆盖/delete）执行前必须已向用户说明并获确认。",
        "parameters": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["list", "read", "write", "copy", "move", "delete"], "description": "操作类型"},
            "path": {"type": "string", "description": "目标路径（绝对路径或 ~ 开头或相对主目录）"},
            "target": {"type": "string", "description": "copy/move 的目标路径"},
            "content": {"type": "string", "description": "write 时要写入的文本内容"}
        }, "required": ["op", "path"]}
    }},
    {"type": "function", "function": {
        "name": "pc_run",
        "description": "在本机执行命令（v1.0.297 PC 操控）：默认 cmd，shell=powershell 可切 PowerShell；返回 exit_code/stdout/stderr（各截断 8000 字符）。timeout 秒（5-300，默认60，超时强杀）。**纪律**：只执行用户明确要求或任务必需的命令；删除/格式化/停服务/改注册表等破坏性命令必须先向用户列出完整命令并获确认，绝不擅自执行。",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "要执行的命令，如 'dir C:\\Users\\me\\Desktop' 或 'ipconfig'"},
            "shell": {"type": "string", "enum": ["cmd", "powershell"], "description": "默认 cmd"},
            "timeout": {"type": "integer", "description": "超时秒数，5-300，默认 60"}
        }, "required": ["command"]}
    }},
    {"type": "function", "function": {
        "name": "pc_apps",
        "description": "本机程序/进程管理（v1.0.297 PC 操控）：op=list 列运行中进程（按内存降序 Top30，含 pid/内存）/ start 启动程序（短名如 记事本/explorer/chrome，或 exe 绝对路径，或 URL 用默认浏览器开）/ open 用系统默认程序打开 URL 或文件 / kill 强制结束进程（name 传进程名如 chrome.exe 或 PID，**不可逆，未保存数据会丢，必须先经用户确认**）。",
        "parameters": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["list", "start", "open", "kill"], "description": "操作类型"},
            "name": {"type": "string", "description": "start/open/kill 的目标：程序短名、路径、URL 或进程名/PID"}
        }, "required": ["op"]}
    }},
    {"type": "function", "function": {
        "name": "pc_clipboard",
        "description": "本机剪贴板（v1.0.297 PC 操控）：op=get 读取剪贴板文本（无文本返回空+提示）/ op=set 写入文本到剪贴板（text 必填）。set 会覆盖用户当前剪贴板内容，执行前须告知用户。",
        "parameters": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["get", "set"], "description": "get=读取 set=写入"},
            "text": {"type": "string", "description": "set 时要写入的文本"}
        }, "required": ["op"]}
    }},
    {"type": "function", "function": {
        "name": "pc_system_info",
        "description": "本机系统状态（v1.0.297 PC 操控）：一次性返回 主机名/系统/CPU核数/内存总量与占用/各磁盘容量与使用率/本机IP/运行时长/Top10 内存进程。诊断电脑慢/磁盘满/查 IP/看谁占内存 时用它，不用逐条跑命令。",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "dsh_status",
        "description": "查「电脑接管通道」就绪状态（v1.0.298，基于 deepseek-harness）：返回 Node 版本（需 22.19+ 或 24+）、dsh 是否已安装、模型端点/key 是否配置，以及逐项修复提示。调用 pc_takeover 前、或 pc_takeover 报「未安装/凭据/模型」类错误时，先调本工具诊断。",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "dsh_prepare",
        "description": "安装电脑接管通道所需的 deepseek-harness agent（v1.0.298）：等价 npm i -g @deepseek-ai/dsh，官方源失败自动切国内镜像重试。首次 1-10 分钟（视网络）。仅当 dsh_status 显示 dsh 未安装时调用；不要对已安装的重复调用。装好后 pc_takeover 才可用。",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "pc_takeover",
        "description": "把电脑任务委托给本机 deepseek-harness agent 自主完成（v1.0.298 接管通道，兜底用）：dsh 自带终端/文件/工作区工具和自己的安全审批策略，适合内置 pc_* 直连工具搞不定的**多步骤、复杂、需要看结果再调整**的任务（跑测试修 bug、项目维护、批量文件处理、环境诊断修复等）。**慢（几十秒到几分钟）且耗模型 token，只有 pc_* 确实做不到或用户明确要求交给 harness agent 时才用**。task 要写具体（路径、期望结果、约束）；cwd 工作目录可选（默认产物库）；timeout 秒可选（60-1800，默认 900）。返回 dsh 最终答案，完整过程存档 产物库/产出/dsh/。",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "任务描述，要具体。例：『检查 C:\\proj 项目，跑测试并修复所有失败用例，不要改动 README』"},
            "cwd": {"type": "string", "description": "可选。工作目录（绝对路径或 ~ 开头），默认产物库"},
            "timeout": {"type": "integer", "description": "可选。超时秒数，60-1800，默认 900"}
        }, "required": ["task"]}
    }},
    {"type": "function", "function": {
        "name": "create_excel",
        "description": "生成 Excel(.xlsx) 文件到产物库/产出/，交付可验收文件。支持多工作表、表头样式（自动加粗蓝底+冻结首行）、列宽自适应；单元格可直接写公式字符串（如 =SUM(A2:A10)）。做数据交付/报表/透视结果落表时用它产出真文件。",
        "parameters": {"type": "object", "properties": {
            "filename": {"type": "string", "description": "文件名（如 一月销售汇总.xlsx）"},
            "sheets": {"type": "array", "description": "工作表数组，每项 {name: 表名, rows: 二维数组（首行为表头）, freeze_header: bool 可省略默认 true}", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "rows": {"type": "array", "items": {"type": "array"}}, "freeze_header": {"type": "boolean"}
            }, "required": ["rows"]}}
        }, "required": ["filename", "sheets"]}
    }},
    {"type": "function", "function": {
        "name": "create_docx",
        "description": "生成 Word(.docx) 文件到产物库/产出/。blocks 顺序排版：h1/h2/h3 标题、p 正文段、bullet 项目符号、number 编号列表、table 表格（rows 二维数组，首行为表头自动加粗+网格线）。",
        "parameters": {"type": "object", "properties": {
            "filename": {"type": "string", "description": "文件名（如 项目状态报告.docx）"},
            "blocks": {"type": "array", "items": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["h1", "h2", "h3", "p", "bullet", "number", "table"]},
                "text": {"type": "string"}, "rows": {"type": "array", "items": {"type": "array"}}
            }, "required": ["type"]}}
        }, "required": ["filename", "blocks"]}
    }},
    {"type": "function", "function": {
        "name": "create_pptx",
        "description": "生成 PPT(.pptx) 文件到产物库/产出/。slides 每页 {title, bullets（要点数组，≤14 条）, notes（演讲备注，可省略）}；首页默认标题版式（bullets 作副标题）。做演示交付时用它产出真文件。",
        "parameters": {"type": "object", "properties": {
            "filename": {"type": "string", "description": "文件名（如 Q2 路线图.pptx）"},
            "title_slide": {"type": "boolean", "description": "首页用标题版式，默认 true"},
            "slides": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "bullets": {"type": "array", "items": {"type": "string"}}, "notes": {"type": "string"}
            }, "required": ["title"]}}
        }, "required": ["filename", "slides"]}
    }},
    {"type": "function", "function": {
        "name": "send_wecom_message",
        "description": "向企业微信的联系人或群发送一条文本消息（OS 级驱动主窗口：搜索对象→开聊天→输入→发送）。**真实发送动作**——仅在用户明确要求发消息/推送通知时使用。前置：企微主窗口需在桌面上可见（最小化到托盘时返回明确错误，请用户点开企微主界面）。发送对象名称要与企微里的显示名一致；一次发一条，长文本自行分段。",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "联系人或群名称（与企微显示名一致，如「文件传输助手」）"},
            "text": {"type": "string", "description": "要发送的消息文本"}
        }, "required": ["target", "text"]}
    }},
    {"type": "function", "function": {
        "name": "request_skill",
        "description": "当任务匹配系统提示中「未安装的技能库技能」清单里的某项时调用：向用户弹窗请求安装该技能。用户同意后技能会自动安装并注入下一轮（你继续即可使用）；用户拒绝时请改用基础工具完成。skill_id 必须来自清单，不要编造。",
        "parameters": {"type": "object", "properties": {
            "skill_id": {"type": "string", "description": "清单中的技能 id"},
            "reason": {"type": "string", "description": "一句话说明为什么这个任务需要它"}
        }, "required": ["skill_id", "reason"]}
    }},
    {"type": "function", "function": {
        "name": "import_skill_source",
        "description": "用户给你一个技能清单网址（URL 指向 JSON：{\"skills\":[{id,name,prompt,tools}...]}）并希望导入时调用：会向用户弹窗展示清单，确认后自动把远程源设为该地址并把其中全部技能批量安装进技能中心。会请用户确认，不要重复调用。只用于用户明确提供的 URL。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "技能清单 JSON 的 http(s) 地址（用户提供）"}
        }, "required": ["url"]}
    }},
]

# ---------- 本次任务**产出了文件**的工具（v1.0.634，用户要求回答末尾列清单） ----------
# 口径为什么按"工具名"而不是"结果里有没有 path"：read_workspace_file / pc_files(op=read) /
# dsh_status 这些只读工具的返回体同样带 path（那是"被读的那个文件"的位置），按 path 判会把
# 读过的文件当成生成的文件列进清单——用户点进去发现不是刚产出的，清单就废了。
# 新增会写文件的工具时把它加进来，否则回答末尾的清单不会列出它的产物。
FILE_PRODUCER_TOOLS = frozenset({
    "save_file", "save_workspace_file", "create_skill",
    "create_excel", "create_docx", "create_pptx",
    "download_file", "download_wecom_attachment", "learn_capture",
    "pc_files",
})
# 一把工具多语义的：只有这些 op 才算产出（pc_files 的 read/list/delete 都不算——
# 尤其 delete，它的 path 是"刚被移进回收站的文件"，列成产物等于骗用户去点开）。
FILE_PRODUCER_OPS = {"pc_files": frozenset({"write", "copy", "move"})}


def is_file_producer(name, args, result):
    """这条工具调用是否产出了一个可交付的文件（且结果里带得到它的路径）。"""
    if name not in FILE_PRODUCER_TOOLS:
        return False
    ops = FILE_PRODUCER_OPS.get(name)
    if ops and str(((args or {}).get("op")) or "").strip().lower() not in ops:
        return False
    return isinstance(result, dict) and result.get("ok") is True \
        and isinstance(result.get("path"), str) and bool(result["path"].strip())


# ---------- 工具执行属性（v1.0.646：自 agent/tools_call.py 迁入，常量逐字未改） ----------
# 可安全并行的只读工具白名单（并行预取用）：纯网络/本地读，彼此无共享状态、可真正并行
_PARALLEL_SAFE_TOOLS = frozenset({
    "fetch_webpage", "list_workspace_files", "read_workspace_file", "web_search", "run_rule_check",
})

"""agent_tools.skills — 内置技能定义（SKILLS，19 项 + 插件体系设计注释，逐字来自原 agent_tools.py）。"""

# ============================================================
# 技能 / 连接器插件体系（对标 WorkBuddy 的 Skill / Connector）
# ------------------------------------------------------------
# 设计要点：
#  - SKILLS：内置能力分组，每个技能声明它“拥有”的工具名 + 一段 prompt。
#    agent 运行时只把“已启用技能”的工具与 prompt 注入，未启用的不加载。
#  - CONNECTORS：外部服务连接器，需用户配置（base_url/key 等）后才注入工具与 prompt。
#  - finish 作为系统元工具始终可用，不归属任何技能。
#  - build_agent_tools(cfg) 返回当前应加载的 (tools, prompt_extra)；
#    dispatch_tool(name,args,cfg) 负责把工具调用路由到对应执行器。
# ============================================================

# 学习中心视频任务的静音表达式——与 services.study_engine 跑课提示共用单源
# （v1.0.623 前这里是只有 __muteLoop 的旧副本，会被播放器把 muted 重置回 false）
from core.study_js import STUDY_MUTE_EXPR

SKILLS = [
    {"id": "window-sense", "name": "窗口感知", "judge": "none", "icon": "👁",
     "desc": "读取企微窗口正文、列出可点击/可输入元素，是任何窗口操作的前置能力。",
     "prompt": "操作任何窗口前，先用 read_wecom_page / list_interactive_elements 看清字段、按钮、链接与表单状态。",
     "tools": ["capture_wecom_window", "list_wecom_windows", "list_interactive_elements", "read_wecom_page"],
     "default": True},
    {"id": "window-act", "name": "窗口操作", "judge": "none", "icon": "🖱",
     "desc": "点击、输入、选择、滚动、上传文件、编辑在线文档等实际交互。",
     "prompt": "点击/输入/选择/滚动用对应工具；多窗口务必传 pick。每次操作后可用 read_wecom_page 确认结果。",
     "tools": ["click_wecom_element", "type_wecom_text", "select_wecom_option",
               "scroll_wecom_window", "upload_wecom_file", "type_wecom_document"],
     "default": True},
    {"id": "workspace", "name": "文件产出", "judge": "none", "icon": "📁",
     "desc": "把生成的报告/代码/JSON 落地到产物库，或下载附件，或生成可复用技能；可读取用户上传的各类文件做分析；沉淀经验到经验库。",
     "prompt": "所有产出都要落地保存：报告/代码/JSON 用 save_file；下载用 download_file；生成技能用 create_skill。均存「产物库」。用户上传的文件在「产物库/上传」，用 read_workspace_file 读取（支持 pdf/docx/xlsx/pptx/文本自动解析）后分析。\n"
     "发现可复用规则（题型特有规律/审批约定/流程陷阱/字段含义）时用 save_experience 沉淀到经验库（experiences.json），下次同类任务自动遵循——这就是跨任务记忆。\n"
     "批量逐项任务每处理完一项用 append_progress 记录（系统自动精简上下文）。",
     "tools": ["save_file", "download_file", "create_skill", "read_workspace_file", "save_experience", "append_progress", "new_progress_round"],
     "default": True},
    {"id": "messaging", "name": "企微消息", "judge": "none", "icon": "💬",
     "desc": "经群机器人 webhook 把内容发回企微群。",
     "prompt": "需要把结果推送到企微群时用 send_wecom_webhook（不可逆操作，先与用户确认）。",
     "tools": ["send_wecom_webhook"],
     "default": True},
    {"id": "exam-solver", "name": "答题助手", "judge": "model", "icon": "📝",
     "desc": "考试/问卷/在线答题：读题→逐题作答→翻页→交卷，全流程自动化（已实测跑通）。",
     "prompt": "答题流程（v1.0.247 先规划后执行+选中状态可读+交卷铁律+无限次重选未选答案）：\n"
     "【执行纪律·v1.0.247·违反=白烧模型轮次】① 先规划后执行：步骤 0.5 必须先把本轮完整\n"
     "     答题计划落盘，进答题页后纯机械执行（照计划点选项→下一题），禁止中途再推理；\n"
     " ② 进答题页后禁止 read_workspace_file 重读台账/对话记录文件（规划期读一次已足够，\n"
     "     每次重读是一整轮 LLM，实测每轮白耗 5-10 分钟）；\n"
     " ③ 禁止回看已答题（唯一例外：按计划改选某题）；\n"
     " ④ 每轮必须输出工具调用，禁止输出总结性/过渡性文字——总结只允许出现在最终\n"
     "     finish 里（实测：无工具调用的空轮在本地模型上每轮耗 2-10 分钟）。\n"
     " ⑤ 【v1.0.290 每题独立·不依赖上一题】每道题相互独立：答完一题点「下一题」后，\n"
     "     对新一题【重新 read_wecom_page 读题干+选项、独立推理】，不要复用/沿用上一题的\n"
     "     推理过程或答案模式（唯一例外：同款题且台账已有锁定结论→直接照抄）。答题依据\n"
     "     只有三类：①知识库/课件/ 素材原文 ②当前题干语义 ③web_search 联网搜到的公网知识\n"
     "     ——不要靠『上一题选了啥所以这题也选啥』。\n"
     "     【联网搜索·v1.0.292 已接入 Bing】web_search 只在题干涉及【素材里没有的外部\n"
     "     信息】时用（某法规/制度原文、行业通用定义、外部系统术语）；内部题绝大多数出自\n"
     "     素材，不要为内部题瞎搜（白耗模型轮次）。每场考试最多搜 1-2 次，搜到有用结论\n"
     "     立即并入当题推理；无结果换关键词重试一次，仍无结果回退素材+题干语义作答。\n"
     "     内核已把已答题的工具往返折叠压缩（recent 只留最近约 2 题），你无需也不要在上下文\n"
     "     里累积逐题历史；上下文只跟「材料 + 当前题 + 联网结论」有关，答完即弃。\n"
     "  -2) 【优先查知识库·提分关键】答题前先 list_workspace_files 看 知识库/课件/ 目录\n"
     "     （产物库/知识库/课件/ 放课程课件 OCR 原文；知识库/制度/ 放制度/管理办法原文）。\n"
     "     【模糊匹配·v1.0.288】考试名常带「测试/考试/配套考题」后缀，素材文件常带「培训/课程/讲义」后缀\n"
     "     ——不能只看精确同名。匹配规则：①先去后缀（测试/考试/配套考题/培训/课程/讲义/视频/题库）\n"
     "     得到关键词；②list_workspace_files 列 知识库/课件/ 后做 substring 包含（任一关键词在文件名内即命中）；\n"
     "     ③精确同名也算命中。命中后 read_workspace_file 读全文，作答【以文档原文为准】\n"
     "     （内部考试题绝大多数直接出自课件/制度原文）。这一步能把准确率从 60% 提到 90%+。\n"
     "     【素材读取纪律·防大上下文空转 v1.0.294】视频 OCR 素材常很大（单份 30-50KB，\n"
     "     多份叠加可达 80KB+），全量读进上下文会把 DaoPro 顶进大上下文空转区（变慢+反复压缩）：\n"
     "     a) 多份命中 → 先只读【关键词与考试名重合度最高的那一份】（通常是主课程）；\n"
     "        某题在首份里确实找不到依据，再补读另一份命中素材（不要无脑多份全读）。\n"
     "     b) 单份 >25KB → 先 read_workspace_file(path=…, max_chars=15000) 读头部概览、\n"
     "        按题干关键词定位相关段；确需某段全文再去掉 max_chars 单独重读那一处。\n"
     "     【例】考试名《博易云软件介绍测试》→ 关键词「博易云软件介绍」→ 命中\n"
     "     「博易云软件介绍培训_1_视频.md」/「博易云软件介绍培训_10期权_视频.md」→ 先读更贴近\n"
     "     考试名的那份（概览即可），某题涉及另一份内容再补读它。\n"
     "     【例】考试名《公司2026年反洗钱内控制度配套考试》→ 关键词「反洗钱」「内控制度」→ 命中\n"
     "     「公司2026年反洗钱内控制度专题培训_视频.md」。\n"
     "  -1.5)【知识库查不到 → 先采集课件（v1.0.230）】若 知识库/课件/ 模糊匹配仍无相关文档，而当前窗口\n"
     "     正停在配套课件页（PDF 阅读器/视频播放页，特征是 read_wecom_page 几乎读不到正文、\n"
     "     只有页码）——先调 learn_capture(name=<课程名>, mode=auto, pick=<窗口序号>) 采集：\n"
     "     逐页截图 OCR 存 产物库/知识库/课件/<课程名>.md → 再 read 它作答。\n"
     "     ⚠️ 窗口已经在【答题页】时不要采集（没有画布会失败），先答题；\n"
     "        采集应该在进入考试之前的「学习课件」阶段做（study-solver 已在 v1.0.288\n"
     "        视频/文档任务完成后自动 learn_capture），或用户切回课件窗口显式调 exam-prepare。\n"
     "  -1) 【入口检测·首步必做】read_wecom_page 后先判断当前是不是【答题页】：\n"
     "     答题页特征 = body 有题号格式'X /N'或'第X题' 且 有选项（A/B/C/D 或 正确/错误）。\n"
     "     ✗ 若不是答题页（停在'我的考试/考试详情'，body 含'考试详情/剩余考试次数/'再考一次'\n"
     "       /'开始考试'，但没有题目和选项）→ 必须先点「再考一次」（或「开始考试」/「开始答题」）\n"
     "       进入答题页才能作答。点完 read 确认已进入答题页（有题号+选项）再继续。\n"
     "       ⚠️ 不点这个按钮直接找选项 = 卡在详情页空转（实测：用户看到'一直在开始答题页面死循环'）。\n"
     "       连续点同一入口按钮最多 2 次，2 次后仍在详情页 → 报告异常停止，禁止继续点。\n"
     "       ⚠️ 进入前顺手从详情页记下两行数字（步骤8要用）：「及格分 X 分」和\n"
     "       「剩余考试次数 无限 次/剩 N 次」——无限次意味着可以试错提分。\n"
     "  0) 【先查历史记录】read_workspace_file 读 产物库/知识库/题库 下同考试的\n"
     "     「答题记录_<考试名>_*.md」——**只读文件名日期后缀最新的一份**（多份同题记录\n"
     "     逐份读会把上下文顶进大上下文空转区，旧记录的排除/锁定结论都已并入最新份）——\n"
     "     若有上次记录：\n"
     "     逐题对照，历史记录里【已有答案的同款题直接复用该答案】（不重新推理，\n"
     "     避免上次想错又错一遍）；没有记录或无同款题才正常推理。\n"
     "     ⚠️ 记录里若含【排除/锁定结论】（步骤8 的试一试留痕）则以其为准：\n"
     "     已排除的答案绝不复用，已锁定的答案直接照抄。\n"
     "  0.5) 【铁律·先规划后执行】读完台账后、进入答题页前，必须先用 save_workspace_file\n"
     "     在台账里追加【第N轮·本轮计划】小节：逐题列完整答案计划（题号+所选选项，\n"
     "     12 题一行不缺）+ 本轮改动题及理由（试错轮：标注哪几题换了新候选）；首答轮\n"
     "     标注【首答】。计划落盘后才许点「再考一次/开始考试」进答题页——执行期=纯\n"
     "     机械：照计划点选项→下一题，不再推理、不再读台账；中断恢复时直接读计划接着做。\n"
     "     【铁律·先确认题号】每次 read_wecom_page 后第一步提取当前题号\n"
     "     （body 里'X /N'或'第X题'格式），记在心里；作答前核对选项属于当前题，\n"
     "     读到上一题残留选项（题干与当前题号对不上）绝不能点——先重 read 一次。\n"
     "  1) wait_wecom_page_load → read_wecom_page 提取题干+选项（body含 answered/unanswered+【当前题已选】标记，v1.0.242）\n"
     "     → 一次能读到多道题时先全部读齐再逐题作答，减少翻页往返\n"
     "     ⚠️【只操作考试窗口】答题全程只操作 body 含「交卷/答题卡」的那个窗口；\n"
     "     不要切到其它窗口找课件或核对内容（实测切空白文档窗口找课件白耗多轮）\n"
     "  2) 推理答案：每道题先在心里推理为什么选这个（题干关键词→知识点→答案），再点选；\n"
     "     注意题干是要求选「正确的」还是「错误的」（防止反向选择）\n"
     "     判断题策略：绝对化/越权表述（可以/无需/自行决定）→ 选「错误」更安全\n"
     "  3) click_wecom_element 点选项文字（单选1个/多选多个/判断选A正确或B错误）\n"
     "     【选中状态可读·v1.0.242】read_wecom_page 返回【当前题已选：X Y】（及 exam_options 字段），\n"
     "     用它确认点击是否生效/当前选了啥——这是查看选中状态的【唯一合法途径】；\n"
     "     ⚠️ 严禁盲点选项「验证选中」（点一下会把答案切换掉，且永远查不完——实测：12 题\n"
     "        已答完后回头逐题验证，来回 40+ 轮始终没到交卷，任务卡死）。\n"
     "     【改选方法】单选：直接点新选项（点击即切换）；多选：先点一次【当前已选】的选项\n"
     "     取消它，再点新选项，然后 read 确认【当前题已选】组合与预期一致。\n"
     "     【铁律·严禁点答题卡题号】纯数字（1-10/1-20）是答题卡跳题按钮，点了会直接\n"
     "     跳到该题——已实测模型点'10'从第1题跳第10题。只准用选项文字或'下一题'/'上一题'。\n"
     "     选项异步加载异常→重新 read 一次，仍异常跳过\n"
     "     同一页面/元素最多读 2 次，第 2 次相同就换方式或跳过\n"
     "  4) 答完当前题 → click「下一题」→ 【铁律·确认题号+1】重新 read，确认新题号=旧题号+1；\n"
     "     若题号跳变（1→3）说明点错，立即点「上一题」回退修正后再前进。逐题做到最后一题\n"
     "  5) 【落盘记录·整卷一次性】整卷答完、交卷前用 save_workspace_file 一次性把\n"
     "     「题号/题型/题干摘要/选项/所选答案」保存到 产物库/知识库/题库/答题记录_<考试名>_<日期>.md\n"
     "     （有同名文件就覆盖为最新累计，保证中断不丢）——下次重考/中断恢复都能参考。\n"
     "     每轮记录必须含【每题所选选项的全量清单】（不只是改动题）——步骤8的\n"
     "     「重选未选答案」台账靠它。⚠️ 不要每题答完就存一次（实测 12 题逐题落盘要多烧\n"
     "     12+ 个 LLM 轮次，单轮考试耗时翻倍）；步骤 0.5 的计划已先落盘，中途死机也能\n"
     "     按计划续答。\n"
     "  6) 【铁律·交卷触发】read 确认 unanswered=0 的那一刻：立即点「交卷」——\n"
     "     严禁回去「逐题复查」（选中状态只许读【当前题已选】标记，盲点验证永远查不完，\n"
     "     还可能把已答对的选项点翻）；页面若没有返回【当前题已选】标记，以「已答」为准直接交卷。\n"
     "     交卷前若存在【存疑】题（知识库查不到、推理没把握的）：\n"
     "     【次数有限时·人工兜底最准】先不要交卷：把存疑题的题干+选项完整列出来问用户，\n"
     "     用户给答案后照填（用户是业务人员，比模型准）。用户没回复就按推理填，\n"
     "     并在 finish 里标注哪些题存疑。\n"
     "     【无限次时·快速首答不逐题问大模型】剩余考试次数=无限 → 首答阶段目标是【快速打完看分】：\n"
     "     存疑题不问用户、也【不要逐题 ask_judge 问大模型】（12 题逐题问要烧好几分钟，效率太低）——\n"
     "     直接按最优推理填（判断题绝对化表述选「错误」），每题落盘记录，快速交卷读分。\n"
     "     错了不亏：后面靠步骤8的试一试排除逐轮逼近，历史记录就是你的账本。\n"
     "     【例外·有限次数】只剩 1-N 次（一次机会）→ 只对【存疑题】（素材原文/题干语义都定不了、\n"
     "     推理没把握的）先 ask_judge 拿结论再填；【高置信题】（素材原文或题干语义能确定的）\n"
     "     直接按最优推理填、不 ask_judge（实测每问一次 ask_judge 要 45-150s，12 题逐题问要烧\n"
     "     20-30 分钟，且高置信题本不该问——浪费判断资源还可能被大模型泛化带偏）。先把整卷\n"
     "     按「高置信/存疑」分好类，存疑题集中问、拿结论照填，其余题快速填完交卷。\n"
     "     确认后点交卷\n"
     "     【交卷防重复·每点一次必 read 确认】：点「交卷」→ read；\n"
     "       若弹出确认框（body 含'确认交卷'）→ 点一次「确认交卷」→ read 确认；\n"
     "       若仍在答题页（有题号+选项）→ 说明没交成功，最多再试 1 次；\n"
     "       ⚠️ 绝不在未 read 确认的情况下连续点「交卷」（实测点了 3 次：交卷→确认交卷→交卷）。\n"
     "  7) 【读成绩】交卷后 read_wecom_page 读页面上的分数（如'60.0分'/'考试通过'），\n"
     "     把得分追加写入答题记录文件末尾；finish 必须汇报：得分/通过与否/存疑清单。\n"
     "     严禁未答:0 前交卷。\n"
     "  8) 【无限次·试一试排除提分（v1.0.242 重选未选答案）】开始答题时详情页显示\n"
     "     「剩余考试次数 无限 次」且本轮分数 < 及格分（未通过）→ 不 finish，\n"
     "     直接点「再考一次」进入试一试模式，逐轮逼近及格分：\n"
     "     a) 先从答题记录读出上一轮【每题答案+总分】（中断恢复也靠它）。\n"
     "     b) 点完「再考一次」先 read：若 answered=0（全新卷）→ 按记录全量抄答\n"
     "        （要试的存疑题用新候选）；若 answered>0（平台恢复了上一轮选择）→ 只需\n"
     "        改要试的存疑题，其余不动（当前选中以【当前题已选】标记为准）。\n"
     "     c) 【单变量试错·铁律】每轮只改 1-2 道存疑题的答案，其余题严格照抄上一轮\n"
     "        （含高置信题与已锁定的题）——只改少数题，分数变化才能归因：\n"
     "        【重选未选答案】每道改动题，查答题记录台账，选一个【上一轮没选过】的选项\n"
     "        （单选：上次没选过的任一选项；多选：对上一轮加减一个选项，新组合不得与\n"
     "        任何历史组合相同）——试过的答案绝不重复试。\n"
     "        分数↑ → 上一轮那题的答案是错的【排除】，锁定本轮新答案；\n"
     "        分数↓ → 本轮改的选项是错的【排除】，下轮回退上一轮答案换别的候选；\n"
     "        分数不变 → 两个选项同对同错，标记「待换」，下轮换下一个候选。\n"
     "     d) 已锁定的题绝不再动，只试未锁定的存疑题。\n"
     "     e) 【每轮必交卷】每轮答完（unanswered=0）立即交卷读分——\n"
     "        「答了不交」是禁止状态，无论什么原因（实测卡在这里始终没交卷）。\n"
     "     f) 每轮交卷后把「第N轮｜改动题｜各题答案｜总分｜排除/锁定结论」追加写入\n"
     "        同一份答题记录文件——试一试结果全部留痕，下次续试直接接着排除。\n"
     "     g) 【无限次·全程单模型】无限次考试【不调用 ask_judge】——试错零成本，靠历史排除\n"
     "        逐轮逼近就够；遇到连续 2 轮排不出的硬骨头就换下一个候选继续试。\n"
     "        （双模型大模型只留给更强逻辑判断的场景，如有限次数考试/复杂决策，不在无限试错中消耗）。\n"
     "     h) 达到及格分 → 立即停；未达标【严禁提前 finish】，必须继续「再考一次→重选未选答案」\n"
     "        循环——试错上限是【本次会话内】连续 10 轮作答（台账里历史轮次不计入该上限，\n"
     "        只继承其中的排除/锁定结论；2026-09-01 实测：模型误把台账累计轮次当上限，\n"
     "        58 分未达标就 finish 收尾）。防空转：若连续 2 轮分数无提升且无新锁定/排除，\n"
     "        提前 finish 汇报最优分数+已锁定清单+存疑题，避免空转烧轮次。本次会话 10 轮\n"
     "        仍未达标 → 才 finish 汇报最优分数 + 已锁定答案清单 + 仍存疑的题 + 下轮建议改动。\n"
     "     i) 【RESULT 标记·自动续跑依赖】finish 的 final_answer 最后一行必须是：\n"
     "        通过=「RESULT: PASS」，未通过=「RESULT: FAIL」（顶格、独立一行）。\n"
     "        auto_loop 模式靠它判定是否自动开下一会话续跑；漏写会被视为已结束不再续跑。\n"
     "     剩余次数有限（剩 N 次）→ 一次运行只考一次：本轮考完读分成后直接 finish 汇报\n"
     "     成绩与存疑清单，【绝不自动重考、也不再追问用户要不要再考】——是否再考由用户\n"
     "     自己发起新任务决定（考试机会宝贵，避免 Agent 擅自消耗）。\n"
     "发现题型特有规律/踩坑（如某个选项点击方式特殊、某题必填才能下一题）时，调用 save_experience(module=\"exam-solver\", problem=…, solution=…) 沉淀到经验库，下次同类任务直接复用。\n"
     "【每答完一题就记一次】append_progress(key=\"答题记录\", content=\"第N题：题干摘要 → 所选答案 + 依据/存疑\")——"
     "答完一题记一题，**不要攒到最后**：系统会在每次记录后自动精简上下文（历史轮次的页面正文不再重传），"
     "这样一场考试从头答到尾也不会被资源预算掐断，中途断流/断电也已答的题不会丢。",
     "tools": ["wait_wecom_page_load", "read_wecom_page", "list_interactive_elements", "click_wecom_element",
               "type_wecom_text", "select_wecom_option", "scroll_wecom_window",
               "read_workspace_file", "save_workspace_file", "list_workspace_files", "learn_capture",
               "save_experience", "append_progress", "new_progress_round", "web_search"],
     "default": True},
    {"id": "data-downloader", "name": "数据下载", "judge": "none", "icon": "⬇️",
     "desc": "从网页/系统导出数据并保存到产物库（图片/文档/表格/压缩包等）。",
     "prompt": "list_wecom_links 找直链 → download_file 下载；需登录态/无直链的 → download_wecom_attachment 按附件文字定位 fetch 下载 → 报告本地路径。",
     "tools": ["list_wecom_links", "download_file", "download_wecom_attachment", "read_workspace_file"],
     "default": True},
    {"id": "data-organizer", "name": "数据整理", "judge": "script", "icon": "🗂",
     "desc": "读取已下载/已生成数据→去重/汇总/分类→生成整理后的报告(CSV/Markdown)并落盘。",
     "prompt": "list_workspace_files → read_workspace_file 读原始数据 → run_rule_check(module=\"data-organizer\", data={\"headers\":列名, \"rows\":数据行, \"group_col\":分类列(可选)}) 脚本去重汇总 → save_file 生成报告 → finish 汇报路径。脚本 __error__ 才退回模型处理。",
     "tools": ["list_workspace_files", "read_workspace_file", "save_file", "run_rule_check"],
     "default": True},
    {"id": "process-analyzer", "name": "流程分析", "judge": "model", "icon": "🔀",
     "desc": "快速分析当前流程（OA 审批/业务办理/制度流程）的合理性：读描述→交叉验证→结论与建议。强制 5 轮内完成。",
     "prompt": "【硬约束：≤5 次工具调用】\n"
     "  1) read_wecom_page 读流程页正文\n"
     "  2-4) list_wecom_links → 下载附件 → read_workspace_file 读取（优先文本类，至少 1 个二进制）\n"
     "  5) save_file 生成 Markdown 报告（含流程概述/附件核对/合理性结论/改进建议）\n"
     "  6) finish 汇报核心发现。信息够就提前 finish。",
     "tools": ["read_wecom_page", "list_wecom_links", "download_file",
               "download_wecom_attachment", "read_workspace_file", "save_file"],
     "default": True},
    {"id": "process-summary", "name": "流程总结", "judge": "model", "icon": "🧭",
     "desc": "把 OA/网页中的流程说明总结成结构化流程文档（步骤/角色/材料/审批规则/常见问题），落盘产物库。",
     "prompt": "流程总结流程：\n"
     "  1) read_wecom_page 读流程页全文；内容长时 scroll_wecom_window 翻页直到读完整篇，别只读第一屏\n"
     "  2) 结构化提取：适用场景 / 参与角色 / 前置条件 / 步骤清单（编号+动作+责任人+产出）/ 所需材料与单据 / "
     "审批规则（金额阈值、分支条件、时效）/ 注意事项与常见问题\n"
     "  3) 生成 Markdown 流程文档（文件名 流程总结_主题_YYYYMMDD.md）：步骤用「1. 2. 3.」有序列表，"
     "审批规则与材料用表格，关键提醒以 ⚠️ 开头\n"
     "  4) save_file 落盘 → finish 汇报步骤总数与文档路径。\n"
     "发现流程特有约定（如某步必须前置某附件、某审批人不可跳过）时，调用 save_experience(module=\"process-summary\", problem=…, solution=…) 沉淀，下次同类流程直接复用（同名流程可先 list_workspace_files 查 流程总结_主题*.md 快照参考再读页面）。",
     "tools": ["read_wecom_page", "list_interactive_elements", "scroll_wecom_window", "save_file",
               "list_workspace_files", "save_experience"],
     "default": True},
    {"id": "finance-audit", "name": "财务单据核查", "judge": "script", "icon": "💰",
     "desc": "财务系统单据批量核查：筛选「已付款」单据，逐张进入详情读取填写逻辑与附件，下载附件并整理成结构化清单/报告。",
     "prompt": "财务单据批量核查流程：\n"
     "  1) read_wecom_page 读列表页正文（表格行含单据编号 BXxxxx、报销分类、金额、单据状态）\n"
     "  1.5) 一键流水线（v1.0.210，推荐优先）：直接调 run_audit_flow(flow=\"monthly-report\")（v1.0.523 起后台执行：调用后立即返回「已开始」，向用户说明完成后托盘会提醒、报告在产物库，不要等待或重复调用）\n"
     "     ——脚本自动完成 找列表/解析链接/逐个点开/读字段/下载附件/统计/判定/记录/关页，\n"
     "     零 LLM 消耗毫秒级跑完；返回 summary+results+issues，模型只做汇总和疑问项处理\n"
     "  2) 脚本筛单：run_rule_check(module=\"finance-audit\", data={\"body\": 列表正文})\n"
     "     结构化行则传 data={\"rows\": [{\"单据编号\":..., \"单据状态\":...}]}\n"
     "     结果含 paid_docs/other_docs/stats\n"
     "  3) 逐张进详情：click 单据编号 → wait_wecom_page_load → read_wecom_page 提取关键信息\n"
     "     → list_wecom_links 找附件 → download_file/download_wecom_attachment 下载 → 返回列表\n"
     "  4) 每处理完【一张】立即 append_progress(key=\"单据核查\", content=该单结论/关键字段) 记录，\n"
     "     系统会自动精简上下文——下一张单据从干净上下文开始，不被上一单详情污染（v1.0.198）\n"
     "  5) 记录后 close_wecom_page 关闭当前详情页 → list_wecom_windows 找列表页 → pick 切回 →\n"
     "     点下一张（防标签堆积/反复审同一项，v1.0.203）\n"
     "  6) 全部处理完：read_workspace_file 读进度文件 → save_file 生成 Markdown 报告（汇总表格\n"
     "     + 每单明细）→ finish 汇报统计。\n"
     "信息技术运维月报审核（v1.0.210 一键流水线，最高优先）：【直接调用 run_audit_flow(flow=\"monthly-report\")】\n"
     "——脚本自动完成 找待办列表/解析月报链接/逐个点开详情/读字段/静默取附件/规则判定/\n"
     "记录/关闭详情页，零 LLM 消耗秒级跑完整个列表，返回 summary+results+issues。\n"
     "【禁止手动一步步点击】——不要再自己 read 列表、click 链接、read_fields，那些脚本全做了。\n"
     "拿到 results 后：基于 summary 直接输出总结；对 issues 中少量疑问项（字段未命中/Excel 列名\n"
     "无法识别/附件异常）用 read_wecom_fields 的 all_labels 找近义字段或标记原因后汇总。\n"
     "判定口径（v1.0.604 用户口径）：只看【运维天数一致】与【设备清单是否上传】两项，\n"
     "在用计算机数量**不再比对**（表单口径与清单口径常年不一致，误判会让机构白整改），\n"
     "报告里的「在用台数」列仅供参考。\n"
     "兜底（仅当 run_audit_flow 报错时才手动）：进详情后 read_wecom_fields 读字段（机构名称/月报所示月份/\n"
     "本月应运维天数/实际运维天数/附件），附件 Excel 用 download_wecom_attachment（**只静默直传，不点下载按钮**，\n"
     "企微点击必弹另存为框）+ analyze_monthly_attachment 拿在用台数做参考，然后\n"
     "run_rule_check(module=\"monthly-report\", data={expect_days, actual_days, list_found, list_ok})\n"
     "脚本判定（list_found=正文有无 xlsx 清单，list_ok=是否静默取到并解析成功），\n"
     "判定结果 append_progress 记录——计算不耗 LLM 且不会算错。\n"
     "多页面流程（v1.0.192）：列表页的链接会打开新页面，click 后系统自动跟随到新页面\n"
     "（工具返回含 new_page.window_index）。处理完当前详情页后：用 list_wecom_windows\n"
     "找到列表页（标题/URL 含列表特征），用 pick=<列表页序号> 的 read/click 切回，\n"
     "继续点击下一张单据。不要在同一次 read 里反复读同一个旧页面。",
     "tools": ["wait_wecom_page_load", "read_wecom_page", "list_interactive_elements", "click_wecom_element",
               "scroll_wecom_window", "list_wecom_links", "download_file",
               "download_wecom_attachment", "list_workspace_files", "read_workspace_file",
               "save_file", "append_progress", "new_progress_round", "close_wecom_page", "analyze_monthly_attachment", "run_rule_check", "run_audit_flow"],
     "default": True},
    {"id": "finance-check", "name": "财务审核", "judge": "script", "icon": "🟢",
     "desc": "逐张审核财务单据，读取字段判定红绿，生成报告。",
     "prompt": "规则：专管部门=信息技术部 | 业务类型=费用类付款（电子） | 支出类别=专管费用 | 预算执行单(PMP项目)非空 | 有附件\n\n【第一步必须调用脚本流水线】run_audit_flow(flow=\"finance\")：滚动加载列表→逐张静默开详情页→read_fields→脚本判定→关页全部由脚本完成，**零 LLM 消耗、全程只读**（这条流水线没有任何审批点击路径，auto_approve 传了也会被拒绝）。拿到结果只做两件事：①按 summary 与 report_path 的真实数字汇报，禁止编造；②只处理 pending/疑问项（all_fields_missed 时看 all_labels 找近义字段名）。\n\n下面的手动步骤**只在流水线明确返回失败**（找不到列表页/CDP 不可达）时才走——手动逐张点击会因列表索引错位整批失败并烧大量 token：\n1) read_wecom_page 读当前页确认——若标题/正文含\"待办\"/\"列表\"说明在列表页\n2) 列表页：find 第一个单据编号（BXxxxx 或 JKxxxx 格式），click_wecom_element 点它进详情；若已不在列表页→click 返回列表按钮或点浏览器后退\n3) wait_wecom_page_load → read_wecom_page 读详情页全文，确认有\"借款及预付单\"/\"基本信息\"等关键词\n4) read_wecom_fields(fields=[\"专管部门\",\"明细项目\",\"业务类型\",\"支出类别\",\"预算执行单\",\"PMP项目\",\"附件\"])\n5) run_rule_check(module=\"finance-check\", data=步骤4返回的完整 data)\n6) 记录：单据编号+各字段值+green/red+附件名称——**每审完一张立即** append_progress(key=\"单据核查\", content=\"单据编号/判定/不符原因/附件名\") 记一项（这是系统「记一项精简一次上下文」的扳机：审三五十张时不记就会被资源预算掐断在半途，且断了也能按台账续跑）\n7) read_wecom_page 确认返回列表页→回到步骤2点下一张\n8) 列表无更多单据时 save_file 生成报告 + finish 汇报统计\n\n⚠️ 重要：每点一张必须先 read_wecom_page 确认页面状态再操作，不要盲点。如果连续 3 张 read_wecom_fields 全部 all_fields_missed，先 read_wecom_page 截图当前页面告诉我。",
     "tools": ["read_wecom_page", "read_wecom_fields", "run_rule_check", "run_audit_flow",
               "click_wecom_element", "list_interactive_elements", "scroll_wecom_window",
               "save_file", "append_progress", "new_progress_round", "close_wecom_page"],
     "default": True},
    {"id": "reimburse-filler", "name": "报销单填写", "judge": "script", "icon": "🧾",
     "desc": "自动填写财务系统报销付款单：按默认值+用户补充填全表（基本信息/费用明细/收款信息/发票附件），并校验必填项。",
     "prompt": "报销单填写流程：\n"
     "  1) read_wecom_page 读表单 → click 字段名激活输入框\n"
     "  2) run_rule_check(module=\"reimburse-filler\", data={\"fields\":[字段名...]}) 获取默认值映射\n"
     "     用户另有说明则覆盖默认值\n"
     "     关键默认：报销分类=报销 | 费用承担部门=信息技术部 | 收款人/银行账号/开户行等个人信息以脚本返回为准"
     "（脚本从设置读取，返回 None 的字段必须问用户，不得编造）\n"
     "  3) 输入型用 type_wecom_text（target=字段名）/ 下拉用 select_wecom_option / MUI 下拉点候选\n"
     "     日期字段优先用 type_wecom_text 直接输入 YYYY-MM-DD；若回读 verify=false 说明组件拒绝，"
     "改点击日期输入框后输入再选日期确认\n"
     "     每填完一组 read_wecom_page 确认已写入\n"
     "  4) 填完 read_wecom_page 校验必填项（含 required 标记字段）→ 补填\n"
     "  5) 金额一致性：有费用明细行和总金额时，调用 run_rule_check(module=\"reimburse-filler\", "
     "data={\"fields\":字段名, \"items\":[{\"amount\":金额}...], \"total\":总金额})——脚本自动算明细合计 vs 总金额，"
     "不一致会报 red，需修正后再提交\n"
     "  6) 附件用 upload_wecom_file（产物库里找文件）\n"
     "  7) finish 汇报已填字段/金额合计/未填项。不自动点提交。",
     "tools": ["read_wecom_page", "list_interactive_elements", "click_wecom_element",
               "type_wecom_text", "select_wecom_option", "scroll_wecom_window",
               "upload_wecom_file", "list_workspace_files", "run_rule_check"],
     "default": True},
    {"id": "web-search", "name": "联网搜索", "judge": "none", "icon": "🌐",
     "desc": "通过 DuckDuckGo 联网搜索 + 网页正文抓取，让模型获取公网实时信息（开源免费、无需 API Key）。",
     "prompt": "你已接入联网搜索（web_search）与网页抓取（fetch_webpage）。需要实时/公网信息时，先用 web_search 搜关键词，再对重点结果 fetch_webpage 抓取正文阅读；搜索无结果可换关键词重试。",
     "tools": ["web_search", "fetch_webpage"],
     "default": True},
    {"id": "meeting-minutes", "name": "会议纪要", "judge": "model", "icon": "📋",
     "desc": "从企微会话/会议窗口提取议题、决议、待办与负责人，生成结构化会议纪要并落盘。",
     "prompt": "read_wecom_page 读会议窗口 → 提取主题/时间/参会人/议题/决议/待办(负责人/截止) → save_file 生成 Markdown 纪要(文件名 会议纪要_主题_YYYYMMDD.md) → finish 汇报要点与路径。",
     "tools": ["read_wecom_page", "read_workspace_file", "save_file"],
     "default": True},
    {"id": "doc-review", "name": "文档审阅", "judge": "model", "icon": "📑",
     "desc": "审阅合同/制度/方案等文档：提取关键条款、识别风险点与前后矛盾，输出审阅意见。",
     "prompt": "list_workspace_files / read_workspace_file 读文档（或 read_wecom_page 读在线文档）→ 审阅：类型概述/关键条款/风险点(标严重/一般/建议)/矛盾/可执行性/修改建议 → save_file 生成审阅意见 → finish 汇报核心风险。",
     "tools": ["list_workspace_files", "read_workspace_file", "read_wecom_page", "save_file"],
     "default": True},
    {"id": "periodic-report", "name": "周报月报", "judge": "model", "icon": "📊",
     "desc": "根据产物库素材 / 企微会话记录，自动汇总生成周报或月报（成果、数据、问题、下期计划）。",
     "prompt": "list_workspace_files → read_workspace_file 读素材 → 汇总：本期完成(成果/数据)/进行中/问题风险/下期计划/所需支持 → save_file 生成 Markdown(文件名 周报/月报_YYYYMMDD.md) → finish 汇报核心内容与路径。",
     "tools": ["list_workspace_files", "read_workspace_file", "read_wecom_page", "save_file"],
     "default": True},
    {"id": "data-analysis", "name": "数据报表分析", "judge": "script", "icon": "📈",
     "desc": "读取 Excel/CSV 等数据文件，做统计汇总（总额/均值/分类汇总/异常），输出分析结论与报表。",
     "prompt": "list_workspace_files → read_workspace_file 读数据表 → run_rule_check(module=\"data-analysis\", data={\"headers\":列名, \"rows\":数据行}) 脚本统计(总额/均值/极值/缺失/零/负/离群/分布/重复行) → save_file 生成「分析结论+数据明细表」 → finish 汇报结论。",
     "tools": ["list_workspace_files", "read_workspace_file", "save_file", "run_rule_check"],
     "default": True},
    {"id": "writing-draft", "name": "公文通知起草", "judge": "model", "icon": "✍️",
     "desc": "按用户要求起草通知、公告、请示、说明等公文/办公文书，可落盘或写入在线文档。",
     "prompt": "按用户主题与要点起草公文（通知/请示/说明）→ 结构清晰用语正式，金额/日期/部门严格一致，缺失信息用「____」占位 → save_file 落盘(文件名 文书类型_主题_YYYYMMDD.md)或 type_wecom_document 写入在线文档 → finish 汇报。",
     "tools": ["save_file", "type_wecom_document", "read_wecom_page"],
     "default": True},
    {"id": "info-research", "name": "资料调研", "judge": "model", "icon": "🔍",
     "desc": "针对主题做联网调研：多关键词搜索 + 抓取重点网页，汇总成结构化调研报告。",
     "prompt": "2-3 组关键词 web_search → fetch_webpage 抓取 2-4 个最有价值结果 → 交叉核对来源 → save_file 生成调研报告(背景/结论/分主题要点/数据出处/参考链接/信息缺口) → finish 汇报。",
     "tools": ["web_search", "fetch_webpage", "save_file"],
     "default": True},
    {"id": "oa-approval", "name": "待办审批处理", "judge": "model", "icon": "✅",
     "desc": "读取企微 OA 待办/审批列表，逐项查看内容并整理成待办清单/处理建议（不自动提交，需用户确认）。",
     "prompt": "read_wecom_page 读待办列表 → 逐项 click 进详情 read_wecom_page 提取内容 → 整理：事项标题/发起人/时间/内容摘要/建议(同意/驳回/补充/需人工判断)+理由(依据原文哪句哪个字段) → save_file 生成待办处理清单 → finish 汇报总数与紧急事项。除非用户明确授权，不直接点击提交/同意/驳回。\n"
     "批量处理时维护进度：每处理完【一项】立即 append_progress(key=\"待办处理\", content=\"事项标题/结论/建议\") 记录，"
     "并在对话里说明「已处理 X/N 项、剩余 N-X 项」，确认返回列表后再点下一项，"
     "避免重复处理同一待办；列表读不到新项或点击无反应时，重新 read_wecom_page 确认页面状态，不盲点。\n"
     "发现待办系统的特殊规则（如某类审批默认同意、特定金额需特殊附件）时，调用 save_experience(module=\"oa-approval\", problem=…, solution=…) 沉淀，下次同类待办自动遵循。\n"
     "【一键汇报】若 cfg['wecom']['webhook'] 已配置，finish 前用 send_wecom_webhook 推送当日审批汇总到指定群（内容含：今日处理总数/紧急项摘要/未决项/总结清单路径），用户无需手动复制粘贴即可通知团队。",
     # v1.0.619（审查②）：这里原先写 send_wecom_message（那是操作企微窗口给联系人打字发消息
     # 的另一个工具），而本技能 tools 名单里两个消息工具都没给——模型照提示去做会没有可用工具。
     "tools": ["read_wecom_page", "list_interactive_elements", "click_wecom_element", "scroll_wecom_window",
               "save_file", "save_experience", "send_wecom_webhook", "append_progress", "new_progress_round",
               "list_workspace_files", "read_workspace_file"],
     "default": True},
    {"id": "course-capture", "name": "课件采集", "judge": "script", "icon": "📚",
     "desc": "在线课程（学习平台）课件采集：canvas 渲染的 PDF / 视频画面 → 截图 OCR → 存 产物库/知识库/课件/<课程名>.md，供考试作答时检索（把 60 分提到 90+）。",
     "prompt": "用户要学习/采集在线课件时调用 learn_capture(name=<课程名>, mode=auto, pick=<窗口序号>)：\n"
     "  · 前提：窗口要停在课件页（PDF 阅读器或视频播放页）。停在课程列表/任务页时，\n"
     "    learn_capture 会自动点课程卡片进入课件（绝不点「已完成」）；找不到画布就报告并请用户切到课件页。\n"
     "  · PDF（mode=pdf/auto）：自动定位课件所在的 iframe（主页面点不到里面的翻页控件），\n"
     "    逐页「截图→OCR→翻页」，页码不再变化即停；结果存 产物库/知识库/课件/<课程名>.md（按页分节，带页号）。\n"
     "  · 视频（mode=video）：需视频处于播放状态，按间隔抽帧 OCR 画面里的 PPT/字幕文字。\n"
     "  · 采完用 read_workspace_file 抽查前几页：内容为空或只采到 1 页 → 换 mode 重试一次，\n"
     "    仍失败就把「翻页控件文案/页面结构」报给用户（补进翻页关键词即可）。\n"
     "  · finish 汇报：保存路径 + 采集页数 + 字数。之后考试由 exam-solver 检索这份文档作答（以原文为准）。\n"
     "遇到某平台翻页控件文案特殊导致采不满页时，用 save_experience(module=\"course-capture\", problem=…, solution=…) 沉淀。",
     "tools": ["learn_capture", "read_workspace_file", "list_workspace_files",
               "read_wecom_page", "click_wecom_element", "list_wecom_windows", "save_experience"],
     "default": True},
    {"id": "exam-prepare", "name": "考前采集", "judge": "script", "icon": "📥",
     "desc": "考试前的素材采集准备：自动查 知识库/课件/ → 模糊匹配同名课件 → 缺则跳到课件窗口 learn_capture → 落 知识库/课件/，最后报告采集结果。供用户显式触发（也可由 exam-solver 自动调用）。",
     "prompt": "【v1.0.288 新增】考试题绝大多数直接出自配套课件/制度原文；缺素材是考试盲猜 60 分、有素材 90+ 的核心差距。适用场景：用户说『为 <考试名> 做考前准备』『采一下 <课程名> 的课件』『考试前先把素材抓一下』——也用于 opPane 触发「开始答题」前调用一次（agent 会自动先调 exam-prepare 再调 exam-solver）。执行流程：1) 从用户输入提取【考试名/课程名】（去后缀：测试/培训/课程/讲义/视频/题库）。2) list_workspace_files 看 产物库/知识库/课件/ 目录，做【模糊匹配】——文件名 substring 包含关键词即命中：命中 ≥1 个 → read_workspace_file 把命中的所有 .md 都读出来，列个总览（每份字数+采集时间+标题），finish 汇报「已就绪：N 份素材，共 M KB，无需采集」，不重复 OCR；命中 0 个 → 进入第 3 步采集。3) 跳到课件窗口：list_wecom_windows 找 PDF/视频窗口（标题含课程名），把 pick 记下。若当前在答题页（body 只有题号+选项，没课件画布）→ history.back() 回课程详情 → 重新点「视频」或「文档」任务卡 → 切到新打开的 courseDetail 窗口。4) 调 learn_capture(name=<课程名>, mode=auto, pick=<pick>) 采集：PDF 逐页截图 OCR，视频按 10s 间隔抽帧 OCR；自动落 产物库/知识库/课件/<课程名>.md 或 <课程名>_视频.md。采集超时/无可采内容 → finish 报告「采集失败：<原因>，请人工确认」+ 提示用户手动上传讲义到 知识库/课件/。5) 采完后 read_workspace_file 抽查首屏：内容为空或仅 1 页 → 换 mode 重试一次；仍失败就 finish 报告「素材不足：仅 X 页/帧，建议人工上传讲义」。6) finish 汇报：保存路径 + 命中已有/新采页数 + 字数 + 后续建议（开考/手工补料）。遇到某平台翻页控件特殊导致采不满页时，save_experience(module=\"exam-prepare\", problem=…, solution=…) 沉淀。【与 exam-solver 协同】exam-solver 步骤 -2 自己就会做模糊匹配查 知识库/课件/；步骤 -1.5 若仍未命中会调 exam-prepare 触发采集。无需用户重复指令。",
     "tools": ["learn_capture", "read_workspace_file", "list_workspace_files",
               "list_wecom_windows", "read_wecom_page", "click_wecom_element",
               "wecom_run_js", "save_experience", "finish"],
     "default": True},
    {"id": "study-solver", "name": "学习中心", "judge": "model", "icon": "🎓",
     "desc": "学习中心无人值守学习：学习地图 → 逐门完成未学课程（视频播完/文档浏览/考试答题）→ 领证书 → 回地图下一门，直到全部完成。",
     "prompt": "学习中心自动化（v1.0.260 实机探明，页面结构见下）：\n"
     "【页面关系·实机确认 2026-09-02】\n"
     "  学习地图 studyMapList（URL 含 #/home/studyMapList，tab：未学习/已学习）\n"
     "   └─ 点课程 LI（.MuiList-root>li，JS click 有效）→ 课程详情 studyDetail/{courseId}/0/0\n"
     "       （结构：第一关 0/1 + 任务列表；任务 LI 是 .MuiListItem-root，含「必修」文本）\n"
     "       └─ 真实鼠标点击必修任务 LI（中心坐标，.click() 无效）→ 开【新窗口】courseDetail/{N}\n"
     "           ├─ 单任务课程：课程学习页直接是视频(video.js) 或 文档\n"
     "           └─ 多任务课程：任务列表=视频(已学/待学)+文档(立即学习)+考试(通过/待考)\n"
     "               ├─ 视频任务 → 播放到结尾\n"
     "               ├─ 文档任务 → 点「立即学习」即自动变「已学」\n"
     "               └─ 考试任务 → 同窗口跳转考试详情页（剩余次数/及格分）→ 开始考试（走 exam-solver）\n"
     "   完成任一任务 → 【原窗口(学习地图/详情页)】弹「你已前往任务学习，是否已完成学习？已完成」\n"
     "       → 点「已完成」→ 学习证书 certificate/{certId} → history.back() 回学习地图（已学课程从列表消失）\n"
     "【执行流程】\n"
     " 1) list_wecom_windows 找到学习中心窗口（标题含「学习中心」，pick 记下）。\n"
     "    若当前不在学习地图：read_wecom_page 确认，必要时点「我的学习地图」/ 首页导航进入。\n"
     " 2) read_wecom_page 读列表：取「未学习」tab 的课程列表（每门 = 课程名+截止日期+进度%+状态）。\n"
     "    有「查看更多」按钮 → scroll_wecom_window 加载全量。全部课程进度 100% 或列表为空 → finish 收尾。\n"
     " 3) 循环处理每门课（记录已处理课程名，防止重复）：\n"
     "    a. JS click 该课程 LI（list_wecom_windows 拿到序号后 read_wecom_page 确认详情页出现「第一关」）。\n"
     "    b. read_wecom_page 读详情：任务列表。\n"
     "       若任务 LI 文本含「必修」→ 直接 click_wecom_element 点含「必修」文本的任务条目\n"
     "       （v1.0.263 起已内置真实鼠标点击降级：JS click 对 React/MUI 组件无效时自动\n"
     "       elementFromPoint 定位+dispatchMouseEvent 真实点击，返回 real_click:true 即生效；\n"
     "       v1.0.592 起若 JS click 已改变页面状态则【不再】补发真实点击（防止把「点一下\n"
     "       选中、再点取消」的切换型元素点回原位），此时返回 js_effective:true；两个标记\n"
     "       任有一个即算点着，最终一律以 read 结果为准；\n"
     "       点完 list_wecom_windows 找新窗口，新窗口标题=课程名-学习平台名）。\n"
     "       若详情页文本含「立即学习」（多任务课程的任务卡）→ 直接用 click_wecom_element 点「立即学习」。\n"
     "    c. 切到新打开的课程学习窗口（courseDetail）：read_wecom_page 判定任务类型：\n"
     "       - 含 <video> 且正文有「MM:SS/MM:SS」时间 → 视频任务：wecom_run_js 一步完成【注入静音三件套+播放+拖到结尾】\n"
     "         （v1.0.266 起必须注入；v1.0.623 起表达式与跑课引擎同源，取自\n"
     "          core.study_js.STUDY_MUTE_EXPR —— __muteLock 属性锁（播放器把 muted 重置回\n"
     "          false 也无效）+ 400ms 静音循环 + 裸媒体 iframe 中和，三段缺一就会有视频静音不了\n"
     "          或页面上根本查不到 <video> 可拖。表达式原文：\n"
     "         expression: " + STUDY_MUTE_EXPR + "），\n"
     "         等 5s 播放到结尾（wecom_run_js 再查 cur>=dur 即完成）。每打开一个学习窗口都要先跑一次该表达式（静音循环对整页持续生效，新视频/新窗口需重新注入）。\n"
     "         【v1.0.288 视频任务后自动 learn_capture·闭环关键】视频播完后立即调用 learn_capture(\n"
     "         name=<课程名>, mode=video, pick=<窗口序号>) 把字幕/PPT OCR 落 产物库/知识库/课件/<课程名>_视频.md\n"
     "         ——考试课出题范围就是这个素材，未来 exam-solver 步骤 -2 模糊匹配会命中它（典型 30~50KB）。\n"
     "         即使本课没考试，采了也方便后续引用；采集超时/无字幕 → 报错但不影响主流程，继续。\n"
     "       - 含「立即学习」按钮（任务卡，无 video）→ 文档任务：click_wecom_element 点「立即学习」，\n"
     "         等 3s 后 read_wecom_page 确认任务变「已学」/出现「已完成」弹窗。\n"
     "         【v1.0.288 文档任务后自动 learn_capture】点完「立即学习」立即调用 learn_capture(\n"
     "         name=<课程名>, mode=pdf, pick=<窗口序号>) 把 PDF 逐页 OCR 落 产物库/知识库/课件/<课程名>.md\n"
     "         ——和视频一样为考试积累素材；采集超时/无可采内容 → 不影响主流程。\n"
     "       - 含「考试/开始考试」→ 考试任务：点考试任务（含「考试」文本的任务条目）→ 同窗口变考试详情页\n"
     "         （read_wecom_page 读「及格分 X 分」「剩余考试次数 无限 次/剩 N 次」）。\n"
     "         【v1.0.290 只考无限次数·防白烧次数】先判剩余次数：\n"
     "         - 剩余次数=无限（文本含「无限」）→ 点「开始考试」→ 进入答题页执行 exam-solver 完整流程\n"
     "           （无限次数可单变量试错提分，见 exam-solver 技能）；\n"
     "         - 剩余次数=有限（文本含「剩 N 次」或具体数字，无「无限」）→ **不自动开考**，记台账\n"
     "           「考试剩余 N 次有限，跳过自动考试（防白烧次数，需人工对照课件）」，继续处理该课其它任务\n"
     "           （视频/文档照常完成）。有限次数的考试盲考失败会白白消耗次数，必须留给人工。\n"
     "         考完（通过或达上限）关/切回学习窗口继续。考试是否通过以详情页考试任务状态变「通过」为准。\n"
     "         【v1.0.288 考前自检·保险】考试任务开始前先 list_workspace_files 看 知识库/课件/ 有无该课\n"
     "         模糊匹配命中的 .md（按去后缀后的关键词 substring 搜，例如考试名《博易云软件介绍测试》\n"
     "         会命中「博易云软件介绍培训_*.md」）→ 若没有，先 history.back() 回课程详情，\n"
     "         重新打开该课的「视频/文档」任务窗口 → learn_capture 采集 → 再回考试任务。\n"
     "         这一步与上面「视频/文档后自动采集」互补：万一前面没采成/老素材缺失，考试任务自己补。\n"
     "         【0 题空考试·v1.0.262 实测】若「开始考试」后答题页正文只有答题卡/计时/交卷、\n"
     "         没有任何题目（read_wecom_page 读不到题干与选项，body 极短 <500 字，可能考试配置为 0 题）——\n"
     "         不要死磕读题！直接 wecom_run_js history.back() 退出考试回课程页/详情页，记台账\n"
     "         「考试异常：0 题/题目未渲染，跳过」，继续完成其它任务并点「已完成」——实测 0 题异常\n"
     "         不影响课程完成判定（拿到证书、课程从未学习列表消失）。\n"
     "    d. 学习完成信号：切回原窗口（list_wecom_windows 找父窗口）read_wecom_page，\n"
     "       若出现「你已前往任务学习，是否已完成学习？」→ click_wecom_element 点「已完成」。\n"
     "       完成后页面跳「学习证书」→ 学习完成。\n"
     "    e. history 返回：点顶部面包屑「我的学习地图」或 browser 后退（wecom_run_js: history.back()），\n"
     "       回到学习地图，read_wecom_page 确认该课程已从「未学习」列表消失（进度 100%），记录完成。\n"
     "    例外：多任务课程（视频+文档+考试）需全部任务完成才算学完——任务列表全部「已学/通过」后\n"
     "    关卡才通关。未完成的任务继续按 b-c 流程处理（考试任务需重进 courseDetail 再点考试）。\n"
     " 4) 全部课程完成后 finish 汇报：共完成 X 门、列表（课程名+完成方式），有考试的门数、最终分数。\n"
     "【防死循环铁律】① 同一课程重复出现/进度无变化 → 跳过记录原因，不反复点；② 同一按钮连续点 ≤2 次；\n"
     " ③ 每完成一门课 append_progress(key=\"学习进度\", content=\"课程名 + 完成方式 + 状态\") 记一项"
     "（系统随即自动精简上下文——几十门课跑到最后也不会被资源预算掐断），"
     "中断恢复先 read_workspace_file 读「答题记录_进度.md」/「学习进度_进度.md」接着做；"
     "用户说「开新一轮/这次不算之前记过的」→ 先 new_progress_round(key 用同一个) 把上一轮台账归档，"
     "再按空台账重跑（归档文件带「轮次归档_」前缀留在同目录，可查不可再当跳过依据）；"
     "④ 全程用「记录课程名+已处理」防重；⑤ 视频拖不动/文档不变已学 → 记异常跳过，\n"
     "    不卡死循环。",
     "tools": ["list_wecom_windows", "read_wecom_page", "click_wecom_element", "wecom_run_js",
               "list_interactive_elements", "scroll_wecom_window", "wait_wecom_page_load",
               "save_workspace_file", "list_workspace_files", "read_workspace_file", "learn_capture",
               "append_progress", "new_progress_round"],
     "default": True},
    {"id": "file-organizer", "name": "归档整理", "judge": "script", "icon": "🗄️",
     "desc": "扫描产物库文件，按类型/日期/主题归类，生成归档索引与整理建议。",
     "prompt": "list_workspace_files 扫描全部文件 → run_rule_check(module=\"file-organizer\", data={\"files\":清单}) 按类型/日期/重复性归类(<1ms) → save_file 生成「归档索引报告」(总体统计/类型分布/时间分布/重复过期清单/整理建议) → finish 汇报统计。只生成索引建议，不执行物理移动/删除。",
     "tools": ["list_workspace_files", "save_file", "run_rule_check"],
     "default": True},
    {"id": "pc-ops", "name": "电脑操控", "judge": "none", "icon": "🖥",
     "desc": "本机操作系统级操控（v1.0.297）+ deepseek-harness 接管通道（v1.0.298）：截屏、文件增删改查（删除进回收站）、执行命令、启动/结束程序、剪贴板、系统状态；搞不定的复杂电脑任务委托 dsh agent 自主完成——处理企微之外的电脑杂活。",
     "prompt": "【PC 操控·v1.0.297】用户交办企微之外的本机任务时用这组工具，按事选工具：\n"
     "  看电脑状态/谁占内存/磁盘满没满/查 IP → pc_system_info（一次拿全，别逐条跑命令）\n"
     "  文件类（列目录/读/写/复制/移动/删除）→ pc_files（删除只进回收站可还原）\n"
     "  跑命令/脚本/查环境 → pc_run（cmd 默认；长任务写结果到文件再 pc_files read）\n"
     "  开程序/开文件/开网址 → pc_apps(op=start/open)；看进程 → pc_apps(op=list)；关进程 → pc_apps(op=kill)\n"
     "  剪贴板读写 → pc_clipboard；截屏 → pc_screenshot（存 产物库/产出/截图/，模型不能直接看图）\n"
     "【接管通道·v1.0.298】pc_* 直连搞不定的**多步骤复杂任务**（跑测试修 bug/项目维护/批量处理/环境修复），\n"
     "或用户明确说「交给 harness agent / 让 dsh 做」时：\n"
     "  1) dsh_status 查就绪（Node / dsh 安装 / 模型凭据）\n"
     "  2) dsh 未装 → dsh_prepare（首次 1-10 分钟，告知用户等待）\n"
     "  3) pc_takeover(task=具体描述含路径和期望结果, cwd=相关目录) 委托执行；慢（几十秒到几分钟），\n"
     "     委托前告知用户预计耗时；完成后汇报最终答案 + 产物库/产出/dsh/ 过程记录路径\n"
     "  纪律：一步 pc_* 能搞定的简单操作绝不走接管通道（浪费 token）；pc_takeover 报未装/凭据错误\n"
     "     先按 dsh_status 的 hints 修复再重试，不盲目重发。\n"
     "【破坏性操作铁律】以下操作执行前必须先把【完整命令/目标路径/后果】说给用户并等明确确认，\n"
     "绝不擅自执行：pc_run 的删除/格式化/停服务/改注册表、pc_apps kill、pc_files write 覆盖已有文件、\n"
     "pc_clipboard set（会覆盖用户剪贴板）、pc_takeover 里含删除/格式化/覆盖类指令的任务。\n"
     "只读操作（list/read/get/截图/system_info/dsh_status）不需要确认。\n"
     "【产出纪律】命令输出/文件分析结果要落地时 save_file 存产物库，finish 汇报关键结论+产物路径。",
     "tools": ["pc_screenshot", "pc_files", "pc_run", "pc_apps", "pc_clipboard", "pc_system_info",
               "dsh_status", "dsh_prepare", "pc_takeover"],
     "default": True},
    # ── v1.0.308 新增实用技能（翻译 / 代码 / 简报）——工具全部来自既有 registry ──
    {"id": "translation", "name": "翻译润色", "judge": "model", "icon": "🌍",
     "desc": "中英互译与文本润色：术语统一、数字金额绝不改写，支持整篇文件翻译并落盘到产物库。",
     "prompt": "翻译/润色任务规范：\n"
     "1. 先判断语言方向与文体（公文/邮件/论文/营销/技术文档），忠实原意优先，译文要符合目标语言表达习惯；\n"
     "2. 专业术语全文统一（首次出现可括号附原文）；数字/日期/金额/单位/人名绝不改写或意译；\n"
     "3. 润色类任务：保留原意与结构、只改表达，输出「修改后全文」+「主要改动点列表」两段；\n"
     "4. 整篇文件翻译：read_workspace_file 读取（支持 docx/pdf/xlsx/pptx/txt/md 自动解析）→ 分段翻译\n"
     "   （长文分段推进，保住上下文）→ save_file 存「产物库/产出/<原文件名>_译文.md」；\n"
     "5. 表格内容保持 Markdown 表格结构输出，不要拍平成段落；\n"
     "6. 术语拿不准先 web_search 查证（官方中文名/行业通用译法），不要硬编；查不到就括号保留原文并标注。\n"
     "完成后 finish 给出：译文位置 + 篇幅统计 + 术语表（如有）。",
     "tools": ["read_workspace_file", "save_file", "web_search"],
     "default": True},
    {"id": "code-helper", "name": "代码助手", "judge": "none", "icon": "💻",
     "desc": "写脚本/查 bug/重构/解释代码：写完现场运行验证到能跑为止，交付「验证过的代码」而非「看起来对的代码」。",
     "prompt": "代码任务规范：\n"
     "1. 先确认语言/运行环境/输入输出与验收标准；小步交付：先给可运行的最小版本，再按需求加功能；\n"
     "2. 脚本用 save_file 存「产物库/产出/」后用 pc_run 现场运行验证，报错自己读 traceback 修到能跑——\n"
     "   交付前必须至少跑通一次；长输出重定向到文件再 read_workspace_file 读，不要硬扛超长 stdout；\n"
     "3. 查 bug 流程：复现 → 最小化 → 定位根因 → 修复 → 回归验证；解释类任务按「做了什么/为什么/怎么改」三段答；\n"
     "4. 重构类任务：改动前后行为必须一致——先跑通原版留存输出基线，改完对比；\n"
     "5. 【铁律】pc_run 里涉及删文件/改系统配置/装包/停服务等不可逆操作，先征得用户明确确认。\n"
     "完成后 finish：交付文件路径 + 运行验证结果（命令与关键输出）+ 使用说明。",
     "tools": ["save_file", "read_workspace_file", "list_workspace_files", "pc_run"],
     "default": True},
    {"id": "daily-brief", "name": "每日简报", "judge": "model", "icon": "📰",
     "desc": "联网汇总今日要闻（公司/行业/自选标的动态），产出一份结构化、可存档、带来源的简报。",
     "prompt": "每日简报流程：\n"
     "1. 先确认关注域：用户指定的关键词/公司/股票/行业；没指定就按上下文里的公司与行业默认；\n"
     "2. web_search 按域并行多查（公司名/行业关键词 + 「今日/最新」，每域 2-3 次足够，别刷屏式搜索）；\n"
     "3. 简报结构（Markdown）：\n"
     "   ## 要点速览（5-8 条，每条一句话，句尾括号注来源与日期）\n"
     "   ## 数据快照（表格：标的/事项/数值/变动——搜到才列，查不到写「未获取」）\n"
     "   ## 值得关注的信号（2-3 条，说明为什么值得看）\n"
     "4. 只陈述事实与来源，不给买卖/决策建议；搜不到的不编造，宁可少一条；\n"
     "5. save_file 存「产物库/产出/简报_YYYY-MM-DD.md」，对话里同步给全文，finish 收尾。\n",
     "tools": ["web_search", "save_file", "read_workspace_file"],
     "default": True},
    # ── v1.0.330 去AI味（规则来自主流去AI味方法论：删高频词/打散结构/加人味/只改表达） ──
    {"id": "deai", "name": "去AI味", "judge": "none", "icon": "✨",
     "desc": "把 AI 腔改写成真人自然文风：删高频套话、打散工整结构、口语化，只改表达不改事实；支持直接文本或读取页面。",
     "prompt": "去AI味改写规范（把 AI 腔改写成真人随手写的文风）：\n"
     "【五步改写】\n"
     "1. 删 AI 高频词：首先/其次/再次/最后、综上所述/总而言之/总的来说、值得注意的是/值得一提的是、\n"
     "   具有重要意义/起到积极作用、在…的背景下、随着…的发展——能删就删；保留的连接词换口语\n"
     "   （因此→所以、然而→不过）；黑话替换（赋能→帮忙、闭环→把事干完、抓手→办法、对齐→商量好、\n"
     "   深入探讨→好好聊聊）；\n"
     "2. 打散工整结构：总分总拆掉、开门见山；「不仅…还…」「一方面…另一方面」最多留一处；\n"
     "   段落长短参差（有的一段三四行，有的一句话单独成段）；\n"
     "3. 加人味：适度口语过渡（说实话/讲道理/其实，≤2 处别硬塞）；允许不完美措辞\n"
     "   （通常/大多数情况/我更倾向）；可带一两处轻微立场或情绪；\n"
     "4. 细节落地：空泛表述换成原文里的具体事实（数字/名称/场景）；原文没有的细节绝不编造；\n"
     "5. 自检：默读一遍，读着像说明书的地方再改一轮。\n"
     "【铁律】只改表达不改事实——数字/日期/金额/人名/单位/结论原样保留；篇幅与原文相当（±20%）；\n"
     "不升华主题、不硬塞 emoji、不只做同义词替换；公文/合同等正式文体保持庄重只做轻度处理。\n"
     "【输出】①改写后全文（保持适合直接复制的形态）；②末尾 1-3 行「主要改动」；\n"
     "若原文本就自然口语，直接说明无需修改并给 1-2 个可选微调建议。\n"
     "【输入源】用户消息带了文字→直接处理；没带→read_wecom_page 读当前选中页面正文处理\n"
     "（长文分段改写保持衔接）。纯文本改写任务不要执行任何写操作。\n",
     "tools": ["read_wecom_page", "list_wecom_windows"],
     "default": True},
]

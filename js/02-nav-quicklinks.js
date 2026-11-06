// ══════ v1.0.635（用户要求）：折叠态统一口径 ══════
// 只写一个属性：aria-expanded。字形（▾）与旋转（收起时 -90°）都在 CSS 的
// .fold-ic / .fold-caret 里，JS 不再 textContent="▶"/"▼"/"▸"/"»" ——
// 那正是"同一个动作五种画法"的来源，也让按钮在 JS 换字前短暂显示错向。
function setFold(el, expanded){
  if(!el) return;
  el.setAttribute("aria-expanded", expanded ? "true" : "false");
}

function openView(name){
  // v1.0.361 审查修复（P17）：切视图前清理各视图轮询定时器（防后台空转打接口）
  if(typeof cleanupViewPollers==="function"){ try{ cleanupViewPollers(); }catch(e){} }
  document.querySelectorAll(".nav-item").forEach(n=>n.classList.toggle("active", n.dataset.view===name));
  // v1.0.490：目标视图在「更多」折叠组里 → 自动展开（否则高亮不可见）
  const _act=document.querySelector('.nav-item.active[data-view="'+name+'"]');
  if(_act && _act.closest("#navMore") && document.getElementById("navMore").style.display==="none"){ toggleNavMore(); }
  document.querySelectorAll(".view").forEach(v=>v.classList.remove("active"));
  const v=$("view-"+name); if(v) v.classList.add("active");
  if(name==="models"){ loadModels(); loadUsageStats(); }
  else if(name==="templates") loadTemplates();
  else if(name==="schedules"){ loadSchedules(); }   // v1.0.568：定时任务独立视图
  // v1.0.606：运行组件独立视图（原在设置页底部）——进页自动体检一次，OCR 状态一并刷新
  else if(name==="components"){ try{ loadComponents($("compProbeBtn")); }catch(e){} try{ loadOcrStatus(); }catch(e){} }
  else if(name==="settings"){ loadWecomOpt(); loadUiPref(); loadAutostart(); loadAdminStatus(); loadFeatureFlags(); loadUserRules(); loadSecurity(); loadVersionList(); try{ loadPersonal(); }catch(e){} try{ loadArchivedList(); }catch(e){} }   // v1.0.642：已归档任务列表
  // v1.0.478 修复：逐个 try/catch——一处死引用/异常不再连坐后续加载
  // （此前 loadMonthlyStats 未定义，把 loadMacroList 一起炸掉，两卡片卡「加载中…」）
  // v1.0.498：workspace/knowledge 接管原 openWorkspace/openKnowledge 语义（关闭文件预览 + 企微文件状态刷新）
  else if(name==="workspace"){ try{ closeWsFile(); }catch(e){} [loadWorkspace, loadMonthlyStats, loadMacroList].forEach(f=>{ try{ f(); }catch(e){ console.error("[workspace]", e); } }); }
  else if(name==="knowledge"){ try{ closeKbFile(); }catch(e){} loadKnowledge(); loadWecomArchive(); }
  else if(name==="study") loadStudy();
  else if(name==="skills") loadSkills();
  else if(name==="experiences") loadExperiences();
  else if(name==="connectors") loadConnectors();
  else if(name==="chat") restoreHistory("setChatPane");
  else if(name==="work"){ refreshWindows(); if(typeof loadConvList==="function") loadConvList(true); }
}

// ---- 左侧栏「快捷入口」（可编辑，持久化到 config.quick_links） ----
const QUICK_LINKS_DEFAULT = [
  {id:"study_auto", icon:"🎓", name:"学习中心", auto_loop:true, auto_send:true, prompt:"完成当前学习中心全部未学课程（无人值守模式）：①list_wecom_windows 找到学习中心窗口（标题含「学习中心」），若不在学习地图先进入（点「我的学习地图」）；②read_wecom_page 读「未学习」tab 课程列表（scroll_wecom_window 翻完「查看更多」），全部学完或列表空 → finish；③逐门课循环：JS click 课程条目进详情 → 找到含「必修」的任务条目（click_wecom_element 或真实坐标点击）打开课程学习新窗口 → 按任务类型处理——视频：wecom_run_js 注入 study-solver 技能的「静音三件套」表达式（__muteLock 属性锁 + 400ms 静音循环 + 裸媒体 iframe 中和）后播放并拖到结尾——只有 setInterval 循环会被播放器反制，三件套以表达式原文为准、勿自行改写；每打开一个新学习窗口都要先注入一次；【倍速上限】playbackRate 最高 2x，严禁 4x 及以上（平台风控检测异常倍速会判无效学习）；文档：点「立即学习」自动变已学；考试：点考试任务进考试详情，先 read 记下「及格分」和「剩余考试次数」——=无限才点「开始考试」走单变量试错答题（参考持续答题技能），有限（「剩 N 次」/具体数字）→ **不自动开考**，记台账「考试剩余 N 次有限，跳过自动考试（防白烧次数，需人工）」并继续该课其它任务；考完回课程页确认任务变「通过」；⚠️若答题页读不到任何题目（0题/空渲染，正文只有答题卡计时）→ 直接后退退出，记台账「考试异常跳过」，不影响完成课程；④切回原窗口点「已完成」（出现「你已前往任务学习，是否已完成学习？」弹窗时）→ 学习证书 → 返回学习地图 → 下一门；⑤每门课完成后 append_progress(key=\"学习进度\", content=\"课程名 + 完成方式 + 状态\") 记一项——学完一门记一门，不要攒到最后（系统会在每次记录后自动精简上下文，几十门课跑到底也不会被资源预算掐断；开跑前先 read_workspace_file 读 产物库/产出/学习进度_进度.md 与 学习进度_*.md，已记过的课直接跳过）；⑥全部完成 finish 汇报共完成 X 门（课程名+完成方式+考试分数），末行【只写一个】RESULT 标记（正文里绝不再出现 RESULT 字样，否则会被误判触发自动续跑）：RESULT: PASS=全部必修任务真完成；RESULT: DEAD=平台侧阻断、重试无意义（考试已结束/0题/无「开始考试」按钮），该行末附一句原因；RESULT: FAIL=临时没做完、续跑有意义。防死循环：同一课程重复/无进展→记录原因跳过，同按钮连续点≤2次。"},
  {id:"exam_auto", icon:"🎯", name:"持续答题", auto_loop:true, auto_send:true, prompt:"完成当前网页的答题/考试（无人值守模式，剩余考试次数=无限才可用）：①先规划：list_workspace_files 看 知识库/课件/ 有无对应课件，知识库/题库/答题记录_*.md 与 产出/答题记录_进度.md 有无历史台账——read 后只继承其中的「已排除/已锁定」结论（已排除的答案绝不复用、已锁定的照抄；注意台账累计轮次≠上限，别把它当成只能考 N 次），再落盘【本轮全部题目的完整答案计划】进答题页；②逐题按选项文字作答（单选1个/多选多个/判断\"A 正确\"/\"B 错误\"，绝对化表述选\"错误\"），严禁点纯数字题号，交卷防重复；③【每答完一题记一次】append_progress(key=\"答题记录\", content=\"第N题：题干摘要 → 所选答案 + 依据/存疑\")——答完一题记一题，不要攒到最后：系统会在每次记录后自动精简上下文（历史轮次的页面正文不再重传），一场考从头答到尾也不会被资源预算掐断，断流时已答题不丢；④交卷后读分，再 save_file 汇总本轮台账到 知识库/题库/答题记录_<考试名>_<日期>.md（轮次｜改动题｜各题答案｜总分｜排除/锁定结论）；未达及格分：点「再考一次」单变量试错——每轮只改 1 道存疑题其余照抄，按分数变化归因（升→锁定、降→排除），每轮追加台账；连续 2 轮分数无提升且无新锁定/排除→提前收尾，别空转烧轮次；⑤每次 finish 末行【只写一个】RESULT 标记（正文里绝不再出现 RESULT 字样，否则会被误判触发自动续跑）：RESULT: PASS（已及格）/ RESULT: FAIL（未过线，软件会自动开新会话按台账续跑）/ RESULT: DEAD（平台侧阻断、重试无意义：0题空考试、考试已结束、次数已用尽——写 DEAD 不要写 FAIL，FAIL 会白烧十几分钟续跑），及格立即停，本次会话最多 10 轮。"},
  {id:"web_form", icon:"📝", name:"网页答题/填表", prompt:"完成这个网页的答题/考试（v1.0.232 知识库+无限次试一试提分）：①【优先查知识库】list_workspace_files 看 知识库/课件/ 目录，若存在与本考试主题相关的课件/制度原文 → read_workspace_file 读全文，作答以原文为准（内部考题多出自课件/制度原文，这步能从 60% 提到 90%+）；②【入口检测】判断是不是答题页（有题号\"X /N\"且有选项）；停在考试详情页就先记下「及格分」和「剩余考试次数」（无限/剩N次），再点「再考一次」进入，最多 2 次；③read 知识库/题库/答题记录_*.md 与 产出/答题记录_进度.md，同款题复用上次答案（记录里有【排除/锁定结论】以其为准：已排除的答案绝不复用，已锁定的照抄）；④逐题按选项文字作答（单选1个/多选多个/判断\"A 正确\"/\"B 错误\"），先推理再选，判断题绝对化表述选\"错误\"更安全；⑤严禁点纯数字题号（答题卡跳题）；「下一题」翻页后确认题号+1，跳变回退；⑥【每答完一题记一次】append_progress(key=\"答题记录\", content=\"第N题：题干摘要 → 所选答案 + 依据/存疑\")——答一题记一题、不要攒到最后（系统每次记录后自动精简上下文，整场答完不被资源预算掐断，断流时已答的题不丢）；交卷后再 save_file 把本场汇总到 知识库/题库/答题记录_<考试名>_<日期>.md；⑦【存疑题】剩余次数有限→先列出来问用户（人工兜底最准），用户没回复再按推理填；剩余次数=无限→不问人，直接按最优推理填；⑧交卷防重复（每点一次 read 确认）；交卷后读成绩汇报；⑨【无限次·试一试排除】剩余考试次数=无限且分数<及格分：不 finish，点「再考一次」试错——每轮只改 1-2 道存疑题、其余严格照抄上一轮，按分数变化归因：升→排除上一轮答案/锁定本轮新答案；降→排除本轮选项、下轮回退换候选；不变→换下一个候选再试（多选可按选项粒度加减一个选项）；每轮把「第N轮｜改动题｜各题答案｜总分｜排除/锁定结论」追加进答题记录；达到及格分立即停；连续 2 轮分数无提升且无新锁定/排除→提前 finish 汇报最优分数+已锁定清单+仍存疑题（别空转烧次数）；本次会话最多 10 轮后同样收尾汇报。"},
  {id:"online_doc", icon:"📄", name:"在线文档", prompt:"处理这个在线文档：read_wecom_page 看正文，需要新增/改写时用 type_wecom_document 写入文档正文，或在指定输入框 type_wecom_text 填写，改完再 read 确认，最后 finish。"},
  {id:"oa_file", icon:"📥", name:"OA文件", prompt:"处理这个 OA 页面：read_wecom_page 看清字段与附件框，填写表单(type_wecom_text / select_wecom_option)，需要上传附件先用 save_file 在产物库生成本地文件再用 upload_wecom_file 上传，需要下载用 list_wecom_links 找直链 + download_file，完成后 finish。"},
  {id:"flow_analyze", icon:"🔀", name:"流程分析", prompt:"分析当前流程（硬约束：总工具调用≤5次，5轮内完成，不要逐个下载全部附件）：1) read_wecom_page 一次读全流程描述（步骤/角色/时限/所需材料/条件分支）；2) list_wecom_links 看附件，只下载 1 个最相关的文本类附件核对，pdf/docx/xlsx 二进制附件不下载、仅按文件名核对，正文够就直接跳过；3) 从步骤完整性/审批链/时限可执行性/风险点给出合理性结论；4) save_file 生成 Markdown 报告到产物库/产出；5) finish 内联完整结论（总体评价+问题+不一致项+建议+报告路径）。信息够就提前收尾。"},
  {id:"finance_bill", icon:"💰", name:"财务单据", prompt:"核查当前财务列表页所有「已付款」单据：1) read_wecom_page 读列表，按「单据状态」列筛出所有已付款单据（记录单据编号清单，去重；有分页用 scroll_wecom_window/下一页加载全）；2) 逐张用 click_wecom_element 点单据编号进详情，read_wecom_page 读取填写逻辑（费用明细/报销分类/审批链/表单字段取值），list_wecom_links 找附件并逐个 download_file 下载到产物库/下载（下载失败记录原因），然后返回列表继续下一张；3) 全部核查完用 save_file 生成结构化报告到产物库/产出（含汇总表+每单明细+附件清单），最后 finish 汇报核查统计与异常。不要中途停止，逐张完整核查。"},
  {id:"finance_check", icon:"🟢", name:"财务审核", prompt:"按红绿规则审核当前财务平台待办/消息中心的单据（最小步骤，每张只做4步，全程禁止用搜索框、禁止 list_interactive_elements）：1) read_wecom_page 读待办/消息中心列表，记录全部主题（单据编号清单，去重；有分页用 scroll_wecom_window/下一页加载全，不逐个 read 列表）；2) 逐张执行：click_wecom_element 点击主题 → 预览页出现后直接 click「查看单据」进详情（按钮文字固定，不要列元素找按钮）→ read_wecom_fields 一次读取 支出类别/专管部门/业务类型/预算执行单/附件（工具会自动等待加载并重试；某字段 count=0 时先看返回的 all_labels 是否有近义字段名如「主管部门」，有就用真实字段名重读一次，别再误报缺失）→ 把 read_wecom_fields 完整结果传给 run_rule_check 脚本判定（支出类别=专管费用 且 专管部门=信息技术部 且 业务类型=费用类付款（电子） 且 预算执行单有内容 且 有附件 → 绿，任一不符 → 红），直接采用脚本结果，不要重复解读；仅当脚本返回 all_fields_missed（5 项全未读到）才 read_wecom_page 确认页面已加载后重读 → 返回列表：优先 list_wecom_windows 查看父子关系、直接切回父窗口(原列表)序号继续下一张（父窗口仍在，无需在页面里找返回按钮）；父窗口不可用时再 click「待办任务」返回；3) 全部审完用 save_file 生成财务审核_YYYYMMDD.md 到产物库/产出（总体统计：共审/绿/红 + 汇总表：单据编号/支出类别/专管部门/业务类型/预算执行单/附件名称/判定绿红/不符原因，字段值直接抄脚本结果）；4) finish 汇报总数、绿数、红数、红色单据清单与原因（含无附件项）、报告路径。注意：严禁在搜索框搜索单据编号，严禁对同一页重复 read，严禁额外 list_interactive_elements；附件只需列出名称，不要下载附件。"},
  {id:"reimburse", icon:"🧾", name:"报销单", prompt:"帮我填写这个报销付款单：1) read_wecom_page 读当前表单，按默认值填写——实报人职位=公司员工、实报日期=今天、费用承担部门=信息技术部、费用承担公司=用 run_rule_check 返回的默认值、支出类别=直接费用、业务类型=费用类付款（非电子类）、收款人/银行账号/开户行/支付方式用 run_rule_check(module=reimburse-filler) 返回的默认值（个人信息在设置里，脚本返回 None 的字段问我）；2) 费用明细按我下面给的内容填写（费用项目+金额，多行用增行）；3) 填完 read_wecom_page 核对必填项，缺失补填；4) 完成后 finish 汇报已填字段和金额合计，不要自动提交。我现在要填：______"},
  {id:"monthly_check", icon:"🗓️", name:"检查月报", prompt:"逐一检查当前待办事宜列表里的全部「分支机构信息技术月报」并形成结论（硬约束：逐张完整检查，禁止遗漏、禁止中途停止；全程在待办事宜页面运行，不搜索单据编号，不重复 read 同一页）：\n1) read_wecom_page 读待办列表，筛出所有单据类型为「分支机构信息技术月报」的待办，记录 摘要/单据编号 清单（去重；有分页用 scroll_wecom_window/下一页加载全）；\n2) 逐张执行：click_wecom_element 点击该月报主题进入详情 → read_wecom_fields 一次读取 机构名称/月报所示月份/本月应运维天数/实际运维天数/附件文件名（某字段 count=0 时先用返回的 all_labels 找近义字段名重读一次）→ 确认有没有设备清单 xlsx（文件名形如 QR-xxx信息技术工作月报-YYYYMM.xlsx 或 xxx信息设备清单YYYYMM.xlsx）：有就用 list_wecom_links 取它的直链再 download_file 静默下载到产物库/下载，**严禁点击页面上的「下载」按钮**（企微 Electron 必弹原生「另存为」对话框打断用户前台，v1.0.604 起该回退已禁用）；下载成功即算「清单已上传」，取不到直链就写「清单已上传但无法静默获取」并继续下一张（不要改用点击补齐）；\n3) 按两项规则核对并记录结论：\n   - 检查项1：本月应运维天数 与 实际运维天数 两个数字是否一致；\n   - 检查项2：设备清单是否已上传（v1.0.604 用户口径：**取消在用设备数比对**——表单口径与清单口径常年不一致，误判会让机构白整改；在用台数可顺带统计写进明细表做参考，但不参与红绿）；\n   - 两项全部满足 → 该月报「通过」；天数不一致 或 清单没传 → 该月报「不通过」并写明具体原因（如：应运维 23 天 / 实际 22 天 不一致；正文未见设备清单 xlsx）；清单传了但我们静默取不到文件 → 记「待定」，不算机构不合规；\n   - 检查完返回列表：优先 list_wecom_windows 查看父子关系、切回父窗口(原待办列表)序号继续下一张（详情页由列表触发打开，父窗口仍在）；父窗口不可用再点「待办任务」或滚动返回；\n4) 全部检查完用 save_file 生成「月报检查_YYYYMMDD.md」到产物库/产出：总体统计（共查 X 张 / 通过 X 张 / 不通过 X 张）+ 明细表（单据编号/机构名称/月份/应运维天数/实际运维天数/设备清单是否上传/在用台数(参考)/结论/不通过原因）；\n5) finish 以总结形式汇报：共查几张、通过几张、不通过几张，逐一列出不通过月报的编号与原因，最后给出整体结论与报告路径。\n\n【防死循环铁律，违反即视为该步失败】① 同一元素（同一 target/pick 或同一文字按钮）连续点击最多 2 次：点一次 → read 确认页面是否变化；页面没变化就立即停止点击它，换其它元素或标记该步异常；② 若「点击→read」两次后页面内容仍与上次基本相同，判定该月报当前无法继续推进，记录原因（如：进入详情失败/页面无响应/元素失效）后返回列表检查下一张，绝不反复点同一元素；③ 任何步骤若已连续 2 次执行相同工具且参数不变仍无进展，立即跳过该步并记录原因；④ 返回列表优先用 list_wecom_windows 查看父子关系、切回父窗口(原待办列表)序号，父窗口不可用再点「待办任务」按钮，找不到就用滚动或其它可见入口，不要重复点击同一元素或猜测点击坐标。"},
  {id:"deai", icon:"✨", name:"去AI味", prompt:"对下面的内容做「去AI味」改写。若横线处有文字→直接改写文字；横线为空→read_wecom_page 读当前选中页面正文再改写（长文分段改保持衔接）。规则：①删AI高频词（首先/其次/最后、综上所述/总而言之、值得注意的是、具有重要意义、在…背景下，黑话如赋能/闭环/抓手换人话）；②打散总分总与排比，开门见山，段落长短参差；③适度口语过渡（说实话/其实≤2处别硬塞），允许「通常/我更倾向」类不完美措辞；④空泛表述换原文具体事实，没有的细节不编；⑤铁律：只改表达不改事实——数字/日期/金额/人名/结论原样保留，篇幅±20%，不升华主题、不硬塞emoji；公文等正式文体只做轻度处理。输出：改写后全文+末尾1-3行主要改动；原文本就自然口语则说明无需修改。文字：______"}
];
let __quickLinks = [];
let __quickLinksDraft = null;

// v1.0.636（用户截图：默认宽度下条目名被截成「学习…」「持续…」）：内置入口改名跟随。
// 「(无人值守)」这 5 个字是 v1.0.251 把 auto_send 类单列成「🤖 自动任务」分组时留下的——
// 那时分组名是唯一信号。v1.0.635 分组并回快捷入口、改由行尾「即发」徽章表达同一件事之后，
// 它成了纯冗余，却正好把名字挤成省略号。改名只动内置默认值；用户配置里已存的旧名
// 在载入时按 id 就地换掉——**只认旧默认名**，用户自己改过的名字一律不碰。
// 纯内存生效、不写回 config（下次在编辑器里保存时才落盘）。
const BUILTIN_NAME_RENAMES = {
  study_auto: {"学习中心(无人值守)": "学习中心"},
  exam_auto:  {"持续答题(无人值守)": "持续答题"},
};
function migrateBuiltinNames(list){
  (list || []).forEach(l=>{
    const m = l && BUILTIN_NAME_RENAMES[l.id];
    if(m && m[l.name]) l.name = m[l.name];
  });
  return list;
}

// v1.0.518 修复（用户反馈"没有月报字样"）：存量配置缺失新增内置入口的增量合并。
// 根因：loadQuickLinks 只在配置为空时才用默认清单——老用户的 config.quick_links
// 是旧版默认集（含财务审核等，但不含后来新增的「检查月报」），新增内置入口永远
// 到不了老用户。现按 id 增量合并：配置里缺的默认入口追加到尾部；用户主动删除过
// 的默认项记入 localStorage 墓碑（ql_removed_ids），合并时跳过——删了不复活。
function _removedIds(){
  try{ return new Set(JSON.parse(localStorage.getItem("ql_removed_ids")||"[]")); }
  catch(e){ return new Set(); }
}
function _tombstone(id){
  if(!id) return;
  try{
    if(!QUICK_LINKS_DEFAULT.some(d=>d.id===id)) return;   // 只对内置入口记墓碑
    const s=_removedIds(); s.add(id);
    localStorage.setItem("ql_removed_ids", JSON.stringify([...s]));
  }catch(e){}
}

async function loadQuickLinks(){
  try{
    const d = await getJson("/api/config");
    if(Array.isArray(d.quick_links) && d.quick_links.length) __quickLinks = d.quick_links;
    else __quickLinks = QUICK_LINKS_DEFAULT.map(x=>({...x}));
  }catch(e){ __quickLinks = QUICK_LINKS_DEFAULT.map(x=>({...x})); }
  // 增量合并缺失的内置入口（墓碑跳过）
  try{
    const removed=_removedIds();
    QUICK_LINKS_DEFAULT.forEach(d=>{
      if(!__quickLinks.some(l=>l.id===d.id) && !removed.has(d.id)) __quickLinks.push({...d});
    });
  }catch(e){}
  migrateBuiltinNames(__quickLinks);   // v1.0.636：内置入口改过名的，存量配置跟着换
  renderQuickLinks();
}
function renderOpChips(){
  const box=$("opChips"); if(!box) return;
  box.innerHTML="";
  const src=(__quickLinks&&__quickLinks.length)?__quickLinks:QUICK_LINKS_DEFAULT;
  // 聊天框上方 chips 只放「提示词类」入口；带 auto_send 的重规则/脚本类只在左侧
  // 「⚡ 快捷入口」分组里（行尾带「即发」标签），不重复出现在这排 chips 上——
  // 一键直发类动作放进聊天工具栏容易误点（v1.0.517 的口径，v1.0.635 分组合并后不变）。
  src.filter(l=>!l.auto_send).forEach(l=>{
    const b=document.createElement("button"); b.className="chip";
    b.title=(l.prompt||l.name||"");
    const t=document.createElement("span"); t.textContent=(l.icon||"⚡")+" "+(l.name||"未命名");
    b.appendChild(t);
    // v1.0.516（用户需求）：每个快捷 chip 加 × 删除按钮——从 quick_links 持久化删除
    const x=document.createElement("span"); x.className="chip-x"; x.textContent="×";
    x.title="删除该快捷入口";
    x.onclick=(ev)=>{ ev.stopPropagation(); removeQuickLink(l.id||l.name); };
    b.appendChild(x);
    // v1.0.517（用户反馈"快捷入口触发的都是属于直接干的"）：chip 点击即预设直接干
    // 模式 + 标记本次发送跳过模式弹窗（静默建会话归入任务栏）
    b.onclick=()=>{ setOpAutoLoop(!!l.auto_loop); setOpMode("agent"); window.__presetDirect = true; fillPrompt("opInput", l.prompt||l.name||""); };
    box.appendChild(b);
  });
  // v1.0.516（用户需求）：＋ 新增按钮——打开既有快捷入口编辑器（名称/图标/提示词/即发/续跑全可配）
  const add=document.createElement("button"); add.className="chip chip-add";
  add.textContent="＋ 新增"; add.title="新增快捷入口（可勾选「即发」=点击直接执行）";
  add.onclick=()=>{ if(typeof openQuickEditor==="function") openQuickEditor(); };
  box.appendChild(add);
}
// v1.0.516：删除快捷入口（持久化到 config.quick_links，侧栏与 chips 同步刷新）
// v1.0.518：删除内置入口时记墓碑——否则下次启动增量合并会把它复活
async function removeQuickLink(id){
  _tombstone(id);
  __quickLinks = (__quickLinks&&__quickLinks.length)?__quickLinks:QUICK_LINKS_DEFAULT.map(x=>({...x}));
  const before=__quickLinks.length;
  __quickLinks = __quickLinks.filter(l=>(l.id||l.name)!==id);
  if(__quickLinks.length===before){ toast("未找到该快捷入口","err"); return; }
  try{
    const r=await fetch("/api/quick_links",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({quick_links:__quickLinks})});
    const d=await r.json();
    if(!r.ok||d.error){ toast("删除失败："+(d.error||"HTTP "+r.status),"err"); return; }
    __quickLinks=d.quick_links;
    renderQuickLinks(); renderOpChips();
    toast("已删除快捷入口","ok");
  }catch(e){ toast(fetchErr(e,"删除快捷入口"),"err"); }
}
// ---- 「⚡ 快捷入口」分组（v1.0.635 起，侧栏唯一的任务入口分组）----
// 演变：v1.0.251 把 auto_send 类入口单列成「🤖 自动任务」；v1.0.633 两组一起收进「➕ 更多」；
// v1.0.635（用户要求）删掉自动任务分组、并回快捷入口。分组名对用户没有区分度
// （点进去都是"发起一次任务"），真正的差异在**点击行为**：auto_send 直接执行、
// 其余只填提示词——这个差异改由行尾「即发」标签 + title 表达，不再靠另立分组。
function renderQuickLinks(){
  renderOpChips();
  const box = $("quickLinksBox"); if(!box) return;
  applyQuickFold();
  box.innerHTML = "";
  const src = (__quickLinks && __quickLinks.length) ? __quickLinks : QUICK_LINKS_DEFAULT;
  if(!src.length){
    const sub = document.createElement("div");
    sub.className = "sub"; sub.style.cssText = "padding:4px 18px;font-size:var(--fs-sm)";
    sub.textContent = "暂无快捷入口——点分组标题旁「✏️ 编辑」添加常用任务，之后一键直达";
    box.appendChild(sub); return;
  }
  src.forEach(l=>{
    const div = document.createElement("div");
    div.className = "side-link";
    div.title = (l.prompt || l.name || "")
      + (l.auto_send ? "\n（点击直接执行，不再弹模式选择）" : "")
      + (l.auto_loop ? "\n（未过线自动开新会话续跑）" : "");
    // v1.0.616（审查 P2）：裸 div + onclick 键盘够不到，补 role/tabindex/名称，
    // Enter·Space 由 js/12-init.js 的统一处理器转成 click()
    div.setAttribute("role", "button");
    div.tabIndex = 0;
    div.setAttribute("aria-label", "快捷入口 " + (l.name || ""));
    div.onclick = ()=>openQuickLink(l.id);
    const icon = document.createElement("span"); icon.textContent = l.icon || "🔗";
    const nm = document.createElement("span"); nm.className = "sl-name"; nm.textContent = l.name || "未命名";
    div.appendChild(icon); div.appendChild(nm);
    if(l.auto_send || l.auto_loop){
      const tags = document.createElement("span");
      tags.className = "sl-tags";
      if(l.auto_send){ const t=document.createElement("span"); t.className="sl-tag"; t.textContent="即发"; t.title="点击直接执行"; tags.appendChild(t); }
      if(l.auto_loop){ const t=document.createElement("span"); t.textContent="🔁"; t.title="自动续跑已启用"; tags.appendChild(t); }
      div.appendChild(tags);
    }
    box.appendChild(div);
  });
}
// 任务区是否处于折叠态（类由 js/10 的 applyRailFold 唯一写入）。
// 「更多」与「快捷入口」两组的折叠钮在折叠期间被锁住——见下面 toggleQuickFold / toggleNavMore。
function railFolded(){
  const sec = document.getElementById("taskSec");
  return !!(sec && sec.classList.contains("folded"));
}
// ---- 快捷入口分组折叠（v1.0.635：用户要求**默认折叠**，展开后记忆） ----
// 与 v1.0.633 那次"删掉内层折叠"相反的判断依据是用户自己的偏好：他们宁可多点一下，
// 也不要分组常年摊开吃掉任务列表。折叠状态只影响本组，外层「更多」独立。
// v1.0.644（用户："放工作台按钮上面 且默认折叠，需要用户使用再去点展开按钮"）：
// 撤掉 v1.0.637 加的「任务区折叠 ⇒ 本组自动展开」联动。判据变了——当时分组埋在「更多」里，
// 折叠任务区是把它掏出来的唯一机会；现在它钉在导航顶端随时可达，再自动摊开就成了
// "我没点它自己开了"。代价要说清：高窗口下折叠任务区后，原来这一级接住的 ~200px 空白
// 会重新出现（第一级「更多」仍照常联动展开）——那是用户自己要的"只有我点才展开"。
let __quickFolded = (function(){ try{ return localStorage.getItem("__quickFold") !== "0"; }catch(e){ return true; } })();
function applyQuickFold(){
  const g = $("quickGroup"), b = $("quickFoldBtn");
  if(g) g.classList.toggle("collapsed", __quickFolded);
  setFold(b, !__quickFolded);
}
// __quickFolded 必须同步改：renderQuickLinks() 每次重绘都会 applyQuickFold()，
// 只改 DOM 会被下一次重绘打回原形。
function setQuickCollapsed(collapsed){
  __quickFolded = collapsed;
  lsSet("__quickFold", collapsed ? "1" : "0");
  applyQuickFold();
}
function toggleQuickFold(){
  // v1.0.640（用户澄清）：这一颗**不**跟着任务区折叠一起锁死。快捷入口与模板/技能中心/经验库
  // 是同级入口，只是自带一个列表；收起这个列表是它自己的事，被任务区的状态没收掉是越权。
  // （v1.0.639 我把它和「更多」一起锁了，锁错了对象。）
  setQuickCollapsed(!__quickFolded);
}
// ---- 自动续跑（auto_loop v1.0.249）：快捷入口可携带，考试未过自动开新会话续跑 ----
// v1.0.498：原工作台 🔁 开关按钮（opAutoLoopBtn）已随 work-chat-row 拆除，仅保留状态位
// （__opAutoLoop 由快捷入口 auto_loop 驱动，随请求体发送）；开关状态经 toast 反馈
let __opAutoLoop = false;
function setOpAutoLoop(v){
  const old = __opAutoLoop;
  __opAutoLoop = !!v;
  if(old !== __opAutoLoop) toast(__opAutoLoop ? "🔁 自动续跑已开启：本轮任务未过线将自动续跑" : "自动续跑已关闭", __opAutoLoop ? "ok" : "info");
}
function openQuickLink(id){
  const l = __quickLinks.find(x=>x.id===id) || __quickLinks[0];
  if(!l) return;
  openView("work");
  setOpAutoLoop(!!l.auto_loop);
  if(l.auto_send){
    // v1.0.517（用户反馈）：自动任务是执行型预设，固定直接干模式（runQuickTask 内
    // 静默建会话，不再弹模式选择弹窗）
    setOpMode("agent");
    runQuickTask(l.prompt || ""); return;
  }  // 一键直发，无需再点「执行」
  setOpMode("agent"); window.__presetDirect = true;   // 快捷入口预设=直接干，发送时跳过模式弹窗
  fillPrompt("opInput", l.prompt || "");
}
function openQuickEditor(){
  __quickLinksDraft = __quickLinks.map(x=>({id:x.id, icon:x.icon||"🔗", name:x.name||"", prompt:x.prompt||"", auto_send:!!x.auto_send, auto_loop:!!x.auto_loop}));
  renderQuickEditor();
  $("quickEditorOverlay").classList.add("show");
}
function closeQuickEditor(){ $("quickEditorOverlay").classList.remove("show"); __quickLinksDraft=null; }
function renderQuickEditor(){
  const box = $("quickEditBox"); if(!box) return;
  box.innerHTML = "";
  __quickLinksDraft.forEach((l,i)=>{
    const row = document.createElement("div"); row.className = "ql-row";
    const idx = document.createElement("span"); idx.className = "ql-idx"; idx.textContent = i+1;
    const wrap = document.createElement("div"); wrap.className = "ql-row-wrap";
    const line = document.createElement("div"); line.style.cssText = "display:flex;gap:8px;align-items:center";
    const ic = document.createElement("input"); ic.className = "ql-icon"; ic.value = l.icon||"🔗"; ic.title = "图标(emoji)";
    ic.oninput = ()=>{ __quickLinksDraft[i].icon = ic.value; };
    const nm = document.createElement("input"); nm.className = "ql-name"; nm.value = l.name||""; nm.placeholder = "名称";
    nm.oninput = ()=>{ __quickLinksDraft[i].name = nm.value; };
    const ops = document.createElement("span"); ops.className = "ql-ops";
    const mkBtn = (txt, fn, dis)=>{ const b = document.createElement("button"); b.textContent = txt; b.disabled = !!dis; b.onclick = fn; ops.appendChild(b); return b; };
    mkBtn("↑", ()=>qeMove(i,-1), i===0);
    mkBtn("↓", ()=>qeMove(i,1), i===__quickLinksDraft.length-1);
    mkBtn("🗑", ()=>qeDel(i), false);
    const cbWrap = document.createElement("span"); cbWrap.style.cssText = "display:flex;flex-direction:column;gap:2px;font-size:var(--fs-xs);color:var(--muted);white-space:nowrap";
    const mkCb = (key,label,tip)=>{ const lb=document.createElement("label"); lb.style.cssText="display:flex;align-items:center;gap:3px;cursor:pointer"; lb.title=tip; const cb=document.createElement("input"); cb.type="checkbox"; cb.checked=!!l[key]; cb.onchange=()=>{ __quickLinksDraft[i][key]=cb.checked; }; lb.appendChild(cb); lb.appendChild(document.createTextNode(label)); return lb; };
    cbWrap.appendChild(mkCb("auto_send","即发","勾选后点击该入口直接发送执行（不再显示在聊天框上方）"));
    cbWrap.appendChild(mkCb("auto_loop","续跑","勾选后启用自动续跑(auto_loop)：考试未过线自动开新会话续跑"));
    const pm = document.createElement("textarea"); pm.className = "ql-prompt"; pm.rows = 3;
    pm.value = l.prompt||""; pm.placeholder = "点击快捷入口时填入工作台的提示词";
    pm.oninput = ()=>{ __quickLinksDraft[i].prompt = pm.value; };
    line.appendChild(ic); line.appendChild(nm); line.appendChild(cbWrap); line.appendChild(ops);
    wrap.appendChild(line); wrap.appendChild(pm);
    row.appendChild(idx); row.appendChild(wrap);
    box.appendChild(row);
  });
}
function qeMove(i, dir){
  const j = i+dir;
  if(j<0 || j>=__quickLinksDraft.length) return;
  const t = __quickLinksDraft[i]; __quickLinksDraft[i] = __quickLinksDraft[j]; __quickLinksDraft[j] = t;
  renderQuickEditor();
}
function qeDel(i){
  // v1.0.518：编辑器删除内置入口同样记墓碑（防增量合并复活）
  const it = __quickLinksDraft[i];
  if(it) _tombstone(it.id);
  __quickLinksDraft.splice(i,1); renderQuickEditor();
}
function qeAdd(){
  __quickLinksDraft.push({id:"q"+Date.now(), icon:"🔗", name:"新快捷入口", prompt:""});
  renderQuickEditor();
}
async function qeSave(){
  for(const l of __quickLinksDraft){
    if(!(l.name||"").trim() && !(l.prompt||"").trim()){ toast("存在空的快捷入口，请填写名称或删除","err"); return; }
    if(!l.id) l.id = "q"+Date.now()+Math.random().toString(36).slice(2,6);
  }
  try{
    const r = await fetch("/api/quick_links", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({quick_links:__quickLinksDraft})});
    const d = await r.json();
    if(!r.ok || d.error){ toast("保存失败："+(d.error||"HTTP "+r.status),"err"); return; }
    __quickLinks = d.quick_links;
    closeQuickEditor(); renderQuickLinks(); toast("快捷入口已保存");
  }catch(e){ toast(fetchErr(e,"保存快捷入口"),"err"); }
}

// ===== 快捷入口 AI 生成（agent 聊天式需求确认） =====
let __qlAI = {msgs:[]};
let __qlAICtrl=null, __qlAIRunning=false, __qlAIResult=null;
// 状态桥：旧全局名（__qlAI/__qlAICtrl/__qlAIRunning/__qlAIResult）保持唯一存储——
// 外部与内联 onclick 可能引用，不改名；公共骨架 makeDesignAssistant（01-core）经取值器读写。
const __qlS = {
  get msgs(){ return __qlAI.msgs; },
  get ctrl(){ return __qlAICtrl; }, set ctrl(v){ __qlAICtrl = v; },
  get running(){ return __qlAIRunning; }, set running(v){ __qlAIRunning = v; },
  get result(){ return __qlAIResult; }, set result(v){ __qlAIResult = v; },
};
// 聊天窗骨架（加消息/选项/点选/发送/SSE）收敛到 makeDesignAssistant；
// 结果渲染 renderQuickAIResult 留在本文件、经 onResult 注入（与模板侧各自的实现不同）。
const __qlAsst = makeDesignAssistant({
  S: __qlS, chatId: "qlaiChat", inputId: "qlaiInput", sendBtnId: "qlaiSendBtn", modelSelId: "qlaiModel",
  endpoint: "/api/quick_links/agent/chat", errLabel: "快捷入口 AI 生成", emptyHint: "请先描述快捷入口需求",
  extract: (j)=>Array.isArray(j.links)?j.links:null, onResult: (links)=>renderQuickAIResult(links),
});
function closeQuickAI(){ $("qlAIOverlay").classList.remove("show"); if(__qlAICtrl) __qlAICtrl.abort(); __qlAIRunning=false; }
function fillQuickAIModel(){
  const sel=$("qlaiModel"); if(!sel) return;
  const cur=sel.value;
  // v1.0.471（UI#14）：value 补 escapeHtml——与 fillTplAIModel（08）一致；拼串本体收敛到 fillModelSelect
  fillModelSelect(sel, models);
  if(cur) sel.value=cur;
}
function openQuickAI(){
  if(!Array.isArray(__quickLinksDraft)) __quickLinksDraft = __quickLinks.map(x=>({id:x.id, icon:x.icon||"🔗", name:x.name||"", prompt:x.prompt||"", auto_send:!!x.auto_send, auto_loop:!!x.auto_loop}));
  __qlAI={msgs:[]}; __qlAICtrl=null; __qlAIRunning=false; __qlAIResult=null;
  const chat=$("qlaiChat"); if(chat) chat.innerHTML="";
  addQuickAIMsg("agent","你好，我是快捷入口设计助手。用一句话描述你要的快捷入口，例如：\n「做一个财务审核快捷入口，按红绿规则逐张核对单据」\n我会与你确认几个关键选项（业务场景、数量、提示词风格等），确认后直接生成快捷入口列表，可一键添加到左侧栏。");
  fillQuickAIModel();
  if(!(models||[]).length){ loadModels().then(fillQuickAIModel).catch(()=>{}); }
  $("qlaiInput").value="";
  $("qlAIOverlay").classList.add("show");
  setTimeout(()=>{ const i=$("qlaiInput"); if(i) i.focus(); },60);
}
// 以下四件与 08 的 tplAI 同名四件是同一套交互的整段克隆——骨架已收敛到
// makeDesignAssistant（01-core），这里只留薄封装保住旧函数名（外部/内联 onclick 引用）。
function addQuickAIMsg(role, text){ return __qlAsst.addMsg(role, text); }
function renderQuickAIOptions(questions){ __qlAsst.renderOptions(questions); }
function renderQuickAIResult(links){
  if(!links || !links.length) return;
  const chat=$("qlaiChat"); if(!chat) return;
  const el=document.createElement("div"); el.className="opt-block";
  el.innerHTML=`<div class="ob-t" style="color:var(--brand-fg)">✅ 已生成 ${links.length} 个快捷入口：</div>`+
    `<div style="margin:6px 0">${links.map((l,i)=>`<div style="font-size:var(--fs-sm);padding:2px 0"><b>${i+1}. ${escapeHtml(l.icon||"🔗")} ${escapeHtml(l.name||"未命名")}</b><div style="color:var(--muted);white-space:pre-wrap;max-height:56px;overflow:auto;margin-left:18px">${escapeHtml((l.prompt||"").slice(0,120))}${(l.prompt||"").length>120?"…":""}</div></div>`).join("")}</div>`+
    `<div class="res-actions"><button class="primary" onclick="applyQuickAIResult()">添加到左侧栏</button>`+
    `<button class="ghost" onclick="regenerateQuickAI()">重新生成</button></div>`;
  chat.appendChild(el); chat.scrollTop=chat.scrollHeight;
}
function quickAIPick(key, value, label){ __qlAsst.pick(key, value, label); }
function quickAISend(){ return __qlAsst.send(); }
function applyQuickAIResult(){
  const links=__qlAIResult; if(!links || !links.length) return;
  if(!Array.isArray(__quickLinksDraft)) __quickLinksDraft=[];
  const existing = new Set(__quickLinksDraft.map(x=>(x.name||"").trim()));
  links.forEach(l=>{
    const nm=(l.name||"").trim();
    if(existing.has(nm)) return;
    existing.add(nm);
    __quickLinksDraft.push({id:"q"+Date.now()+Math.random().toString(36).slice(2,6), icon:(l.icon||"🔗").toString(), name:nm, prompt:(l.prompt||"")});
  });
  renderQuickEditor();
  closeQuickAI();
  toast("已添加到编辑列表，可保存","ok");
}
function regenerateQuickAI(){
  if(__qlAIRunning){ toast("正在生成中，请稍候…","warn"); return; }
  __qlAIResult=null;
  const chat=$("qlaiChat"); if(chat){
    const blocks=chat.querySelectorAll(".opt-block"); const last=blocks[blocks.length-1];
    if(last) last.remove();
  }
  __qlAI.msgs.push({role:"user", content:"【重新生成】请基于当前需求重新生成快捷入口列表。"});
  addQuickAIMsg("user","重新生成");
  quickAISend();
}

// ---- 经验库：模块 → 问题 → 解决方法，可增删改查 ----


// ---------- 侧栏「更多」折叠组（v1.0.490，低频入口收纳） ----------
// v1.0.637（用户：任务区折叠后那块空间该给「更多」全展开）：这里有了第二种"打开"，
// 于是必须分清两条状态——
//   · navMoreOpen（localStorage）＝**用户偏好**，只有他自己点「更多」才写；
//   · __moreForced＝任务区折叠带出来的**联动临时态**，不落盘。
// 不分开的后果：折叠一次任务区就把偏好改成"展开"，下次启动「更多」莫名其妙是开的；
// 反过来任务区一展开就强制收起，又会把用户刚刚手动点开的「更多」吞掉。
let __moreForced = false;
function setNavMoreOpen(open, persist){
  const box=document.getElementById("navMore");
  const btn=document.getElementById("navMoreBtn");
  if(!box) return;
  box.style.display = open ? "" : "none";
  setFold(btn, open);
  if(persist !== false){
    lsSet("navMoreOpen", open?"1":"");
  }
}
function toggleNavMore(){
  const box=document.getElementById("navMore"); if(!box) return;
  if(railFolded()){
    // v1.0.639（用户："只要把更多折叠回去以后就会出现空白"）：任务区折叠期间「更多」就是
    // 那块空间的 occupants，收起它 = 立刻回到用户投诉的那一大片空白。
    // 所以这里不是"点不动"，而是明确告诉用户该按哪个键换回来。
    toast("任务区已折叠，「更多」正占着这块位置——先点任务区右侧的 ▶ 展开任务列表，再收起「更多」","info");
    return;
  }
  __moreForced = false;                 // 用户手动接管，联动从这一刻起不再插手
  setNavMoreOpen(box.style.display==="none");
}
// 任务区折叠 ↔ 「更多」展开的唯一接口（由 js/10 的 applyRailFold 调用）
function forceNavMoreForRail(fold){
  const box=document.getElementById("navMore"); if(!box) return;
  if(fold){
    __moreForced = true;
    setNavMoreOpen(true, false);
  }else if(__moreForced){
    __moreForced = false;
    let pref=false;
    try{ pref = localStorage.getItem("navMoreOpen")==="1"; }catch(e){}
    setNavMoreOpen(pref, false);         // 回到偏好态（含用户在折叠期间的手动点击结果）
  }
}
(function restoreNavMore(){
  try{ if(localStorage.getItem("navMoreOpen")==="1") setNavMoreOpen(true, false); }
  catch(e){}
})();


// ---- v1.0.505 侧栏深色/浅色模式快捷切换（设置页 checkbox 同步） ----
function toggleDarkSidebar(){
  const on = !document.body.classList.contains("dark");
  applyDarkMode(on);
  lsSet("darkMode", on?"1":"0");
  const dk = $("opt_dark_mode"); if(dk) dk.checked = on;
  const b = $("sbDarkBtn"); if(b){ b.textContent = on ? "☀️" : "🌙"; b.title = on ? "切换浅色模式" : "切换深色模式"; }
}
function initSbDarkBtn(){
  const dark = (()=>{ try{ return localStorage.getItem("darkMode")==="1"; }catch(e){ return false; } })();
  applyDarkMode(dark);
  const b = $("sbDarkBtn"); if(b){ b.textContent = dark ? "☀️" : "🌙"; b.title = dark ? "切换浅色模式" : "切换深色模式"; }
}

// ---- v1.0.499 侧栏折叠 + 拖拽调宽 ----
// 收成 54px 图标条；展开后右缘拖拽调宽（150~420px，双击回到 css 的默认宽）；状态均存 localStorage
function toggleSidebar(){
  const app=document.querySelector(".app");
  if(!app) return;
  const collapsed=app.classList.toggle("sb-collapsed");
  const t=document.getElementById("sbToggle");
  // v1.0.635：原来这里换字 »/«（全软件唯一不用箭头的折叠钮）。现在与其余折叠钮同一原语：
  // "侧栏展开中"= aria-expanded=true（▾ 朝下），收成图标条时 =false（▾ 转成朝右）。
  setFold(t, !collapsed);
  if(t) t.title = collapsed ? "展开侧栏" : "收起侧栏";
  if(collapsed){
    // v1.0.637：细条下强制收起子菜单是**视图动作**，不该顺手改写用户的「更多」偏好
    // （原先调 toggleNavMore()，等于"把侧栏收成图标条"就把 navMoreOpen 落盘成关闭）。
    setNavMoreOpen(false, false);
  } else if(typeof applyRailFold === "function"){
    // 重新展开图标条时把联动补回来：任务区还折叠着 ⇒ 「更多」与「快捷入口」该是展开的
    const sec=document.getElementById("taskSec");
    if(sec && sec.classList.contains("folded")) applyRailFold(true);
  }
  lsSet("sbCollapsed", collapsed?"1":"");
}
function initSidebarLayout(){
  try{
    const w=parseInt(localStorage.getItem("sbw")||"0",10)||0;
    if(w>=150 && w<=420) document.documentElement.style.setProperty("--sbw", w+"px");
    if(localStorage.getItem("sbCollapsed")==="1"){
      const app=document.querySelector(".app");
      if(app) app.classList.add("sb-collapsed");
      const t=document.getElementById("sbToggle");
      if(t){ setFold(t, false); t.title="展开侧栏"; }   // v1.0.635：不再换字 »
    }
    // v1.0.637：任务区折叠记忆挪到这里。原先它挂在 renderTaskRail() 末尾，于是
    // "会话列表接口还没回来 / 拉失败"时折叠记忆整个不生效（重启后看到的是展开的列表），
    // 而且每 10 秒的静默轮询都会把它重刷一遍——侧栏状态不该依赖列表数据。
    if(typeof applyRailFold === "function")
      applyRailFold(localStorage.getItem("taskRailFold")==="1");
  }catch(e){}
  const h=document.getElementById("sbResize"); if(!h) return;
  let lastDown = 0;   // v1.0.499：双击判定用手动时间窗（preventDefault+pointer-events 切换会吞掉原生 dblclick）
  h.addEventListener("mousedown", ev=>{
    ev.preventDefault();
    const now = Date.now();
    if(now - lastDown < 320){   // 双击：恢复默认宽并跳过本次拖拽
      lastDown = 0;
      // v1.0.636：不再在这里手抄一遍默认宽度——抹掉行内值与记忆，让 css 的
      // var(--sbw,192px) 成为唯一事实源（原来 JS/CSS 各写一个 168，改一处就漂一处）。
      document.documentElement.style.removeProperty("--sbw");
      try{ localStorage.removeItem("sbw"); }catch(e){}
      return;
    }
    lastDown = now;
    const sidebar=document.getElementById("sidebar");
    if(!sidebar) return;
    const startW=sidebar.offsetWidth, x0=ev.clientX;
    document.body.classList.add("sb-resizing");
    h.classList.add("on");
    const move=e=>{
      const w2=Math.min(420, Math.max(150, startW+(e.clientX-x0)));
      document.documentElement.style.setProperty("--sbw", w2+"px");
    };
    const up=()=>{
      document.removeEventListener("mousemove",move);
      document.removeEventListener("mouseup",up);
      document.body.classList.remove("sb-resizing");
      h.classList.remove("on");
      const sb=document.getElementById("sidebar");
      if(sb) lsSet("sbw", String(sb.offsetWidth));
    };
    document.addEventListener("mousemove",move);
    document.addEventListener("mouseup",up);
  });
}

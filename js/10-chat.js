const LONG_TEXT_FLAT = 8000;   // v1.0.471（UI#3）：20000→8000——段落级增量渲染兜底阈值
const __tw = {};                // paneId -> {el, target, shown, timer}
// v1.0.341 智能跟随：仅当用户本就停在底部附近（±90px）时才强制滚到底；
// 流式输出中途上翻阅读历史时不再被拽回底部，翻回底部自动恢复跟随。
//
// ══ v1.0.615 根因修复：以下整套跟随/回到最新必须盯着「真正的滚动容器」══
// `.conv-view` 自 v1.0.495 起**刻意**不做滚动容器（css/style.css:1103：它的高度=内容高度，
// 永远滚不动，滚动条在宿主 #opPane 上——那是为了修「overscroll-behavior:contain 吞掉滚轮」）。
// 但 __nearBottom/__follow/「↓ 回到最新」/程序化滚底全都拿 cv_<key> 量，于是：
//   · 距离恒等于 0（实测 40 条消息把 pane 撑到 3172px，scrollHeight-scrollTop-clientHeight==0）
//     → 跟随永远算成「在底部」，上翻不再被承认；
//   · 按钮的显示条件永远不成立 → **翻上去以后没有回最新的入口**（用户本轮反馈）；
//   · `pane.scrollTop=pane.scrollHeight` 全成空写 → 新消息不会把视图带到最新。
function _scrEl(p){
  // 按 overflow 判定而非量高度：调用点就在滚动逻辑里，读 scrollHeight 会强制布局。
  if(!p || !p.closest) return p || null;
  const scrollable = (el) => {
    if(el.nodeType !== 1) return false;
    const c = getComputedStyle(el);
    return c.overflowY === "auto" || c.overflowY === "scroll";
  };
  if(scrollable(p)) return p;                     // #setChatPane / #opPane 自己就是容器
  let a = p.parentElement, guard = 0;
  while(a && guard++ < 25){ if(scrollable(a)) return a; a = a.parentElement; }
  return document.scrollingElement || p;          // 兜底：整页滚动
}
function __dist(p){                               // 距底部多少 px（一律按滚动容器量）
  const sc = _scrEl(p); if(!sc) return 0;
  return sc.scrollHeight - sc.scrollTop - sc.clientHeight;
}
function __nearBottom(paneId){
  const pane=_paneEl(paneId); if(!pane) return true;
  return __dist(pane) < 90;
}
// v1.0.594 跟读改成「用户意图」判定，不再用瞬时距离。
// 旧判据 `scrollHeight - scrollTop - clientHeight < 90` 有个致命场景：流式期间内容
// 一次性长高几百 px（工具卡出现、推理框展开、表格整块渲染）时，这个差值瞬间就超过 90，
// 于是被误判成「用户上翻了」——从此再也不跟随，用户只能自己滚轮往下找最新回复。
// 现在：只有【用户真的滚了】才改跟随状态（程序滚动期间用 __prog 屏蔽那一次 scroll 事件），
// 停回底部 24px 内自动恢复跟随；发消息/继续任务时强制跟随。
const __follow = {};              // paneId -> true/false（undefined=还没判定过）
// 全部按 _normKey 归一后再存：'opPane' 只是历史兼容名，真实容器是 scratch / conv_<id>。
// 不归一就会重演 v1.0.590b 那类 id 错位——写入用 'opPane'、读取用 'conv_x'，跟随状态永远读不到。
function __fk(paneId){ return _normKey(paneId); }
function __visibleKey(sc){
  // 工作台一个宿主装多个会话容器，同一时刻只有一个可见（_showPane 把其余设成 display:none）；
  // 滚动事件发生在宿主上，得把「用户滚了」记到当前那个会话的跟随状态上。
  if(sc.dataset && sc.dataset.jumpkey) return sc.dataset.jumpkey;   // 自身即容器（#setChatPane）
  const kids = [...sc.children].filter(x => x.dataset && x.dataset.panekey && x.offsetParent !== null);
  return kids.length ? kids[kids.length - 1].dataset.panekey : null;
}
function __bindStickEl(p, k){
  // 直接收元素、不再回头调 _paneEl：否则 _paneEl 里挂这个绑定会互相递归。
  // 由 _paneEl 在返回容器时调用，保证「容器一出现就有监听器」——
  // 拖到第一次查询才绑会漏掉用户第一次滚动，跟随状态就一直停在 undefined。
  if(!p || p.__stickBound) return;
  p.__stickBound = true;
  const sc = _scrEl(p); if(!sc) return;
  if(sc === p && k) p.dataset.jumpkey = k;
  if(sc.__scrBound){ __syncJump(sc); return; }
  sc.__scrBound = true;
  __jumpEl(sc);
  sc.addEventListener('scroll', function(){
    if(sc.__prog){ sc.__prog=false; return; }                  // 我们自己滚的，不算用户意图
    const d = sc.scrollHeight - sc.scrollTop - sc.clientHeight;
    const kk = __visibleKey(sc);
    if(kk) __follow[__fk(kk)] = d < 24;                          // 回到底部→继续跟；离开→停
    __syncJump(sc);
  }, {passive:true});
  __syncJump(sc);
}
// v1.0.595「↓ 回到最新」：用户翻上去看历史之后，跟随会停下（这是刻意的，见上），
// 但回到最新处就得手动滚很久。按钮挂在**滚动容器**末尾，离开底部 240px 以上才出现，
// 点一下回到底部并重新开启跟随。用 sticky 而不是 absolute，是为了不必给容器加
// position:relative（那会牵连一批既有绝对定位规则的锚点）。
function __jumpEl(sc){
  if(sc.__jump && sc.__jump.isConnected) return sc.__jump;
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'jump-latest'; b.textContent = '↓ 回到最新';
  b.title = '跳到对话最新处，并恢复自动跟随';
  b.setAttribute('aria-label', '跳到对话最新处');
  b.style.display = 'none';
  b.addEventListener('click', function(e){
    e.stopPropagation();
    const kk = __visibleKey(sc);
    if(kk) __follow[__fk(kk)] = true;
    __scrollBottom(kk || sc.id || 'opPane', true);
  });
  sc.appendChild(b); sc.__jump = b; return b;
}
function __syncJump(sc){
  if(!sc) return;
  // 自愈：clearChat / 恢复历史会整片重写容器的 innerHTML，按钮被连带抹掉，而绑定是一次性的
  // （p.__stickBound 已置）→ 不重建就那个面版永远再无回最新入口。这里发现掉线就补回。
  let b = sc.__jump;
  if(!b || !b.isConnected) b = __jumpEl(sc);
  if(!b) return;
  const show = (sc.scrollHeight - sc.scrollTop - sc.clientHeight) > 240;
  if(show){
    // sticky 只在元素自身流位置低于视口底时才"贴住"。容器末尾可能被新消息追在后面，
    // 所以每次要显示前把它挪到末尾（已在末尾时不重复 append，避免每帧 DOM 抖动）。
    if(sc.lastElementChild !== b) sc.appendChild(b);
    b.style.display = '';
  }else{
    b.style.display = 'none';
  }
}
function __bindStick(paneId){ const k=__fk(paneId); __bindStickEl(_paneEl(k), k); }
function __stick(paneId){
  const k=__fk(paneId);
  __bindStick(k);
  if(__follow[k] === undefined) return __nearBottom(k);   // 首次：沿用老判据
  return !!__follow[k];
}
function __scrollBottom(paneId, force){
  const k=__fk(paneId), p=_paneEl(k); if(!p) return;
  const sc=_scrEl(p); if(!sc) return;
  if(!force && !__stick(k)){ __syncJump(sc); return; }
  sc.__prog = true;
  sc.scrollTop = sc.scrollHeight;
  __syncJump(sc);
  // 已经贴在底部时赋值不触发 scroll 事件，__prog 会一直挂着、把用户下一次滚动误吞一次；
  // 补一个超时兜底清掉。
  setTimeout(function(){ sc.__prog = false; }, 120);
}
// ===== v1.0.616：确认弹窗排队（多会话并行不再互相吞） =====
// 四类弹窗此前都写成 `if(document.getElementById("<全局 id>")) return;`——同一时刻只认
// 一个：A 会话的弹窗没关时，B 会话推来的 approval_request / ask_user / 技能安装请求
// 被**静默丢弃**，B 在后端永久等待，用户只看到"那个任务卡住了"。
// 现在：同类型已有弹窗就先入队并明说"已排队 N 个"，前一个节点被移除时自动出队下一个。
const __modalQ = { skillReq: [], skillSrc: [], appr: [], askUser: [] };
const __MODAL_HOST = { skillReq: "skillReqModal", skillSrc: "skillSrcModal",
                       appr: "apprModal", askUser: "askUserModal" };
const __MODAL_OPEN = { skillReq: null, skillSrc: null, appr: null, askUser: null };
function modalBusy(kind, paneId, evt, opts){
  // true = 已入队（调用方直接 return）；false = 当前没占用，可以继续弹
  if(!document.getElementById(__MODAL_HOST[kind])) return false;
  __modalQ[kind].push({paneId: paneId, evt: evt, opts: opts});
  toast("已排队 " + __modalQ[kind].length + " 个待确认（当前那个处理完会自动弹出）", "warn", 2600);
  return true;
}
function _drainModals(){
  for(const kind of Object.keys(__modalQ)){
    if(!__modalQ[kind].length) continue;                 // 队列空 → 不查 DOM（流式期间每帧都进这里）
    if(document.getElementById(__MODAL_HOST[kind])) continue;
    const it = __modalQ[kind].shift();
    const fn = __MODAL_OPEN[kind];
    if(fn) fn(it.paneId, it.evt, it.opts);
  }
}
if(document.body){
  try{ new MutationObserver(_drainModals).observe(document.body, {childList:true, subtree:true}); }catch(e){}
}

// v1.0.471（UI#3）：段落级增量渲染状态——paneId -> {el, doneHtml, doneLen}
const __tw_pc = {};
// 找最后一个「代码围栏外」的段落分隔（\n\n）位置，返回其后内容的起点下标；-1 表示没有。
// 围栏内可能有空行（代码空行），直接按 \n\n 切会破坏 ``` 代码块的富渲染。
function __last_safe_break(text){
  let inFence = false, pos = -1, offset = 0;
  const lines = text.split("\n");
  for(let i=0;i<lines.length;i++){
    const ln = lines[i];
    if(/^\s*```/.test(ln)) inFence = !inFence;
    if(!inFence && ln === "" && offset > 0) pos = offset;   // 空行后的起点
    offset += ln.length + 1;
  }
  return pos;
}
function __tw_render(st, paneId){
  const mc = st.el && st.el.querySelector(".mc");
  if(!mc) return;
  const shownText = st.target.slice(0, st.shown);
  const _rb = mc.querySelector(".reasoning-box");
  const _tm = mc.querySelector(".tm");
  if(st.target.length > LONG_TEXT_FLAT){ mc.textContent = shownText; }
  else {
    // v1.0.346 流式光标：内联插入到最后一个字符后（纯 CSS ::after 会落到块级元素下方，
    // 遇代码块时光标与正文脱节一行）。非流式态（补渲染/收尾）不插。
    const _caret = (st.el.classList && st.el.classList.contains("streaming"))
      ? '<span class="type-caret"></span>' : "";
    // v1.0.471（UI#3）：段落级增量渲染——原每 33ms 对从头到已显示位置的全部文本重跑
    // renderRich（逐行正则+全文 DOM 重建），长回答时主线程持续高负载、滚动抖动。
    // 现缓存「已完成段落」的 HTML（围栏外段落渲染结果不再变化），每 tick 只对
    // 尾部进行中段落（通常 ≤ 数百字）跑 renderRich；流结束由 __tw_stop 清缓存。
    let pc = __tw_pc[paneId];
    if(!pc || pc.el !== st.el){ pc = __tw_pc[paneId] = {el: st.el, doneHtml: "", doneLen: 0}; }
    const brk = __last_safe_break(shownText);
    if(brk > pc.doneLen){
      pc.doneHtml = renderRich(shownText.slice(0, brk));
      pc.doneLen = brk;
    } else if(brk >= 0 && brk < pc.doneLen){
      // 内容变短（新流/重发）→ 缓存失效重建
      pc.doneHtml = ""; pc.doneLen = 0;
    }
    if(pc.doneLen > 0){
      mc.innerHTML = pc.doneHtml + renderRich(shownText.slice(pc.doneLen)) + _caret;
    } else {
      mc.innerHTML = renderRich(shownText) + _caret;
    }
  }
  if(_rb) mc.prepend(_rb);
  if(_tm) mc.appendChild(_tm);
}
function __tw_set(paneId, el, target){
  let st = __tw[paneId];
  if(!st || st.el !== el){ __tw_stop(paneId); st = __tw[paneId] = {el: el, target: target, shown: 0, timer: null}; }
  st.target = target;
  if(st.shown > target.length) st.shown = 0;   // 新流（继续/重发）buf 从零重涨：揭示进度同步归零
  if(st.timer) return;
  st.timer = setInterval(function(){
    const remain = st.target.length - st.shown;
    if(remain <= 0) return;                                // 已追平，等下一个 token
    // v1.0.346 吐字曲线调优：旧 /10 且下限 1——大 buffer 首帧喷几百字（闪现感）、
    // 尾段每 tick 爬 1 字（拖尾感）。新曲线 clamp(3, remain/6, 120)：
    // 5K 字约 1.4s 匀速吐完，尾段 ≥3 字/tick 不拖，单 tick 上限 120 字防大段闪现。
    const _step = Math.min(120, Math.max(3, Math.round(remain / 6)));
    st.shown = Math.min(st.target.length, st.shown + _step);
    __tw_render(st, paneId);
    __scrollBottom(paneId);
  }, 33);
}
function __tw_stop(paneId){
  const st = __tw[paneId];
  if(!st) return;
  if(st.timer) clearInterval(st.timer);
  delete __tw[paneId];
  delete __tw_pc[paneId];   // v1.0.471（UI#3）：清段落渲染缓存
}

async function consumeStream(reader, paneId, agentEl, ctrl){
  let buf=""; let lastToolEl=null;
  // 看门狗：SSE 超过 120s 无任何数据（LLM 卡住/网络慢）时中断，避免 WebView2 默默断连报 network error（本地模型慢，已放宽）
  // v1.0.309：原 Promise.race 版 readTick 改为 interval+abort——abort 后挂起的 reader.read()
  // 会 reject AbortError，从 consumeSSE 冒出后被 catch，语义等价且与模板 run 的看门狗模式统一。
  const STALL_MS=120000;
  // P3-13：流式渲染相关魔法值统一为具名常量（v1.0.308：LONG_TEXT_FLAT 已提升到模块级供打字机共用；
  // token 渲染改为打字机队列，RENDER_THROTTLE_MS/_lastRender 节流机制随之废弃）
  // v1.0.645：工具结果摘要的 400 字上限（原 TOOL_SUMMARY_MAX）跟着 applyToolResult 一起搬到 js/09。
  let _stalled=false;
  // 2026-09-10：finished 标记「终止事件已处理」——done/error/need_continue/aborted 任一
  // 分支把最终 UI 写好后置 true。runStream 据此判断：finished 时绝不能再 innerHTML 覆写
  // .mc（否则会把推理折叠框/错误提示/「已停止」标记冲掉），仅在「无终止事件即关闭」时兜底。
  let _finished=false;
  let lastData=Date.now();
  const watchdog=setInterval(()=>{
    if(Date.now()-lastData>STALL_MS){
      _stalled=true; try{ctrl.abort();}catch(e){}
      // P1-3：SSE 看门狗中断时同步通知后端停止该 pane 的生成，避免后端空耗 LLM 额度与 CPU
      try{ postJson("/api/chat/abort",{pane_id:paneId}).catch(()=>{}); }catch(e){}
    }
  },3000);
  // v1.0.481：等待期逐秒反馈——模型首包（token/工具/错误）到达前，状态条显示已等待秒数。
  // 基准用流起始时刻（lastData 会被 8s 心跳不断刷新，不能用作计时基准）。
  let _firstData=false;
  const _waitT0=Date.now();
  const _clearWait=()=>{ if(!_firstData){ _firstData=true; clearInterval(waitTimer); } };
  const waitTimer=setInterval(()=>{
    if(_firstData) return;
    const s=Math.round((Date.now()-_waitT0)/1000);
    if(s>=2) setChatState(paneId,"⏳ 模型响应中… "+s+"s（推理模型先思考几分钟属正常；可点 ⏹ 停止）","on");
  },1000);
  const onEvent=(j)=>{
      // v1.0.479：首个实质事件（token/工具/推理/错误/终止）到达 → 停止等待计时。
      // meta 不算（plan 模式在 LLM 调用前就发 meta，真正的等待在其后）。
      if(j.type!=="meta") _clearWait();
      if(j.type==="meta"){
        // v1.0.367：rounds=0 = 新一次运行开始（loop.py 每次入口 yield）→ 重置 chip 计数/状态，
        // 上一任务的「✅ 完成」不再残留到新任务
        if(j.rounds===0) __chipState[paneId]={ops:0,phase:"think",label:""};
        updateModelChip(paneId,j);
      }
      else if(j.type==="skill_request"){ showSkillRequestModal(paneId, j); }
      else if(j.type==="skill_source_request"){ showSkillSourceModal(paneId, j); }
      else if(j.type==="ask_user"){ showAskUserModal(paneId, j); }
      else if(j.type==="approval_request"){ showApprovalModal(paneId, j); }   // v1.0.498 修复：审批弹窗断链——后端事件此前无前端分支，弹窗永不出现
      else if(j.type==="approval_denied"){ toast("🛑 "+(j.reason||"该操作被拒绝（不可撤销且无弹窗可确认）"),"warn",4500); }   // v1.0.616：后台拒绝要看得见
      else if(j.type==="task_progress"){
        // v1.0.373（体验A1）：批量任务进度——「已完成 N 项」实时状态
        setChatState(paneId, "✅ 已完成 "+(j.done||0)+" 项，继续处理中…", "on");
      }
      else if(j.type==="task_calls"){
        // v1.0.373（体验C9）：任务成功完成的工具序列——气泡操作栏可「💾 存为宏」
        __lastCalls[paneId] = j.calls || [];
      }
      else if(j.type==="usage"){
        const m=__metaState[paneId]||{}; m.usage=j.usage; updateModelChip(paneId,m);
        // v1.0.650：这里原本还自计一份 completion_tokens 供气泡统计条用——
        // 统计条现在整条改由 done 收据生成（见 applyRunStat），自计的那份删掉。
      }
      else if(j.type==="token"){
        buf+=j.text;
        // v1.0.366（轮次展示替换）：token 流 = 模型思考阶段——chip 从"正在执行工具"切回
        // "模型思考中"（仅状态切换时刷新，不逐 token 重绘）
        const cs=__chipState[paneId];
        if(cs && cs.phase!=="think"){ cs.phase="think"; updateModelChip(paneId, __metaState[paneId]); }
        // v1.0.308 打字机：只更新目标缓冲，由 33ms tick 匀速揭示（网络包成块到达也不再顿挫）；
        // 推理折叠框/.tm 保留逻辑在 __tw_render 内。
        __tw_set(paneId, agentEl, stripFinishMark(buf));
        agentEl.classList.remove("typing"); agentEl.classList.add("streaming");
        __scrollBottom(paneId);
      }
      else if(j.type==="tool"){
        lastToolEl=addToolMsg(paneId, j.name, j.args, agentEl); pushHistory(paneId,{role:"tool",text:"🔧 "+j.name+"\n"+JSON.stringify(j.args)});
        // v1.0.366（轮次展示替换）：工具事件驱动 chip 显示真实状态——正在执行哪个工具
        const cs=__chipState[paneId]||(__chipState[paneId]={ops:0,phase:"tool",label:""});
        cs.ops=(cs.ops||0)+1; cs.phase="tool"; cs.label=_tool_label(j.name, j.args);
        updateModelChip(paneId, __metaState[paneId]);
      }
      else if(j.type==="tool_result"){
        // v1.0.645：状态图标/结果摘要/必填徽标/产物文件行整套落到 js/09 的 applyToolResult——
        // 历史重放要画一模一样的卡，只能共用同一个函数（各自一份必然漂）。
        applyToolResult(lastToolEl, j);
      }
      else if(j.type==="reasoning"){
        // 推理模型思考流（Qwen3.8-27B 等先吐 reasoning 再吐 content）：
        // 默认折叠——summary 只显示「🧠 推理中…」让用户知道没卡死；点击才展开看尾部片段。
        // 不进 buf（非最终回答）
        if(!__reasonBuf[paneId]) __reasonT0[paneId]=Date.now();   // v1.0.573：思考计时起点
        __reasonBuf[paneId]=(__reasonBuf[paneId]??"")+j.text;
        const rBox=agentEl.querySelector(".reasoning-box")||createReasoningBox(agentEl);
        const len=__reasonBuf[paneId].length;
        // v1.0.573：思考超过 90s 给出可行动提示（用户此前只会看到"卡在思考环节"）
        const thinkS=Math.round((Date.now()-(__reasonT0[paneId]||Date.now()))/1000);
        const late=(thinkS>=90)?" ⏱ 思考较久（"+thinkS+"s）——可点「停止」或换模型":"";
        rBox.querySelector(".reasoning-summary").textContent="🧠 推理中…（已接收 "+(len>1000?(len/1000).toFixed(1)+"k":len)+" 字，点击展开）"+late;
        rBox.querySelector(".reasoning-tail").textContent=__reasonBuf[paneId].slice(-400);
        agentEl.classList.add("typing");
        // v1.0.590 修复（P0：思考模型在会话任务里回复永远不显示）：这里原来写
        // `$(paneId).scrollTop=...`，而会话任务的真实容器 id 是 `cv_<paneId>`（见 _paneEl），
        // `$(paneId)` 取到 null → 读 .scrollTop 抛 TypeError → 整个 SSE 读取中断，
        // 表现为气泡永久停在「🧠 推理中…（已接收 N 字）正在思考」，而后端照常跑完并弹
        // 「✅ 任务完成」通知（通知用的是服务端文本，不走这条渲染路径）。
        // 不思考的模型不进本分支，所以只有思考模型复现——线上实测 qwen3.6 / DaoPro 均中招。
        // 统一走 _paneEl + 空值保护，与 token 分支的写法一致。
        __scrollBottom(paneId);
      }
      else if(j.type==="skill_suggest"){
        // v1.0.583（用户反馈：流内卡片不醒目，改弹窗选择）：相似任务检测命中 →
        // 模态弹窗（软件同款风格），选择经 /api/skills/auto_create 回传。
        showSkillSuggest(j);
      }
      else if(j.type==="error"){
        _finished=true; __tw_stop(paneId);
        const t="错误："+j.text; addMsg(paneId,"err",t,"err"); agentEl.classList.remove("typing"); agentEl.classList.remove("streaming");
        const _tmErr=agentEl.querySelector(".mc .tm");
        setChatState(paneId,"❌ 出错（可点「继续」重试）","err");
        // v1.0.367：终止事件同步 chip 终态——回答结束后不再停留「思考中」
        const csE=__chipState[paneId]; if(csE){ csE.phase="err"; updateModelChip(paneId, __metaState[paneId]); }
        // 修复（2026-08-27）：error 分支此前无「继续」入口——后端已保存断点，
        // 与 need_continue 分支对齐，提供可点击的恢复按钮
        let btn=$("btnCont_"+paneId);
        if(!btn){
          btn=document.createElement("button"); btn.className="btn"; btn.id="btnCont_"+paneId;
          btn.textContent="继续"; btn.onclick=()=>continueChat(paneId);
          agentEl.querySelector(".mc").innerHTML=escapeHtml(j.text||"")+"<br>"+btn;
          if(_tmErr) agentEl.querySelector(".mc").appendChild(_tmErr);
        } else { btn.disabled=false; btn.textContent="继续"; }
        pushHistory(paneId,{role:"err",text:t});
      }
      else if(j.type==="need_continue"){
        _finished=true; __tw_stop(paneId);
        agentEl.classList.remove("typing"); agentEl.classList.remove("streaming");
        const _tmNc=agentEl.querySelector(".mc .tm");
        agentEl.querySelector(".mc").innerHTML=escapeHtml(j.text)+' <button class="ghost smbtn" id="btnCont_'+paneId+'" onclick="continueChat(\''+paneId+'\')">继续</button>';
        if(_tmNc) agentEl.querySelector(".mc").appendChild(_tmNc);
        pushHistory(paneId,{role:"err",text:j.text});
        // P3-2：状态置为「可继续」，与「继续」按钮语义一致，避免误以为任务已完成
        setChatState(paneId,"⏸ 已达轮次上限，可继续","off");
        const csN=__chipState[paneId]; if(csN){ csN.phase="paused"; updateModelChip(paneId, __metaState[paneId]); }   // v1.0.367
        __needContinueState[paneId]=true;
        return {needContinue:true, buf:buf};   // → earlyRet（保持原提前返回语义）
      }
      else if(j.type==="done"){
        _finished=true; __tw_stop(paneId);
        agentEl.classList.remove("typing"); agentEl.classList.remove("streaming");
        // 保留推理折叠框（可展开复盘思考过程）与 .tm 时间戳；清空缓冲防跨轮累积
        const _mcD=agentEl.querySelector(".mc");
        const _rb=_mcD.querySelector(".reasoning-box");
        const _tm=_mcD.querySelector(".tm");
        if(buf){ const _disp=stripFinishMark(buf);
                 _mcD.innerHTML=renderRich(_disp); agentEl.dataset.raw=_disp; }   // v1.0.334：raw 同步最终文本（复制/编辑用）
        if(_rb && buf) _mcD.prepend(_rb);
        if(_rb) _rb.querySelector(".reasoning-summary").textContent="🧠 推理过程（点击展开）";
        if(_tm) _mcD.appendChild(_tm);
        // v1.0.627：正文里模型写出的「产物库/产出/x.md」原地变可点按钮（收尾后做，
        // 不打断流式；失败只少个入口，不影响回答显示）
        if(buf){ try{ linkifyWorkspacePaths(_mcD); }catch(e){} }
        __reasonBuf[paneId]="";
        // v1.0.367：done = 整个 agent 任务结束（loop.py 仅此处 yield 一次，带 summary 收据）→
        // chip 切「✅ 完成」终态并展示真实统计（操作次数/耗时/累计 token），修复「回答完了还显示思考中」
        const csD=__chipState[paneId]||(__chipState[paneId]={ops:0,phase:"think",label:""});
        csD.phase="done"; csD.summary=j.summary||null;
        updateModelChip(paneId, __metaState[paneId]);
      }
      else if(j.type==="aborted"){
        _finished=true; __tw_stop(paneId);
        const t=(buf?stripFinishMark(buf)+"\n\n":"")+"（已停止）";
        const _mcA=agentEl.querySelector(".mc");
        const _rb=_mcA.querySelector(".reasoning-box");
        const _tm=_mcA.querySelector(".tm");
        _mcA.innerHTML=renderRich(t); agentEl.dataset.raw=buf?stripFinishMark(buf):t;   // v1.0.334：raw 存已生成内容
        if(_rb) _mcA.prepend(_rb);
        if(_rb) _rb.querySelector(".reasoning-summary").textContent="🧠 推理过程（点击展开）";
        if(_tm) _mcA.appendChild(_tm);
        agentEl.classList.remove("typing"); agentEl.classList.remove("streaming"); setChatState(paneId,"⏹ 已停止","off"); pushHistory(paneId,{role:"agent",text:t}); __reasonBuf[paneId]="";
        const csA=__chipState[paneId]; if(csA){ csA.phase="stopped"; updateModelChip(paneId, __metaState[paneId]); }   // v1.0.367
        return {done:true, buf:buf, finished:true};   // → earlyRet
      }
    };   // ← onEvent 回调体结束
    let earlyRet=null;
    try{
      const rr=await consumeSSE(reader, onEvent, ()=>{ lastData=Date.now(); if(ctrl) ctrl._t=Date.now(); });
      if(rr && rr.early) earlyRet=rr.early;
    }catch(e){
      if(e.name!=="AbortError"){ clearInterval(watchdog); clearInterval(waitTimer); throw e; }
      // v1.0.500（用户实测）：主动点停止 → abort() 掐断流，后端 aborted 事件收不到，
      // catch 又静默吞掉 → phase 停在 "think"，chip 永远「思考中」。无条件补终态
      // （__chipState 可能尚未初始化——meta rounds!=0 时不会建对象，不能 if(cs) 判空跳过）。
      __chipState[paneId]=Object.assign({ops:0}, __chipState[paneId], {phase:"stopped"});
      updateModelChip(paneId, __metaState[paneId]);
    }
    clearInterval(watchdog); clearInterval(waitTimer); _firstData=true;
    if(earlyRet) return earlyRet;   // need_continue / aborted 的精确保留
    return {done:true, buf:buf, stalled:_stalled, finished:_finished};
  }

// 上传文件给 agent 分析：存到产物库/上传
// v1.0.370 附件 chip 化（对齐主流 AI 输入框）：拖入/上传的文件显示为输入框上方可删除胶囊，
// 不再把「📎 已上传：xxx」文本塞进输入框；发送时统一注入上下文。
const __attaches={};   // inputId -> [{type:"path"|"upload", path, name, isDir?, kind?, preview?, files?}]
function _att(iid){ return __attaches[iid]||(__attaches[iid]=[]); }
function addAttach(iid,a){ _att(iid).push(a); renderAttaches(iid); }
function removeAttach(iid,idx){ _att(iid).splice(idx,1); renderAttaches(iid); }
function renderAttaches(iid){
  const strip=$(iid==="opInput"?"attachStrip_op":"attachStrip_set"); if(!strip) return;
  const list=_att(iid);
  strip.style.display=list.length?"flex":"none";
  strip.innerHTML="";
  list.forEach((a,i)=>{
    const el=document.createElement("span"); el.className="attach-chip"; el.title=a.path||a.name||"附件";
    const ic=document.createElement("span"); ic.textContent=a.isDir?"📁":"📄";
    const nm=document.createElement("span"); nm.className="ac-name";
    nm.textContent=a.name+(a.files?("（"+a.files.length+" 个文件）"):"");
    const x=document.createElement("button"); x.className="ac-x"; x.textContent="×"; x.title="移除";
    x.onclick=()=>removeAttach(iid,i);
    el.appendChild(ic); el.appendChild(nm); el.appendChild(x); strip.appendChild(el);
  });
}
function pickFile(inputId){
  if(!__fileInput){ const fi=document.createElement("input"); fi.type="file"; fi.style.display="none";
    fi.addEventListener("change", async ()=>{ const f=fi.files && fi.files[0]; fi.value="";
      if(!f){ return; }
      const fd=new FormData(); fd.append("file", f);
      try{
        const r=await fetch("/api/workspace/upload",{method:"POST",body:fd});
        const d=await r.json();
        if(!d.ok){ toast("上传失败："+(d.error||"未知错误"),"err"); return; }
        addAttach(inputId,{type:"upload", path:d.path, name:d.filename, kind:d.kind, preview:d.preview||""});
        toast("已上传："+d.filename,"ok",1800);
      }catch(e){ toast("上传失败："+e.message,"err"); }
    });
    document.body.appendChild(fi); __fileInput=fi;
  }
  __fileInput.click();
}
let __fileInput=null;

// ---- v1.0.369/370 拖拽文件/文件夹到聊天框 ----
// 主通道（pywebview 桌面环境）：Python 侧 DOM 事件桥拿 pywebviewFullPath（完整绝对路径，
// WebView2 的 JS File 对象没有 path）→ evaluate_js 推回 window.__nativeDrop(paths)
// → 登记只读白名单 → 附件 chip（只引用不复制，agent 直接分析原路径）。
// 兜底通道（浏览器预览等拿不到路径的场景）：webkitGetAsEntry 读内容上传（文件夹聚合 chip）。
const _DND_MAX_FILES=50, _DND_MAX_SIZE=10*1024*1024, _DND_MAX_DEPTH=3;
let __dndReady=false, __dndLastInput="opInput";
function initChatDnd(){
  if(__dndReady) return; __dndReady=true;
  // 全局防默认行为：WebView2 拖入文件默认会直接导航打开该文件，必须拦掉 dragover+drop 默认
  ["dragover","drop"].forEach(ev=>document.addEventListener(ev,(e)=>{ e.preventDefault(); },false));
  // v1.0.373（P3-11）：拖到聊天窗格以外时给明确反馈（默认行为仍拦——防 WebView 导航打开文件）
  document.addEventListener("drop",(e)=>{
    if(e.target && e.target.closest && e.target.closest(".chatpane")) return;
    toast("请把文件拖到上方的对话区域（松开在聊天窗格内）","warn");
  },false);
  [["opPane","opInput"],["setChatPane","setChatInput"]].forEach(([pid,iid])=>{
    const pane=$(pid); if(!pane) return;
    pane.addEventListener("dragenter",()=>{ __dndLastInput=iid; pane.classList.add("dnd-hover"); });
    pane.addEventListener("dragover",()=>{ __dndLastInput=iid; pane.classList.add("dnd-hover"); });
    pane.addEventListener("dragleave",(e)=>{ if(!pane.contains(e.relatedTarget)) pane.classList.remove("dnd-hover"); });
    pane.addEventListener("drop",(e)=>{
      e.preventDefault(); pane.classList.remove("dnd-hover"); __dndLastInput=iid;
      if(window.__pywebviewNativeDrop) return;   // pywebview 环境：真实路径由 Python 桥推回（只引用不复制）
      try{ _handleDrop(e, iid); }catch(err){ toast("拖拽处理失败："+(err.message||err),"err"); }
    });
  });
}
// Python 桥（backend._bind_native_drop）把 pywebviewFullPath 推回这里：
// 登记只读白名单 → 附件 chip（零拷贝，agent 直接分析原路径）
// v1.0.405（H4 纵深防御）：改 Object.defineProperty 不可枚举+不可覆写——
// 页面注入的脚本无法重定义/枚举发现该入口（若未来出现 XSS 也抬高利用门槛）；
// 正常的 evaluate_js 调用不受影响（只读属性）。
Object.defineProperty(window, "__nativeDrop", {
  value: async function(paths){
    const iid=__dndLastInput||"opInput";
    try{
      const r=await fetch("/api/workspace/allow",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paths:paths})});
      const d=await r.json();
      const items=(d && d.items)||[];
      if(!items.length){ toast("无法识别拖入的本地路径","err"); return; }
      items.forEach(it=>addAttach(iid,{type:"path", path:it.path, name:it.name, isDir:!!it.is_dir}));
      toast("已添加 "+items.length+" 个路径（仅引用，不复制）","ok");
    }catch(e){ toast("登记拖入路径失败："+(e.message||e),"err"); }
  },
  writable:false, configurable:false, enumerable:false
});
async function _handleDrop(e, inputId){
  const dt=e.dataTransfer; if(!dt) return;
  const files=Array.from(dt.files||[]);
  // 兜底通道一：极少数环境 File 带 path（Electron 系）→ 同样走 chip 引用
  const pathHits=files.filter(f=>typeof f.path==="string" && f.path).map(f=>f.path);
  if(pathHits.length){
    try{ postJson("/api/workspace/allow",{paths:pathHits}).then(d=>{
      const items=(d&&d.items)||[];
      if(items.length){ items.forEach(it=>addAttach(inputId,{type:"path",path:it.path,name:it.name,isDir:!!it.is_dir})); toast("已添加 "+items.length+" 个路径（仅引用，不复制）","ok"); }
    }).catch(()=>{}); }catch(e){}
    return;
  }
  // 兜底通道二：webkitGetAsEntry 读内容上传（浏览器预览等拿不到真实路径的环境）
  const items=dt.items ? Array.from(dt.items) : [];
  const entries=items.map(it=>{ try{ return it.webkitGetAsEntry ? it.webkitGetAsEntry() : null; }catch(err){ return null; } }).filter(Boolean);
  if(!entries.length){
    if(files.length) toast("无法读取拖入内容的本地路径，请改用 📎 按钮上传","err");
    return;
  }
  const jobs=[]; let truncated=false; let dirCount=0;
  const markTrunc=()=>{ truncated=true; };
  for(const en of entries){
    if(en.isFile) jobs.push({entry:en, rel:en.name});
    else if(en.isDirectory){ dirCount++; await _collectDirEntries(en, en.name, jobs, markTrunc, 1); }
  }
  if(!jobs.length && !dirCount){ toast("拖入的内容无法读取","err"); return; }
  await _uploadDndJobs(jobs, inputId, truncated, dirCount>0);
}
async function _collectDirEntries(dirEntry, rel, jobs, onTrunc, depth){
  if(depth>_DND_MAX_DEPTH){ onTrunc(); return; }
  const reader=dirEntry.createReader();
  // 注意：readEntries 每次最多返回 100 条，必须循环读到空批次
  const readBatch=()=>new Promise(res=>{ try{ reader.readEntries(ents=>res(ents||[]), ()=>res([])); }catch(e){ res([]); } });
  let ents;
  do{
    ents=await readBatch();
    for(const en of ents){
      if(jobs.length>=_DND_MAX_FILES){ onTrunc(); return; }
      const childRel=rel+"/"+en.name;
      if(en.isFile) jobs.push({entry:en, rel:childRel});
      else if(en.isDirectory) await _collectDirEntries(en, childRel, jobs, onTrunc, depth+1);
    }
  }while(ents.length>0);
}
function _entryFile(entry){
  return new Promise(res=>{ try{ entry.file(f=>res(f), ()=>res(null)); }catch(e){ res(null); } });
}
async function _uploadDndJobs(jobs, inputId, truncated, hadDir){
  const ok=[]; let failed=0;
  for(const job of jobs.slice(0,_DND_MAX_FILES)){
    try{
      const f=await _entryFile(job.entry);
      if(!f || f.size>_DND_MAX_SIZE){ failed++; continue; }
      const fd=new FormData(); fd.append("file", f, job.rel.replace(/[\\/]+/g,"_"));
      const r=await fetch("/api/workspace/upload",{method:"POST",body:fd});
      const d=await r.json();
      // v1.0.373（P3-13）：保留 rel——聚合根目录用拖入文件夹名（原按文件名 `_` 切分，
      // 文件名本身含 `_` 时会错聚）
      if(d.ok) ok.push({path:d.path, filename:d.filename, kind:d.kind, preview:d.preview||"", rel:job.rel});
      else failed++;
    }catch(err){ failed++; }
  }
  const byRoot={};
  ok.forEach(p=>{
    const root=(p.rel.split("/")[0]||"dropped");
    (byRoot[root]=byRoot[root]||[]).push(p);
  });
  const roots=Object.keys(byRoot);
  if(hadDir && roots.length===1 && ok.length>1){
    addAttach(inputId,{type:"upload", name:roots[0], files:byRoot[roots[0]]});
  }else{
    ok.forEach(p=>addAttach(inputId,{type:"upload", path:p.path, name:p.filename, kind:p.kind, preview:p.preview}));
  }
  const note=(truncated||jobs.length>_DND_MAX_FILES)?("（内容较多仅取前 "+ok.length+" 个；桌面版拖拽可直接引用原文件夹，不限数量）"):"";
  toast("已添加 "+ok.length+" 个文件"+(failed?("，"+failed+" 个失败（过大或不可读）"):"")+note, ok.length?"ok":"err");
}
// 脚本可能晚于 DOMContentLoaded 加载，两种时机都兜住
if(document.readyState==="loading") document.addEventListener("DOMContentLoaded", initChatDnd);
else initChatDnd();

// ---- v1.0.373（体验C9）存为宏：取 __lastCalls[paneId] 命名保存 ----
function saveMacroPrompt(paneId){
  const calls = __lastCalls[paneId];
  if(!calls || !calls.length){ toast("本任务没有可保存的操作序列","err"); return; }
  // v1.0.471（UI#6）：原生 prompt → promptAsk 输入弹层
  promptAsk("保存任务宏", "给这个任务宏起个名字（如「每周财务审核」）：", "我的任务宏", (name)=>{
    fetch("/api/macro/save",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:name.trim(), calls:calls})})
      .then(r=>r.json())
      .then(d=>{
        if(!d.ok){ toast(d.error||"保存失败","err"); return; }
        toast("✅ 已保存宏（"+d.steps+" 步）——产物库页可一键重放","ok");
      })
      .catch(e=>toast(fetchErr(e,"保存宏"),"err"));
  }, "保存");
}

// ---- v1.0.373（体验B3）剪贴板截图直发：输入框内 Ctrl+V 粘贴截图 → 自动上传为附件 ----
// 图片上传后端自动 OCR（kind=image-ocr，preview 即识别文字），agent 直接从 preview 拿截图内容；
// 支持视觉的模型可用 read_workspace_file 读原图。零后端改动，复用附件 chip。
function initChatPaste(){
  ["opInput","setChatInput"].forEach(iid=>{
    const ta=$(iid); if(!ta || ta.dataset.pasteBound) return;
    ta.dataset.pasteBound="1";
    ta.addEventListener("paste", async (e)=>{
      const items=(e.clipboardData && e.clipboardData.items)||[];
      let img=null;
      for(const it of items){
        if(it.type && it.type.indexOf("image/")===0){ img=it.getAsFile(); break; }
      }
      if(!img) return;   // 文本粘贴走默认行为
      e.preventDefault();
      const fd=new FormData(); fd.append("file", img, "clipboard_"+Date.now()+".png");
      toast("截图上传中…","ok",1500);
      try{
        const r=await fetch("/api/workspace/upload",{method:"POST",body:fd});
        const d=await r.json();
        if(!d.ok){ toast("截图上传失败："+(d.error||"未知错误"),"err"); return; }
        addAttach(iid,{type:"upload", path:d.path, name:d.filename, kind:d.kind, preview:d.preview||""});
        toast("✅ 截图已添加为附件"+(d.kind==="image-ocr"?"（已自动识别图中文字）":""),"ok");
      }catch(err){ toast("截图上传失败："+err.message,"err"); }
    });
  });
}
document.addEventListener("DOMContentLoaded", initChatPaste);
if(document.readyState!=="loading") initChatPaste();

async function streamChat(url, body, paneId, inputId){
  // v1.0.327 会话：发消息时带上当前会话 id，后端据此拼上下文并落库
  if(window.__pendingConvId) body.conv_id = window.__pendingConvId;
  const input=$(inputId); const text=input.value.trim(); if(!text) return;
  // v1.0.370/373：附件 chip 队列（按输入框分组）——发送时①拼上下文消息给 LLM ②气泡里显示文件引用
  const atts=(__attaches[inputId]||[]).splice(0); renderAttaches(inputId);
  // 气泡展示用引用行（不影响发给 LLM 的上下文结构）
  const attachTags=atts.map(a=>{
    if(a.type==="path") return (a.isDir?"📁 ":"📄 ")+a.name+"（引用本地"+(a.isDir?"文件夹":"文件")+"，未复制）";
    if(a.files && a.files.length) return "📁 "+a.name+"（已镜像 "+a.files.length+" 个文件到产物库）";
    return "📄 "+a.name+"（已上传到产物库）";
  });
  if(atts.length){
    const parts=atts.map(a=>{
      if(a.type==="path"){
        return a.isDir
          ? "（我拖入了一个本地文件夹供你分析，已授权只读白名单）路径："+a.path+
            "。请先用 list_workspace_files(path=\""+a.path+"\") 列出内容，再按用户要求分析。"
          : "（我拖入了一个本地文件供你分析，已授权只读白名单）路径："+a.path+
            "。请用 read_workspace_file 读取该文件并按用户要求分析。";
      }
      if(a.files && a.files.length){
        const previews=a.files.map(f=>f.preview?("· "+f.path+" 预览：\n"+f.preview.slice(0,800)):"").filter(Boolean).join("\n\n");
        return "（我拖入了文件夹「"+a.name+"」，内容已镜像到产物库/上传，共 "+a.files.length+" 个文件）"+
          "文件清单："+a.files.map(f=>f.path).join("、")+
          "。请用 read_workspace_file 逐个读取并按用户要求分析。"+(previews?"\n\n部分文件内容预览：\n"+previews:"");
      }
      const preview=(a.preview||"").slice(0,1500);
      return "（我上传了一个文件供你分析）文件名："+a.name+
        "，已保存到产物库："+a.path+"（类型："+(a.kind||"unknown")+"）。请用 read_workspace_file 读取该文件并按用户要求分析。"+
        (preview?"\n\n文件内容预览（开头部分）：\n"+preview:"");
    });
    const fileMsg={role:"user", content:parts.join("\n\n---\n\n")};
    body.messages=[fileMsg].concat(body.messages||[]);
  }
  if(activeControllers[paneId]){ toast("正在生成中，请先停止","err");
    atts.forEach(a=>addAttach(inputId,a));   // 发送被拦截 → 附件退回 chip，不丢
    return; }
  // v1.0.373：用户气泡与聊天记录显示文件引用（输入框文本 + 附件引用行）
  const uiText=attachTags.length?(text+"\n\n"+attachTags.join("\n")):text;
  // v1.0.645：带附件那轮，气泡显示的是「输入 + 附件引用行」，而喂给模型的是另一套拼装。
  // 把界面看到的那份一起送后端存成 display，重开时用户气泡和当时一字不差。
  if(attachTags.length) body.ui_text = uiText;
  addMsg(paneId,"user",uiText); input.value=""; autoResize(input); input.focus();
  // v1.0.594：主动发起一轮 = 明确想看结果，强制恢复跟随（哪怕上一轮他翻上去过）
  __follow[__fk(paneId)] = true;
  pushHistory(paneId,{role:"user",text:uiText});
  const agentEl=addMsg(paneId,"agent","正在思考","agent typing");
  const ctrl=new AbortController(); ctrl._t=Date.now(); activeControllers[paneId]=ctrl; setStop(paneId,true);
  try{ agentEl.dataset.conv = __curConv || ""; }catch(e){}
  // P3-5：与 continueChat 共享流式处理（fetch→consumeStream→stalled/needContinue→finalizeChat）
  body.pane_id=_normKey(paneId);
  // v1.0.486：每任务独立模型——选择器值随请求发送（""=全局默认）
  const _mid=_modelSelValue(paneId);
  if(_mid) body.model_id=_mid;
  // v1.0.590：每任务独立权限级别（ask_every/auto/full），后端随断点持久化
  body.permission_level = permSelValue(paneId);
  body.thinking = thinkSelValue(paneId);   // v1.0.601 思考档位（""=跟随模型）
  await runStream(url, body, paneId, agentEl, ctrl);
}

// 推理流缓冲（按 pane 累积 delta.reasoning，默认折叠展示，不污染最终回答）
const __reasonBuf={};
const __reasonT0={};   // v1.0.573：思考开始时间（paneId -> ms），用于"思考较久"提示
function createReasoningBox(agentEl){
  // 2026-09-10 修复：此处原为 <div>，但 CSS 早已按 <details> 写就
  //（`.reasoning-box summary::-webkit-details-marker`、`.reasoning-box[open] summary::before`），
  // 导致「点击展开」点了没反应、reasoning-tail 永久铺开占位——与「默认折叠」的设计意图相悖。
  const box=document.createElement("details");
  box.className="reasoning-box";
  const sum=document.createElement("summary");
  sum.className="reasoning-summary";
  sum.textContent="🧠 推理中…（点击展开）";
  // v1.0.327 ②：展开后 summary 文案随状态切换（此前展开后仍显示「点击展开」）
  box.addEventListener("toggle", function(){
    if(sum.textContent.indexOf("推理中…") < 0){
      sum.textContent = box.open ? "🧠 推理过程（点击收起）" : "🧠 推理过程（点击展开）";
    }
  });
  const tail=document.createElement("div");
  tail.className="reasoning-tail";
  box.appendChild(sum); box.appendChild(tail);
  agentEl.querySelector(".mc").prepend(box);
  return box;
}
// P3-5：公共流式执行器，供 streamChat / continueChat 复用，消除重复逻辑
// v1.0.373（体验A2）：__lastRun 记录本次请求（url/body/agentEl）——失败终态挂「🔄 重试」
const __lastRun={};
const __lastCalls={};   // v1.0.373（体验C9）：paneId -> 最近成功任务的工具调用序列
function attachRetryBtn(agentEl, paneId){
  const mc=agentEl.querySelector(".mc"); if(!mc || mc.querySelector(".retry-run")) return;
  const b=document.createElement("button"); b.className="ghost smbtn retry-run";
  b.textContent="🔄 重试本次请求";
  b.onclick=()=>{ b.remove(); retryLastRun(paneId); };
  mc.appendChild(b);
}
async function retryLastRun(paneId){
  const lr=__lastRun[paneId]; if(!lr){ toast("没有可重试的请求","err"); return; }
  if(activeControllers[paneId]){ toast("正在生成中，请先停止","err"); return; }
  const el=lr.agentEl;
  if(el && el.querySelector(".mc")){
    el.classList.add("typing"); el.classList.remove("streaming");
    el.querySelector(".mc").innerHTML="正在思考";
  }
  const ctrl=new AbortController(); ctrl._t=Date.now(); activeControllers[paneId]=ctrl; setStop(paneId,true);
  try{ await runStream(lr.url, lr.body, paneId, el, ctrl); }
  catch(e){ toast(fetchErr(e,"重试"),"err"); }
}
async function runStream(url, body, paneId, agentEl, ctrl){
  __lastRun[paneId]={url:url, body:body, agentEl:agentEl};   // v1.0.373（体验A2）重试依据
  // P1-3: SSE断流指数退避重连（最多3次）+ stall时自动 /continue续跑
  const MAX_RETRIES = 3;
  let retryCount = 0;

  while (true) {
    // v1.0.590：result 必须声明在 try 外——下方 catch 分支要用 result.buf 拼接
    // 「连接中断，N 秒后自动重试」的提示。原来声明在 try 内（const result），catch 里
    // 引用会抛 ReferenceError: result is not defined，于是任何流内异常都会被这个二次
    // 异常盖掉并直接冲出 runStream：自动重试 UI 永远不显示、finalizeChat 不执行、
    // typing 与状态条全部残留。真实根因（如上面的 $(paneId) TypeError）反而看不到。
    let result = null;
    try {
      const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                                   body:JSON.stringify(body), signal:ctrl.signal});
      if (!r.ok) {
        let msg = "HTTP " + r.status;
        if (url.endsWith("/continue") && r.status===400) msg="没有可继续的对话（断点不存在或已失效），请重新发起对话";
        else if (r.status===404) msg="接口不存在（HTTP 404），请检查后端版本";
        agentEl.querySelector(".mc").textContent=msg; agentEl.classList.remove("typing");
        attachRetryBtn(agentEl, paneId);   // v1.0.373（体验A2）
        finalizeChat(paneId); return;
      }
      result = await consumeStream(r.body.getReader(), paneId, agentEl, ctrl);
      if (result.stalled) {
        // P1-3: stall(120s无输出)且首次时，自动尝试 /continue续跑
        if (retryCount === 0 && !url.endsWith("/continue") && result.buf) {
          retryCount = 1;
          url = "/api/chat/continue";
          continue;
        }
        const t = (result.buf ? result.buf+"\n\n" : "") + "⚠️ 响应中断：超过 2 分钟无任何输出（推理模型长思考或网络异常）。已保留已生成内容，可点「继续」重试。";
        agentEl.querySelector(".mc").innerHTML = renderRich(t); agentEl.classList.remove("typing");
        attachRetryBtn(agentEl, paneId);   // v1.0.373（体验A2）
        setChatState(paneId,"⏸ 已中断","off");
        pushHistory(paneId,{role:"agent",text:t});
        finalizeChat(paneId); return;
      }
      if (!result.needContinue) {
        // 2026-09-10 修复（聊天界面「每字一行」怪象根治）：
        // ① 收尾无条件移除 typing——此前仅 done/error/aborted 分支移除，当流在「推理帧之后」
        //    被后端裸异常/断流终止（无终止事件）时 typing 残留，`.msg.agent.typing .mc` 的
        //    inline-flex 让气泡塌成窄列、每字一行、头像区被挤掉。
        // ② 仅当「无终止事件即关闭」（finished=false）才兜底渲染 buf；finished 时
        //    done/error/aborted 分支已写好最终 UI（推理折叠框/错误提示/「已停止」标记），
        //    此处再 innerHTML 覆写会把它们冲掉。
        agentEl.classList.remove("typing"); agentEl.classList.remove("streaming");
        if (!result.finished) {
          if (result.buf) {
            const _mc=agentEl.querySelector(".mc");
            const _rb=_mc.querySelector(".reasoning-box");
            const _tm=_mc.querySelector(".tm");
            _mc.innerHTML = renderRich(result.buf);
            if(_rb) _mc.prepend(_rb);   // 同样保留推理折叠框
            if(_tm) _mc.appendChild(_tm); // 保留时间戳
          }
          else { agentEl.querySelector(".mc").textContent = "（无输出）"; }
          pushHistory(paneId,{role:"agent",text:result.buf||"（无输出）"});
        }
      }
      break;
    } catch(e) {
      if (e.name === "AbortError") {
        const t = "（已停止）";
        agentEl.querySelector(".mc").textContent = t; agentEl.classList.remove("typing");
        // v1.0.479：状态条复位——原实现漏掉这行，点停止后状态条永远停在「⏹ 停止中…」
        setChatState(paneId,"⏹ 已停止","off");
        pushHistory(paneId,{role:"agent",text:t});
        break;
      }
      retryCount++;
      if (retryCount > MAX_RETRIES) {
        const t = "请求失败（已重试"+MAX_RETRIES+"次）：" + e.message + "。可点下方按钮重试，或检查网络后重新发送。";
        agentEl.querySelector(".mc").textContent = t; agentEl.classList.remove("typing");
        attachRetryBtn(agentEl, paneId);   // v1.0.373（体验A2）
        pushHistory(paneId,{role:"agent",text:t});
        break;
      }
      const delay = Math.pow(2, retryCount) * 1000;
      agentEl.querySelector(".mc").innerHTML = renderRich(
        (result && result.buf ? result.buf+"\n\n" : "") + "⚠️ 连接中断，"+Math.round(delay/1000)+" 秒后自动重试（"+retryCount+"/"+MAX_RETRIES+"）…"
      );
      await new Promise(res => setTimeout(res, delay));
      if (url.endsWith("/continue")) url = url.replace("/api/chat/continue", "/api/chat");
    }
  }
  finalizeChat(paneId);
  if(_isWork(paneId) && __opPick!=null && !__followOn){ setTimeout(()=>refreshSelection(), 400); }
}

async function continueChat(paneId, resumeNote){
  if(activeControllers[paneId]){ toast("正在生成中，请先停止","err"); return; }
  const btn=$("btnCont_"+paneId);
  if(btn){ btn.disabled=true; btn.textContent="继续中..."; }
  const pane=_paneEl(paneId);
  let agentEl=pane.lastElementChild;
  while(agentEl && !agentEl.classList.contains("agent")) agentEl=agentEl.previousElementSibling;
  if(!agentEl){ agentEl=addMsg(paneId,"agent","🤔 继续思考中…","agent typing"); }
  else { agentEl.classList.add("typing"); agentEl.querySelector(".mc").textContent="🤔 继续思考中…"; }
  const ctrl=new AbortController(); ctrl._t=Date.now(); activeControllers[paneId]=ctrl; setStop(paneId,true);
  // P3-5：复用公共流式执行器（fetch→consumeStream→stalled/needContinue→finalizeChat）
  // v1.0.317：resumeNote=按需安装的用户决定，随 /chat/continue 透传给断点恢复
  const body={pane_id:paneId};
  if(resumeNote) body.resume_note=resumeNote;
  // v1.0.590：带上本会话当前的权限级别；后端未收到时沿用断点里持久化的档位（不静默放宽）
  body.permission_level = permSelValue(paneId);
  body.thinking = thinkSelValue(paneId);   // v1.0.601 思考档位（""=跟随模型）
  await runStream("/api/chat/continue", body, paneId, agentEl, ctrl);
  if(btn){ btn.disabled=false; btn.textContent="继续"; }
}

// ---- 按需安装技能弹窗（v1.0.317）：agent 调 request_skill → 后端挂起 → 此处问用户 ----
async function showSkillRequestModal(paneId, evt){
  const sid=evt.skill_id, reason=evt.reason||"";
  if(modalBusy("skillReq", paneId, evt)) return;   // 防重复弹（v1.0.616：改成排队，不吞后来的会话）
  // 从技能库清单取名称/描述/工具（本地内置快照即时返回）
  let name=sid, desc="", tools=[];
  try{
    const d=await getJson("/api/skills/hub");
    const hit=(d.skills||[]).find(s=>s.id===sid);
    if(hit){ name=hit.name||sid; desc=hit.desc||""; tools=hit.tools||[]; }
  }catch(e){}
  const ov=document.createElement("div");
  ov.id="skillReqModal";
  ov.className="modal-veil";
  const toolsHtml=tools.map(t=>`<code>${escapeHtml(t)}</code>`).join(" ");
  ov.innerHTML=
    `<div class="modal-card" style="--mw:440px">`+
      `<div style="font-weight:500;font-size:var(--fs-lg);margin-bottom:8px">🧩 任务建议安装技能</div>`+
      `<div style="font-size:var(--fs-md);line-height:1.6;margin-bottom:6px"><b>${escapeHtml(name)}</b> <code>${escapeHtml(sid)}</code></div>`+
      (desc?`<div style="font-size:var(--fs-sm);color:var(--muted,#8792a3);margin-bottom:6px">${escapeHtml(desc)}</div>`:"")+
      (toolsHtml?`<div style="margin-bottom:6px">${toolsHtml}</div>`:"")+
      `<div style="font-size:var(--fs-sm);background:var(--panel2,#f6f8fa);border-radius:var(--r-md);padding:8px 10px;margin-bottom:12px">💡 agent 的理由：${escapeHtml(reason)}</div>`+
      `<div class="row" style="gap:8px;justify-content:flex-end">`+
        `<button class="ghost" id="skReqSkip">跳过，用其他方式</button>`+
        `<button class="primary" id="skReqInstall">⬇️ 安装并继续</button>`+
      `</div>`+
    `</div>`;
  document.body.appendChild(ov);
  const close=()=>{ if(ov.parentNode) ov.remove(); };
  $("skReqSkip").onclick=()=>{
    close();
    toast("已跳过，agent 将用其他方式完成任务","ok");
    continueChat(paneId, `用户拒绝安装技能「${name}」（id=${sid}）。请不要再次请求安装，改用基础工具（文件读写/联网检索/页面操作等）以其他方式完成本任务。`);
  };
  $("skReqInstall").onclick=async()=>{
    const ib=$("skReqInstall"); ib.disabled=true; ib.textContent="安装中…";
    try{
      const d=await api("/api/skills/hub/install",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:sid})});
      // v1.0.649（用户："agent 聊天过程中安装的技能也要在技能中心中可以管理"）：
      // ① 装完**回读一次**技能中心列表再报成功——"toast 说装好了、技能中心里没有"
      //    是最难自证的失败，回读把它变成一条看得见的结论；
      // ② 清掉技能库面板的已加载标记并刷新技能列表，用户切过去就是新状态。
      let visible = false;
      try{
        const lst = await getJson("/api/skills");
        visible = (lst.skills||[]).some(s=>s.id===d.id);
      }catch(e){}
      try{ const hl=$("hubList"); if(hl) hl.dataset.loaded=""; }catch(e){}
      try{ if(typeof loadSkills==="function") loadSkills(); }catch(e){}
      close();
      if(visible){
        toast(`技能「${name}」已安装并启用，可在「技能中心」编辑/停用/删除`,"ok",3600);
      }else{
        toast(`技能「${name}」写入配置成功，但技能中心列表里还没读到它（id=${d.id}）——`
              +"请刷新技能中心；仍看不到请反馈","warn",7000);
      }
      continueChat(paneId, `用户已同意并完成安装技能「${name}」（id=${sid}），该技能已启用且工具已注入。请直接使用它继续完成任务。`);
    }catch(e){
      ib.disabled=false; ib.textContent="⬇️ 安装并继续";
      toast(fetchErr(e,"安装技能"),"err");
    }
  };
}

// ---- 技能源导入弹窗（v1.0.318）：agent 调 import_skill_source → 用户确认 → 设源+批量安装 ----
async function showSkillSourceModal(paneId, evt){
  const url=evt.url||"";
  if(modalBusy("skillSrc", paneId, evt)) return;   // v1.0.616：排队而非丢弃
  const ov=document.createElement("div");
  ov.id="skillSrcModal";
  ov.className="modal-veil";
  ov.innerHTML=
    `<div class="modal-card" style="--mw:480px">`+
      `<div style="font-weight:500;font-size:var(--fs-lg);margin-bottom:8px">🧩 导入技能源</div>`+
      `<div style="font-size:var(--fs-sm);word-break:break-all;background:var(--panel2,#f6f8fa);border-radius:var(--r-md);padding:8px 10px;margin-bottom:8px"><code>${escapeHtml(url)}</code></div>`+
      `<div style="font-size:var(--fs-sm);color:var(--muted,#8792a3);margin-bottom:8px">确认后：该地址将设为技能库远程源，其中的全部技能会批量安装进技能中心（已装的自动更新）。<br/>⚠️ 技能提示词会注入后续对话——只导入你信任的来源。</div>`+
      `<div id="skSrcMsg" class="sub" style="margin-bottom:8px;display:none"></div>`+
      `<div class="row" style="gap:8px;justify-content:flex-end">`+
        `<button class="ghost" id="skSrcSkip">跳过</button>`+
        `<button class="primary" id="skSrcGo">确认导入</button>`+
      `</div>`+
    `</div>`;
  document.body.appendChild(ov);
  const close=()=>{ if(ov.parentNode) ov.remove(); };
  const msg=(t)=>{ const m=$("skSrcMsg"); m.style.display="block"; m.textContent=t; };
  $("skSrcSkip").onclick=()=>{
    close();
    toast("已跳过技能源导入","ok");
    continueChat(paneId, "用户拒绝导入该技能源。请改用基础工具完成当前任务，不要再请求导入。");
  };
  $("skSrcGo").onclick=async()=>{
    const gb=$("skSrcGo"); gb.disabled=true; gb.textContent="拉取并导入中…";
    try{
      const d=await api("/api/skills/hub/import_source",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
      close();
      toast(`已导入 ${d.imported} 个技能（新装 ${d.created} 更新 ${d.updated}）并设为远程源`,"ok",3000);
      continueChat(paneId, `用户已确认导入技能源 ${url}：${d.imported} 个技能已全部安装进技能中心并启用（新装 ${d.created}、更新 ${d.updated}）。请直接使用这些技能继续完成任务，并告知用户可在技能中心查看。`);
    }catch(e){
      gb.disabled=false; gb.textContent="确认导入";
      msg("❌ "+(e.message||"导入失败"));
      toast(fetchErr(e,"导入技能源"),"err");
    }
  };
}

function finalizeChat(paneId){
  // v1.0.334 补挂兜底：done/aborted/兜底渲染等路径都会 innerHTML 覆写 .mc（保留推理框/.tm
  // 时把操作栏冲掉）——收尾统一确保最后一个 agent 气泡有操作栏，raw 缺失时取渲染后文本
  try{
    const pane = _paneEl(paneId);
    const lastAgent = pane && [...pane.querySelectorAll(".msg.agent")].pop();
    if(lastAgent && !lastAgent.querySelector(".msg-ops")){
      try{ attachMsgOps(lastAgent, paneId, "agent"); }catch(e){}
      if(!lastAgent.dataset.raw || lastAgent.dataset.raw.indexOf("正在思考") >= 0){
        const _mcR = lastAgent.querySelector(".mc");
        if(_mcR) lastAgent.dataset.raw = _mcR.innerText.slice(0, 20000);
      }
    }
  }catch(e){}
  // v1.0.650（F2）：统计条改由 done 收据单一生成——原来这里读前端自己累加的
  // 输出 token 与墙上时间，既少了轮次/失败数/记账项数，也因为它
  // 不落库而让重开历史任务时整条消失。现在实时与重放都走 applyRunStat。
  try{
    const csS = __chipState[paneId];
    const _paneNode = _paneEl(paneId);
    const lastAgentS = _paneNode && [..._paneNode.querySelectorAll(".msg.agent")].pop();
    if(lastAgentS){
      const raw = lastAgentS.dataset.raw || (lastAgentS.querySelector(".mc")||{}).textContent || "";
      applyRunStat(lastAgentS, (csS && csS.summary) || null, raw.length);
    }
  }catch(e){}

  __tw_stop(paneId);   // v1.0.308：所有收尾路径的公共出口——停掉打字机（防终端渲染后被过期 tick 覆写）
  if(activeControllers[paneId]) delete activeControllers[paneId];
  setStop(paneId,false);
  // v1.0.479：任何收尾路径都不该残留「⏹ 停止中…」——异常路径漏设状态时兜底复位
  try{
    const _st=$(paneId==="opPane"?"opState":"setState");
    if(_st && _st.textContent.indexOf("停止中")>=0) _st.textContent="⏹ 已停止";
  }catch(e){}
  // P3-2：若刚才是 need_continue（轮次上限），覆盖 setStop 置的「已完成」为「可继续」
  if(__needContinueState[paneId]){
    setChatState(paneId,"⏸ 已达轮次上限，可继续","off");
    delete __needContinueState[paneId];
  }
  const cb=$("btnCont_"+paneId); if(cb){ cb.disabled=false; cb.textContent="继续"; }
  // 2026-09-10 兜底：清掉最后一个 agent 气泡可能残留的 typing 类。
  // 防「流在推理帧后被异常/断流终止」导致气泡卡在「打字中」浅灰 + inline-flex 窄列
  // （即用户看到的「每字一行」怪界面）。主修在 runStream 收尾分支，此处二次保险。
  const _fp=_paneEl(paneId);
  let _fa=_fp && _fp.lastElementChild;
  while(_fa && !_fa.classList.contains("agent")) _fa=_fa.previousElementSibling;
  if(_fa){ _fa.classList.remove("typing"); _fa.classList.remove("streaming"); }
  // v1.0.500 兜底：异常收尾路径（断流/abort/mock 短流）chip 停在 think/tool 或尚未初始化 →
  // 补「⏹ 已停止」终态；有活跃 controller 的（后台会话收尾误入等）不动。
  try{
    const csF=__chipState[paneId];
    if((!csF || csF.phase==="think" || csF.phase==="tool") && !activeControllers[paneId]){
      __chipState[paneId]=Object.assign({ops:0}, csF, {phase:"stopped"});
      updateModelChip(paneId, __metaState[paneId]);
    }
  }catch(e){}
  if(typeof renderTaskRail==="function") renderTaskRail();   // v1.0.489：⏳ 运行标记同步
}
function setChatState(paneId, text, cls){
  // v1.0.486 多会话：bar 是共享的——只有「当前查看中」的会话能更新；后台会话的状态
  // 记进 __barMemo，切回时由 syncConvBar 恢复显示。
  paneId=_normKey(paneId);
  __barMemo[paneId]={text:text||"", cls:cls||""};
  if(_isWork(paneId) && paneId!==_activeKey()) return;   // 后台会话：不抢共享条
  const st=paneId==="setChatPane"?"setState":"opState";
  const s=$(st); if(!s) return;
  s.className="chat-state"+(cls?" "+cls:"");
  s.textContent=text||"";
}
const __barMemo={};   // paneKey -> {text,cls}（后台会话的最近状态，切回时恢复）
// 切换查看的会话后调用：把该会话的状态恢复到共享 bar
function syncConvBar(paneKey){
  const m=__barMemo[_normKey(paneKey)];
  const st=$("opState");
  if(st) st.textContent = m ? (m.text||"") : "";
}
// 模型/上下文信息标签：agent_loop 每轮推送 meta（模型/上下文字符数/轮次），usage 追加 token 用量
const __metaState={};
// v1.0.366（轮次展示替换）：每个 pane 的实时运行状态 {ops, phase: tool|think, label}
const __chipState={};
// 工具名 → 用户可读动作（chip 实时显示"正在执行 X"）
const _TOOL_LABEL={
  read_wecom_page:"读页面", read_wecom_fields:"读表单字段", list_wecom_windows:"列企微窗口",
  list_interactive_elements:"扫描可交互元素", click_wecom_element:"点击", type_wecom_text:"输入文字",
  select_wecom_option:"选择下拉项", scroll_wecom_window:"滚动页面", wait_wecom_page_load:"等待页面加载",
  capture_wecom_screenshot:"截图", upload_wecom_file:"上传附件", download_file:"下载文件",
  save_workspace_file:"写文件", list_workspace_files:"列工作区文件", read_workspace_file:"读工作区文件",
  run_rule_check:"跑规则脚本", run_command:"跑命令", pc_run:"跑命令", ask_judge:"询问判断模型",
  request_skill:"请求安装技能", import_skill_source:"导入技能源", save_experience:"沉淀经验",
  append_progress:"记录批量进度", close_wecom_page:"关闭页面", list_wecom_links:"列附件链接",
  finish:"收尾汇报", create_excel:"生成 Excel", create_docx:"生成 Word", create_pptx:"生成 PPT",
  ask_user:"🙋 向你提问"
};
function _tool_label(name, args){
  const base=_TOOL_LABEL[name] || ("执行 "+name);
  let target="";
  try{
    const a=args||{};
    const t=a.target||a.title||a.name||a.path||a.skill_id||a.question||a.filename||"";
    if(t) target="「"+safeSlice(t,14)+"」";
  }catch(e){}
  return base+target;
}
// v1.0.367：数值缩写（12345→12K / 5678→5.7K / 890→890）——chip 防「token 显示不全」
function _kfmt(n){
  n=Number(n)||0;
  if(n>=10000) return (n/1000).toFixed(0)+"K";
  if(n>=1000) return (n/1000).toFixed(1)+"K";
  return String(n);
}
function updateModelChip(paneId, meta){
  if(!meta && !__chipState[paneId]) return;
  if(meta) __metaState[paneId]=meta;
  paneId=_normKey(paneId);
  if(_isWork(paneId) && paneId!==_activeKey()) return;   // 后台会话不更新共享 chip
  const el=$(paneId==="setChatPane"?"setModelChip":"opModelChip"); if(!el) return;
  const m=__metaState[paneId]||{};
  const cs=__chipState[paneId]||{};
  // v1.0.367（chip 两 bug 修复）：①文案精简防截断——去「上下文≈/正在/（第N次操作）/tok」等冗余，
  // 数值走 K 格式化；CSS 另有 max-width+ellipsis 兜底（title 悬浮看全文）。
  // ②终态齐全——done/error/need_continue/aborted 均刷新 chip，不再停留「思考中」。
  let t="🧠 "+(m.ctx_chars?_kfmt(m.ctx_chars)+"字":"…");
  const phase=cs.phase||"think";
  // done 态用 summary 的累计 token（收据语义）；运行中用最近一轮 usage
  const u=(phase==="done" && cs.summary)?cs.summary:(m.usage||{});
  if(phase==="tool" && cs.label) t+=" · 🔧 "+cs.label+"×"+(cs.ops||1);
  else if(phase==="tool") t+=" · 🔧 操作×"+(cs.ops||1);
  else if(phase==="done") t+=" · ✅ "+(cs.summary?((cs.summary.tool_calls||0)+"次操作 · "+(cs.summary.elapsed_s||0)+"s"):"完成");
  else if(phase==="err") t+=" · ❌ 出错";
  else if(phase==="paused") t+=" · ⏸ 待继续";
  else if(phase==="stopped") t+=" · ⏹ 已停止";
  else t+=(cs.ops?" · 思考中×"+cs.ops:" · 思考中");
  if(u.prompt_tokens!=null) t+=" · ↑"+_kfmt(u.prompt_tokens)+" tok/↓"+(u.completion_tokens!=null?_kfmt(u.completion_tokens):"?")+" tok";
  el.textContent=t;
  el.title="模型："+(m.model||"?")+"　"+t;
  // 预算临界提醒（≥80%）：黄色警示，平时不占视觉；仅运行中（think/tool）警示，终态不再警示
  const mr=m.max_rounds||0, rd=m.rounds||0;
  const budgetHot=mr>0 && rd>=Math.ceil(mr*0.8) && (phase==="think"||phase==="tool");
  if(budgetHot){ t+=" · ⚠️ 轮次 "+rd+"/"+mr+"（到上限会暂停，可点继续）"; el.textContent=t; el.classList.add("warn"); }
  else el.classList.remove("warn");
  // v1.0.366 修复"操作几次就不显示"：显式 inline-block（原 display="" 依赖样式表默认值，
  // 折叠/重渲染等状态可能使其不可见）
  el.style.display="inline-block";
}
function clearModelChip(paneId){
  paneId=_normKey(paneId);
  if(_isWork(paneId) && paneId!==_activeKey()) return;   // 后台会话不抢共享 chip
  const el=$(paneId==="setChatPane"?"setModelChip":"opModelChip"); if(!el) return;
  el.style.display="none"; el.textContent=""; delete __metaState[paneId];
}
function setStop(paneId,on){
  paneId=_normKey(paneId);
  const viewing = paneId==="setChatPane" || paneId===_activeKey();
  const id=paneId==="setChatPane"?"setStop":"opStop";
  const b=$(id);
  if(b && viewing){ b.disabled=!on; b.classList.toggle("busy", !!on); }
  // v1.0.373 WorkBuddy 化：composer 发送钮同步双态（↑ 发送 ↔ ⏹ 停止）
  const sb=$(paneId==="setChatPane"?"setSendBtn":"opSendBtn");
  if(sb && viewing){ sb.classList.toggle("stopping",!!on); sb.textContent=on?"⏹":"↑"; sb.title=on?"停止生成":"发送（Enter）"; }
  if(on){ setChatState(paneId,"⏳ 生成中…","on"); }
  else{
    setChatState(paneId,"✅ 已完成","done");
    if(viewing){
      const s=$(paneId==="setChatPane"?"setState":"opState");
      if(s){ clearTimeout(s._t); s._t=setTimeout(()=>setChatState(paneId,"",""), 3000); }
    }
  }
}
// v1.0.373 WorkBuddy 化：composer 发送钮双态（↑ 发送 / ⏹ 停止）——由 setStop 切外观，这里分发点击
function composerFire(paneId, inputId, fnName){
  const sb=$(paneId==="setChatPane"?"setSendBtn":"opSendBtn");
  if(sb && sb.classList.contains("stopping")){ stopChat(paneId); return; }
  if(typeof window[fnName]==="function") window[fnName]();
}
async function stopChat(paneId){
  paneId=_normKey(paneId);   // v1.0.486：'opPane' → 当前查看的会话 key
  // v1.0.472 修复（停止无反应）：原先 await 后端 abort 再 abort()——后端 abort 请求
  // 慢/卡时 UI 长时间无任何反馈。现在顺序反转：先断 SSE（界面立即出现「已停止」），
  // 再异步通知后端置 abort 标志+硬停止 LLM 连接（不阻塞 UI）。
  const ctrl = activeControllers[paneId];
  if(!ctrl){
    // 无进行中流：仍通知后端清 abort 残留（保险），并复位按钮态
    postJson("/api/chat/abort",{pane_id:paneId}).catch(()=>{});
    setStop(paneId,false);
    return;
  }
  setChatState(paneId,"⏹ 停止中…","off");
  // v1.0.500：点停瞬间 chip 立即切「⏹ 已停止」（不等流收尾）——修复「停止后仍显示思考中」
  __chipState[paneId]=Object.assign({ops:0}, __chipState[paneId], {phase:"stopped"});
  updateModelChip(paneId, __metaState[paneId]);
  try{ ctrl.abort(); }catch(e){}
  // v1.0.479：不再 await——abort 请求挂住（后端忙）时收尾流程被拖死，状态条永远「停止中…」。
  // 控制器已断、界面收尾由 runStream 的 AbortError 分支完成，这里只负责通知后端清理。
  postJson("/api/chat/abort",{pane_id:paneId}).catch(()=>{});
  // 保险丝：若 1.5s 内正常收尾没跑（流已僵死等），强制复位界面状态
  setTimeout(()=>{
    if(activeControllers[paneId]===ctrl){
      delete activeControllers[paneId];
      __tw_stop(paneId);
      setStop(paneId,false);
      setChatState(paneId,"⏹ 已停止","off");
    }
  },1500);
}
function clearChat(paneId){
  // v1.0.502：具名会话清空直通服务端（/api/conversations/clear）——取代旧的
  // 「引导去 ⋯ → 删除」提示（用户实测反馈提示语义难懂）。确认后清服务端消息+本地视图。
  const nk=_normKey(paneId);
  if(String(nk).indexOf("conv_")===0){
    confirmAsk("清空对话", "清空该对话的全部消息？会话本身保留，不可恢复。", async ()=>{
      try{
        await apiToast("/api/conversations/clear",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:nk.slice(5)})},"清空对话");
        const pv=_paneEl(nk); if(pv){ pv.innerHTML=""; pv.dataset.loaded=""; }
        localStorage.removeItem("hist_"+_histKey(paneId)); clearModelChip(nk);
        const st=$("opState"); if(st && _activeKey()===nk) st.textContent="";
        toast("已清空对话消息","ok"); loadConvList(true);
      }catch(_){}
    }, "清空");
    return;
  }
  if(activeControllers[nk]) stopChat(nk); const pv=_paneEl(nk); if(pv) pv.innerHTML=""; localStorage.removeItem("hist_"+_histKey(paneId)); clearModelChip(nk);
  if(nk==="scratch"||nk==="opPane"){ pv.innerHTML=`<div class="empty" style="padding:40px 16px"><span class="em-ic">💬</span>左侧选中企微窗口后，在这里指挥 agent 操作它<br/><span style="font-size:var(--fs-sm)">例如：左侧「任务」区点 ＋ 新建任务 · 答题 · 下载数据 · 可直接拖入文件/文件夹分析</span></div>`; }
  else { pv.innerHTML=`<div class="empty" style="padding:40px 16px"><span class="em-ic">🤖</span>在下方输入框直接对话（支持拖入文件/文件夹）<br/><span style="font-size:var(--fs-sm)">可分析窗口内容、起草文档、按模板产出结果</span></div>`; }
}
// v1.0.306：时间显隐为全局偏好，开关移入「设置 → 界面偏好」（聊天栏不再放按钮）。
// 偏好持久化在 localStorage.timesHidden，applyTimesPref 即时应用到所有聊天窗格。
function timesHiddenPref(){ try{ return localStorage.getItem("timesHidden")==="1"; }catch(e){ return false; } }
function applyTimesPref(){
  const hidden=timesHiddenPref();
  document.querySelectorAll(".chatpane").forEach(p=>{ p.classList.toggle("hide-times", hidden); });
  const st=$("opt_show_times");
  if(st) st.checked=!hidden;
}
function pushHistory(paneId,item){ paneId=_histKey(paneId); let h=[]; try{ h=JSON.parse(localStorage.getItem("hist_"+paneId)||"[]"); }catch(e){ h=[]; }
  // P3-1：单条过长先截断，避免单条消息撑爆配额
  if(item && typeof item.text==="string" && item.text.length>200000){ item={...item, text:item.text.slice(0,200000)+"…（内容过长已截断）"}; }
  // 2026-09-13：记录发送时刻——恢复历史时按原时间显示（此前恢复的消息全无时间戳，🕘 开关无物可切）
  if(item && typeof item==="object" && !item.t){ item={...item, t:new Date().toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"})}; }
  h.push(item);
  // P3-1：按总字节估算裁剪（localStorage 约 5MB 配额），超预算按条丢弃最旧
  const BUDGET=4*1024*1024;
  let enc; try{ enc=JSON.stringify(h); }catch(e){ enc=null; }
  while(enc && enc.length>BUDGET && h.length>1){ h.shift(); enc=JSON.stringify(h); }
  if(enc===null){ h=h.slice(-50); enc=JSON.stringify(h); }
  lsSet("hist_"+paneId, enc);   // 配额仍不足则静默丢弃（lsSet 吞写失败，与原行为一致）
}

// v1.0.603（用户要求"每次打包的版本没有历史对话"）：build.py 打包时会把服务端
// 对话数据（会话/对话记录/断点）整簇搬到 build/_chat_archive，并把 config.json 的
// chat_epoch +1。但 localStorage 里还有一份窗口级气泡缓存 hist_<pane>，不失效的话
// 「临时对话」那一栏会把上一版的旧气泡复活——用户看到的就是"没清干净"。
// 这里只删 hist_*：模型/权限/思考档位/深色模式/折叠状态等偏好一律保留。
function syncChatEpoch(epoch){
  epoch = (epoch === undefined || epoch === null) ? 0 : (Number(epoch) || 0);
  let last = 0;
  try{ last = Number(localStorage.getItem("chatEpoch") || 0); }catch(e){}
  if(last === epoch) return false;
  let n = 0;
  try{
    const doomed = [];
    for(let i=0;i<localStorage.length;i++){
      const k = localStorage.key(i);
      if(k && k.indexOf("hist_") === 0) doomed.push(k);
    }
    doomed.forEach(k=>{ localStorage.removeItem(k); n++; });
    localStorage.setItem("chatEpoch", String(epoch));
  }catch(e){ return false; }
  if(n){
    // 已经按旧缓存渲染出来的那一屏也一起清掉（正在流式输出的 pane 不动），
    // 否则"清了但屏幕上还在"，要等用户手动切换才干净。
    try{
      document.querySelectorAll(".conv-view").forEach(v=>{
        const k = v.id.replace(/^cv_/, "");
        if(!activeControllers[k]) v.innerHTML = "";
      });
    }catch(e){}
    try{ console.log("[chat] 打包纪元 " + epoch + "：已清除上一版会话气泡缓存 " + n + " 项"); }catch(e){}
  }
  return true;
}

function restoreHistory(paneId){
  paneId=_histKey(paneId);
  let h=[]; try{ h=JSON.parse(localStorage.getItem("hist_"+paneId)||"[]"); }catch(e){}
  // P3-1：恢复时同样按字节预算裁剪，避免超大历史拖慢渲染
  const BUDGET=4*1024*1024; let enc=JSON.stringify(h);
  while(enc && enc.length>BUDGET && h.length>1){ h.shift(); enc=JSON.stringify(h); }
  h.forEach(it=>{
    // 跳过空白历史消息（text 为空/纯空白），避免恢复出「白板气泡」——历史上断流中断可能写入过空文本
    if(!it || !String(it.text||"").trim()) return;
    // 2026-09-13：带原始发送时间恢复（旧存档无 t 字段则仍无时间戳，向后兼容）
    // v1.0.471（UI#8）：noScroll=true 批量追加，循环结束一次性滚底（省 O(n) 次强制布局）
    if(it.role==="user") addMsg(paneId,"user",it.text,null,false,it.t,true);
    else if(it.role==="tool") addMsg(paneId,"tool",it.text,null,false,it.t,true);
    else if(it.role==="err") addMsg(paneId,"err",it.text,"err",false,it.t,true);
    else addMsg(paneId,"agent",it.text,null,false,it.t,true);
  });
  const _p=_paneEl(paneId);
  // v1.0.627：恢复的历史气泡也要能点（否则刷新一下页面功能就"消失"了）
  if(_p){ try{ linkifyWorkspacePaths(_p); }catch(e){} }
  if(_p && h.length) __scrollBottom(paneId, true);   // 历史恢复：落到最新一处
}
function toggleFollow(){
  __followOn = $("followChk").checked;
  $("followState").textContent = __followOn ? STATUS_ICON.ok + " 跟随中 · " + (FOLLOW_MS/1000) + "s 自动刷新" : STATUS_ICON.off + " 已暂停自动刷新";
  // 内容区刷新由全局心跳（FOLLOW_MS，见 init）统一负责：仅开启时自动刷；这里负责状态显示与勾选时立即刷一次
  if (__followOn && __opPick!=null) refreshSelection();
}

async function sendSetChat(){
  await streamChat("/api/chat", {messages:[{role:"user",content:$("setChatInput").value.trim()}], use_wecom_context:$("setCtxChk").checked}, "setChatPane", "setChatInput");
}
function inputKey(e, sendFn){
  if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey){
    e.preventDefault(); window[sendFn] && window[sendFn]();
    return;
  }
  // v1.0.570：Ctrl+Enter 换行（textarea 默认不响应 Ctrl+Enter，手动插入并触发 autoResize）
  if (e.key === "Enter" && e.ctrlKey && !e.altKey && !e.metaKey){
    e.preventDefault();
    const el = e.target, s = el.selectionStart, en = el.selectionEnd;
    el.value = el.value.slice(0, s) + "\n" + el.value.slice(en);
    el.selectionStart = el.selectionEnd = s + 1;
    el.dispatchEvent(new Event("input", {bubbles:true}));
  }
}
function fillPrompt(inputId, text){ const el=$(inputId); if(el){ el.value=text; el.focus(); } }
// 工作台快捷任务：带任务级轮次预算（max_rounds）直接发起，如流程分析 5 轮约束
// 2026-09-13：未选企微窗口（含未启动企微）也允许对话——去掉硬阻断。
// use_wecom_context 跟随选中态：选中窗口才附窗口正文与 pick；未选中时后端注入
// 「未附加企微窗口」提示，agent 仍可用 pc_* 本机操控 / 文件 / 联网搜索等工具，不被窗口缺失卡死。
async function runQuickTask(prompt, maxRounds){
  const inp=$("opInput"); if(inp){ inp.value=prompt; }
  // v1.0.517 修复（用户反馈"自动任务和快捷入口触发的都是属于直接干的"）：预设任务
  // 不再弹模式选择弹窗——固定直接干（openQuickLink 已 setOpMode("agent")），静默
  // 建会话归入任务栏（标题按内容自动生成）。
  if(!__curConv){
    await _createTaskConv(_deriveTaskName(prompt));
  }
  const body={messages:[{role:"user",content:prompt}], use_wecom_context:(__opPick!=null)};
  if(__opPick!=null) body.pick=__opPick;
  if(maxRounds) body.max_rounds=maxRounds;
  if(__opAutoLoop) body.auto_loop=true;
  if(__opTeam) body.team=true;
  if(__opMode!=="agent") body.mode=__opMode;
  await streamChat("/api/chat", body, _activeKey(), "opInput");
  if(__opMode==="plan") attachPlanExecButton();
}
async function sendOperate(){
  const text=$("opInput").value.trim(); if(!text){ toast("请输入内容后再发送","err"); return; }
  const body={messages:[{role:"user",content:text}], use_wecom_context:(__opPick!=null)};
  if(__opPick!=null) body.pick=__opPick;
  if(__opAutoLoop) body.auto_loop=true;
  if(__opTeam) body.team=true;
  if(__opMode!=="agent") body.mode=__opMode;
  await streamChat("/api/chat", body, _activeKey(), "opInput");
  if(__opMode==="plan") attachPlanExecButton();
}
// ---- 三工作模式（v1.0.313，对齐 WorkBuddy Agent/Plan/Ask） ----
let __opMode = "agent";
const _MODE_HINT = {
  agent: "agent 直接执行任务（可操作页面/文件/电脑）",
  plan: "先产出可确认的执行方案，不动手——确认后点「按此方案执行」",
  ask: "只读取与问答，不执行任何写操作（不点页面/不写文件/不跑命令）"
};
function setOpMode(m){
  if(!(m in _MODE_HINT)) m="agent";
  __opMode=m;
  // v1.0.485：模式持久化——重启后不再重置回「直接干」（用户以为还在只问答，
  // 实际 agent 模式全量工具在跑；实测只问答任务重启后 pc_run 真实执行）
  lsSet("opMode", m);
  // v1.0.592：删掉对 `modeBtn_<k>` 的高亮同步——该组按钮随 v1.0.519「工具栏模式行」一起移除了
  // （模式选择收敛到新建任务弹窗），全库只剩这一处引用、且被 if(b) 静默吞掉，恒 no-op。
}
// 启动时恢复上次选择的模式（12-init.js 页面加载后调用）
function restoreOpMode(){
  let m="agent";
  m = lsGet("opMode") || "agent";
  setOpMode(m in _MODE_HINT ? m : "agent");
}
// Plan 流结束后：在最后一条 agent 消息尾部加「按此方案执行」按钮
function attachPlanExecButton(){
  const pane=$("opPane");
  const cards=pane.querySelectorAll(".msg.agent");
  const last=cards[cards.length-1];
  if(!last) return;
  const mc=last.querySelector(".mc");
  if(!mc || mc.querySelector(".plan-exec-btn")) return;
  const planText=(mc.innerText||"").trim();
  if(!planText.includes("执行方案")) return;
  const row=document.createElement("div");
  row.style.cssText="margin-top:8px";
  const btn=document.createElement("button");
  btn.className="btn plan-exec-btn";
  btn.textContent="▶ 按此方案执行";
  btn.onclick=()=>{
    const inp=$("opInput");
    if(inp){ inp.value="请严格按以下方案执行（不要重新规划）：\n\n"+planText; inp.focus(); }
    setOpMode("agent");
    toast("已切换到 Agent 模式并载入方案，确认输入框后发送即可","ok",2600);
  };
  row.appendChild(btn);
  mc.appendChild(row);
  __scrollBottom(_activeKey(), true);   // v1.0.615：统一走容器解析（原来写的是 $("opPane")，恰好对，但两套机制迟早分叉）
}
// 👥 智能体团队开关（v1.0.312）：点亮点亮本轮起走「规划→执行→审查」流水线；
// 总闸在 设置→功能管理·智能体团队（后端 feature_flags.team，关了请求里带 team 也会被忽略）
let __opTeam = false;
function toggleOpTeam(){
  __opTeam = !__opTeam;
  const btn=$("opTeamBtn");
  if(btn){
    btn.classList.toggle("teamOn", __opTeam);
    btn.title = __opTeam ? "智能体团队：已开启（规划→执行→审查，更慢更严谨）。点击关闭"
                         : "智能体团队：本轮任务走「规划 → 执行 → 审查」多智能体分工（审查不通过自动修补；更慢更严谨，适合复杂任务）";
  }
  toast(__opTeam ? "智能体团队已开启：本轮任务将规划→执行→审查" : "智能体团队已关闭","ok",1600);
}

// ---- 产物库 ----


// ---- 安全审批弹窗（v1.0.320）：危险命令/批量删除 → 批准发令牌 or 拒绝改路 ----
async function showApprovalModal(paneId, evt, opts){
  // v1.0.616：第三个参数 opts——宏重放等「非 agent 会话」也复用这同一个弹窗与同一套
  // 一次性令牌，但批准后不是 continueChat（那边没有 LLM 在等），而是回调 onGrant/onDeny。
  // opts.noRelax：宏重放没有"本任务权限档"可调（它是固定序列），不给降档按钮。
  opts = opts || {};
  if(modalBusy("appr", paneId, evt, opts)) return;   // 排队而非丢弃（多会话并行）
  // v1.0.590：审批来源扩展——除危险命令/删除外，「每次询问」档的任意写类操作、
  // 以及页面脚本执行（kind=js，原实现落到 else 分支被显示成「删除：」）都走这个弹窗。
  const kind = evt.kind || "delete";
  const _SUBJ = {
    command: ()=> "执行命令：" + (evt.command||""),
    js:      ()=> "在企微页面执行脚本：" + (evt.command||""),
    tool:    ()=> (evt.command || ("执行 " + (evt.tool||"未命名操作"))),
    file:    ()=> "覆盖/写入文件：" + (evt.path||""),
    delete:  ()=> "删除：" + (evt.path||""),
  };
  const subject = ((_SUBJ[kind]||_SUBJ.delete)());
  const _NOTE = {
    command: "⚠️ 该命令命中危险模式，请确认是您授意的操作。",
    js: "⚠️ 页面脚本可读写页面内容与发起请求，请确认是您授意的操作。",
    tool: "⚠️ 当前权限级别为「每次询问」——批准仅对本次重试放行。",
    file: "⚠️ 覆盖前会自动备份到 产物库/备份/；批准仅对本次重试放行。",
    delete: "⚠️ 删除始终只进回收站（可还原）；批准仅对本次重试放行。",
  };
  const curLv = opts.noRelax ? "" : permSelValue(paneId);
  const nextLv = curLv === "ask_every" ? "auto" : (curLv === "auto" ? "full" : "");
  const relaxBtn = nextLv
    ? `<button class="ghost" id="apprRelax">批准，并把本任务改为「${__PERM_LABEL[nextLv]}」</button>` : "";
  const ov=document.createElement("div");
  ov.id="apprModal";
  ov.className="modal-veil";
  ov.innerHTML=
    `<div class="modal-card" style="--mw:480px">`+
      `<div style="font-weight:500;font-size:var(--fs-lg);margin-bottom:8px">🛡 需要你确认${opts.noRelax?"（宏重放）":"（权限级别："+escapeHtml(__PERM_LABEL[curLv]||"自动审批")+"）"}</div>`+
      `<div style="font-size:var(--fs-md);margin-bottom:6px;word-break:break-all"><b>${escapeHtml(subject)}</b></div>`+
      `<div style="font-size:var(--fs-sm);color:var(--muted,#8792a3);margin-bottom:8px">原因：${escapeHtml(evt.reason||"命中安全规则")}</div>`+
      `<div style="font-size:var(--fs-sm);background:var(--panel2,#f6f8fa);border-radius:var(--r-md);padding:8px 10px;margin-bottom:12px">`+
        (_NOTE[kind]||_NOTE.delete)+
      `</div>`+
      `<div class="row" style="gap:8px;justify-content:flex-end;flex-wrap:wrap">`+
        `<button class="ghost" id="apprDeny">拒绝，用其他方式</button>`+
        relaxBtn+
        `<button class="primary" id="apprGrant">批准执行</button>`+
      `</div>`+
    `</div>`;
  document.body.appendChild(ov);
  const close=()=>{ if(ov.parentNode) ov.remove(); };
  const grantThen = async (after)=>{
    const gb=$("apprGrant"); if(gb){ gb.disabled=true; gb.textContent="批准中…"; }
    const rb=$("apprRelax"); if(rb){ rb.disabled=true; }
    try{
      await api("/api/security/grant",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({hash:evt.hash,nonce:evt.nonce})});   // v1.0.652：双因子——hash 可自算，nonce 是弹窗签发凭证
      close();
      if(after) after();
      if(opts.onGrant){ opts.onGrant(); return; }
      continueChat(paneId, "用户已批准该操作（一次性放行令牌已发放）。请原样重试刚才被拦截的操作。");
    }catch(e){ if(gb){ gb.disabled=false; gb.textContent="批准执行"; } if(rb){ rb.disabled=false; } toast(fetchErr(e,"批准"),"err"); }
  };
  $("apprGrant").onclick=()=>grantThen(()=>{ if(!opts.onGrant) toast("已批准，agent 将重试该操作（一次性放行）","ok"); });
  const relax=$("apprRelax");
  if(relax) relax.onclick=()=>grantThen(()=>{
    _panePermSet(paneId, nextLv);
    toast("已批准，本任务权限级别改为「"+__PERM_LABEL[nextLv]+"」", nextLv==="full"?"warn":"ok", 2400);
    if(nextLv==="full") toast("⚠️ 完全访问只是不再逐次询问；致命命令与系统保护目录仍会被直接拒绝","warn",3600);
  });
  $("apprDeny").onclick=()=>{
    close();
    if(opts.onDeny){ opts.onDeny(); return; }
    toast("已拒绝，agent 将改用其他方式","ok");
    continueChat(paneId, "用户拒绝了该操作，且不会发放放行令牌。请不要原样重试，改用其他安全的方式完成当前步骤。");
  };
}


// ---- v1.0.373 用户抉择弹窗（用户需求 1）：agent 调 ask_user → 弹选择卡 → 回答走 continue 恢复 ----
function showAskUserModal(paneId, evt){
  if(modalBusy("askUser", paneId, evt)) return;   // v1.0.616：排队而非丢弃
  const ov=document.createElement("div");
  ov.id="askUserModal";
  ov.className="modal-veil";
  const opts=(evt.options||[]);
  const optBtns=opts.map(o=>
    `<button class="ask-opt" data-v="${escapeHtml(o)}">▶ ${escapeHtml(o)}</button>`).join("");
  const allowText=evt.allow_text!==false;
  ov.innerHTML=
    `<div class="modal-card" style="--mw:520px">`+
      `<div style="font-weight:600;font-size:var(--fs-lg);margin-bottom:10px">🙋 任务需要你做出选择</div>`+
      `<div style="font-size:var(--fs-md);line-height:1.6;margin-bottom:12px">${escapeHtml(evt.question||"请选择")}</div>`+
      (optBtns?`<div id="askOpts" style="display:flex;flex-direction:column;gap:8px;margin-bottom:12px">${optBtns}</div>`:"")+
      (allowText?
        `<div style="margin-bottom:12px">`+
          `<textarea id="askUserText" placeholder="也可以直接输入你的想法…" `+
            `style="width:100%;min-height:64px;resize:vertical;padding:8px 10px;border:1px solid var(--border);border-radius:var(--r-md);font:inherit;font-size:var(--fs-md)"></textarea>`+
        `</div>`:"")+
      `<div class="row" style="gap:8px;justify-content:flex-end">`+
        `<button class="ghost" id="askSkip">跳过，让 agent 自行决定</button>`+
        (allowText?`<button class="primary" id="askSubmit">提交</button>`:"")+
      `</div>`+
    `</div>`;
  document.body.appendChild(ov);
  const close=()=>{ if(ov.parentNode) ov.remove(); };
  const submit=(prefix)=>{
    close();
    continueChat(paneId, prefix);
  };
  ov.querySelectorAll(".ask-opt").forEach(b=>{
    b.onclick=()=>submit("【用户的选择】"+b.dataset.v);
    b.onmouseenter=()=>{ b.style.borderColor="var(--accent)"; b.style.background="var(--accent-soft)"; };
    b.onmouseleave=()=>{ b.style.borderColor="var(--border)"; b.style.background="var(--panel2,#f6f8fa)"; };
  });
  const ta=$("askUserText");
  if(ta) ta.onkeydown=(e)=>{ if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); submit("【用户的输入】"+ta.value.trim()); } };
  const sb=$("askSubmit");
  if(sb) sb.onclick=()=>{
    const v=(ta?ta.value:"").trim();
    if(!v){ toast("请输入内容或点选上方选项","warn"); return; }
    submit("【用户的输入】"+v);
  };
  const sk=$("askSkip");
  if(sk) sk.onclick=()=>submit("用户跳过了这个问题，由你按最常见/最稳妥的默认方案自行决定，不要再重复询问。");
}

// ===== v1.0.327 =====
// ---------- ① 工作台工具栏折叠（localStorage 记忆） ----------
function toggleOpToolbar(){
  const extra = document.getElementById("opToolbarExtra");
  const btn = document.getElementById("opFoldBtn");
  if(!extra || !btn) return;
  const folded = extra.classList.toggle("folded");
  setFold(btn, !folded);            // v1.0.635：统一折叠原语（原来换字 ▸/▾）
  btn.title = folded ? "展开快捷提示词与模式栏" : "折叠快捷提示词与模式栏，给聊天内容腾地方";
  lsSet("__tbFold", folded ? "1" : "0");
}
(function restoreToolbarFold(){
  try{
    const v = localStorage.getItem("__tbFold");
    const fold = (v === null) ? true : (v === "1");   // v1.0.332：首次无记录默认折叠
    const extra=document.getElementById("opToolbarExtra"), btn=document.getElementById("opFoldBtn");
    if(extra && btn){
      extra.classList.toggle("folded", fold);          // v1.0.635：双向（原来只加不减）
      setFold(btn, !fold);
      btn.title = fold ? "展开快捷提示词与模式栏" : "折叠快捷提示词与模式栏，给聊天内容腾地方";
    }
  }catch(e){}
})();

// ---------- ④⑥ 会话系统前端 ----------
let __curConv = null;            // 当前会话 id（null=尚未有会话，发首条消息时自动创建）
let __convListCache = [];

// ---------- 多任务会话容器（v1.0.486）----------
// paneKey：「scratch」（工作台临时对话）|「conv_<id>」（具名会话）|「setChatPane」（助手对话）。
// opPane 容器化：每个 paneKey 一个 .conv-view 容器；切换会话只切 display——
// 后台会话的 SSE 流继续写自己的容器，真正并行；bar-top（状态条/停止/chip）跟随当前查看的会话。
function _normKey(k){ return (k==="opPane") ? (__curConv ? "conv_"+__curConv : "scratch") : k; }
function _isWork(k){ k=_normKey(k); return k==="scratch" || String(k).indexOf("conv_")===0; }
function _histKey(k){ k=_normKey(k); return k==="scratch" ? "opPane" : k; }   // 兼容旧 hist_opPane
function _paneEl(key){
  key = _normKey(key);
  if(key === "setChatPane"){ const sp = document.getElementById("setChatPane"); __bindStickEl(sp, key); return sp; }
  const host = document.getElementById("opPane"); if(!host) return null;
  let v = document.getElementById("cv_"+key);
  if(!v){
    v = document.createElement("div");
    v.className = "chatpane conv-view"; v.id = "cv_"+key; v.style.padding = "14px";
    v.dataset.panekey = key;
    // 首次创建：把 opPane 里既有的静态内容（空态占位/已渲染消息）收进第一个容器
    // v1.0.615：两处老毛病一起收——
    //  ① 旧代码在这里无条件 new 一个 div#cv_scratch，而 key==='scratch' 时上面刚
    //     new 过同一个 id 的 v（尚未入文档，getElementById 找不到）→ boot 后 DOM 里
    //     **两个 #cv_scratch**（实测：一个装着「企微伴侣 · 待命中」占位、一个空的），
    //     而 `_paneEl` 与 `#cv_x` 的选取都只认第一个 → 消息落在空的那个、占位留在另一个。
    //  ② 「↓ 回到最新」按钮挂在宿主上（宿主才是滚动容器，sticky 才生效），属界面外壳，
    //     不该被当成对话内容搬进容器里。
    const stray = [...host.children].filter(x =>
      !x.classList.contains("conv-view") && !x.classList.contains("jump-latest"));
    if(stray.length){
      let first = document.getElementById("cv_scratch");
      if(!first){
        if(key === "scratch"){ first = v; }                 // 就是刚建的那个，别再造一个同 id 的
        else {
          first = document.createElement("div");
          first.className = "chatpane conv-view"; first.id = "cv_scratch"; first.style.padding = "14px";
          first.dataset.panekey = "scratch";
        }
      }
      stray.forEach(x=>first.appendChild(x));
      if(first.parentElement !== host) host.appendChild(first);
    }
    host.appendChild(v);
  }
  __bindStickEl(v, key);
  return v;
}
function _showPane(key){
  key = _normKey(key);
  const host = document.getElementById("opPane"); if(!host) return;
  host.querySelectorAll(":scope > .conv-view").forEach(x=>{
    x.style.display = (x.id === "cv_"+key) ? "" : "none";
  });
  // 换会话后宿主的内容高度变了，按钮显隐与跟随状态要按新可见会话重算一次
  // （否则从长会话切到短会话，「↓ 回到最新」会挂在屏幕上不动；反向切则该出现的不出现）。
  const p = document.getElementById("cv_" + key);
  __syncJump(_scrEl(p || host));
}
function _activeKey(){ return _normKey("opPane"); }   // 当前查看的工作台会话

function _convTitleOf(m){
  const first = (m.messages||[]).find(x=>x.role==="user");
  return m.title || (first ? safeSlice(first.content,24) : "（空对话）");
}

async function loadConvList(silent){
  try{
    const d = await api("/api/conversations");
    __convListCache = d.conversations||[];
    renderConvList();
  }catch(e){ if(!silent) toast(fetchErr(e,"加载会话列表"),"err"); }
}

let __convBatchMode = false;      // v1.0.375 批量管理模式（勾选多选/全选/反选删除）
const __convSel = {};             // 会话id -> 是否勾选（跨重渲染保持）

function toggleConvBatch(){
  __convBatchMode = !__convBatchMode;
  if(!__convBatchMode){ for(const k in __convSel) delete __convSel[k]; }
  renderConvList();
}
function convPick(id, checked){
  __convSel[id] = !!checked;
  renderConvList();
}
function convSelectAll(){
  // v1.0.497：只全选进行中的会话——任务栏里归档条目不提供勾选（有 ⋯ 菜单单独操作）
  (__convListCache||[]).forEach(m=>{ if(!m.archived) __convSel[m.id] = true; });
  renderConvList();
}
function convSelectInvert(){
  (__convListCache||[]).forEach(m=>{ if(!m.archived) __convSel[m.id] = !__convSel[m.id]; });
  renderConvList();
}
async function convBatchDelete(){
  const ids = Object.keys(__convSel).filter(k=>__convSel[k]);
  if(!ids.length){ toast("先勾选要删除的对话","err"); return; }
  // v1.0.373（体验4）：原生 confirm → 统一自定义确认弹层
  confirmAsk("批量删除对话", "删除选中的 "+ids.length+" 条对话？消息不可恢复。", async ()=>{
    try{
      const d = await apiToast("/api/conversations/batch_delete", {method:"POST",
        headers:{"Content-Type":"application/json"}, body: JSON.stringify({ids: ids})}, "批量删除");
      // v1.0.592 修复（P0）：#opPane 是所有会话容器的宿主（_paneEl 把每个会话渲染成它下面的
      // cv_<key> 子节点），旧实现直接 opPane.innerHTML=占位，会连带抹掉其它后台会话的 DOM，
      // 却不 abort 其控制器、不删 cv_ 节点、不清 __barMemo——仍在跑的流会写进已移除的节点。
      // 改为与单条删除 deleteConversation 一致的逐会话清理。
      for(const id of ids){
        const ck = "conv_" + id;
        if(activeControllers[ck]){ try{ activeControllers[ck].abort(); }catch(e){} delete activeControllers[ck]; }
        const cv = document.getElementById("cv_" + ck);
        if(cv) cv.remove();
        delete __barMemo[ck];
      }
      if(ids.includes(__curConv)){
        __curConv = null;
        _showPane("scratch");
        const sp = _paneEl("scratch");
        if(sp) sp.innerHTML = `<div class="msg agent"><div class="mc">🗑 对话已删除。点左侧任务区「＋」开始新的对话。</div></div>`;
      }
      for(const k in __convSel) delete __convSel[k];
      toast("已删除 "+((d.deleted||[]).length)+" 条对话"+((d.missing||[]).length?("，"+d.missing.length+" 条已不存在"):""),"ok");
      loadConvList();
    }catch(_){}
  }, "删除");
}

function renderConvList(){
  // v1.0.497：conv-bar 与下拉列表已整行拆除，会话列表唯一渲染出口是侧栏任务栏；
  // 本函数收敛为纯转发（批量勾选状态、工具行、当前会话高亮全部由 renderTaskRail 承接）。
  if(typeof renderTaskRail==="function") renderTaskRail();
}

async function newConversation(){
  // v1.0.514（用户反馈"新建任务没有提示用什么模式"）：点「＋」新建同样先弹
  // 模式+命名弹窗——确认才创建；取消不建。任务名非必填，留空先空标题，
  // 首条消息发出后任务栏按内容回退显示（_convTitleOf）。
  const choice=await showFirstTaskModal("");
  if(!choice) return;
  setOpMode(choice.mode);
  try{
    const d = await apiToast("/api/conversations/create", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({title: choice.name||""})}, "新建对话");
    __curConv = d.id;
    // v1.0.486 多任务：新会话开独立容器视图（旧视图保留在后台，运行中的任务继续跑）
    const key = "conv_"+d.id;
    const pane = _paneEl(key);
    if(pane){
      pane.innerHTML = `<div class="msg agent"><div class="mc">🆕 新对话已创建——上下文从此开始。历史对话在左侧任务列表随时打开；其他会话运行中也可随时切换。</div></div>`;
      pane.dataset.loaded = "1";
    }
    _showPane(key);
    syncConvBar(key);
    // v1.0.497：新会话立即进侧栏任务栏（不等 10s 轮询），再后台拉权威列表校准
    if(!(__convListCache||[]).some(m=>m.id===d.id)){
      (__convListCache=__convListCache||[]).unshift({ id:d.id, title:"", archived:false, messages:[] });
    }
    renderConvList();
    loadConvList(true);
    toast("新对话已创建","ok");
  }catch(_){}
}

// v1.0.641：两条"从会话库渲染"的路径（switchConv / reloadPaneFromStore）共用这一个渲染器。
// linkify 只在这里做一次——原先两条都漏，同一个 bug 要修两遍本身就是分叉的信号。
// 用户实报"跑完当时能点，关掉软件再点开就不行"：走的就是这条从没 linkify 过的路径。
// v1.0.645（用户："当时的对话输出的版式是怎么样的，即使退出软件重新再点开那个任务，
// 版面也要一样"）：会话存档带 blocks（渲染日志）时按日志**重放**——步骤卡、产物文件行、
// 推理框、错误/已停止标记全部走实时流那同一批函数（addToolMsg / applyToolResult /
// createReasoningBox），不另写一套渲染器。
// 时间戳也在这里对齐：以前恢复历史时 addMsg 拿的是"现在"，重开一次所有气泡的时间全变。
function _renderStoredMsgs(key, msgs){
  const pane = _paneEl(key); if(!pane) return;
  pane.innerHTML = "";
  (msgs||[]).forEach(m=>{
    if(m.role==="user"){
      // display 优先：带附件的那轮，当时气泡里就有那几行引用，重开不能少
      addMsg(key, "user", m.display || m.content, null, true, m.ts||"", true); return;
    }
    if(m.role==="agent" || m.content){
      if((m.blocks||[]).length) _replayAgentMsg(key, m);
      else addMsg(key, "agent", m.content, null, true, m.ts||"", true);
    }
  });
  // 只对 agent 气泡做 linkify——与实时流同范围（done 分支就只处理那一个 .mc）。
  // 整块 pane 一起处理会让步骤卡结果行里的路径也长出按钮，"重开比当时多东西"同样是版式不一致。
  try{
    pane.querySelectorAll(".msg.agent .mc").forEach(function(mc){ linkifyWorkspacePaths(mc); });
  }catch(e){}
  __scrollBottom(key, true);   // 上面每条都传了 noScroll，这里一次性滚到底
}

// 重放一条 agent 消息。blocks 由 routes/chat.py::_memory_watch 在流式过程中采集，
// 裁剪与字段白名单在 services/conversations.py。
function _replayAgentMsg(key, m){
  let reason=null, stat=null; const notes=[];
  (m.blocks||[]).forEach(b=>{
    if(!b || !b.k) return;
    if(b.k==="tool"){
      const el=addToolMsg(key, b.name, null, null, {step:b.step, argsText:b.args});
      // 没有结果就是"当时也还没等到结果"——停止/断流那一档，卡片该保持 🔄 执行中，
      // 不能因为重放就凭空盖一个 ✅（那是把没发生的事画出来）。
      if(b.res != null) applyToolResult(el, {res:b.res, ok:b.ok, path:b.path});
    }
    else if(b.k==="reason") reason=b;
    else if(b.k==="stat") stat=b;      // v1.0.650 F2：耗时/次数/轮次/token 成本
    else notes.push(b);
  });
  let text=m.content||"";
  if(notes.some(b=>b.k==="stopped")) text += "\n\n（已停止）";
  const el=addMsg(key, "agent", text, null, true, m.ts||"", true);
  if(stat && el) applyRunStat(el, stat, (m.content||"").length);
  if(reason && el){
    try{
      const box=createReasoningBox(el);
      box.querySelector(".reasoning-summary").textContent="🧠 推理过程（点击展开）";
      box.querySelector(".reasoning-tail").textContent=reason.tail||"";
      box.title="本轮思考共 "+(reason.n||0)+" 字";
    }catch(e){}
  }
  // 收尾标记排在正文之后——与实时流一致（错误/上限提示都出现在回答下面）。
  // 「继续」按钮不在这里挂：断点不随历史会话存档，重开后它指向的已经不是可续的状态，
  // 挂一个点了会报错的按钮比不挂更糟。
  notes.forEach(b=>{
    if(b.k==="err") addMsg(key, "err", "错误："+(b.text||""), null, true, m.ts||"", true);
    else if(b.k==="cont") addMsg(key, "agent", b.text||"", null, true, m.ts||"", true);
    else if(b.k==="more") addMsg(key, "agent", "↑ 更早 "+(b.n||0)+" 步未记录（本版起新任务会完整复原）", null, false, "", true);
  });
}
async function switchConv(id){
  // v1.0.486 多任务并行：**不再杀流**——每个会话有独立容器（cv_conv_<id>）与独立
  // controller/断点（后端 pane_id=conv_<id>），切换只是显示层切换；后台会话的
  // 生成继续跑，会话列表标 ⏳，切回时输出都在。（v1.0.361 P17 的「detached DOM」
  // 问题随容器化根治——流持有的 DOM 一直挂在 opPane 下。）
  const key = "conv_"+id;
  try{
    __curConv = id;
    // v1.0.590：切任务时刷新 composer 上的模型/权限下拉标签。两者都是"按 pane 记忆、
    // 发送时才取值"，取值一直是对的，但标签此前不跟着切——切到任务 B 仍显示任务 A 的
    // 模型/权限档，用户会以为没生效。
    renderModelDD(); renderPermDD(); renderThinkDD();
    const pane = _paneEl(key);
    if(pane && !pane.dataset.loaded && !activeControllers[key]){
      const d = await apiToast("/api/conversations/messages?id="+encodeURIComponent(id), undefined, "切换对话");
      _renderStoredMsgs(key, d.messages);
      if(!(d.messages||[]).length){
        pane.innerHTML = `<div class="msg agent"><div class="mc">（此对话暂无内容）</div></div>`;
      }
      pane.dataset.loaded = "1";
    }
    // v1.0.615：滚底必须在 _showPane 之后——切换前该会话还是 display:none，
    // 内容高度算不出来，那时的 scrollTop 赋值等于什么都没做（用户反馈的
    // 「切完对话看不到最新一条 / 没有回最新的入口」有这一半原因）。
    _showPane(key);
    if(pane) __scrollBottom(key, true);
    syncConvBar(key);
    renderConvList();
    toast("已切换对话","ok");
  }catch(_){}
}
// 返回「临时对话」（scratch 视图）：__curConv=null
function openScratchConv(){
  __curConv = null;
  _showPane("scratch");
  syncConvBar("scratch");
  renderConvList();
}

async function deleteConversation(id, after, confirmTitle){
  const target = id || __curConv;
  if(!target){ toast("没有可删除的对话","err"); return; }
  // v1.0.373（体验4）：原生 confirm → 统一自定义确认弹层
  // v1.0.642：标题可由调用方给——从「已归档任务」进来时叫「永久删除归档任务」，
  // 弹层文案和点的那个按钮得是同一件事，否则用户会怀疑删错了地方。
  confirmAsk(confirmTitle || "删除对话", "删除该对话？消息不可恢复。", async ()=>{
    try{
      await apiToast("/api/conversations/delete", {method:"POST",
        headers:{"Content-Type":"application/json"}, body: JSON.stringify({id: target})}, "删除对话");
      if(target === __curConv){
        __curConv = null;
      }
      // v1.0.486：容器与控制器一并清理
      const ck = "conv_"+target;
      if(activeControllers[ck]){ try{ activeControllers[ck].abort(); }catch(e){} delete activeControllers[ck]; }
      const cv = document.getElementById("cv_"+ck);
      if(cv) cv.remove();
      if(target === __curConv || !__curConv){
        _showPane("scratch");
        const sp = _paneEl("scratch");
        if(sp) sp.innerHTML = `<div class="msg agent"><div class="mc">🗑 对话已删除。点左侧任务区「＋」开始新的对话。</div></div>`;
        delete __barMemo[ck];
      }
      loadConvList();
      toast("已删除","ok");
      // v1.0.642：「设置 → 已归档任务」的永久删除走的就是这一个删除路径
      // （不再另写一份 fetch+清理，两份删除代码迟早对不上），删完回调刷新那份列表。
      if(after){ try{ after(); }catch(e){} }
    }catch(_){}
  }, "删除");
}

// ---------- v1.0.642「设置 → 已归档任务」：归档即离开侧栏，这里是唯一的落脚地 ----------
// 数据源直接用 /api/conversations（已带 archived 标记与消息条数），不另开一个
// "归档列表"接口——两处各持一份真相，改名/删除之后迟早对不上。
async function loadArchivedList(){
  const box = document.getElementById("archivedListBox"); if(!box) return;
  const cntEl = document.getElementById("archivedCount");
  try{
    const d = await getJson("/api/conversations");
    const arch = (d.conversations || []).filter(c => c.archived);
    if(cntEl) cntEl.textContent = arch.length ? "（" + arch.length + "）" : "";
    if(!arch.length){
      box.innerHTML = '<div class="sub">还没有归档的任务。在侧栏任务上点「⋯ →  归档」，'
                    + '它就会离开任务列表、到这里等你。</div>';
      return;
    }
    box.innerHTML = '<div style="font-size:var(--fs-sm)">' + arch.map(c => {
      const t = escapeHtml(c.title || "（未命名任务）");
      const n = (c.messages || []).length;
      return '<div style="padding:7px 4px;border-top:1px solid var(--border);display:flex;'
           + 'align-items:center;gap:8px">'
           + '<span style="min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;'
           + 'white-space:nowrap" title="' + t + '">' + t + '</span>'
           + '<span class="sub" style="font-size:var(--fs-xs);flex:0 0 auto">'
           + escapeHtml(String(c.updated || "").slice(0, 16)) + ' · ' + n + ' 条</span>'
           + '<button class="ghost smbtn" style="flex:0 0 auto" '
           + 'onclick="restoreArchived(\'' + jsStr(c.id) + '\')">↩️ 恢复</button>'
           + '<button class="ghost smbtn danger" style="flex:0 0 auto" '
           + 'onclick="purgeArchived(\'' + jsStr(c.id) + '\')">🗑 永久删除</button>'
           + '</div>';
    }).join("") + '</div>';
  }catch(e){
    box.innerHTML = '<div class="sub">加载失败：' + escapeHtml(fetchErr(e, "已归档任务")) + '</div>';
  }
}
async function restoreArchived(id){
  try{
    await apiToast("/api/conversations/unarchive", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({id: id})}, "恢复归档任务");
    toast("已恢复到任务列表","ok");
    loadArchivedList();
    loadConvList();
  }catch(_){}
}
function purgeArchived(id){
  // 复用 deleteConversation 那一条删除路径（确认弹层、abort、容器清理、__curConv 复位都在里面）
  deleteConversation(id, loadArchivedList, "永久删除归档任务");
}

async function archiveConversation(id){
  const target = id || __curConv;
  if(!target){ toast("没有可归档的对话","err"); return; }
  try{
    const d = await apiToast("/api/conversations/archive", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({id: target})}, "归档对话");
    if(d.summary_note) toast("已归档 · 经验总结已沉淀到记忆，可在「设置 → 已归档任务」找回","ok",3600);
    else toast("已归档 · 已离开任务列表，可在「设置 → 已归档任务」找回","ok",3600);
    loadConvList();
  }catch(_){}
}

// 发消息时自动建会话 + 带 conv_id
// v1.0.512（用户需求）：刚打开软件直接发消息 → 先弹窗让其选择三工作模式之一
// （直接干/先出方案/只问答）并命名任务——任务名字非必填，留空按首行内容自动生成。
// 确认才建会话并发送；取消则中止（文本保留在输入框）。
// v1.0.514：每个新建任务都弹窗（原每启动只弹一次，用户反馈新建任务没提示选模式）。
// 附带修复：原实现在 scratch 视图下创建会话后不切换容器——消息写进隐藏的
// cv_conv_* 容器且 scratch 空态仍可见；现在创建后立即 _showPane 切换。
async function _createTaskConv(title){
  try{
    const d=await postJson("/api/conversations/create",{title:String(title||"").slice(0,60)});
    if(d.ok){
      __curConv=d.id;
      __convListCache.unshift({id:d.id,title:title||"",messages:[],updated:new Date().toISOString().slice(0,19).replace("T"," "),archived:false});
      renderConvList();
      _showPane("conv_"+d.id);
      syncConvBar("conv_"+d.id);
      return true;
    }
  }catch(e){}
  return false;
}
// 任务名推导：取第一行非空文本，去首尾标点，压缩空白，截 18 字
function _deriveTaskName(text){
  let t=String(text||"").split("\n").map(s=>s.trim()).filter(Boolean)[0]||String(text||"").trim();
  t=t.replace(/^[，。、；：！？,.:;!\s]+/,"").replace(/[，。、；：！？,.:;!]+$/,"").replace(/\s+/g," ").trim();
  if(!t) return "";
  // v1.0.616：按码点截断——`t.slice(0,18)` 会把 emoji（代理对）切成半个字符，
  // 任务名末尾出现方块乱码（用户给的任务描述里带 emoji 很常见）。
  const cps = Array.from(t);
  return cps.length > 18 ? cps.slice(0, 18).join("") + "…" : t;
}
// 首次任务弹窗：选模式 + 任务名（非必填）。resolve({mode,name}) 或取消 resolve(null)
function showFirstTaskModal(text){
  return new Promise((resolve)=>{
    if(document.getElementById("firstTaskModal")) return resolve(null);
    const sug=_deriveTaskName(text);
    const ov=document.createElement("div");
    ov.id="firstTaskModal";
    ov.className="modal-veil";
    const modes=[["agent","🤖 直接干",_MODE_HINT.agent],["plan","📋 先出方案",_MODE_HINT.plan],["ask","💬 只问答",_MODE_HINT.ask]];
    const _curPerm = _panePermGet("opPane");
    const permRows = __PERM_LEVELS.map(l=>
      `<label class="ftm-mode" data-perm="${l[0]}" style="display:flex;gap:9px;align-items:flex-start;padding:8px 11px;border:1px solid var(--border,#e4e8ef);border-radius:var(--r-md);cursor:pointer">`+
        `<input type="radio" name="ftmPerm" value="${l[0]}" ${_curPerm===l[0]?"checked":""} style="margin-top:2px" />`+
        `<span><b style="font-size:var(--fs-sm)">${__PERM_ICON[l[0]]} ${escapeHtml(l[1])}</b><br/>`+
        `<span class="ftm-desc" style="font-size:var(--fs-xs);color:var(--muted,#8792a3)">${escapeHtml(l[2])}</span></span></label>`).join("");
    ov.innerHTML=
      `<div class="modal-card" style="--mw:440px">`+
        `<div style="font-weight:600;font-size:var(--fs-md);margin-bottom:4px">开始新任务</div>`+
        `<div style="font-size:var(--fs-sm);color:var(--muted,#8792a3);margin-bottom:12px">选择工作模式与权限级别；任务名字可留空，将按发送内容自动生成</div>`+
        `<div style="font-size:var(--fs-sm);color:var(--muted,#8792a3);margin:0 0 5px">任务名称（非必填）</div>`+
        `<input id="ftmName" value="${escapeHtml(sug)}" placeholder="不填则按内容自动命名" style="width:100%;padding:8px 10px;border:1px solid var(--border,#e4e8ef);border-radius:var(--r-md);font-size:var(--fs-md);outline:none;background:var(--panel2,#fafbfd);color:var(--ink,#1c2333)" />`+
        `<div style="display:flex;flex-direction:column;gap:6px;margin:12px 0 10px">`+
          modes.map((m,i)=>`<label class="ftm-mode" data-mode="${m[0]}" style="display:flex;gap:9px;align-items:flex-start;padding:9px 11px;border:1px solid var(--border,#e4e8ef);border-radius:var(--r-md);cursor:pointer">`+
            `<input type="radio" name="ftmMode" value="${m[0]}" ${i===0?"checked":""} style="margin-top:2px" />`+
            `<span><b style="font-size:var(--fs-md)">${m[1]}</b><br/><span class="ftm-desc" style="font-size:var(--fs-xs);color:var(--muted,#8792a3)">${escapeHtml(m[2])}</span></span></label>`).join("")+
        `</div>`+
        `<div style="font-size:var(--fs-sm);color:var(--muted,#8792a3);margin:0 0 5px">权限级别（agent 动手前要不要先问你）</div>`+
        `<div style="display:flex;flex-direction:column;gap:5px;margin:0 0 14px">`+ permRows + `</div>`+
        `<div class="row" style="gap:8px;justify-content:flex-end">`+
          `<button class="ghost" id="ftmCancel">取消</button>`+
          `<button class="primary" id="ftmGo">开始任务</button>`+
        `</div>`+
      `</div>`;
    document.body.appendChild(ov);
    const close=()=>{ if(ov.parentNode) ov.remove(); };
    // v1.0.514 修复（用户反馈"选中那种字体颜色很浅看不出来"）：选中卡高亮——
    // 蓝边框 + 浅蓝底 + 标题转深色加粗，未选中恢复灰
    const syncSel=()=>{
      ov.querySelectorAll(".ftm-mode").forEach(l=>{
        const on=l.querySelector("input").checked;
        l.style.borderColor=on?"var(--accent-fg)":"var(--border,#e4e8ef)";
        l.style.background=on?"var(--accent-soft,#eaf1ff)":"transparent";
        const b=l.querySelector("b"); if(b) b.style.color=on?"var(--accent-fg)":"var(--ink,#1c2333)";
        const d=l.querySelector(".ftm-desc"); if(d) d.style.color=on?"var(--ink,#1c2333)":"var(--muted,#8792a3)";
      });
    };
    ov.querySelectorAll(".ftm-mode").forEach(l=>{
      l.addEventListener("click",()=>{ const rd=l.querySelector("input"); if(rd) rd.checked=true; syncSel(); });
      l.querySelector("input").addEventListener("change",syncSel);
    });
    syncSel();
    const confirmGo=()=>{
      const mode=(ov.querySelector("input[name=ftmMode]:checked")||{}).value||"agent";
      const perm=(ov.querySelector("input[name=ftmPerm]:checked")||{}).value||_curPerm||"auto";
      const name=($("ftmName").value||"").trim();
      close(); resolve({mode:mode, name:name, permission:_permNorm(perm)||"auto"});
    };
    $("ftmCancel").onclick=()=>{ close(); resolve(null); };
    $("ftmGo").onclick=confirmGo;
    $("ftmName").addEventListener("keydown",(e)=>{ if(e.key==="Enter"){ e.preventDefault(); confirmGo(); } });
    ov.addEventListener("keydown",(e)=>{ if(e.key==="Escape"){ close(); resolve(null); } });
    setTimeout(()=>{ const i=$("ftmName"); if(i){ i.focus(); i.select(); } },50);
  });
}
// 无会话时的统一闸门：每个新建任务都弹窗选模式+命名（v1.0.514 用户反馈：
// "新建任务没有提示用什么模式"——原实现每启动只弹一次，之后静默）。
// 任务名非必填，留空按内容自动生成。返回 false=用户取消，调用方应中止发送。
async function _ensureTaskGate(text){
  if(__curConv) return true;
  const choice=await showFirstTaskModal(text);
  if(!choice) return false;
  setOpMode(choice.mode);
  // v1.0.590：权限级别在建会话前落盘——新会话按「全局最近手选」继承同一档位
  // （与每任务独立模型的 v1.0.573 继承机制同一条路）
  _panePermSet("opPane", choice.permission || "auto");
  await _createTaskConv(choice.name || _deriveTaskName(text));
  return true;
}
(async function patchSendWithConv(){
  const _origSend = window.sendOperate;
  if(typeof _origSend !== "function") return;
  // v1.0.517：快捷入口/自动任务点击置位 __presetDirect——预设触发属于"直接干"，
  // 本次发送跳过模式选择弹窗（02-nav-quicklinks.js 在点击时已 setOpMode("agent")）
  window.__presetDirect = false;
  window.sendOperate = async function(){
    const _text=(($("opInput")||{}).value||"").trim();
    if(!__curConv && _text){
      if(window.__presetDirect){
        window.__presetDirect = false;   // 一次性：仅跳过紧随预设点击的那次发送
        await _createTaskConv(_deriveTaskName(_text));
      } else {
        const ok=await _ensureTaskGate(_text);
        if(!ok) return;   // 用户在弹窗取消：不发送
      }
    }
    // 给即将发出的 body 注入 conv_id：拦截 streamChat 内的 fetch 不可行，改为改写 runQuickTask/sendOperate 的 body 构造——
    // 简化：把 conv_id 挂到全局，streamChat 组 body 时读取（见 10-chat.js streamChat patch）
    window.__pendingConvId = __curConv;
    return _origSend.apply(this, arguments);
  };
})();

// ---------- ⑤ 消息悬浮操作条已由气泡底部操作栏（v1.0.334）取代 ----------



// ---------- 每任务独立模型（v1.0.488 文字式下拉）----------
const __modelsCache = [];        // /api/models 缓存（id/name/enabled）
const __paneModel = {};          // paneKey -> model_id（""=默认）；懒加载 localStorage
function _paneModelGet(key){
  const nk = _normKey(key);
  if(!(nk in __paneModel)){
    let v = lsGet("selmodel_"+_histKey(key), "");
    __paneModel[nk] = v;
  }
  // v1.0.573（用户反馈"选择模型没用"）：该会话未显式选择时，回落到**全局最近一次
  // 手选的模型**（而非直接全局默认）——用户选过一次后新任务/新会话自动沿用，
  // 直到再选别的。每任务显式覆盖仍然优先。
  if(!__paneModel[nk]){
    __paneModel[nk] = lsGet("selmodel_global_last", "");
  }
  return __paneModel[nk] || "";
}
function _paneModelSet(key, mid){
  const nk = _normKey(key);
  __paneModel[nk] = mid || "";
  lsSet("selmodel_"+_histKey(key), __paneModel[nk]);
  // v1.0.573：手选动作同时记全局最近选择（新会话/新任务的继承来源）
  lsSet("selmodel_global_last", __paneModel[nk]);
  renderModelDD();
  renderThinkDD();     // v1.0.601：换了模型，思考档位的可用范围与提示也跟着换
}
function _modelNameOf(mid){
  const m = (__modelsCache||[]).find(x=>x.id===mid);
  return m ? (m.name||mid) : null;
}
function _modelSelValue(paneId){ return _paneModelGet(paneId); }   // streamChat 发送读取
// ==================== v1.0.590 权限级别（询问范围轴） ====================
// 三档与「每任务独立模型」同构：按任务记忆 + 全局最近手选继承，随 /chat 与
// /chat/continue 发送；后端 agent_loop 解析后写入 pane 侧表并随断点持久化，
// 挂起在审批弹窗后点「继续」不会掉回默认档。
// 与「命令模式」正交：命令模式管*命中危险规则后怎么处置*（拦截/审批/放行），
// 权限级别管*平时要不要先问*。选「完全访问」不会解除致命命令与系统保护目录的拦截。
const __PERM_LEVELS = [
  ["ask_every", "每次询问", "执行命令、修改文件、操作页面或访问网络前，始终先问你"],
  ["auto", "自动审批", "仅在检测到潜在风险时询问（危险命令 / 系统保护目录 / 批量删除 / 页面脚本）"],
  ["full", "完全访问", "不再询问，可自由访问你的文件、终端和网络"],
];
const __PERM_ICON = {ask_every:"🔒", auto:"🛡", full:"⚡"};
const __PERM_LABEL = {ask_every:"每次询问", auto:"自动审批", full:"完全访问"};
const __PERM_DESC = {};
__PERM_LEVELS.forEach(l=>{ __PERM_DESC[l[0]] = l[2]; });
const __panePerm = {};
// 全局默认档：来自 设置→安全中心→权限级别（config.security.permission_level）。
// 未拉到时按 auto。没有这一层的话，设置页改了默认档会被前端兜底吃掉、永远不生效。
let __cfgPermDefault = "auto";
async function loadPermDefault(){
  try{
    const c = await getJson("/api/config");
    const lv = _permNorm(((c||{}).security||{}).permission_level);
    if(lv){ __cfgPermDefault = lv; renderPermDD(); }
  }catch(e){ /* 拉不到按 auto，不影响发送 */ }
}

function _permNorm(v){ return __PERM_LABEL[v] ? v : ""; }
function _panePermGet(key){
  const nk = _normKey(key);
  if(!(nk in __panePerm)){
    let v = ""; try{ v = _permNorm(localStorage.getItem("perm_"+_histKey(key))); }catch(e){}
    __panePerm[nk] = v;
  }
  if(!__panePerm[nk]){
    try{ __panePerm[nk] = _permNorm(localStorage.getItem("perm_global_last")); }catch(e){}
  }
  return __panePerm[nk] || __cfgPermDefault;
}
function _panePermSet(key, lv){
  lv = _permNorm(lv) || "auto";
  __panePerm[_normKey(key)] = lv;
  try{
    localStorage.setItem("perm_"+_histKey(key), lv);
    localStorage.setItem("perm_global_last", lv);
  }catch(e){}
  renderPermDD();
}
function permSelValue(paneId){ return _panePermGet(paneId); }   // 发送时读取
// ---------- v1.0.650 U3（用户："全部优化"）：三个下拉的键盘通路 ----------
// 模型/权限/思考的选项原来是纯 div：实测 tabIndex=-1、role=null，触发钮也没有
// aria-haspopup/expanded —— 键盘 Tab 直接跳过整份菜单，读屏软件念不出有什么可选项。
// 现在选项可聚焦、Enter/Space 等同点击、Esc 收起并把焦点还给触发钮。
// closeDDs/_syncDDAria 是两个新的单点：关菜单原来手抄 7 处、开合态同步要跟着一起改，
// 抄 7 遍必漏 1 遍（v1.0.590 标签重算就是同一笔账）。
function closeDDs(){
  document.querySelectorAll(".model-dd.open").forEach(x=>x.classList.remove("open"));
  _syncDDAria();
}
function _syncDDAria(){
  document.querySelectorAll(".model-dd").forEach(dd=>{
    const btn = dd.querySelector(".model-dd-btn");
    if(btn) btn.setAttribute("aria-expanded", dd.classList.contains("open") ? "true" : "false");
  });
}
function ddItemKey(ev){
  if(ev.key==="Enter" || ev.key===" "){
    ev.preventDefault();
    ev.currentTarget.click();
  } else if(ev.key==="Escape"){
    closeDDs();
    const dd = ev.currentTarget.closest(".model-dd");
    const btn = dd && dd.querySelector(".model-dd-btn");
    if(btn) btn.focus();
  }
}
document.addEventListener("keydown", (ev)=>{
  if(ev.key!=="Escape") return;
  const open = document.querySelector(".model-dd.open");
  if(!open) return;
  closeDDs();
  const btn = open.querySelector(".model-dd-btn");
  if(btn) btn.focus();
});
function renderPermDD(){
  const put = (id, key)=>{
    const el = document.getElementById(id); if(!el) return;
    const lv = _panePermGet(key);
    el.textContent = (__PERM_ICON[lv]||"🛡") + " " + (__PERM_LABEL[lv]||"自动审批");
    el.title = "权限级别：" + (__PERM_DESC[lv]||"") + "\n与「设置 → 安全中心 → 危险命令」的处置模式相互独立。";
  };
  put("opPermName", _activeKey());
  put("setPermName", "setChatPane");
}
function renderPermMenus(){
  const list = (which)=>{
    const cur = _panePermGet(which==="set" ? "setChatPane" : _activeKey());
    return __PERM_LEVELS.map(l=>{
      const sel = cur===l[0] ? " sel" : "";
      return `<div class="mdd-item perm${sel}" role="menuitem" tabindex="0" aria-selected="${cur===l[0]}"`+
             ` onclick="pickPerm('${which}','${l[0]}')" onkeydown="ddItemKey(event)">`+
             `<span><b>${__PERM_ICON[l[0]]} ${escapeHtml(l[1])}</b>`+
             `<span class="mdd-desc">${escapeHtml(l[2])}</span></span>`+
             `<span class="mdd-chk">${cur===l[0]?"✅":""}</span></div>`;
    }).join("");
  };
  const m1 = document.getElementById("opPermMenu"); if(m1) m1.innerHTML = list("op");
  const m2 = document.getElementById("setPermMenu"); if(m2) m2.innerHTML = list("set");
}
function togglePermDD(ev, which){
  ev && ev.stopPropagation();
  const dd = document.getElementById(which==="set" ? "setPermDD" : "opPermDD");
  if(!dd) return;
  const opening = !dd.classList.contains("open");
  closeDDs();
  dd.classList.toggle("open", opening);
  _syncDDAria();
  // 打开即按当前 pane 重算标签与菜单：__curConv 有多个赋值点（新建/删除/切回/scratch），
  // 逐处加刷新容易漏；在用户正要看的这一刻重算最稳。
  if(opening) renderPermDD();
  if(opening) renderPermMenus();
}
function pickPerm(which, lv){
  const key = which==="set" ? "setChatPane" : _activeKey();
  _panePermSet(key, lv);
  closeDDs();
  toast("本任务权限级别：" + __PERM_LABEL[lv], lv==="full" ? "warn" : "ok", 2000);
  if(lv === "full"){
    toast("⚠️ 完全访问只是不再逐次询问；致命命令与系统保护目录仍会被直接拒绝", "warn", 3600);
  }
}

// ---------- v1.0.601 思考档位（按任务记忆，只覆盖本次请求） ----------
// 此前思考参数只有「模型管理里逐模型配置」一个来源：用户在对话里想临时"这次别想了"
// 或"多想一会儿"，只能去改模型配置——而那份配置会影响所有任务。这里做请求级覆盖：
// 前端记住每个 pane 的选择 → body.thinking → 后端 with_thinking_override 只改本次
// 请求的模型浅拷贝，模型管理里的配置一个字节都不动。
const __THINK_LEVELS = [
  ["",        "🧠 跟随模型", "用「模型管理」里为该模型设好的思考参数（默认）"],
  ["off",     "🚫 关闭思考", "让模型直接给答案，不输出思考过程"],
  ["minimal", "🌱 最低",     "尽量少想（OpenAI o 系的 minimal 档）"],
  ["low",     "🌿 低",       "轻度思考，响应更快"],
  ["medium",  "🌳 中",       "折中：质量与速度兼顾"],
  ["high",    "🔥 高",       "充分思考，慢但更周全"],
];
const __THINK_LABEL = {};
__THINK_LEVELS.forEach(l=>{ __THINK_LABEL[l[0]] = l[1]; });
const __paneThink = {};
function _thinkNorm(v){
  v = String(v == null ? "" : v).toLowerCase();
  return __THINK_LEVELS.some(l=>l[0]===v) ? v : "";
}
function _paneThinkGet(key){
  const nk = _normKey(key);
  if(!(nk in __paneThink)){
    let v = ""; try{ v = _thinkNorm(localStorage.getItem("think_" + _histKey(key))); }catch(e){}
    __paneThink[nk] = v;
  }
  return __paneThink[nk];
}
function _paneThinkSet(key, v){
  v = _thinkNorm(v);
  __paneThink[_normKey(key)] = v;
  lsSet("think_" + _histKey(key), v);
  renderThinkDD();
}
function thinkSelValue(paneId){ return _paneThinkGet(paneId); }   // 发送时读取
// 当前选中模型的能力位：能不能关思考、支持哪几档强度。拿不到模型信息时全给
// （宁可不裁剪，也不把用户能用的档位藏掉）。
function _thinkCapsFor(key){
  const out = { canOff: true, levels: ["minimal","low","medium","high"], named: false,
                effort: "", thinking: null };
  let m = null;
  try{
    const mid = _modelSelValue(key);
    m = mid ? (__modelsCache||[]).find(x=>x.id===mid) : null;
  }catch(e){ return out; }
  if(!m) return out;
  const caps = m.caps || {};
  if(caps.can_disable_thinking === false) out.canOff = false;
  if(Array.isArray(m.thinking_supported_levels) && m.thinking_supported_levels.length){
    out.levels = m.thinking_supported_levels.map(x=>String(x).toLowerCase());
    out.named = true;
  }
  out.effort = m.reasoning_effort || "";
  out.thinking = (typeof m.enable_thinking === "boolean") ? m.enable_thinking : null;
  return out;
}
function renderThinkDD(){
  const put = (id, key)=>{
    const el = document.getElementById(id); if(!el) return;
    const v = _paneThinkGet(key);
    el.textContent = v ? __THINK_LABEL[v] : "🧠 跟随模型";
    el.title = "思考：关闭或调整推理强度（只作用于本任务，不改模型管理里的配置）\n"
             + (v ? "本次覆盖为「" + __THINK_LABEL[v] + "」" : "跟随模型自身配置");
  };
  put("opThinkName", _activeKey());
  put("setThinkName", "setChatPane");
}
function renderThinkMenus(){
  const list = (which)=>{
    const key = which==="set" ? "setChatPane" : _activeKey();
    const cur = _paneThinkGet(key);
    const caps = _thinkCapsFor(key);
    let h = __THINK_LEVELS.map(l=>{
      const v = l[0];
      if(v === "off" && !caps.canOff) return "";
      if(v && v !== "off" && caps.levels.indexOf(v) < 0) return "";
      return `<div class="mdd-item think${cur===v?" sel":""}" role="menuitem" tabindex="0" aria-selected="${cur===v}"`+
             ` onclick="pickThink('${which}','${v}')" onkeydown="ddItemKey(event)">`+
             `<span><b>${escapeHtml(l[1])}</b><span class="mdd-desc">${escapeHtml(l[2])}</span></span>`+
             `<span class="mdd-chk">${cur===v?"✅":""}</span></div>`;
    }).join("");
    h += `<div class="mdd-desc" style="padding:6px 10px 2px">是否真生效取决于服务商：`
       + `OpenAI 系认 reasoning_effort，Qwen/vLLM 认 enable_thinking；`
       + (caps.effort || caps.thinking !== null
          ? `当前模型配置为 ${escapeHtml(caps.thinking===false ? "思考关" : (caps.effort || "思考开"))}。`
          : `当前模型未声明思考参数。`)
       + `</div>`;
    return h;
  };
  const m1 = document.getElementById("opThinkMenu"); if(m1) m1.innerHTML = list("op");
  const m2 = document.getElementById("setThinkMenu"); if(m2) m2.innerHTML = list("set");
}
function toggleThinkDD(ev, which){
  ev && ev.stopPropagation();
  const dd = document.getElementById(which==="set" ? "setThinkDD" : "opThinkDD");
  if(!dd) return;
  const opening = !dd.classList.contains("open");
  closeDDs();
  dd.classList.toggle("open", opening);
  _syncDDAria();
  if(opening){ renderThinkDD(); renderThinkMenus(); }
}
function pickThink(which, v){
  const key = which==="set" ? "setChatPane" : _activeKey();
  _paneThinkSet(key, v);
  closeDDs();
  toast(v ? ("本任务思考档位：" + __THINK_LABEL[v]) : "已恢复跟随模型配置", "ok", 2200);
}
function renderModelDD(){
  const wk = _modelNameOf(_paneModelGet(_activeKey()));  const w1 = document.getElementById("opModelName");
  if(w1){ w1.textContent = wk || "默认模型"; w1.title = wk || "使用模型管理页设置的默认模型"; }
  const st = _modelNameOf(_paneModelGet("setChatPane"));
  const w2 = document.getElementById("setModelName");
  if(w2){ w2.textContent = st || "默认模型"; }
}
function renderModelMenus(){
  const mk = (key)=> _paneModelGet(key);
  const item = (which, mid, name)=>{
    const cur = mk(which==="set"?"setChatPane":_activeKey());
    const sel = cur===mid ? " sel" : "";
    // v1.0.588：mid 走 jsStr 而非 escapeHtml——模型 id 可能来自「导入配置」的外部 JSON，
    // 而这里是 HTML 属性里的 JS 字符串字面量位。escapeHtml 把 ' 转成 &#39;，属性值会先被
    // HTML 解码回真正的 ' 再交给 JS 解析，等于没防住跳出字面量（同 v1.0.373 P2-4 根因）。
    // v1.0.650 U3：补 role/tabindex/aria-selected + 键盘 Enter/Space（原来是纯 div，Tab 进不去）。
    return `<div class="mdd-item${sel}" role="menuitem" tabindex="0" aria-selected="${cur===mid}"`+
           ` onclick="pickModel('${which}','${jsStr(mid)}')" onkeydown="ddItemKey(event)"`+
           ` title="${escapeHtml(name)}"><span>${escapeHtml(name)}</span><span class="mdd-chk">${mid?(cur===mid?"✅":""):cur===""?"✅":""}</span></div>`;
  };
  const list = (which)=>{
    let h = item(which, "", "🧠 默认模型（全局设置）");
    (__modelsCache||[]).filter(m=>m.enabled!==false).forEach(m=>{ h += item(which, m.id, m.name); });
    return h;
  };
  const m1 = document.getElementById("opModelMenu"); if(m1) m1.innerHTML = list("op");
  const m2 = document.getElementById("setModelMenu"); if(m2) m2.innerHTML = list("set");
}
function toggleModelDD(ev, which){
  ev && ev.stopPropagation();
  const dd = document.getElementById(which==="set"?"setModelDD":"opModelDD");
  if(!dd) return;
  const opening = !dd.classList.contains("open");
  closeDDs();
  dd.classList.toggle("open", opening);
  _syncDDAria();
  if(opening) renderModelDD();   // v1.0.590：打开时按当前 pane 重算标签（切任务后不再显示上一个任务的模型）
  if(opening) renderModelMenus();
}
function pickModel(which, mid){
  _paneModelSet(which==="set" ? "setChatPane" : _activeKey(), mid);
  closeDDs();
  toast("本任务模型："+(_modelNameOf(mid)||"默认模型"), "ok", 1500);
}
document.addEventListener("click", ev=>{
  if(!ev.target.closest(".model-dd"))
    closeDDs();
});
async function fillModelSels(){
  let ms = [];
  try{ ms = await getJson("/api/models"); }catch(e){ return; }
  if(!Array.isArray(ms)) return;
  __modelsCache.length = 0; ms.forEach(m=>__modelsCache.push(m));
  renderModelDD(); renderModelMenus();
}

// ---------- 任务栏（v1.0.489，对齐多任务并行）----------
function _relTime(s){
  if(!s) return "";
  const d = new Date(String(s).replace(/-/g,"/"));
  if(isNaN(d)) return String(s).slice(5,10);
  const diff = (Date.now()-d.getTime())/60000;
  if(diff < 1) return "刚刚";
  if(diff < 60) return Math.floor(diff)+" 分钟前";
  if(diff < 1440) return Math.floor(diff/60)+" 小时前";
  if(diff < 2880) return "昨天";
  return String(s).slice(5,10);
}
// v1.0.599：后台任务（批审）跑完后，把服务端新落的结果消息拉回当前视图。
// 不复用 switchConv：① 它有 `!pane.dataset.loaded` 缓存判断，正在看的会话不会重取；
// ② 它会 toast「已切换对话」并改任务栏选中态——对"原地刷新"是噪声。
function reloadPaneFromStore(key){
  key = _normKey(key);
  if(!String(key).startsWith("conv_")) return;          // scratch 无会话落点
  if(activeControllers[key]) return;                    // 该 pane 正在流式输出，别打断
  const pane = _paneEl(key); if(!pane) return;
  getJson("/api/conversations/messages?id=" + encodeURIComponent(key.slice(5)))
    .then(d=>{
      if(!d || !d.ok) return;
      const atBottom = __dist(pane) < 60;   // v1.0.615：按真滚动容器量，原来盯 cv_* 恒为 0
      _renderStoredMsgs(key, d.messages);
      pane.dataset.loaded = "1";
      if(atBottom) __scrollBottom(key, true);
      renderTaskRail();
    })
    .catch(()=>{ /* 刷新失败不影响看板与批审本身 */ });
}

function railOpen(key){
  // v1.0.491：任务栏在侧栏——非工作台视图点击任务时先跳回工作台
  const av = document.querySelector(".view.active");
  if(!av || av.id !== "view-work") openView("work");
  if(key === "scratch") openScratchConv();
  else switchConv(key.slice(5));
}
// v1.0.637：折叠任务区的**唯一写入点**。三件事必须同时发生，所以不许在别处各写一半：
//   ① .tasksec.folded —— 列表/检索收起；
//   ② .sidebar.rail-folded —— 放宽 .nav 的 58vh 上限（CSS 选不到父元素，只能挂在这）；
//   ③ 「➕ 更多」联动展开 —— 任务区让出来的高度正好是它展开后需要的，不接手的后果就是
//      用户截图里那块红框空白。
//   （v1.0.638 曾在这里补过第二级「⚡ 快捷入口」联动，v1.0.644 按用户要求撤掉：
//     分组已提到导航顶端、随时可达，"折叠任务区就自己摊开"不再是帮忙而是越权。）
function applyRailFold(fold){
  const sec = document.getElementById("taskSec"); if(!sec) return;
  const sb = sec.closest(".sidebar");
  sec.classList.toggle("folded", fold);
  if(sb) sb.classList.toggle("rail-folded", fold);
  setFold(document.getElementById("taskFoldBtn"), !fold);
  if(typeof forceNavMoreForRail === "function") forceNavMoreForRail(fold);
}
function toggleTaskRail(){
  const sec = document.getElementById("taskSec"); if(!sec) return;
  const fold = !sec.classList.contains("folded");
  applyRailFold(fold);
  lsSet("taskRailFold", fold?"1":"");
}
// ---------- C8（v1.0.598）任务检索 + 按天分组 ----------
// 会话数上去以后（今天 80+），一维长列表既找不到旧任务、也看不出哪些是今天动的。
// 检索只按标题子串过滤（不做全文：全文要拉消息体，侧栏每次刷新都扫一遍不划算）。
let __trQuery = "";
function trSearch(v){ __trQuery = String(v||"").trim().toLowerCase(); renderTaskRail(); }
function trSearchClear(){ __trQuery = ""; const el=document.getElementById("trSearchInput"); if(el) el.value=""; renderTaskRail(); }
function _trStamp(s){ const d = new Date(String(s||"").replace(/-/g,"/")); return isNaN(d) ? 0 : d.getTime(); }
function _trBucket(ts){
  if(!ts) return "更早";
  const now = new Date();
  const d0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if(ts >= d0) return "今天";
  if(ts >= d0 - 86400000) return "昨天";
  if(ts >= d0 - 6*86400000) return "近 7 天";
  return "更早";
}
function _trRunning(m){ return !!activeControllers["conv_"+(m && m.id)]; }
// v1.0.642：能进这里的只有**未归档**的行（归档项已在 renderTaskRail 处离场），
// 所以原先散在行内的 `m.archived` 分支（.arch 灰化、"已归档 · " 前缀、菜单的"取消归档"）
// 一并删掉——留着就是三条永远不会执行的死分支，而且会让人以为侧栏还会显示归档项。
function _trRow(m){
  const k = "conv_"+m.id, run = _trRunning(m), title = escapeHtml(_convTitleOf(m));
  // v1.0.497：批量模式——进行中的会话变勾选行，整行点击切换勾选，隐藏 ⋯ 菜单
  if(__convBatchMode){
    const sel = !!__convSel[m.id];
    return `<div class="tr-item${sel?" sel":""}" role="button" tabindex="0" aria-label="选择任务" onclick="convPick('${m.id}',${sel?"false":"true"})" title="${title}">`+
      `<div class="tr-t"><input type="checkbox" class="tr-ck" ${sel?"checked":""} onclick="event.stopPropagation();convPick('${m.id}',this.checked)"/>${run?'<span class="tr-run">⏳</span>':""}${title}</div>`+
      `<div class="tr-m">${_relTime(m.updated||"")}${run?" · 运行中":""}</div></div>`;
  }
  return `<div class="tr-item${k===_activeKey()?" act":""}${run?" run":""}" role="button" tabindex="0" aria-label="打开任务" onclick="railOpen('${k}')" title="${title}">`+
    `<div class="tr-t">${run?'<span class="tr-run">⏳</span>':""}${title}<button class="tr-more" onclick="trMenu(event,'${k}')" title="操作">⋯</button></div>`+
    `<div class="tr-m">${_relTime(m.updated||"")}${run?" · 运行中":""}</div></div>`;
}
// 一段列表 → 带日期分组头的 HTML。分组头只在变化时插，所以调用方给的顺序就是显示顺序
function _trSection(items, bucketOf){
  const parts = []; let last = null;
  items.forEach(m=>{
    const b = bucketOf(m);
    if(b !== last){ last = b; parts.push(`<div class="tr-day">${b}</div>`); }
    parts.push(_trRow(m));
  });
  return parts;
}
function renderTaskRail(){
  const list = document.getElementById("taskRailList"); if(!list) return;
  const parts = [];
  const sc = document.getElementById("cv_scratch");
  const scratchUsed = sc && (activeControllers["scratch"] || sc.querySelector(".msg"));
  // v1.0.497 批量管理（原 conv-bar「☑ 批量管理」迁入）：临时对话不可删，批量模式下不显示
  if(scratchUsed && !__convBatchMode){
    parts.push(`<div class="tr-item${_activeKey()==="scratch"?" act":""}" role="button" tabindex="0" aria-label="打开临时对话" onclick="railOpen('scratch')" title="未入会话库的临时对话">`+
      `<div class="tr-t">${activeControllers["scratch"]?'<span class="tr-run">⏳</span>':""}🗂 临时对话</div>`+
      `<div class="tr-m">${activeControllers["scratch"]?"运行中":""}</div></div>`);
  }
  const all = (__convListCache||[]).slice().sort((a,b)=>_trStamp(b.updated)-_trStamp(a.updated));
  // v1.0.642（用户："任务归档以后不在任务模块中显示，按归档逻辑：离开当前任务列表、
  // 保留在设置里的已归档任务中、之后在那里永久删除"）：归档项**整个不再进侧栏**。
  // 原先是"排在最后加一个「已归档」段"——段一多就把任务列表拉长，归档反而更像
  // "还没删的任务"；用户要的是归档即离场，找回的地方在设置。
  const hit = __trQuery ? all.filter(m=>String(_convTitleOf(m)||"").toLowerCase().indexOf(__trQuery)>=0) : all;
  const live = hit.filter(m=>!m.archived);
  const runN = live.filter(_trRunning).length;
  // v1.0.633：进行中的任务置顶并归进「⏳ 进行中」。跑了 40 分钟的任务按时间排会沉到
  // 几十条之后，而用户回到侧栏要找的正是它——它同时也在自己那天的桶里消失（只出现一次）。
  const ordered = __convBatchMode ? live : live.filter(_trRunning).concat(live.filter(m=>!_trRunning(m)));
  parts.push.apply(parts, _trSection(ordered, m => (runN && !__convBatchMode && _trRunning(m))
    ? "⏳ 进行中" : _trBucket(_trStamp(m.updated))));
  const n = live.length;
  if(!parts.length){
    parts.push(__trQuery ? `<div class="tr-empty">无匹配任务 · 共 ${all.filter(m=>!m.archived).length} 条</div>`
                         : `<div class="tr-empty">暂无任务 · 发一条消息即开始</div>`);
  }
  list.innerHTML = parts.join("");
  const cnt = document.getElementById("taskRailN");
  const liveTotal = all.filter(m=>!m.archived).length;
  const _tail = runN ? ` · ${runN} 进行中` : "";
  if(cnt) cnt.textContent = n ? (__trQuery ? `（${n}/${liveTotal}${_tail}）` : `（${n}${_tail}）`) : "";
  // 检索行：批量模式让位给勾选工具行
  const sr = document.getElementById("trSearchRow");
  if(sr) sr.style.display = __convBatchMode ? "none" : "";
  const si = document.getElementById("trSearchInput");
  if(si && document.activeElement !== si && si.value !== __trQuery) si.value = __trQuery;
  // v1.0.497：批量工具行 + ☑ 按钮态（原 conv-dd-toolbar 迁入侧栏）
  const tb = document.getElementById("trBatchOps");
  if(tb) tb.style.display = __convBatchMode ? "flex" : "none";
  const bb = document.getElementById("taskBatchBtn");
  if(bb){
    bb.classList.toggle("on", __convBatchMode);
    bb.textContent = __convBatchMode ? "❌" : "☑";
    bb.title = __convBatchMode ? "退出批量管理" : "批量管理：勾选多条会话批量删除";
  }
  const nEl = document.getElementById("convBatchN");
  if(nEl) nEl.textContent = String(Object.keys(__convSel).filter(k=>__convSel[k]).length);
  // v1.0.637：折叠态恢复挪到 js/02 的 initSidebarLayout()——侧栏状态不该等列表数据。
}

// ---------- 任务条目操作菜单（v1.0.493：⋯ → 归档/删除） ----------
const __trCtx = { key: "" };
function trMenu(ev, key){
  ev.stopPropagation();
  __trCtx.key = key;
  let m = document.getElementById("trCtxMenu");
  if(!m){
    m = document.createElement("div");
    m.id = "trCtxMenu"; m.className = "tr-ctx-menu";
    document.body.appendChild(m);
    document.addEventListener("click", e=>{ if(!e.target.closest("#trCtxMenu")) m.classList.remove("open"); });
  }
  // v1.0.642：这里只会给未归档的行弹菜单（归档项已离开侧栏），所以"取消归档"这一支删掉，
  // 归档的去处写进 title——用户点之前就知道它会从列表消失、以及去哪里找回。
  m.innerHTML =
    `<div class="trc-item" onclick="trCtxAct('archive')" title="归档后离开任务列表，可在「设置 → 已归档任务」里恢复或永久删除">📥 归档</div>`+
    `<div class="trc-item danger" onclick="trCtxAct('delete')">🗑 删除任务</div>`;
  m.classList.add("open");
  // 定位：贴点击点，右侧/底部越界翻转
  m.style.left = "0px"; m.style.top = "0px";
  const w = m.offsetWidth || 150, h = m.offsetHeight || 90;
  let x = ev.clientX + 4, y = ev.clientY - 6;
  if(x + w > window.innerWidth - 8) x = ev.clientX - w - 4;
  if(y + h > window.innerHeight - 8) y = window.innerHeight - h - 8;
  m.style.left = x + "px"; m.style.top = y + "px";
}
function trCtxAct(act){
  const key = __trCtx.key; if(!key) return;
  document.getElementById("trCtxMenu")?.classList.remove("open");
  const cid = key.slice(5);
  if(act === "archive") archiveConversation(cid);
  else if(act === "delete") deleteConversation(cid);
}

// v1.0.583：相似任务固化建议——模态弹窗版（原流内卡片不醒目）
function showSkillSuggest(d){
  const ov=$("skillSuggestOverlay"); if(!ov) return;
  $("ssCount").textContent=d.count||0;
  $("ssTitle").textContent=d.title||"相似任务";
  $("ssPrompt").value=d.prompt||"";
  ov.dataset.prompt=d.prompt||""; ov.dataset.title=d.title||"相似任务"; ov.dataset.count=d.count||0;
  ov.style.display="flex";
}
function skillSuggestDismiss(){
  const ov=$("skillSuggestOverlay"); if(ov) ov.style.display="none";
}
async function createSkillFromSuggest(btn){
  const ov=$("skillSuggestOverlay");
  const prompt=ov.dataset.prompt||"", title=ov.dataset.title||"相似任务", count=ov.dataset.count||0;
  btn.disabled=true; btn.textContent="创建中…";
  try{
    const r=await fetch("/api/skills/auto_create",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({prompt,title,count})});
    const d=await r.json();
    if(d.ok){ toast("✅ 技能「"+title+"」已创建，可在技能中心查看/编辑", "ok", 5000); skillSuggestDismiss(); }
    else { toast("创建失败："+(d.error||"未知原因"), "err", 5000); btn.disabled=false; btn.textContent="⚙️ 固化为技能"; }
  }catch(e){ toast(fetchErr(e,"创建技能"), "err", 5000); btn.disabled=false; btn.textContent="⚙️ 固化为技能"; }
}

// v1.0.616：登记四类确认弹窗的开启函数——队列排空时由 _drainModals 出队调用，
// 保证"A 会话弹窗未关时 B 会话的请求被静默丢弃"不再发生。
__MODAL_OPEN.skillReq = showSkillRequestModal;
__MODAL_OPEN.skillSrc = showSkillSourceModal;
__MODAL_OPEN.appr     = showApprovalModal;
__MODAL_OPEN.askUser  = showAskUserModal;

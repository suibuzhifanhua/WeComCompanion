// init
try{ if(localStorage.getItem("liveFold")==="1"){ const _lc=$("liveCard"); if(_lc){ _lc.classList.add("collapsed"); const _wg=_lc.closest(".work-grid"); if(_wg) _wg.classList.add("folded"); const _bf=$("liveFoldBtn"); if(_bf){ _bf.title="展开窗口栏"; setFold(_bf,false); } } } }catch(e){}   // v1.0.635：恢复折叠态时同步箭头（否则 ▾ 还指着"展开"）
refreshStatus(); refreshWindows(); loadWecomOpt(); loadQuickLinks();   // v1.0.633：分组折叠函数随「收进更多」删除（loadQuickLinks 内部已渲染两组）
try{ loadDashboard(); }catch(e){ console.warn('[dash] 看板加载失败(不影响主界面)', e); }   // v1.0.523-2：首次加载（防御：失败不中断初始化链）
initSidebarLayout();   // v1.0.499：侧栏折叠态/宽度恢复 + 拖拽条挂载
initSbDarkBtn();     // v1.0.505：侧栏深色切换按钮图标恢复
toggleFollow(); // 初始化「实时跟随」状态文本（默认开启）
checkCloseAsk(); // v1.0.581：启动关闭确认层常驻轮询（500ms，点 X 后 ~0.5s 内弹层）
try{ if(localStorage.getItem("opTargetMin")==="1"){ const _ot=$("opTargetHint"); if(_ot){ _ot.dataset.min="1"; _ot.classList.add("min"); } } }catch(e){}   // v1.0.562：状态条收起态恢复
restoreHistory("opPane"); restoreHistory("setChatPane");
// v1.0.615：两个对话面版开屏就把滚动监听 + 「↓ 回到最新」挂上。
// 此前只有走 _paneEl 的路径（发消息/收流）才绑定，纯恢复历史的 #setChatPane
// 一辈子没有按钮（实测 C7：面板里 40 条消息、上翻后 querySelector 拿不到按钮）。
try{ __bindStick("setChatPane"); }catch(e){}
restoreOpMode();   // v1.0.485：恢复上次的工作模式（直接干/先出方案/只问答）
fillModelSels();   // v1.0.486：每任务独立模型选择器
loadPermDefault(); // v1.0.590：拉全局权限级别默认档并渲染两个 composer 的权限下拉
renderThinkDD();   // v1.0.601：恢复各任务记忆的思考档位标签（值本身在 localStorage 里）
// 实时跟随心跳的说明跟着 FOLLOW_MS 走：改了常量，标题与状态文字一起变，不会自相矛盾
(function(){ const lb=document.querySelector(".follow-toggle");
  if(lb) lb.title = "开启后每 " + (FOLLOW_MS/1000) + " 秒自动刷新选中窗口的截图/正文/可操作元素；"
                  + "关闭则仅在选中窗口、点「刷新」或 agent 操作完成后刷新"; })();
loadConvList();    // v1.0.489：任务栏数据源
setInterval(()=>loadConvList(true), 10000);   // v1.0.493：任务栏标题/时间自动跟进（静默，失败不弹）
// v1.0.615：「↓ 回到最新」按钮挂在宿主 opPane 上（宿主才是滚动容器，sticky 才生效），
// 它属于界面外壳、不是对话内容——判"对话是否为空"必须只数内容节点，
// 否则按钮一出现，这两处 clearChat 就永远不触发（空态占位不再渲染）。
const __chatKids = (el) => el ? [...el.children].filter(x => !x.classList.contains("jump-latest")) : [];
if(!__chatKids($("opPane")).length) clearChat("opPane");
if(!__chatKids($("setChatPane")).length) clearChat("setChatPane");
setInterval(refreshStatus, 8000);
// 窗口列表 + 选中窗口内容（正文/元素/截图）每 3 秒自动刷新
// v1.0.602（用户要求 5s→3s）：心跳变密后一轮没跑完就撞上下一轮的概率上升，
// refreshSelection 里加了 __selBusy 重入守卫 + updateScreenshot 的"最新帧才落屏"，
// 否则 3 秒心跳会堆叠 CDP 请求、旧截图反过来覆盖新截图。
// v1.0.471（UI#4）：视图感知暂停——工作台不是当前视图时窗口列表/内容区刷新对用户不可见，
// 仍照常打 CDP（含数百 KB 截图）纯浪费；status 常显在侧栏保持全局刷新。
setInterval(()=>{
  // v1.0.510 优化（P2-11）：窗口最小化/系统锁屏（document.hidden）时跳过工作台轮询——
  // 用户看不到画面时每 3s 打一次 CDP 截图（数百 KB base64 + WS 往返）纯浪费；
  // 视图切走已有 workActive 守卫（v1.0.471），这里补上"整窗不可见"这一层。
  if(document.hidden) return;
  const vw = $("view-work");
  const workActive = vw && vw.classList.contains("active");
  if(workActive) refreshWindows();
  // 内容区（截图/正文/元素）仅在「实时跟随」开启时自动刷新；关闭时由选中窗口、手动刷新、agent 完成后回看触发
  if(workActive && __followOn && __opPick!=null){
    // agent 运行中也不再跳过刷新：截图/正文/元素均为独立 CDP 只读连接，不会干扰 agent 的点击输入，
    // 避免 agent 长时间运行（如答题空转）时预览画面被永久冻结、看起来「坏了不更新」。
    // v1.0.592 删掉此处的「清理 activeControllers["opPane"] 残留」防御：本刷新路径根本不读该表，
    // 而且 streamChat 存的键是 _normKey 后的 scratch/conv_<id>/setChatPane（opPane 只是 hist 兼容名），
    // 那句恒 undefined、从未生效。也不能改成遍历全表按 _t 清——_t 只在创建时置一次、流中不续期，
    // 会把跑满 3 分钟的活任务控制器误删（连带放开「正在生成中」重复发起与停止按钮失效）。
    refreshSelection();
  }
}, FOLLOW_MS);
// 白屏自愈：WebView2 偶发首次加载竞态导致 127.0.0.1 没起来，窗口就卡白屏；
// 这里 2.5s 后若关键状态区还全是空，强制 reload 一次（sessionStorage 限一次防循环）
setTimeout(()=>{
  // v1.0.526 修复（QA）：connBadge 已随侧栏三点化删除（恒空）——白屏自愈
  // 的空态判定退化为只看 winList；现显式只用 winList，语义恢复。
  const wl=$("winList");
  const empty = (!wl || !wl.textContent.trim());
  if(empty){
    if(!sessionStorage.__wblank){ sessionStorage.__wblank="1"; location.reload(); }
  } else { sessionStorage.removeItem("__wblank"); }
}, 2500);

// v1.0.616（审查 P2）：JS 拼出来的可点行（侧栏快捷入口 / 窗口行 / 任务行）此前是裸
// div + onclick，键盘完全够不到；补了 tabindex 之后还差最后一步——Enter/Space 不会
// 自动触发 click。统一在这里转接，原生控件（input/button/a[href]）不碰。
document.addEventListener("keydown", function(e){
  if(e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
  const t = e.target;
  if(!(t instanceof Element)) return;
  if(t.matches("input,textarea,select,button,a[href]")) return;
  if(t.getAttribute("tabindex") !== "0") return;
  e.preventDefault();                       // Space 默认是翻页滚动
  t.click();
});

// v1.0.471（UI#11）：全局快捷键——Esc 关闭弹层；Ctrl+1..9 按侧栏顺序切视图。
// 输入框/文本域内 Esc 也放行关弹层（弹层输入框 Esc 关闭是通用预期），但
// Ctrl+数字 在输入框内不拦截（避免干扰输入）。
document.addEventListener("keydown", function(e){
  if(e.key==="Escape"){
    // v1.0.602：实时预览大图优先关（它是最上层视图，Esc 关它符合预期）
    if(typeof shotViewerOpen === "function" && shotViewerOpen()){ closeShotViewer(); e.preventDefault(); return; }
    const open=document.querySelectorAll(".overlay.show");
    if(open.length){ open.forEach(o=>o.classList.remove("show")); e.preventDefault(); return; }
    // v1.0.614：JS 拼出来的弹窗（.modal-veil）原先不在 Esc 覆盖范围内。
    // 只接管"本来就允许点背景关闭"的那些（遮罩自身挂了 onclick 的），并用它自己的关闭路径触发
    // —— 审批弹窗没有背景关闭，绝不能被 Esc 顺手当成"拒绝"。
    const veils=[].slice.call(document.querySelectorAll(".modal-veil"))
      .filter(v=>v.offsetParent!==null && typeof v.onclick==="function");
    if(veils.length){
      const v=veils[veils.length-1];
      try{ v.onclick({target:v}); e.preventDefault(); }catch(err){}
    }
    return;
  }
  // v1.0.471（UI#16）：nav-item 聚焦时 Enter/Space 触发（div 无原生键盘行为）
  if((e.key==="Enter"||e.key===" ") && document.activeElement &&
     document.activeElement.classList && document.activeElement.classList.contains("nav-item")){
    e.preventDefault();
    const dv=document.activeElement.dataset.view;
    if(dv) openView(dv);
    return;
  }
  if(e.ctrlKey && !e.altKey && !e.shiftKey && e.key>="1" && e.key<="9"){
    const tag=(document.activeElement && document.activeElement.tagName)||"";
    if(tag==="INPUT"||tag==="TEXTAREA") return;
    const items=document.querySelectorAll(".nav-item");
    const it=items[parseInt(e.key,10)-1];
    if(it && it.dataset.view){ e.preventDefault(); openView(it.dataset.view); }
  }
});

// v1.0.493：聊天区滚轮兜底——WebView2 宿主下偶发滚轮默认行为被吞（必须拖滚动条），
// 这里显式接管 .chatpane 垂直滚轮（preventDefault + 手动 scrollTop），不依赖浏览器默认滚动。
// 事件委托挂在 document，动态创建的 conv-view 容器天然覆盖。
document.addEventListener("wheel", function(e){
  const t = e.target;
  if(!(t instanceof Element)) return;
  const pane = t.closest(".chatpane");
  if(!pane) return;
  const cv = t.closest(".conv-view");
  // 注意：会话容器 cv_* 自 v1.0.495 起刻意不滚动（css:1103），它 scrollHeight==clientHeight
  // → 下面那句判据直接 return，滚轮交回浏览器默认滚动（滚宿主 opPane）。
  // 也就是说这条兜底只对「自己就是滚动容器」的面板生效（如 #setChatPane）。
  // 想让它也接管工作台的滚轮，必须先排掉消息内部的局部滚动区（长 tool 输出 <pre>），
  // 否则会把卡片内的滚轮抢走——本版刻意没动这里的实际行为。
  const scroller = (cv && cv !== pane) ? pane : pane;   // 滚动容器永远是 .chatpane 本体
  if(scroller.scrollHeight <= scroller.clientHeight) return;
  if(e.ctrlKey) return;   // Ctrl+滚轮留给缩放类功能
  e.preventDefault();
  let d = e.deltaY;
  if(e.deltaMode === 1) d *= 33;                 // 行模式 → 像素
  else if(e.deltaMode === 2) d *= scroller.clientHeight;  // 页模式
  scroller.scrollTop += d;
}, { passive: false });

// ---- v1.0.614：JS 拼出来的弹窗补无障碍语义（集中一处，7 个 .modal-veil 调用点不动）----
// 这些弹层此前既没有 role=dialog / aria-modal，也不会被屏幕阅读器当作对话层呈现；
// 键盘 Esc 的接管在同文件的 keydown 处理里（只接管允许点背景关闭的那些）。
function armDialogVeils(root){
  const scope = root || document;
  try{
    (scope.matches && scope.matches('.modal-veil') ? [scope] : []).forEach(v => { v.dataset.armed || (v.dataset.armed = '1', v.setAttribute('role', 'dialog'), v.setAttribute('aria-modal', 'true')); });
    scope.querySelectorAll('.modal-veil').forEach(v => {
      if (v.dataset.armed) return;
      v.dataset.armed = '1';
      if (!v.getAttribute('role')) v.setAttribute('role', 'dialog');
      if (!v.getAttribute('aria-modal')) v.setAttribute('aria-modal', 'true');
    });
  }catch(e){}
}
try{
  armDialogVeils(document);
  new MutationObserver(ms => ms.forEach(m => Array.prototype.forEach.call(m.addedNodes, n => { if (n.nodeType === 1) armDialogVeils(n); })))
    .observe(document.body, {childList: true, subtree: true});
}catch(e){}

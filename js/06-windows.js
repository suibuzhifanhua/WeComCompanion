let __winData = [];
let __winSigList = [];
let __opPickSig = null;     // 选中窗口的稳定签名（host|title），index 漂移时据此重定位
let __followOn = true;
let __lastShot = null;

// —— 跑分测试（v1.0.270）：选模型 + 并发/逐个；测速 + 质量 + 工具调用 ——
function benchModel(){
  const card = $("benchCard");
  card.style.display = "block";
  renderBenchOpts();
  card.scrollIntoView({behavior:"smooth", block:"nearest"});
}
function renderBenchOpts(){
  const box = $("benchOpts"); if(!box) return;
  if(!models || !models.length){ box.innerHTML = '<span class="sub">暂无模型——到「🧠 模型」页点「+ 添加模型」或「扫描本地模型」</span>'; return; }
  let h = '<button class="ghost smbtn" onclick="__benchCheckAll(true)">全选</button>';
  h += '<button class="ghost smbtn" onclick="__benchCheckAll(false)">清空</button>';
  h += '<span style="width:6px"></span>';
  models.forEach(m=>{
    const tag = m.is_default ? ' <span class="tag">默认</span>' : (m.enabled === false ? ' <span class="tag dim">已禁用</span>' : '');
    h += '<label style="display:inline-flex;align-items:center;gap:5px;font-size:var(--fs-sm);cursor:pointer;border:1px solid var(--border);border-radius:var(--r-lg);padding:3px 10px;background:var(--panel)">'
       + '<input type="checkbox" class="benchModelCk" value="'+escapeHtml(m.id)+'" '+(m.is_default?"checked":"")+'/>'
       + escapeHtml(m.name||m.id) + tag + '</label>';
  });
  box.innerHTML = h;
}
function __benchCheckAll(v){
  document.querySelectorAll(".benchModelCk").forEach(c=>{ c.checked = v; });
}
// v1.0.319：跑分升级为 7 项打分评测（/models/bench7，逐模型 SSE），保留多选/并发入口
const _BENCH7_COLS = [
  ["resp", "响应"], ["instr", "指令"], ["chinese", "中文"], ["logic", "逻辑"],
  ["toolcall", "工具"], ["extract", "提取"], ["speed", "速度"],
];
function _benchScoreColor(s){
  return s >= 80 ? "var(--brand-fg)" : s >= 50 ? "var(--warn-fg)" : "var(--err-fg)";
}
function _benchOneModel(id, signal, onItem){
  // 调 /models/bench7（SSE），返回 Promise<bench_done 事件>
  // v1.0.342：支持 AbortController（停止评测）+ onItem 回调（逐项进度上屏）
  // v1.0.471（UI#15）：手写 read 循环改用公共 consumeSSE（01-core）
  return new Promise((resolve, reject) => {
    fetch("/api/models/bench7", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({model_id: id}), signal: signal}).then(async resp=>{
      if(!resp.ok){ const e=await resp.json().catch(()=>({})); reject(new Error(e.error||("HTTP "+resp.status))); return; }
      try{
        const r = await consumeSSE(resp.body.getReader(), (j)=>{
          if(j.type === "bench_item" && onItem){ try{ onItem(j); }catch(e2){} }
          if(j.type === "bench_done") return j;   // 非 undefined → consumeSSE 提前返回 {early:j}
        });
        resolve(r && r.early ? r.early : null);
      }catch(e){ reject(e); }
    }).catch(reject);
  });
}
// v1.0.342：列表版评测可停止——运行中再点「开始评测」即中断，已完成部分照常展示
let __benchListAbort = null;
function _benchAbortErr(e){ return e && (e.name === "AbortError"); }
async function runBench(){
  if(__benchListAbort){ try{ __benchListAbort.abort(); }catch(e){} return; }   // 运行中点击 = 停止
  const ids = [...document.querySelectorAll(".benchModelCk")].filter(c=>c.checked).map(c=>c.value);
  if(!ids.length){ toast("请至少勾选一个模型", "warn"); return; }
  const mode = (document.querySelector("input[name=benchMode]:checked")||{}).value || "sequential";
  const btn = $("benchRunBtn"); if(btn){ btn.textContent = "⏹ 停止评测"; btn.title = "点击中断本次评测（已完成的部分会保留）"; }
  const ctrl = new AbortController(); __benchListAbort = ctrl;
  const body = $("benchBody");
  // 每个模型一行实时进度（v1.0.342：此前整轮跑完前面板只有一句"请稍候"，像卡死）
  const allModels = (typeof models!=="undefined") ? (models||[]) : [];
  body.innerHTML = ids.map(id=>{
    const m = allModels.find(x=>x.id===id);
    const nm = m ? (m.name||m.id) : id;
    return '<div data-brow="'+escapeHtml(id)+'" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:var(--fs-sm)">'
         + '<b style="min-width:130px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+escapeHtml(nm)+'</b>'
         + '<span style="flex:1;color:var(--muted,#8792a3)">排队中…</span></div>';
  }).join("");
  const rowOf = (id)=>{ const r = body.querySelector('[data-brow="'+(window.CSS&&CSS.escape?CSS.escape(id):id)+'"] span'); return r; };
  const setRow = (id, html, color)=>{ const s=rowOf(id); if(s) s.innerHTML = '<b style="color:'+(color||"inherit")+'">'+html+'</b>'; };
  const runOne = async (id)=>{
    try{
      const done = await _benchOneModel(id, ctrl.signal, (j)=>{
        const c = j.score>=80?"var(--brand-fg)":j.score>=50?"var(--warn-fg)":"var(--err-fg)";
        setRow(id, escapeHtml(j.name||"")+' <b style="color:'+c+'">'+j.score+' 分</b> · '+escapeHtml(j.detail||"")+' · '+((j.cost_ms||0)/1000).toFixed(1)+'s', c);
      });
      if(done){ setRow(id, '完成 · 总分 <b>'+(done.total!=null?done.total:"—")+'</b>'+(done.grade?'（'+escapeHtml(done.grade)+'）':''), "var(--brand-fg)"); }
      else { setRow(id, "连接中断（无结果）", "var(--err-fg)"); }
      return done;
    }catch(e){
      if(_benchAbortErr(e)){ setRow(id, "已停止", "var(--warn-fg)"); }
      else { setRow(id, "失败：" + escapeHtml(fetchErr(e, "评测")), "var(--err-fg)"); }
      throw e;
    }
  };
  let aborted = false, results = [];
  try {
    if(mode === "parallel"){
      const settledAll = await Promise.allSettled(ids.map(runOne));
      results = settledAll.filter(s=>s.status==="fulfilled").map(s=>s.value);
      aborted = settledAll.some(s=>s.status==="rejected" && _benchAbortErr(s.reason));
    } else {
      for(const id of ids){
        try{ results.push(await runOne(id)); }
        catch(e){ if(_benchAbortErr(e)){ aborted = true; break; } throw e; }
      }
    }
  } catch(e){
    if(!_benchAbortErr(e) && ids.length){ setRow(ids[0], "评测失败：" + escapeHtml(fetchErr(e, "请求")), "var(--err-fg)"); }
    aborted = aborted || _benchAbortErr(e);
  } finally {
    __benchListAbort = null;
    if(btn){ btn.textContent = "开始评测"; btn.title = ""; }
  }
  const kept = results.filter(Boolean);
  if(kept.length){
    renderBenchResults(kept, aborted);
  } else if(aborted){
    body.innerHTML = '<div style="color:var(--muted)">⏹ 已停止评测（未产生已完成的结果）</div>';
  }
}
function renderBenchResults(results, aborted){
  const body = $("benchBody");
  if(!results.length){ body.innerHTML = '<div style="color:var(--muted)">无结果</div>'; return; }
  results.sort((a,b)=>(b.total||0)-(a.total||0));
  let h = (aborted ? '<div style="color:var(--warn-fg);font-size:var(--fs-sm);margin-bottom:6px">⏹ 已停止：以下为已完成模型的结果</div>' : '')
    + '<div style="overflow:auto"><table style="width:100%; font-size:var(--fs-sm); border-collapse:collapse; min-width:560px">';
  h += '<tr style="color:var(--muted); text-align:left"><td style="padding:4px 6px">模型</td>';
  _BENCH7_COLS.forEach(([k, label])=>{ h += '<td style="padding:4px 6px">'+label+'</td>'; });
  h += '<td style="padding:4px 6px">总分</td><td>等级</td></tr>';
  results.forEach(r=>{
    const byKey = {}; (r.items||[]).forEach(it=>{ byKey[it.key]=it; });
    h += '<tr style="border-top:1px solid var(--border)"><td style="padding:4px 6px;font-weight:600">'+escapeHtml(r.model||"?")+'</td>';
    _BENCH7_COLS.forEach(([k])=>{
      const it = byKey[k];
      const s = it ? it.score : null;
      h += '<td>'+(s==null?'—':'<span style="color:'+_benchScoreColor(s)+'">'+s+'</span>')+'</td>';
    });
    h += '<td><b style="font-size:var(--fs-md);color:'+_benchScoreColor(r.total||0)+'">'+(r.total!=null?r.total:'—')+'</b></td>';
    h += '<td>'+(r.grade?'<span class="pill">'+escapeHtml(r.grade)+'</span>':'—')+'</td></tr>';
  });
  h += '</table></div>';
  results.forEach(r=>{
    h += '<details style="margin-top:8px"><summary style="cursor:pointer;font-size:var(--fs-sm);color:var(--muted)">'
       + escapeHtml(r.model||"") + ' · 逐项明细</summary><div style="padding:6px 2px">';
    (r.items||[]).forEach(it=>{
      h += '<div style="font-size:var(--fs-sm);padding:2px 0"><b>'+escapeHtml(it.name)+'</b>（权重 '+it.weight+'）：'
         + '<span style="color:'+_benchScoreColor(it.score)+'">'+it.score+' 分</span> · '
         + escapeHtml(it.detail||"") + ' · ' + ((it.cost_ms||0)/1000).toFixed(1) + 's</div>';
    });
    h += '</div></details>';
  });
  body.innerHTML = h;
}

function winSig(w){
  // 稳定签名：host + path（不含 query/hash/标题）。标题变化（文档改名/动态标题）不影响选中稳定性，
  // 仅当页面真正跳转到其它路径时才视为不同窗口。
  const u=(w.url||"").match(/^https?:\/\/([^/]+)/); const host=u?u[1]:(w.url||"");
  let path="";
  try{ const u2=new URL(w.url||""); path=u2.pathname||""; }catch(e){ path=(w.url||"").replace(/^https?:\/\/[^/]+/,""); }
  return host+"|"+path.replace(/\/+$/,"");
}
function markActive(){
  document.querySelectorAll(".win").forEach(x=>x.classList.toggle("active", +x.dataset.pick===__opPick));
}
// v1.0.616（审查 P2）：拉取失败此前直接 `return`，界面永远停在「自动刷新中…」——
// 用户分不清"没有窗口"和"接口坏了"。本函数按秒轮询，所以只改列表区文字、不弹 toast（会刷屏）。
function _winListFailed(msg){
  const box = $("winList"); if(!box || box.dataset.err === "1") return;
  box.dataset.err = "1";
  box.innerHTML = `<div class="sub">⚠️ 窗口列表读取失败：${escapeHtml(msg||"接口无响应")} —— 恢复后会自动重新列出</div>`;
}
function _winListErrClear(){ const box=$("winList"); if(box) box.dataset.err = ""; }
async function refreshWindows(){
  let d;
  try { d = await getJson("/api/wecom/windows"); }
  catch(e){ _winListFailed(String((e&&e.message)||e)); return; }
  if (!Array.isArray(d)){ _winListFailed("接口返回结构异常"); return; }
  _winListErrClear();
  __winData = d;
  // 依据签名重定位当前选中窗口（index 会随窗口开合漂移）
  if (__opPickSig){
    const idx=d.findIndex(w=>winSig(w)===__opPickSig);
    if(idx>=0){ __opPick=idx; }
    else if(__opPick==null || __opPick>=d.length){
      // 签名失配且原 index 已越界（窗口被关闭/列表收缩）：才放弃选中
      __opPick=null; __opPickSig=null;
    }
    // 签名失配但原 index 仍有效：保留原 pick 兜底继续预览，
    // 避免标题/URL 漂移时 __opPick 被置空导致 refreshSelection 短路、预览永久冻结
  }
  // 列表重渲染以「title+url」为 key（标题变化时左侧列表标题仍会更新），
  // 选中窗口重定位则用稳定签名 winSig（host+path，标题变化不丢 pick），两者解耦
  const newKeys = d.map(w=>(w.title||"")+"\u0000"+(w.url||""));
  const prevSet = new Set(__winSigList);
  const isFirst = __winSigList.length===0;   // 首次加载不要把所有窗口都标成「新」
  const sigChanged = JSON.stringify(newKeys)!==JSON.stringify(__winSigList);
  const box = $("winList");
  const wc=$("winCount"); if(wc) wc.textContent = d.length? ("共 "+d.length+" 个"): "";
  if (!d.length){ box.innerHTML='<span style="color:var(--muted);font-size:var(--fs-sm)">无窗口（企微需带 CDP 启动且打开 webview 窗口）</span>'; __opPick=null; __opPickSig=null; return; }
  if (sigChanged){
    __winSigList = newKeys;
    box.innerHTML="";
    // 父子树渲染顺序：根窗口在前、其子窗口紧随其后（仅调整显示顺序，pick 仍用原始 index，
    // 选中/操作语义不受影响）。让"待办列表→详情→附件"这类触发链在视觉上归组，找窗口更高效。
    const byPar = {};
    d.forEach(w=>{ const k=(w.parent!=null&&w.parent>=0&&w.parent<d.length)?w.parent:'r'; (byPar[k]=byPar[k]||[]).push(w); });
    const ordered = [];
    const pushed = new Set();
    const pushTree = (w)=>{ if(pushed.has(w.index)) return; pushed.add(w.index); ordered.push(w); (byPar[w.index]||[]).forEach(pushTree); };
    d.forEach(w=>{ if(w.parent==null || w.parent<0 || w.parent>=d.length) pushTree(w); });
    d.forEach(w=>pushTree(w));
    ordered.forEach(w=>{
      const i = w.index;
      const isChild = w.parent!=null && w.parent>=0 && w.parent<d.length;
      const el=document.createElement("div"); el.className="win"+(isChild?" win-child":""); el.dataset.pick=i;
      el.setAttribute("role","button"); el.tabIndex=0;                 // v1.0.616：键盘可选中窗口
      el.setAttribute("aria-label","选中窗口 "+String(w.title||"").slice(0,40));
      if(i===__opPick) el.classList.add("active");
      const isNew = !isFirst && !prevSet.has(winSig(w));
      const badge = (i===__opPick? '<span class="badge cur">当前</span>' : (isNew? '<span class="badge new">新</span>':''));
      const pw = isChild ? d[w.parent] : null;
      const parTxt = pw ? `<span class="par">由 [${w.parent}] ${escapeHtml(safeSlice(pw.title||"",14))} 打开</span>` : "";
      // v1.0.650 U2：徽章以前拼在 .t 里，而 .t 是 nowrap+overflow:hidden+ellipsis ——
      // 标题一长按钮整枚「当前/新」被省略号吃掉，恰恰在最需要认出选中项的时候看不见。
      // 现在 .t 是两段：标题段自己省略，徽章段 flex:0 0 auto 永不参与截断。
      el.innerHTML=`<div class="t"><span class="tt">${isChild?'└ ':''}[${i}] ${escapeHtml(w.title)}</span>${badge}</div><div class="u">${escapeHtml(w.url)}</div>${parTxt}`;
      el.onclick=()=>pickWindow(i, el);
      // v1.0.272：手动关闭该窗口（stopPropagation 防止触发选中；按钮在 .t 右侧）
      const xb=document.createElement("button"); xb.className="winx"; xb.type="button";
      xb.textContent="×"; xb.title="关闭窗口 ["+i+"] "+(w.title||"").slice(0,30);
      xb.onclick=(e)=>{ e.stopPropagation(); closeWindow(i); };
      el.appendChild(xb);
      box.appendChild(el);
    });
  } else {
    markActive();
  }
}
async function pickWindow(i, el){
  document.querySelectorAll(".win").forEach(x=>x.classList.remove("active")); el&&el.classList.add("active");
  __opPick = i; __opPickSig = __winData[i]?winSig(__winData[i]):null;
  // 「返回父窗口」按钮：仅当当前窗口存在父窗口时显示
  const pb=$("parentBtn"), w=__winData[i];
  if(pb){ pb.style.display = (w && w.parent!=null && w.parent>=0 && w.parent<__winData.length) ? "inline-block":"none"; }
  const t = (__winData[i]&&__winData[i].title)||"";
  setOpTarget("🎯 正在操作《"+safeSlice(t,24)+"》 [窗口 "+i+"]", true);
  await refreshSelection();
}
function goParent(){
  if(__opPick==null) return;
  const w=__winData[__opPick];
  if(!w || w.parent==null || w.parent<0 || w.parent>=__winData.length) return;
  const el=document.querySelector(`.win[data-pick="${w.parent}"]`);
  pickWindow(w.parent, el||undefined);
}
async function closeWindow(i){
  // v1.0.272：手动关闭窗口。confirm 防误点；关的是当前选中窗口时清空选中，
  // 避免 __opPick 序号漂移后预览/操作静默指向别的窗口（其余情况由 refreshWindows
  // 的 winSig 重定位自动纠正序号漂移）。
  const w=__winData[i]; if(!w) return;
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("关闭窗口", "关闭窗口 ["+i+"]「"+(w.title||"无标题").slice(0,40)+"」？", async ()=>{
    let r;
    try{
      r=await (await fetch("/api/wecom/close",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pick:i})})).json();
    }catch(e){ _hintRefreshFail("关闭窗口"); return; }
    if(!r || !r.ok){ toast('关闭失败：'+((r&&r.error)||"未知错误"),"err"); return; }
    if(i===__opPick){
      __opPick=null; __opPickSig=null;
      const pb=$("parentBtn"); if(pb) pb.style.display="none";
    }
    refreshWindows(); refreshSelection();
  }, "关闭");
}
// v1.0.471（UI#10）：刷新失败轻提示——原空 catch 用户分不清「没变化」和「坏了」。
// 借用 shotHint 状态条（下轮正常刷新会自动覆盖恢复），不打扰。
function _hintRefreshFail(what){
  const h=$("shotHint"); if(!h) return;
  h.textContent = "⚠️ " + what + "刷新失败（后端/CDP 异常），将在下轮自动重试";
  h.style.display = "block";
}
// 实时跟随心跳（v1.0.602 用户要求 5s→3s）。周期与界面文案都读这一个常量，
// 免得改了定时器忘了改标签、下次又对不上。
const FOLLOW_MS = 3000;
let __selBusy = false;      // v1.0.602：3 秒心跳下上一轮可能还在飞（capture+元素+截图三段）
async function refreshSelection(){
  if (__opPick==null) return;
  if (__selBusy) return;    // 重入守卫：宁可跳过这一拍，也不堆叠 CDP 请求
  __selBusy = true;
  try{
    try{ const d = await getJson("/api/wecom/capture?pick="+__opPick); showCapture(d); }catch(e){ _hintRefreshFail("正文"); }
    try{ await loadElements(__opPick); }catch(e){ _hintRefreshFail("元素"); }
    await updateScreenshot(__opPick);
  }finally{ __selBusy = false; }
}
function shotFolded(){ return localStorage.getItem("__shotFold")!=="0"; } // 缺省即折叠，避免大图顶掉下方窗口列表
function setShotSize(im, folded){
  if(folded){ im.style.maxHeight="56px"; im.style.objectFit="cover"; im.style.objectPosition="top center"; im.title="预览条已折叠（点预览图看大图；标题行的箭头可拉高这条）"; }
  else { im.style.maxHeight="280px"; im.style.objectFit="contain"; im.style.objectPosition="center"; im.title="点预览图看大图（看清页面具体内容）"; }
  const btn=$("shotFoldBtn");
  // v1.0.635：原来按钮文字在「▸ 展开」/「▾ 折叠」之间换（唯一一个带文字的折叠钮，
  // 窄栏下还会把标题行挤到换行）。现在与其余折叠钮同一原语：图标恒定、只切 aria-expanded。
  if(btn){ setFold(btn, !folded);
           btn.title = folded ? "展开预览条（占更多竖向空间）" : "折叠预览条，露出下方窗口列表"; }
}
function toggleShotFold(){
  const im=$("liveShot"); if(!im) return;
  const folded = shotFolded();
  setShotSize(im, !folded);
  localStorage.setItem("__shotFold", folded?"0":"1");
}
function applyShotFold(){ const im=$("liveShot"); if(im) setShotSize(im, shotFolded()); }

// ---------- v1.0.602 实时预览大图 ----------
// 预览条默认只有 56px（刻意折叠，否则大图把下方窗口列表顶没了），可这点高度什么都读不出来：
// 用户想看清页面具体内容时，点一下反而在折叠/展开之间来回跳。现在**点预览图=放大看**，
// 折叠/展开交给标题行的「▾ 折叠」按钮。大图里的画面跟着实时刷新一起更新（不是快照）。
function openShotViewer(){
  const im = $("liveShot");
  if(!im || im.style.display === "none" || !im.getAttribute("src")){
    toast("还没有可放大的画面——先在下方选一个窗口", "warn", 2400); return;
  }
  const v = $("shotViewer"), big = $("shotViewerImg");
  if(!v || !big) return;
  big.src = im.src;
  let title = "";
  try{ if(__opPick != null && __winData && __winData[__opPick]) title = __winData[__opPick].title || ""; }catch(e){}
  const t = $("shotViewerTitle");
  if(t) t.textContent = "📸 " + (title ? String(title).slice(0, 60) : "实时预览");
  v.style.display = "flex";
}
function closeShotViewer(){
  const v = $("shotViewer"); if(v) v.style.display = "none";
}
function shotViewerOpen(){
  const v = $("shotViewer"); return !!v && v.style.display !== "none";
}
let __shotGen = 0;   // v1.0.602：3 秒心跳下"后发先至"会让旧截图盖掉新截图——只认最新一轮
async function updateScreenshot(pick){
  if (pick==null) return;
  const gen = ++__shotGen;
  const img=$("liveShot");
  try{
    // 截图请求前端限时：后端 CDP 合成帧挂起时最长等 12s 会阻塞预览观感，这里 6.5s 超时即中止本轮刷新
    const ctl = new AbortController();
    const timer = setTimeout(()=>ctl.abort(), 6500);
    const d = await (await fetch("/api/wecom/screenshot?pick="+pick, {signal:ctl.signal})).json();
    clearTimeout(timer);
    if(gen !== __shotGen) return;   // 期间已发起更新的一轮，这张旧帧直接丢
    const _em = d.__error__||d.error; if (_em){ img.style.display="none"; $("shotHint").textContent="截图不可用："+_em; $("shotHint").style.display="block"; return; }
    if (d.data && d.data!==__lastShot){ __lastShot=d.data; img.src="data:image/png;base64,"+d.data; }
    img.style.display="block"; applyShotFold();
    // 大图开着就跟着一起刷（用户放大是为了盯页面变化，不是看一张快照）
    if(shotViewerOpen()){
      const big=$("shotViewerImg"); if(big) big.src = img.src;
      const tip=$("shotViewerTip");
      if(tip) tip.textContent = "画面实时刷新中 · 最近一次 " + new Date().toLocaleTimeString();
    }
    // 降级画面（DOM 绘制）在画面下方给出轻量说明，便于区分真实截图
    if (d.render==="dom"){ $("shotHint").textContent="（降级画面：合成帧不可用，以 DOM 绘制预览）"; $("shotHint").style.display="block"; }
    else { $("shotHint").style.display="none"; }
  }catch(e){ img.style.display="none"; }
}
async function captureAuto(){
  let d;
  try { d = await getJson("/api/wecom/capture"); }
  catch(e){
    // v1.0.616：静默 return 会让截图区停在上一帧，用户以为"画面卡住是软件的问题"。
    // 这里只标一行提示（本函数在轮询链上，不弹 toast），成功一次即被 showCapture 覆盖。
    const h=$("shotHint"); if(h){ h.style.display="block"; h.textContent="截图读取失败，正在自动重试…"; }
    return;
  }
  showCapture(d);
  const host = (d.url||"").match(/https?:\/\/([^/]+)/); const h = host?host[1]:"";
  if (h && __winData.length){
    const idx = __winData.findIndex(w=>(w.url||"").includes(h));
    if (idx>=0){
      __opPick=idx; __opPickSig=winSig(__winData[idx]); markActive();
      const t=__winData[idx].title||"";
      setOpTarget("🎯 正在操作《"+safeSlice(t,24)+"》 [窗口 "+idx+"]", true);
      await loadElements(idx); await updateScreenshot(idx); return;
    }
  }
  setOpTarget("⚠️ 未选企微窗口：可直接对话或操控本机；点选窗口后可操作企微页面", false);
  $("liveShot").style.display="none";
}
function showCapture(d){
  const _em = d.__error__||d.error; if (_em){ $("captureView").innerHTML=`<span style="color:var(--err-fg)">失败：${escapeHtml(_em)}</span>`; return; }
  const body=(d.body||"").trim()||"（正文为空）";
  $("captureView").textContent=`《${d.title||""}》\n${d.url||""}\n\n${body}`;
}
function elemMeta(e){
  const tag=(e.tag||"").toLowerCase();
  if(tag==="button") return {ic:"🔘", txt:"按钮", cls:"t-btn"};
  if(tag==="input"){
    const ty=(e.type||"").toLowerCase();
    if(ty==="checkbox") return {ic:"☑️", txt:"勾选框", cls:"t-inp"};
    if(ty==="radio") return {ic:"🔘", txt:"单选", cls:"t-inp"};
    if(ty==="submit"||ty==="button") return {ic:"🔘", txt:"按钮", cls:"t-btn"};
    return {ic:"✏️", txt:"输入框", cls:"t-inp"};
  }
  if(tag==="textarea") return {ic:"✏️", txt:"多行输入", cls:"t-inp"};
  if(tag==="select") return {ic:"▾", txt:"下拉选择", cls:"t-sel"};
  if(tag==="a") return {ic:"🔗", txt:"链接", cls:"t-lnk"};
  if(tag==="label") return {ic:"🏷️", txt:"标签", cls:"t-lbl"};
  return {ic:"⚙️", txt:tag||"其他", cls:"t-oth"};
}
// v1.0.471（UI#4）：元素列表内容指纹——内容没变就跳过重建，消除「正要点击时列表
// 被轮询重建导致 hover/点击丢失」；同时省一轮 DOM 重建开销。
let __lastElsSig = "";
async function loadElements(pick){
  let d;
  try { d = await getJson("/api/wecom/interactive?pick="+pick); } catch(e){ return; }
  const box = $("elemList");
  const els = (d.elements||[]);
  let sig;
  try{ sig = pick + "|" + JSON.stringify(els.slice(0,120).map(e=>[e.index,e.tag,e.text,e.placeholder,e.name,e.value])); }catch(_){ sig = ""; }
  if(!els.length){
    if(__lastElsSig !== "empty"){ box.innerHTML='<div class="elem-empty">没有可操作元素（该页面无可点击/可输入内容）</div>'; __lastElsSig="empty"; }
    return;
  }
  if(sig && sig === __lastElsSig) return;   // 内容未变，不重建
  __lastElsSig = sig;
  box.innerHTML="";
  els.slice(0,120).forEach(e=>{
    const m = elemMeta(e);
    const el=document.createElement("div"); el.className="elem "+m.cls;
    const label = (e.text||e.placeholder||e.name||e.value||"(无文字)");
    el.title="点击把「点序号 "+e.index+"」填入聊天框";
    el.innerHTML=`<span class="idx">${e.index}</span>`+
      `<span class="kind"><i>${m.ic}</i>${m.txt}</span>`+
      `<span class="tg">&lt;${escapeHtml(e.tag||"")}&gt;</span>`+
      `<span class="tx">${escapeHtml(label)}</span>`+
      `<span class="go">点击填入 →</span>`;
    el.onclick=()=>{ $("opInput").value = `点击序号 ${e.index}（${label.slice(0,20)}）`; $("opInput").focus(); };
    box.appendChild(el);
  });
}

// ---- 模型 ----

// v1.0.581：关闭确认层——点 X 后 close_guard（GUI 线程）只设 pending 标志，
// checkCloseAsk 常驻轮询发现 pending → 弹确认层；选择 POST
// /api/close_ask/choice 回传执行。**禁止在此用 evaluate_js / pywebview api
// 同步桥**——钩子线程调它们会与 GUI 主线程互等死锁（v1.0.580 实锤）。
// 本地请求开销可忽略（<1ms），换来点 X 后 ~0.5s 内弹层（v1.0.581 的 8s 延迟根除）。
// （v1.0.653 瘦身：零调用的 showCloseAsk 包装已删，展开/收起内联在 checkCloseAsk 里）
let __closeAskTimer=null;
async function checkCloseAsk(){
  try{
    const d=await getJson("/api/close_ask");
    const ov=$("closeAskOverlay");
    if(d.pending && ov && ov.style.display!=="flex"){ ov.style.display="flex"; }
    if(!d.pending && ov && ov.style.display==="flex"){ ov.style.display="none"; }   // 后端已处理（降级路径）
  }catch(e){ /* 网络抖动忽略 */ }
  __closeAskTimer=setTimeout(checkCloseAsk, document.hidden?3000:500);   // v1.0.652（审计 D14）：窗口隐藏降频（原 2 次/秒无条件常驻，空闲每小时 7200 次请求）
}
async function closeAskChoose(minimize){
  const ov=$("closeAskOverlay"); if(ov) ov.style.display="none";
  const remember=!!($("closeAskRemember")&&$("closeAskRemember").checked);
  try{ await postJson("/api/close_ask/choice",{minimize,remember}); }catch(e){}
}

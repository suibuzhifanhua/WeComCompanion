const $ = (id) => document.getElementById(id);
let models = [], templates = [];
let __opPick = null;

function escapeHtml(s){ return (s||"").replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// 用于拼接内联 onclick 的 JS 字符串字面量：转义引号/反斜杠/换行，防属性与 JS 注入（P2-5）
// v1.0.373 审查修复（P2-4）：`"` 须编码为 &quot;——原 `\"` 是 JS 层转义，但外层是 HTML
// 双引号属性，HTML 解析器不认 `\"`，含 `"` 的数据（导入的模型/模板 id）可直接跳出属性边界注入事件。
function jsStr(s){ return (s||"").replace(/\\/g,"\\\\").replace(/'/g,"\\'").replace(/"/g,"&quot;").replace(/\r?\n/g,"\\n"); }
// v1.0.616（审查 P2）：标题截断统一入口。`String.slice(0,n)` 按 UTF-16 码元切，
// 遇到 emoji/生僻字（代理对）会切出半个字符 → 界面显示成 ?/方块乱码。
// Array.from 按码点切，再拼回来；后缀由调用方自己决定（有的带 …，有的不带）。
function safeSlice(s, n){ return Array.from(String(s == null ? "" : s)).slice(0, n).join(""); }
function autoResize(el){ if(!el) return; el.style.height='auto'; el.style.height=Math.min(el.scrollHeight,180)+'px'; }
// ---------- v1.0.650 U6：状态口径单一事实源 ----------
// 同一件"装好了"以前在三个地方三种叫法两样图标：组件视图 ✅+「可用」、
// 同页汇总行「全部就绪」、设置页绿点+「已安装」；「安装中」在组件视图是 ⏳、
// 设置页是黄点（而 css 顶部 v1.0.509 的规范里黄=降级/注意，进行中=⏳）。
// 规范本来就写在 css 注释里，没法被 JS 引用，于是每页各自抄一遍、抄一版漂一版。
// 现在图标与用词都从这里取；css 的规范注释与本表由测试互相核对。
const STATUS_ICON = {ok:"✅", err:"❌", degraded:"🟡", missing:"❌", installing:"⏳", unknown:"⚪", outdated:"⬆️", na:"➖", off:"⚪"};
const STATUS_WORD = {ok:"可用", degraded:"降级", missing:"未就绪", installing:"安装中", unknown:"未知", outdated:"可升级", na:"不适用", off:"未安装"};
function fetchErr(e, action){
  // v1.0.373（体验A2）：常见错误人话化——技术报错翻译成"发生了什么 + 下一步做什么"
  const m = (e && e.message) || "";
  if(m === "Failed to fetch") return action + "失败：后端未运行，请重新启动企微伴侣.exe";
  if(/HTTP\s*5\d\d/i.test(m)) return action + "失败：服务内部错误，请稍后重试；反复出现请重启软件";
  if(/timeout|timed?\s*out|ETIMEDOUT/i.test(m)) return action + "失败：请求超时，请检查网络后重试";
  if(/HTTP\s*4\d\d/i.test(m)) return action + "失败：请求被拒绝（" + m + "），请检查配置后重试";
  return action + "失败：" + (m || "网络错误");
}
// 通用 SSE 消费器（v1.0.309 提取自 4 处重复实现：主聊天/模板 run/快捷入口 AI/模板 AI）：
// reader 字节流 → decode → 按 "\n\n" 分帧 → "data:" JSON 解析 → 逐事件回调 onEvent(j)。
// onData：每收到一批原始字节回调一次（看门狗喂狗用，可传 null）。
// onEvent 返回非 undefined → consumeSSE 立即停止并以 {early:值} 返回（主聊天 need_continue/
// aborted 的提前返回依赖此契约；其余调用方 onEvent 恒返回 undefined，不受影响）。
// reader.read() 抛错（含 AbortError）原样上抛，由调用方按各自 UI 语义处理。
async function consumeSSE(reader, onEvent, onData){
  const dec=new TextDecoder(); let chunk="";
  while(true){
    const {done,value}=await reader.read(); if(done) break;
    if(onData) onData();
    chunk+=dec.decode(value,{stream:true});
    const parts=chunk.split("\n\n"); chunk=parts.pop();
    for(const p of parts){
      if(!p.startsWith("data:")) continue;
      let j; try{ j=JSON.parse(p.slice(5).trim()); }catch(_){ continue; }
      const r=onEvent(j);
      if(r!==undefined){ try{ reader.cancel(); }catch(_){} return {early:r}; }   // v1.0.652（审计 D15）：提前返回显式取消 reader，不悬挂连接
    }
  }
}
function toast(msg, type='ok', ms=2500){
  const t=$("toast");
  // v1.0.471（UI#2）：info 补 ℹ️ 图标；warn/info 现有专属背景色（style.css）
  // v1.0.650 U6：err 原来也画 ⚠️，和规范（错误=❌、⚠️=警告）打架——
  // 红色底的 toast 配一个"警告"图标，用户分不清这次到底成没成。改回 ❌。
  const ic = type==='err' ? STATUS_ICON.err + " " : (type==='warn' ? '⚠️ ' : (type==='info' ? 'ℹ️ ' : STATUS_ICON.ok + " "));
  t.textContent = ic + msg;
  t.className=type; t.classList.add("show");
  if(toast._tmr) clearTimeout(toast._tmr);   // 防连续 toast 时前一个定时器提前隐藏后一个
  toast._tmr = setTimeout(()=>{ t.classList.remove("show"); }, ms);
}
// v1.0.471（UI#5）：统一 fetch 封装——收敛 ~40 处 fetch→json→ok判断→toast 样板。
// 成功返回解析后的 JSON；失败抛 Error（message 已是人话），由 apiToast 统一 toast。
// 覆盖后端两种失败形态：HTTP 4xx/5xx 与 HTTP 200 + {error} / {ok:false}。
async function api(url, opts){
  const r = await fetch(url, opts);
  let d = null;
  try{ d = await r.json(); }catch(_){ /* 非 JSON 响应按状态码判 */ }
  if(!r.ok || (d && (d.error || d.ok === false))){
    throw new Error((d && (d.error || d.detail)) || ("HTTP " + r.status));
  }
  return d || {};
}
async function apiToast(url, opts, action){
  try{ return await api(url, opts); }
  catch(e){ toast(fetchErr(e, action || "操作"), "err"); throw e; }
}
// fetch 样板收敛（C 档）：GET/POST JSON 薄封装——只补最常见的两种 opts 形态，
// 全部复用 api() 的同一契约（非 JSON 响应按状态码判；d.error / d.ok===false 抛错）。
// 带 AbortController、FormData、额外 headers、非 json 响应的调用继续走 api()/fetch 原样。
async function getJson(url){ return api(url); }
async function postJson(url, data){
  return api(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)});
}
function showLoading(el, text){
  if(typeof el==="string") el=$(el);
  el.innerHTML=`<div class="loading-row"><span class="spinner"></span>${escapeHtml(text||"加载中…")}</div>`;
}
function showEmpty(el, text){
  // 契约：text 是**受信任的 HTML 片段**（本项目内的字面量，常带 <br/> 分行），本函数不转义。
  // 调用方拼入任何外部/动态内容（错误消息、文件名、后端返回）时请自行 escapeHtml。
  if(typeof el==="string") el=$(el);
  el.innerHTML=`<div class="empty"><span class="em-ic">📭</span>${text}</div>`;
}
function setBtnLoading(btn, loading){
  if(!btn) return;
  if(loading){ btn.classList.add("loading"); btn.dataset.oldText=btn.textContent; btn.textContent="处理中..."; }
  else { btn.classList.remove("loading"); if(btn.dataset.oldText) btn.textContent=btn.dataset.oldText; }
}

// ---- 侧栏视图切换 ----

// ---- 大输入框（v1.0.646：自 js/16-bigeditor.js 并入，函数体逐字未改） ----
// 只依赖本文件顶部的 $，且调用方是 index.html 里的内联 onclick（运行时才解析），
// 因此从 12 号位提到 01 号位不影响任何加载时序。
function openBigEditor(inputId, sendFn){
  const ov=$("bigEditorOverlay"); if(!ov) return;
  const src=$(inputId); if(!src) return;
  ov._inputId=inputId; ov._sendFn=sendFn;
  $("bigEditText").value=src.value;
  ov.classList.add("show");
  $("bigEditText").focus();
}
function closeBigEditor(){ const ov=$("bigEditorOverlay"); if(ov) ov.classList.remove("show"); }
function bigEditorSend(){
  const ov=$("bigEditorOverlay"); if(!ov) return;
  const inp=$(ov._inputId); if(inp) inp.value=$("bigEditText").value;
  closeBigEditor();
  const fn=window[ov._sendFn]; if(fn && typeof fn==="function") fn();
}

// ---- js 小重复合并（审计 A10）：以下为多处同款样板的公共实现 ----

// 数字格式化（亿/万/k）：07 的 fmtTok 与 13 的 fmtDashNum 此前是两份逐字同款
// （仅 Number(n)/+n 一词之差，语义相同），收敛为单一实现后两处以薄封装保留旧函数名。
function fmtNum(n){
  n = +n || 0;
  if(n >= 1e8) return (n/1e8).toFixed(2)+"亿";
  if(n >= 1e4) return (n/1e4).toFixed(1)+"万";
  if(n >= 1e3) return (n/1e3).toFixed(1)+"k";
  return String(n);
}

// 模型下拉填充：02（fillQuickAIModel）/07（loadModels 的 t_model）/08（fillTplAIModel）
// 三处逐字同款的 option 拼串收敛到这里（value 一律 escapeHtml——v1.0.471 UI#14 的口径）。
// 只做填充：当前选中值的保存/恢复是各调用方自己的事（07 那处本来就不恢复，不替它改行为）。
function fillModelSelect(sel, models){
  if(!sel) return;
  sel.innerHTML='<option value="">（用默认模型）</option>'+(models||[]).map(m=>`<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join("");
}

// localStorage 读写样板：原先 ~20 处 try{localStorage.setItem/getItem(...)}catch(e){} 手包一层。
// lsSet 吞掉写失败（隐私模式/配额超限，与原 catch 空体一致）；lsGet 读失败/缺键时回默认值。
function lsSet(k, v){ try{ localStorage.setItem(k, v); }catch(e){} }
function lsGet(k, d){ try{ const v = localStorage.getItem(k); return v == null ? d : v; }catch(e){ return d; } }

// AI 设计助手迷你聊天窗工厂（quickAI/tplAI 两套整段克隆的公共骨架）。
// js/02 的 addQuickAIMsg/renderQuickAIOptions/quickAIPick/quickAISend 与 js/08 的
// tplAI 同名四件是同一套「需求确认 → 可点选项 → SSE 流式生成 → 结果卡片」交互的复制粘贴
// （~90 行 ×2，仅 DOM id/端点/文案/结果提取不同），收敛到这里后两处各留薄封装。
// cfg：{S, chatId, inputId, sendBtnId, modelSelId, endpoint, errLabel, emptyHint, extract, onResult}
//   S＝状态载体 {msgs, ctrl, running, result}——调用方用取值器桥回各自的旧全局名（不改名）；
//   extract(j)＝从 result 事件取出结果（quick 取 j.links、tpl 取 j.template）；
//   onResult(res)＝结果渲染（renderQuickAIResult / renderTplAIResult），提取为 null 时不渲染，
//   与原实现「__xxAIResult=...; if(__xxAIResult) render...」逐条一致。
function makeDesignAssistant(cfg){
  const S = cfg.S;
  const chatEl = ()=>$(cfg.chatId);
  function addMsg(role, text){
    const chat=chatEl(); if(!chat) return null;
    const el=document.createElement("div");
    el.className="msg "+(role==="user"?"user":"agent");
    const mc=document.createElement("div"); mc.className="mc";
    if(role==="agent" && !text){ mc.innerHTML='<span class="spinner" style="display:inline-block;vertical-align:-3px"></span> 正在思考…'; }
    else mc.innerHTML=renderMarkdown(text);
    el.appendChild(mc); chat.appendChild(el);
    chat.scrollTop=chat.scrollHeight;
    return el;
  }
  function renderOptions(questions){
    const chat=chatEl(); if(!chat) return;
    (questions||[]).forEach(q=>{
      const blk=document.createElement("div"); blk.className="opt-block";
      const t=document.createElement("div"); t.className="ob-t"; t.textContent=(q.label||"请选择")+(q.desc?(" · "+q.desc):"");
      blk.appendChild(t);
      const row=document.createElement("div"); row.className="row";
      (q.options||[]).forEach(o=>{
        const b=document.createElement("button"); b.type="button";
        b.className="opt-chip"+(o.value==="custom"?" opt-custom":"");
        b.textContent=o.label||o.value; b.title=o.desc||"";
        b.onclick=()=>pick(q.key,o.value,o.label||o.value);
        row.appendChild(b);
      });
      blk.appendChild(row); chat.appendChild(blk);
    });
    chat.scrollTop=chat.scrollHeight;
  }
  function pick(key, value, label){
    if(S.running){ toast("正在生成中，请稍候…","warn"); return; }
    const content = value==="custom" ? `【${label}】请向我询问具体需要什么内容` : `【选项选择】${label}`;
    S.msgs.push({role:"user", content});
    addMsg("user","选择："+label);
    send();
  }
  async function send(){
    if(S.running) return;
    const inp=$(cfg.inputId); const text=inp?inp.value.trim():"";
    if(text){ S.msgs.push({role:"user",content:text}); addMsg("user",text); if(inp) inp.value=""; }
    if(!S.msgs.length){ toast(cfg.emptyHint,"warn"); return; }
    S.running=true; const sb=$(cfg.sendBtnId); if(sb) sb.disabled=true;
    const agentEl=addMsg("agent","");
    let out="";
    const model_id=$(cfg.modelSelId)?$(cfg.modelSelId).value:"";
    if(S.ctrl) S.ctrl.abort(); S.ctrl=new AbortController();
    let r;
    try{
      r=await fetch(cfg.endpoint,{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({messages:S.msgs, model_id}), signal:S.ctrl.signal});
    }catch(e){
      if(e && e.name==="AbortError"){ S.running=false; if(sb) sb.disabled=false; return; }
      S.running=false; if(sb) sb.disabled=false;
      if(agentEl) agentEl.querySelector(".mc").innerHTML='<span style="color:var(--err-fg)">⚠️ '+escapeHtml(fetchErr(e,cfg.errLabel))+'</span>';
      return;
    }
    if(!r.ok || !r.body){
      S.running=false; if(sb) sb.disabled=false;
      if(agentEl) agentEl.querySelector(".mc").innerHTML='<span style="color:var(--err-fg)">⚠️ 生成失败（HTTP '+r.status+'）</span>';
      return;
    }
    const reader=r.body.getReader();
    try{
      await consumeSSE(reader, (j)=>{
        if(j.type==="token"){ out+=(j.text||""); if(agentEl){ agentEl.querySelector(".mc").innerHTML=renderMarkdown(out); const c=chatEl(); if(c) c.scrollTop=c.scrollHeight; } }
        else if(j.type==="options"){ renderOptions(j.questions||[]); }
        else if(j.type==="result"){ S.result=cfg.extract(j); if(S.result) cfg.onResult(S.result); }
        else if(j.type==="error"){ toast(j.text||"生成失败","err"); if(agentEl) agentEl.querySelector(".mc").innerHTML='<span style="color:var(--err-fg)">⚠️ '+escapeHtml(j.text||"生成失败")+'</span>'; }
      });
    }catch(e){
      if(e && e.name!=="AbortError" && agentEl){ agentEl.querySelector(".mc").innerHTML='<span style="color:var(--err-fg)">⚠️ '+escapeHtml(e.message||"读取响应失败")+'</span>'; }
    }finally{
      S.running=false; if(sb) sb.disabled=false;
      if(out) S.msgs.push({role:"assistant", content:out});
      const chat=chatEl(); if(chat) chat.scrollTop=chat.scrollHeight;
    }
  }
  return {addMsg, renderOptions, pick, send};
}

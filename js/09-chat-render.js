function inlineMd(s){
  let t = escapeHtml(s);
  t = t.replace(/`([^`]+)`/g, (_,c)=>`<code class="md-code">${c}</code>`);
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_,txt,url)=>`<a href="${url.replace(/["']/g,'')}" target="_blank" rel="noopener">${txt}</a>`);
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  t = t.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  return t;
}
function isTableSep(line){
  // 表格分隔行：只含 | - : 空格
  return /^\s*\|?[\s|:\-]+\|?\s*$/.test(line) && line.includes("-");
}
function renderMarkdown(src){
  const lines = (src||"").split("\n");
  let html="", para=[], listType=null, tbl=false;
  const flushPara = ()=>{ if(para.length){ html+='<p class="md-p">'+para.map(inlineMd).join("<br/>")+'</p>'; para=[]; } };
  const closeList = ()=>{ if(listType){ html+="</"+listType+">"; listType=null; } };
  const closeTable = ()=>{ if(tbl){ html+="</tbody></table>"; tbl=false; } };
  const cell = c=>c.trim();
  const isTblRow = l=>{ const t=l.trim(); return t.startsWith("|") && t.endsWith("|"); };
  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    if(!tbl && isTblRow(line) && i+1<lines.length && isTableSep(lines[i+1])){
      closeList(); flushPara();
      const head=line.split("|").slice(1,-1).map(cell);
      html+='<table class="md-table"><thead><tr>'+head.map(c=>`<th>${inlineMd(c)}</th>`).join("")+'</tr></thead><tbody>';
      tbl=true; i++; // 跳过分隔行
      continue;
    }
    if(tbl){
      if(!isTblRow(line)){ closeTable(); }
      else{
        const cells=line.split("|").slice(1,-1).map(cell);
        html+="<tr>"+cells.map(c=>`<td>${inlineMd(c)}</td>`).join("")+"</tr>";
        continue;
      }
    }
    if(/^\s*$/.test(line)){ flushPara(); closeList(); continue; }
    let m;
    if(m=/^(#{1,4})\s+(.*)$/.exec(line)){ flushPara(); closeList(); const l=m[1].length; html+=`<h${l} class="md-h">${inlineMd(m[2])}</h${l}>`; continue; }
    if(m=/^\s*[-*+]\s+\[([ xX])\]\s+(.*)$/.exec(line)){
      flushPara(); if(listType!=="ul.md-tl"){ closeList(); html+='<ul class="md-tl">'; listType="ul.md-tl"; }
      html+=`<li>${m[1].toLowerCase()==="x"?'<input type="checkbox" checked disabled/>':'<input type="checkbox" disabled/>'}${inlineMd(m[2])}</li>`; continue;
    }
    if(m=/^\s*[-*+]\s+(.*)$/.exec(line)){
      flushPara(); if(listType!=="ul"){ closeList(); html+='<ul class="md-ul">'; listType="ul"; }
      html+=`<li>${inlineMd(m[1])}</li>`; continue;
    }
    if(m=/^\s*\d+[.)]\s+(.*)$/.exec(line)){
      flushPara(); if(listType!=="ol"){ closeList(); html+='<ol class="md-ol">'; listType="ol"; }
      html+=`<li>${inlineMd(m[1])}</li>`; continue;
    }
    if(m=/^\s{2,}([-*+]\s+.*)$/.exec(line)){
      if(listType){ html+=`<li class="sub">${inlineMd(m[1].replace(/^[-*+]\s+/, ""))}</li>`; }
      else { para.push(line); }
      continue;
    }
    if(/^---+$/.test(line)){ flushPara(); closeList(); html+='<hr class="md-hr"/>'; continue; }
    if(m=/^>\s?(.*)$/.exec(line)){ flushPara(); closeList(); html+=`<blockquote class="md-quote">${inlineMd(m[1])}</blockquote>`; continue; }
    para.push(line);
  }
  flushPara(); closeList(); closeTable();
  return html;
}
// v1.0.645：「【完成】」是 agent 事件流里的协议标记（loop.py 在最终答复前吐一次），
// 不是给用户看的话。会话落库一直取的是标记**之后**那半（routes/chat.py::_memory_watch），
// 而界面此前把标记一起显示——于是"当时看到的"和"重开看到的"差一截。两边同一口径：
// 取最后一次（团队模式 REVISE 补跑会有两段，与后端 rsplit 同规则）。
function stripFinishMark(t){
  const s = String(t || "");
  const i = s.lastIndexOf("【完成】");
  return i < 0 ? s : s.slice(i + "【完成】".length);
}
function renderRich(text){
  // 代码块正则：未闭合的 ``` 也安全处理（流式输出中间态不会吞掉后续内容）
  let html=""; const re=/```(\w*)\n?([\s\S]*?)(```|$)/g; let last=0; let m;
  while((m=re.exec(text))){
    if(m.index>last) html+=renderMarkdown(text.slice(last,m.index));
    const code=escapeHtml(m[2].replace(/\n$/,""));
    html+='<div class="code-wrap"><button class="code-copy" onclick="copyText(this)">📋 复制</button><pre class="codeblock">'+code+'</pre></div>';
    last=m.index+m[0].length;
  }
  html+=renderMarkdown(text.slice(last));
  return html;
}
function copyText(btn){ const pre=btn.nextElementSibling; if(pre) navigator.clipboard.writeText(pre.textContent); toast("已复制","ok",1200); }
function addMsg(paneId, role, text, cls, showTime, tmText, noScroll){
  const pane=_paneEl(paneId);
  const em=pane.querySelector(".empty"); if(em) em.remove();
  const el=document.createElement("div"); el.className="msg "+(cls||role);
  el.dataset.raw = text || "";   // v1.0.334：原文（编辑/复制用），渲染后 HTML 反推不了
  const mc=document.createElement("div"); mc.className="mc";
  if(role==="user"){ mc.textContent=text; }
  else {
    const html=renderRich(text);
    // 渲染健壮性：空/纯空白内容不显示成「白板气泡」（白底+边框线+头像，即截图问题），给占位提示
    mc.innerHTML = html || '<span class="msg-empty">（内容为空）</span>';
  }
  // 2026-09-13：支持 tmText（恢复历史时显示原始发送时间而非恢复时刻）；
  // showTime=false 但带了 tmText 也显示——否则重启恢复的历史消息全都没有时间戳，🕘 开关无物可切。
  if(showTime!==false || tmText){
    const tm=document.createElement("div"); tm.className="tm";
    tm.textContent=tmText || new Date().toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"});
    mc.appendChild(tm);
  }
  el.appendChild(mc);
  // v1.0.334：user/agent 气泡挂底部操作栏（复制原悬浮 📋 一并收编；tool/err 不挂）
  if(role==="user" || (cls||role).indexOf("agent")===0){
    try{ attachMsgOps(el, paneId, role==="user" ? "user" : "agent"); }catch(e){}
  }
  // v1.0.471（UI#8）：noScroll——批量恢复历史时每条都读 scrollHeight 赋值会强制
  // 布局 O(n) 次；调用方循环结束后一次性滚到底即可
  pane.appendChild(el);
  // v1.0.615：滚底走 __scrollBottom（会解析真滚动容器 + 尊重用户上翻）——
  // 直接写 pane.scrollTop 在会话容器上是空写（cv_* 自 v1.0.495 起不滚动），
  // 而且会把"用户正在翻历史"这件事无视掉、每来一条消息就拽回底部。
  if(!noScroll) __scrollBottom(paneId);
  trimPane(paneId); return el;
}

// ---- v1.0.334 气泡底部操作栏：统计(agent) + 💡追问 + 📋复制 + ✏️编辑 + 🗑删除 ----
function attachMsgOps(el, paneId, role){
  const mc = el.querySelector(".mc"); if(!mc) return;
  const ops = document.createElement("div"); ops.className = "msg-ops";
  if(role === "agent"){
    const stat = document.createElement("span"); stat.className = "stat";
    stat.dataset.role = "stat";   // 流结束后由 finalizeChat 填充统计
    ops.appendChild(stat);
  }
  const mkBtn = (txt, title, fn) => {
    const b = document.createElement("button"); b.textContent = txt; b.title = title;
    b.onclick = (ev) => { ev.stopPropagation(); fn(el, paneId, role); };
    return b;
  };
  if(role === "agent"){
    ops.appendChild(mkBtn("💡", "让 agent 针对本条回答给出追问建议", msgAskBtn));
    // v1.0.373（体验C9）：任务宏——仅当本 pane 拿到了成功任务的工具序列时显示
    const calls = (typeof __lastCalls !== "undefined") ? __lastCalls[paneId] : null;
    if(calls && calls.length) ops.appendChild(mkBtn("💾 存为宏", "把本次任务的工具操作序列存为宏（同构页面可一键重放）", ()=>saveMacroPrompt(paneId)));
  }
  ops.appendChild(mkBtn("📋 复制", "复制本条内容", msgCopyMsg));
  ops.appendChild(mkBtn("✏️ 编辑", "编辑本条消息", msgEditMsg));
  ops.appendChild(mkBtn("🗑", "删除本条消息", msgDeleteMsg));
  mc.appendChild(ops);
}
// ---------- v1.0.650（F2 + 重开版式复原）：气泡底部的运行统计 ----------
// 后端自 v1.0.344 起就把 rounds / tool_calls / tool_errors / batch_done / elapsed_s /
// prompt_tokens / completion_tokens 打包成 `_task_summary` 随 done 下发，但前端只读了
// 其中两项，而且整条 chip 从不落库——退出软件重开，统计凭空消失（v645 复原了步骤卡、
// 推理框、错误行，唯独漏了这条）。现在一处生成、实时与重放共用，缺哪项就不显示哪项：
// 服务端没回 usage 就不假装有 token，退而显示答案字数（两边都能从同一文本算出，不落库也不漂）。
function _fmtK(n){
  n = Number(n) || 0;
  return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
}
function runStatHTML(s, textLen){
  s = s || {};
  const el = Number(s.elapsed_s) || 0, out = Number(s.completion_tokens) || 0, pt = Number(s.prompt_tokens) || 0;
  const parts = [];
  if (el > 0) parts.push('<span title="总耗时">⏱ <b>' + el.toFixed(1) + '</b> s</span>');
  if (Number(s.tool_calls)) parts.push('<span title="工具调用次数">🔧 <b>' + Number(s.tool_calls) + '</b></span>');
  if (Number(s.rounds)) parts.push('<span title="agent 轮次">🔁 <b>' + Number(s.rounds) + '</b></span>');
  if (pt) parts.push('<span title="输入 token（上下文每轮重传，故为累计）">↑ <b>' + _fmtK(pt) + '</b></span>');
  if (out) parts.push('<span title="输出 token">↓ <b>' + _fmtK(out) + '</b></span>');
  if (out && el) parts.push('<span title="生成速度">⚡ <b>' + (out / el).toFixed(1) + '</b> tok/s</span>');
  if (!out && textLen) parts.push('<span title="答案字数（服务端未上报 usage，只给字数）">📝 <b>' + _fmtK(textLen) + '</b> 字</span>');
  if (Number(s.tool_errors)) parts.push('<span title="工具执行失败次数">' + STATUS_ICON.err + ' <b>' + Number(s.tool_errors) + '</b></span>');
  if (Number(s.batch_done)) parts.push('<span title="已逐项记账完成的条目数">' + STATUS_ICON.ok + ' <b>' + Number(s.batch_done) + '</b> 项</span>');
  return parts.join("");
}
function applyRunStat(bubble, s, textLen){
  const stat = bubble && bubble.querySelector('.msg-ops .stat[data-role="stat"]');
  if (!stat) return false;
  stat.innerHTML = runStatHTML(s, textLen);
  return true;
}
function _msgOrdinal(el){
  // 同 pane 同 role 的第几条（0 起）——与落库顺序一一对应
  const role = el.classList.contains("user") ? "user" : "agent";
  const pane = el.closest(".chatpane"); if(!pane) return -1;
  return [].indexOf.call(pane.querySelectorAll(".msg." + role), el);
}
function _msgRaw(el){ return el.dataset.raw != null ? el.dataset.raw : (el.querySelector(".mc")||{textContent:""}).textContent; }
async function msgCopyMsg(el){
  const text = _msgRaw(el);
  try{ await navigator.clipboard.writeText(text); toast("已复制","ok"); }
  catch(e){
    const ta = document.createElement("textarea"); ta.value = text;
    document.body.appendChild(ta); ta.select();
    try{ document.execCommand("copy"); toast("已复制","ok"); }catch(e2){ toast("复制失败","err"); }
    ta.remove();
  }
}
function msgAskBtn(el, paneId){
  // v1.0.471 修复：助手对话页输入框 id 是 setChatInput（原写 setInput 不存在，
  // $() 返回 null 静默 return——追问按钮在该页完全失效）
  const inp = $(paneId==="setChatPane" ? "setChatInput" : "opInput"); if(!inp) return;
  inp.value = "针对你上面这条回答，给出 3 个值得继续深入的追问方向，并简述每个方向能解决什么问题。";
  inp.focus(); try{ inp.dispatchEvent(new Event("input")); }catch(e){}
}
function msgEditMsg(el, paneId, role){
  const mc = el.querySelector(".mc"); if(!mc || mc.querySelector(".edit-area")) return;
  const raw = _msgRaw(el);
  const tm = mc.querySelector(".tm");
  const ops = mc.querySelector(".msg-ops");
  mc.innerHTML = "";
  const ta = document.createElement("textarea"); ta.className = "edit-area"; ta.value = raw;
  const btns = document.createElement("div"); btns.className = "edit-btns";
  const save = document.createElement("button"); save.className = "primary smbtn"; save.textContent = "保存";
  const cancel = document.createElement("button"); cancel.className = "ghost smbtn"; cancel.textContent = "取消";
  btns.appendChild(save); btns.appendChild(cancel);
  mc.appendChild(ta); mc.appendChild(btns);
  const restore = () => {
    mc.innerHTML = "";
    if(role === "user"){ mc.textContent = raw; } else { mc.innerHTML = renderRich(raw); }
    if(tm) mc.appendChild(tm);
    if(ops) mc.appendChild(ops);
  };
  cancel.onclick = restore;
  save.onclick = async () => {
    const val = ta.value.trim(); if(!val){ toast("内容不能为空","err"); return; }
    el.dataset.raw = val;
    mc.innerHTML = "";
    if(role === "user"){ mc.textContent = val; } else { mc.innerHTML = renderRich(val); }
    if(tm) mc.appendChild(tm);
    if(ops) mc.appendChild(ops);
    // 会话落库同步（有会话时）；失败不回滚 UI，只提示
    const cid = el.dataset.conv || __curConv || "";
    const ordinal = _msgOrdinal(el);
    if(cid && ordinal >= 0){
      try{
        await postJson("/api/conversations/update_message",
          {id: cid, role: role, ordinal: ordinal, content: val});
      }catch(e){ toast("已改显示但落库失败："+(e.message||e), "err", 2600); }
    }
    toast("已保存","ok",1200);
  };
  ta.focus();
}
async function msgDeleteMsg(el, paneId, role){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("删除消息", "删除这条消息？（会话记录同步删除，不可恢复）", async ()=>{
    const cid = el.dataset.conv || __curConv || "";
    const ordinal = _msgOrdinal(el);
    if(cid && ordinal >= 0){
      try{
        await postJson("/api/conversations/delete_message",
          {id: cid, role: role, ordinal: ordinal});
      }catch(e){ toast("删除失败："+(e.message||e), "err"); return; }
    }
    el.remove();
    toast("已删除","ok",1200);
  }, "删除");
}
// opts（v1.0.645，重放历史用）：{step:固定步骤号, argsText:已序列化好的入参}
// 为什么要开这个口子：重开软件复原版式时得按**当时**的步骤序号与入参重建卡片，
// 而 pane._toolStep 是内存计数器、args 在流里是对象、在存档里是字符串。
// 宁可多两个参数，也不复制一遍"这张卡怎么拼"——两处各写一套正是这次 bug 的成因。
function addToolMsg(paneId, name, args, beforeEl, opts){
  const pane=_paneEl(paneId);
  const el=document.createElement("div"); el.className="msg tool";
  const mc=document.createElement("div"); mc.className="mc";
  const o=opts||{};
  let argStr=o.argsText;
  if(argStr==null){
    argStr="{}";
    try{ argStr=JSON.stringify(args,null,2); }catch(e){ argStr=String(args); }
  }
  // P4-1：分析过程可视化——工具调用按「技能分类 + 步骤序号」渲染为可折叠步骤卡片，
  // 长任务也能看清整体执行链路（读了什么→判了什么→存了什么）。
  const cat=toolCategory(name);
  const step=o.step || (pane._toolStep=(pane._toolStep||0)+1);
  mc.innerHTML=`<div class="tool-h" onclick="toggleToolArgs(this)"><span class="tool-st run" title="执行中">🔄</span><span class="tool-ic">${cat.split(" ")[0]}</span>`+
               `<span class="tool-cat">${cat}</span>`+
               `<span class="tool-step">步骤 ${step}</span> <b>${escapeHtml(name||"")}</b>`+
               `<span class="tool-fold">展开</span></div>`+
               `<div class="tool-body folded">`+
                 `<div class="tool-res">执行中…</div>`+
                 `<pre class="tool-args">${escapeHtml(argStr)}</pre>`+
               `</div>`;
  el.appendChild(mc);
  // v1.0.650 U4：原来是 opacity:0 的无 tabindex <span>——鼠标悬停才存在，键盘永远够不着。
  // 换成真 <button>（自带 tabindex 与 Enter/Space），样式见 css `.msg .copy`。
  const cp=document.createElement("button"); cp.type="button";
  cp.className="copy"; cp.textContent="📋"; cp.title="复制";
  cp.setAttribute("aria-label","复制本步骤卡片文本");
  cp.onclick=()=>{ navigator.clipboard.writeText(mc.innerText); toast("已复制","ok",1200); };
  el.appendChild(cp);
  if(beforeEl){ pane.insertBefore(el, beforeEl); } else { pane.appendChild(el); }
  __scrollBottom(paneId); trimPane(paneId); return el;   // v1.0.615：同 addMsg，盯真容器且尊重上翻
}
// 工具分类映射：按工具名前缀分组，用于步骤卡片的分组标签
const TOOL_CATS=[
  [/^(read_wecom_fields|capture_wecom_window|read_wecom_page|list_wecom_windows)/,"📖 读取页面"],
  [/^run_rule_check/,"⚖️ 规则判定"],
  [/^(read|list|save)_.*(workspace|file)|^download_file|^download_wecom_attachment/,"📁 文件操作"],
  [/^web_search|^fetch_webpage/,"🌐 网页检索"],
  [/^(select_|scroll_|click_|type_|upload_|send_|list_wecom_links)/,"🖱️ 页面交互"],
  [/^finish/,"✅ 收尾"],
];
function toolCategory(name){
  for(const pair of TOOL_CATS){ if(pair[0].test(name||"")) return pair[1]; }
  return "🔧 工具";
}
function toggleToolArgs(el){
  const mc=el.closest(".mc");
  const body=mc.querySelector(".tool-body");
  if(!body) return;
  const folded=body.classList.toggle("folded");
  const btn=mc.querySelector(".tool-fold");
  if(btn) btn.textContent=folded?"展开":"收起";
  if(!folded){ const box=mc.parentElement; if(box&&box.scrollIntoView) box.scrollIntoView({block:"nearest"}); }
}
// v1.0.645（用户："退出软件重新点开那个任务，版面也要一样"）：把「一条工具结果怎么落到
// 卡片上」从流处理里抽出来，实时流与历史重放走同一扇门。ev 两种形态都认：
// 流里是 {summary, path}，存档里是 {res, ok, path}——字段名不同而已，画法必须相同。
const TOOL_RES_MAX = 400;     // 工具结果摘要最大长度（原 js/10 的 TOOL_SUMMARY_MAX）
function applyToolResult(hostEl, ev){
  if(!hostEl || !ev) return;
  const mc = hostEl.querySelector(".mc") || hostEl;
  const summary = ev.res != null ? String(ev.res) : String(ev.summary || "");
  const r = mc.querySelector(".tool-res");
  const isErr = summary.startsWith("失败") || ev.ok === false;
  // v1.0.373 WorkBuddy 化：步骤卡状态图标（执行中 🔄 → 完成 ✅/❌）
  const st = mc.querySelector(".tool-st");
  if(st){ st.textContent = isErr ? "❌" : "✅"; st.classList.remove("run"); st.classList.add(isErr?"err":"ok"); }
  if(r){ r.textContent = summary.slice(0, TOOL_RES_MAX); r.classList.add(isErr?"err":"ok"); }
  // 表单必填进度徽标：read_wecom_page 返回 form_progress 时展示「必填 X/Y」
  try{
    const obj=JSON.parse(summary||"{}");
    const fp=obj&&obj.form_progress;
    if(fp && typeof fp.required==="number" && r){
      const done = fp.required>0 && fp.filled>=fp.required;
      const b=document.createElement("span");
      b.textContent="📋 必填 "+fp.filled+"/"+fp.required+(fp.required>0&&!done?" · 未填完":"");
      b.style.cssText="margin-left:8px;font-size:var(--fs-xs);font-weight:600;padding:1px 9px;border-radius:var(--r-md);display:inline-block;vertical-align:middle;"+
        (done?"color:var(--ok-fg);background:var(--ok-bg);":"color:var(--warn-fg);background:var(--warn-bg);");
      r.appendChild(b);
    }
  }catch(e){}
  // v1.0.627：写文件的工具（save_file / save_workspace_file / append_progress /
  // download_file / learn_capture …）产出文件，就在这张步骤卡上挂一条文件行。
  // v1.0.629：路径优先取事件直给的 path；拿不到再退一步从 summary 反解——
  // summary 是结果 JSON 的前 400 字，路径靠后或被截断时 JSON.parse 会抛，那条行就静默没了。
  try{
    let _fp = (typeof ev.path=="string" && ev.path) ? ev.path : "";
    if(!_fp){
      const o=JSON.parse(summary||"{}");
      if(o && !o.__error__ && typeof o.path=="string") _fp=o.path;
    }
    if(_fp) attachFileRefToTool(hostEl, _fp);
  }catch(e){}
}
// P3-10：限制单面板 DOM 消息节点数量，超长会话时裁剪最旧节点，避免 DOM 无限膨胀卡顿；
// 仅裁剪内存中的节点，完整历史仍在 localStorage（pushHistory），刷新后可恢复。
const PANE_MSG_MAX=200;
function trimPane(paneId){
  const pane=_paneEl(paneId); if(!pane) return;
  let cut=0;
  while(true){
    const msgs=pane.querySelectorAll(".msg");
    if(msgs.length<=PANE_MSG_MAX) break;
    const first=msgs[0];
    if(!first||first.parentNode!==pane) break;
    pane.removeChild(first); cut++;
  }
  // v1.0.614：原来静默裁掉最旧气泡，用户看到的是"上面的记录没了"。
  // 留一行可解释的提示（完整历史仍在 localStorage，刷新即回）。
  if(cut>0){
    let hint=pane.querySelector(".pane-trimmed");
    if(!hint){
      hint=document.createElement("div");
      hint.className="pane-trimmed";
      pane.insertBefore(hint, pane.firstChild);
    }
    const n=Number(hint.dataset.cut||0)+cut;
    hint.dataset.cut=n;
    hint.textContent="↑ 更早 "+n+" 条已折叠（刷新可看全部）";
  }
}
let activeControllers={};
// P3-2：need_continue（轮次上限）时记录状态，finalizeChat 不覆盖为「已完成」
let __needContinueState={};

// 解析 SSE 流并更新 UI，返回 {done, buf, needContinue}
// P2-8: Tab切换（替代:has() CSS hack，提升浏览器兼容性）
function switchWorkTab(tab) {
  document.querySelectorAll(".wt").forEach(b => b.classList.remove("active"));
  const btn = document.getElementById("tab-" + tab);
  if (btn) btn.classList.add("active");
  const grid = document.querySelector(".work-grid");
  if (grid) {
    grid.classList.remove("tab-chat","tab-pane");
    grid.classList.add("tab-" + tab);
  }
  const lc = document.getElementById("liveCard");
  if (lc) lc.classList.remove("collapsed");
  localStorage.setItem("workTab", tab);
}
document.addEventListener("DOMContentLoaded", () => {
  const saved = localStorage.getItem("workTab");
  if (saved) switchWorkTab(saved);
  // 时间显隐偏好持久化：重启后保持上次的开关状态（applyTimesPref 统一应用到所有 chatpane）
  applyTimesPref();
  // v1.0.373（体验1/10）：未配置模型时，工作台空态直接给「去配置模型」引导（原发消息才报错）
  (async function(){
    try{
      if(typeof loadModels === "function") await loadModels();
      if((typeof models !== "undefined") && models && models.length) return;
      const pane = document.getElementById("opPane"); if(!pane) return;
      const sub = pane.querySelector(".empty .em-sub"); if(!sub) return;
      sub.innerHTML = '<b>开始前先完成一次配置：</b>① <a href="#" onclick="openView(\'models\');return false" style="color:var(--accent-fg);font-weight:600">配置模型（填 API 地址与密钥）</a> → ② 直接开始问（<b>不用先绑定企业微信</b>：问答、生成文档/表格/PPT、联网检索、本机文件与命令都照常可用）→ ③ 需要我操作企微页面时，再到 <a href="#" onclick="goBindSettings();return false" style="color:var(--accent-fg)">设置 → 企业微信</a> 绑定并选中窗口';
    }catch(e){}
  })();
});

// ---- 流式出字打字机（v1.0.308）：网络包成块到达，直接按块渲染会「一顿一顿」；
// 改为把 token 追加进目标缓冲，33ms tick 匀速揭示（步长=剩余/10 自适应追赶，最少 1 字），
// 观感=匀速流出的打字机；收尾（done/停止/出错/断流）由 __tw_stop 一次性交还最终渲染。
// 渲染时保留推理折叠框与 .tm 时间戳（与原 token 渲染同规约）；超长缓冲走纯文本直写防卡顿。


// ==================== AI 产出文件的可点入口（v1.0.627） ====================
// 用户要求：AI 生成的文件要能在对话框里点一下就到它所在的位置，不用再自己翻产物库。
// 两个入口共用同一后端 /api/workspace/locate（只认解析后仍在产物库内的路径，越界 404）：
//   ① 步骤卡——凡结果里带 path 的写文件工具（save_file / save_workspace_file /
//      append_progress / download_file / learn_capture …）在卡头下方插一条文件行；
//   ② 正文——finish 后的气泡与恢复的历史气泡里，形如「产物库/产出/x.md」「产出/x.md」
//      的路径文本原地变成可点按钮。
// 全程 createElement + textContent，不把路径拼成 HTML 字符串：路径文本来自模型输出，按不可信内容处理。

// v1.0.634：扩展名补 py——跑课/做PPT 这类任务常产出脚本（产物库/产出/make_x.py），
// 回答末尾的「本次生成的文件」清单里它是常客，缺了就只是一行不能点的灰字。
// v1.0.641：再加一条**盘符绝对路径**分支——AI 把交付物直接写到桌面/下载是常见形态
// （用户实报：`C:\Users\…\Desktop\付款凭证汇总.pdf` 在正文里是灰字，点不动）。
// 只认 Windows 盘符，不加 POSIX `/a/b.pdf` 分支：那种写法会把 URL 里的
// `http://host/x.pdf` 咬成半截按钮，而这台机器上的产出不会有纯 POSIX 路径。
// 前面的 `(?<![A-Za-z0-9/])` 是同一类防误伤：少了它，`https://example.com/a.md`
// 会被当成盘符 `s:` 开头的路径咬出 `s://example.com/a.md`（台子 T4 实测到的）。
// 扩展名这份清单必须与 `agent/loop.py::_PRODUCED_EXT` 同步——两边各自匹配一份就会漂。
const _WS_EXT = "md|txt|csv|tsv|xlsx?|json|html?|pdf|docx?|pptx?|png|jpe?g|webp|zip|py";
const _WS_PATH_RE = new RegExp(
  "(?:产物库/)?(?:产出|下载|知识库|台账|日志|归档)/"
  + "[^\\s\"'，。；、）】《》|]{1,160}?\\.(?:" + _WS_EXT + ")"
  + "|(?<![A-Za-z0-9/])[A-Za-z]:[\\\\/][^\\s\"'，。；、）】《》|<>?*]{1,200}?\\.(?:" + _WS_EXT + ")",
  "g");
const _BSL = String.fromCharCode(92);      // 反斜杠：不用正则字面量写它，免得转义层数出错

async function locateWorkspaceFile(ref, action, btn){
  if(!ref) return;
  if(btn){ btn.disabled = true; }
  try{
    const d = await api("/api/workspace/locate", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ref: String(ref), action: action || "reveal"})});
    if(d && d.ok === false){ toast(d.error || "定位失败", "err", 3200); }
    else{ toast(action === "open" ? "已用系统关联程序打开该文件" : "已在资源管理器中打开并选中该文件", "ok", 2000); }
  }catch(e){ toast(fetchErr(e, "定位文件"), "err", 3200); }
  finally{ if(btn){ btn.disabled = false; } }
}

// v1.0.645（用户："产生的 md 文件不能在对话界面展开看详情"）：文件行原来只有
// 「📂 所在文件夹」和「📄 打开」（后者交给系统关联程序，等于跳出软件）。文本类文件补一枚
// 「👁 预览」，就地展开读内容——md 走 renderRich，其余文本走 <pre>；非文本类不给这个按钮，
// 免得又是一个"点了没东西"的把手。
const _PREVIEW_EXT = /^(md|markdown|txt|csv|tsv|json|jsonl|yaml|yml|ini|conf|toml|xml|html|htm|log|py|js|ts|css|sql)$/i;
function _refExt(ref){
  const b = String(ref).split(_BSL).pop().split("/").pop();
  const i = b.lastIndexOf(".");
  return i < 0 ? "" : b.slice(i + 1);
}
function _fmtBytes(n){
  n = Number(n) || 0;
  return n > 1024 * 1024 ? (n / 1048576).toFixed(1) + " MB"
       : n > 1024 ? (n / 1024).toFixed(1) + " KB" : n + " B";
}
async function toggleFilePreview(btn){
  const row = btn.closest(".file-ref"); if(!row) return;
  const opened = row.nextElementSibling;
  if(opened && opened.classList.contains("file-preview")){   // 再点一下收起
    opened.remove(); btn.textContent = "👁 预览"; return;
  }
  const ref = row.getAttribute("data-ref") || "";
  if(!ref) return;
  const panel = document.createElement("div");
  panel.className = "file-preview";
  panel.textContent = "读取中…";
  row.after(panel);
  btn.textContent = "👁 收起";
  try{
    const d = await api("/api/workspace/file?ref=" + encodeURIComponent(ref));
    if(!d || d.ok === false) throw new Error((d && d.error) || "预览失败");
    const hd = document.createElement("div");
    hd.className = "fp-hd";
    hd.textContent = (d.name || "") + " · " + _fmtBytes(d.size) + (d.truncated ? "（文件较大，只预览开头）" : "");
    const body = document.createElement("div");
    body.className = "fp-body";
    const txt = String(d.content || "");
    if(/^(md|markdown)$/i.test(d.ext || "")){ body.innerHTML = renderRich(txt); }
    else {
      const pre = document.createElement("pre");
      pre.className = "fp-raw"; pre.textContent = txt;      // 不拼 HTML，按不可信内容处理
      body.appendChild(pre);
    }
    panel.textContent = ""; panel.appendChild(hd); panel.appendChild(body);
  }catch(e){
    panel.className = "file-preview fp-err";
    panel.textContent = "预览失败：" + (e && e.message || e);
    btn.textContent = "👁 预览";
  }
}

function _fileRefRow(ref, compact){
  const row = document.createElement("div");
  row.className = "file-ref" + (compact ? " file-ref-compact" : "");
  const s = String(ref);
  row.setAttribute("data-ref", s);              // 预览要拿整句路径回查后端，不能只留文件名
  const base = s.split(_BSL).pop().split("/").pop() || s;
  const nm = document.createElement("span");
  nm.className = "file-ref-name";
  nm.textContent = "📄 " + base;
  nm.title = s;                                   // 悬停看完整路径
  row.appendChild(nm);
  const acts = _PREVIEW_EXT.test(_refExt(s)) ? [["preview", "👁 预览"], ["reveal", "📂 所在文件夹"], ["open", "📄 打开"]]
                                             : [["reveal", "📂 所在文件夹"], ["open", "📄 打开"]];
  acts.forEach(function(kv){
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ghost smbtn file-ref-btn";
    b.textContent = kv[1];
    b.setAttribute("aria-label", kv[1] + "：" + base);
    if(kv[0] === "preview"){
      b.title = "在对话里展开看这个文件的内容";
      b.onclick = function(ev){ ev.stopPropagation(); toggleFilePreview(b); };
    }else{
      b.title = kv[0] === "open" ? "用系统关联程序打开这个文件" : "点击在资源管理器中定位这个文件";
      b.onclick = function(ev){ ev.stopPropagation(); locateWorkspaceFile(ref, kv[0], b); };
    }
    row.appendChild(b);
  });
  return row;
}

// ① 步骤卡：插在卡头与折叠体之间（塞进 .tool-body 的话要点开「展开」才看得见，白做）
function attachFileRefToTool(hostEl, ref){
  if(!hostEl || !hostEl.querySelector || !ref) return;
  const mc = hostEl.querySelector(".mc") || hostEl;
  if(mc.querySelector("[data-fileref]")) return;  // 一张步骤卡只挂一条
  const body = mc.querySelector(".tool-body");
  const row = _fileRefRow(ref, true);
  row.setAttribute("data-fileref", String(ref));
  if(body){ mc.insertBefore(row, body); } else { mc.appendChild(row); }
}

// ② 正文：文本节点里的产物库路径原地变成可点按钮（跳过代码块/已在文件行里的内容）
function linkifyWorkspacePaths(scope){
  if(!scope || !scope.ownerDocument) return;
  const doc = scope.ownerDocument;
  const SKIP = {SCRIPT:1, STYLE:1, TEXTAREA:1, INPUT:1, BUTTON:1, PRE:1};
  const walker = doc.createTreeWalker(scope, NodeFilter.SHOW_TEXT, {
    acceptNode(n){
      const v = n.nodeValue;
      // 便宜的预筛：两种写法都没有就跳过（这条正则跑在整条消息树的每个文本节点上）。
      // v1.0.641：必须把反斜杠也算进来——`C:\Users\…\x.pdf` 里一个正斜杠都没有，
      // 只查 "/" 会让纯反斜杠的 Windows 路径在进正则之前就被拒掉（台子 T8 实测到的）。
      if(!v || (v.indexOf("/") < 0 && v.indexOf(_BSL) < 0)) return NodeFilter.FILTER_REJECT;
      const p = n.parentNode;
      if(!p || SKIP[p.nodeName]) return NodeFilter.FILTER_REJECT;
      // v1.0.629：行内 code 不再整类跳过——模型写文件路径几乎都带反引号
      // （`产物库/产出/x.pptx`），markdown 渲染成 <code class="md-code">，
      // 于是本功能最主场的输入被自己的防误伤规则挡掉了（用户实报"还是点不动"）。
      // 只跳代码块里的 code（在 <pre> 内），示例代码不会被改成按钮。
      if(p.nodeName === "CODE" && p.closest && p.closest("pre")) return NodeFilter.FILTER_REJECT;
      _WS_PATH_RE.lastIndex = 0;
      _WS_PATH_RE.test(v);
      return _WS_PATH_RE.lastIndex > 0 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    }
  });
  const nodes = [];
  while(walker.nextNode()){ nodes.push(walker.currentNode); }
  nodes.forEach(function(node){
    const text = node.nodeValue;
    const parent = node.parentNode;
    if(!parent) return;
    const frag = doc.createDocumentFragment();
    let last = 0, m, hit = 0;
    _WS_PATH_RE.lastIndex = 0;
    while((m = _WS_PATH_RE.exec(text))){
      // v1.0.630 修复（用户实报"点路径没打开文件夹"）：必须**逐次捕获**匹配结果。
      // 原来 onclick 里写的是 m[0]，而 m 是 while 条件里被反复赋值的同一个变量——
      // 循环正常退出时 m === null，于是每次点击都在 handler 里抛
      // "Cannot read properties of null"，按钮看着在、点了没反应（后端连日志都没有）。
      const ref = m[0];
      if(m.index > last){ frag.appendChild(doc.createTextNode(text.slice(last, m.index))); }
      const btn = doc.createElement("button");
      btn.type = "button";
      btn.className = "file-ref-link";
      btn.textContent = ref;
      btn.title = "点击在资源管理器中定位这个文件";
      btn.setAttribute("aria-label", "定位文件：" + ref);
      btn.onclick = function(){ locateWorkspaceFile(ref, "reveal", btn); };
      frag.appendChild(btn);
      last = m.index + ref.length;
      hit++;
    }
    if(!hit) return;
    if(last < text.length){ frag.appendChild(doc.createTextNode(text.slice(last))); }
    parent.replaceChild(frag, node);
  });
}

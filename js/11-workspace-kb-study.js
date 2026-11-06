async function loadWorkspace(){
  const tree=$("wsTree"); showLoading(tree,"加载产物库中…");
  try{
    const [d,sr]=await Promise.all([
      getJson("/api/workspace"),
      getJson("/api/workspace/size").catch(()=>null)
    ]);
    $("wsRoot").textContent="保存位置："+d.root;
    // 产物库体积概览：总计 + 大头构成 + 大文件提示（复用后端 workspace_size_info）。
    // v1.0.614（审查 ㉜）：后端口径改成「整个产物库」后总计会与资源管理器一致（实测
    // 23.2 → 1109.4MB），所以必须同时讲清大头是 组件/ 运行环境——否则用户会以为
    // 一堆历史下载吃掉了磁盘，去删真正要留的产出。
    let sizeHtml="";
    if(sr && typeof sr.total_mb==="number"){
      const big=(sr.large_files||[]).slice(0,5);
      const rt=Number(sr.runtime_mb)||0;
      const tops=Object.keys(sr.dirs||{}).map(k=>({k:k,v:Number(sr.dirs[k])||0}))
        .sort((a,b)=>b.v-a.v).slice(0,4).map(o=>`${escapeHtml(o.k)} ${o.v.toFixed(1)}MB`).join(" · ");
      // 警示底/边/字全走令牌：原来硬编码 #d97706 + rgba(217,119,6,.08)，深色模式下
      // 会变成"深底上贴一块浅琥珀"，且对比度不受令牌契约管。
      sizeHtml=`<div style="margin:6px 2px 10px;padding:8px 10px;border:1px solid ${big.length?"var(--warn-bd)":"var(--border)"};border-radius:var(--r-md);background:${big.length?"var(--warn-bg)":"rgba(120,140,170,.05)"};font-size:var(--fs-sm);color:var(--muted)">`+
        `📦 产物库总计 <b>${sr.total_mb.toFixed(1)} MB</b>`+
        (rt>=1?` · 其中 <b>组件 ${rt.toFixed(1)} MB</b> 是运行环境（OCR/语音/便携 Node·Git），占盘大头但<b>别手动删</b>——删了下次用要重新下载`:"")+
        (tops?`<div class="dim" style="margin-top:3px">构成：${tops}${sr.cached?`（${escapeHtml(String(sr.stat_at||""))} 统计）`:""}</div>`:"")+
        (big.length?` · ⚠️ 含 ${sr.large_files.length} 个大文件（≥10MB）：`+big.map(b=>`<span title="${escapeHtml(b.path)}">${escapeHtml(String(b.path).split(/[\\/]/).pop())}(${b.size_mb}MB)</span>`).join("、")+(sr.large_files.length>5?" 等":"")+`，可在「下载/产出」中清理`:"")+
        `</div>`;
    }
    if(!d.tree || !d.tree.length){ showEmpty(tree,"📂 产物库为空<br/>agent 产生的文件会出现在这里"); return; }
    const html=d.tree.map(g=>{
      const files=g.files.length? g.files.map(f=>{
        const kb=(f.size/1024).toFixed(1);
        const enc=encodeURIComponent(f.name);
        // v1.0.361 审查修复（P17）：文件名来自下载/上传/agent 产出（页面可控数据），
        // 原实现未转义直接进 innerHTML+onclick（存储型 XSS）；enc 也不转义单引号可逃逸
        const safeName=escapeHtml(f.name);
        const encQ=enc.replace(/'/g,"%27");
        // v1.0.650（F5）：进度台账行尾补「↻ 开新一轮」——它是纯追加的，上一轮记过的条目
        // 会让这一轮的"已处理就跳过"判定喂错，而"开新一轮"此前只有 agent 工具一条路，
        // 用户在界面里看着满屏旧记录却没有任何出口。
        const roundBtn = f.name.endsWith("_进度.md")
          ? `<button class="ghost" onclick="newWsRound('${g.sub}','${encQ}')" title="把本轮记录改名归档（带日期，留在同一目录可查），本轮从空文件重新开始">↻ 开新一轮</button>`
          : "";
        return `<div class="wrow"><span class="wname" onclick="viewWsFile('${g.sub}','${encQ}')">📄 ${safeName}</span>`
          + `<span class="wmeta">${kb}KB · ${f.mtime}</span>`
          + `<button class="ghost" onclick="openWsFile('${g.sub}','${encQ}')">打开</button>${roundBtn}</div>`;
      }).join("") : `<div class="wmeta" style="padding:4px 0">${g.readonly?"（此目录由软件自己写：台账/日志。可查看，agent 不会往里放文件）":"（此分类暂无文件——agent 产出对应类型文件后会自动归入此处）"}</div>`;
      return `<div class="wgroup"><h4>${escapeHtml(g.sub)}（${g.files.length}）${g.readonly?' <span class="dim">只读</span>':""}</h4>${files}</div>`;
    }).join("");
    $("wsTree").innerHTML=sizeHtml+html;
  }catch(e){ showEmpty(tree,"⚠️ 加载失败<br/>"+escapeHtml(e.message||"网络错误")); }
}
async function viewWsFile(sub, name){
  try{
    const d=await getJson("/api/workspace/file?sub="+encodeURIComponent(sub)+"&name="+name);
    $("wsFileName").textContent=d.name;
    $("wsFileContent").textContent=d.content;
    $("wsTree").style.display="none"; $("wsFileView").style.display="block";
  }catch(e){ toast(e.message||"读取失败","err"); }
}
function closeWsFile(){ $("wsTree").style.display="block"; $("wsFileView").style.display="none"; }
async function newWsRound(sub, encName){
  // v1.0.650（F5）：进度台账开新一轮（与 agent 工具 new_progress_round 同一后端函数）。
  const name = decodeURIComponent(encName);
  confirmAsk("开新一轮",
    "把「"+escapeHtml(name)+"」里已记的条目改名归档（同目录，带日期，随时可查），本轮从空文件重新开始。<br/>"
    + "用途：快捷入口会先读进度台账、把记过的条目当作“已处理”跳过——开新一轮就是告诉它“这些重来不算”。",
    async ()=>{
      try{
        const d=await postJson("/api/workspace/new-round",{sub:sub, name:name});
        toast(`已归档上一轮 ${d.items||0} 条 → ${d.archived}`,"ok",4200);
        loadWorkspace();
      }catch(e){ toast(e.message||"开新一轮失败","err"); }
    });
}
async function openWsFolder(){
  try{ await postJson("/api/workspace/open",{}); }catch(e){ toast(e.message||"打开失败","err"); }
}
async function openWsFile(sub, name){
  try{ await postJson("/api/workspace/open",{sub:sub, name:decodeURIComponent(name)}); }catch(e){ toast(e.message||"打开失败","err"); }
}

// ---- v1.0.478 修复：本月自动化月报统计（loadMonthlyStats 此前从未定义——
// 切 workspace 视图时同步 ReferenceError 连坐 loadMacroList，两卡片一起卡「加载中…」）----
async function loadMonthlyStats(){
  const box=$("monthlyStats"); if(!box) return;
  try{
    const d=await api("/api/audit/monthly_stats");
    const retry='<span class="sub" style="cursor:pointer;color:var(--accent-fg)" onclick="loadMonthlyStats()">🔄</span>';
    if(!d.audited){
      box.innerHTML='<b style="font-size:var(--fs-md)">📊 本月自动化月报</b>'
        +'<span class="sub">本月暂无自动审计记录——到「工作台」跑一次，或等定时任务触发</span>'+retry;
      return;
    }
    box.innerHTML='<b style="font-size:var(--fs-md)">📊 本月自动化月报</b>'
      +'<span class="tag def">'+escapeHtml(d.month)+'</span>'
      +'<span class="sub">'+d.runs+' 轮 · 审阅 <b>'+d.audited+'</b> 份：'
      +'<b style="color:var(--brand-fg)">✅ '+d.green+'</b> · '
      +'<b style="color:var(--err-fg)">❌ '+d.red+'</b>'
      +(d.other?' · ⏳ 待复检 '+d.other+' 份':'')+'</span>'
      // v1.0.650（F3）：结果能落地却不能发出去——明细在月报正文里，通道（群机器人）也在设置里，
      // 中间那一步以前只能手抄。现在点一下就推，且**只在用户点了才推**：不进定时任务、不看阈值。
      +(d.red>0?'<button class="ghost smbtn" onclick="pushRejectList()" title="把最近一轮判为不通过的机构清单发到群里（发前给你看内容；只发消息，不碰企微页面、不点任何审批按钮）">📤 推不通过清单</button>':'')
      +(d.last_ts?'<span class="sub" style="margin-left:auto">最近：'+escapeHtml(d.last_ts)+'</span>':'')
      +retry;
  }catch(e){
    box.innerHTML='<b style="font-size:var(--fs-md)">📊 本月自动化月报</b>'
      +'<span class="sub" style="color:var(--err-fg)">加载失败：'+escapeHtml(e.message||"网络错误")+'</span>'
      +'<span class="sub" style="cursor:pointer;color:var(--accent-fg)" onclick="loadMonthlyStats()">重试</span>';
  }
}
async function pushRejectList(){
  // v1.0.650（F3）：先读预览 → 用户确认 → 才真发。发出去的是群消息，不可撤回，
  // 所以这一步没有"顺手自动推"的分支，也没有定时任务会调它。
  let d;
  try{ d = await (await fetch("/api/audit/reject_list")).json(); }
  catch(e){ toast(fetchErr(e,"读取不通过清单"),"err"); return; }
  if(!d.ok){ toast(d.error||"没读到不通过清单","err"); return; }
  if(!d.red){ toast("最近一轮没有判定为不通过的机构，无需推送","info"); return; }
  const preview = d.lines.slice(0,5).map(l=>l.replace(/^·\s*/,"")).join("；");
  confirmAsk("推送不通过清单到群",
    "最近一轮（"+(d.ts||"")+"，共审阅 "+d.total+" 份）有 "+d.red+" 家不通过：\n"
    + preview + (d.lines.length>5?"……等 "+d.red+" 家":"")
    + "\n\n将通过群机器人发出，群里所有人都看得到、不可撤回。确定发送？",
    async ()=>{
      toast("正在推送…","info",1500);
      try{
        const r = await fetch("/api/audit/push_rejects",{method:"POST",headers:{"Content-Type":"application/json"},
                                                       body:JSON.stringify({})});
        const j = await r.json();
        if(j.ok) toast(`已推送到群：${j.items} 家 / ${j.sent} 条消息`,"ok",4200);
        else toast(j.error||"推送失败","err",6000);
      }catch(e){ toast(fetchErr(e,"推送不通过清单"),"err"); }
    }, "确认推送");
}
// ---- v1.0.405（M6 死引用补齐）：loadMacroList / openWebUI ----
// 背景：index.html 的「🔄 刷新宏列表」按钮与 02-nav-quicklinks.js 切换 workspace
// 视图都在调 loadMacroList，但该函数从未落地——按钮点击即 ReferenceError，
// 工作台宏卡片永远停在「加载中…」。openWebUI 同理（后端端点也缺）。
async function loadMacroList(){
  const box=$("macroListBox"); if(!box) return;
  try{
    const d=await getJson("/api/macro/list");
    const items=(d && d.macros)||[];
    if(!items.length){ box.innerHTML='<div class="sub">暂无宏——成功完成任务后，agent 气泡底部「💾 存为宏」可保存操作序列</div>'; return; }
    box.innerHTML=items.map((m,i)=>{
      const idQ=String(m.id).replace(/'/g,"%27");
      return `<div class="wrow"><span class="wname" title="${escapeHtml(m.created)}">🎬 ${escapeHtml(m.name)}</span>`
        + `<span class="wmeta">${m.steps} 步 · ${escapeHtml(m.created||"")}</span>`
        + `<button class="ghost" onclick="runMacro('${idQ}',this)">▶ 重放</button>`
        + `<button class="ghost" onclick="delMacro('${idQ}')">🗑</button></div>`;
    }).join("");
  }catch(e){ box.innerHTML='<div class="sub">加载失败：'+escapeHtml(e.message||"网络错误")+'</div>'; }
}
// v1.0.616：宏重放接权限闸门（后端 routes/b_macro.py 逐步过 gate_write_tool）。
// 需要确认时后端推 approval_request + macro_paused(i) 并停流；批准后带 start=i 从
// **未执行的那一步**续跑——从头重放会把已经点过的「通过」再点一次，那是不可撤销的。
// 执行中按钮变「⏹ 点击停止」，再点一次 abort 掉 SSE（后端在下一步 yield 时收到断流）。
const __macroCtl = {};
async function runMacro(id, btn, start){
  const running = __macroCtl[id];
  if(running){
    try{ running.abort(); }catch(e){}
    delete __macroCtl[id];
    if(btn){ btn.textContent="正在停止…"; }
    toast("已请求停止：当前这一步跑完就停，未执行的步骤不再执行","warn",3000);
    return;
  }
  const go = ()=>_macroRun(id, btn, start||0);
  if(start) { await go(); return; }
  confirmAsk("重放宏", "请先把企微页面恢复到任务起始状态，再执行重放。", go, "重放");
}
async function _macroRun(id, btn, start){
  const ctrl = new AbortController();
  __macroCtl[id] = ctrl;
  const setBtn=(txt,dis)=>{ if(btn){ btn.textContent=txt; btn.disabled=!!dis; } };
  setBtn(start?("⏹ 点击停止（续跑）"):"⏹ 点击停止", false);
  let pausedAt = -1;
  try{
    const r=await fetch("/api/macro/run",{method:"POST",signal:ctrl.signal,
      headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id,start:start})});
    // 手写分帧/解析循环收敛到 consumeSSE（01-core）：分帧（\n\n 切、余段留待下轮）、
    // data: 前缀、JSON 失败跳过与原实现逐条一致。macro_paused 的 return 映射 consumeSSE
    // 的提前返回契约（onEvent 返回非 undefined → 取消 reader 并返回 {early:...}，等价于
    // 原来的 return，只是不再悬挂连接）。
    const early = await consumeSSE(r.body.getReader(), (j)=>{
      if(j.type==="macro_step"){ toast(`宏执行 ${j.i}/${j.total}：${j.name} ${j.ok?"✅":"❌"}`, j.ok?"ok":"warn", 1500); }
      else if(j.type==="approval_request"){
        showApprovalModal("scratch", j, { noRelax:true,
          onGrant: ()=>{ toast("已批准，从第 "+(pausedAt+1)+" 步继续重放","ok"); _macroRun(id, btn, pausedAt); },
          onDeny:  ()=>{ toast("已拒绝，重放停在第 "+(pausedAt+1)+" 步（前面已执行的步骤不会回退）","warn",4000); } });
      }
      else if(j.type==="macro_paused"){ pausedAt = j.i; setBtn("▶ 重放", false); return true; }
      else if(j.type==="macro_done"){ toast(`宏完成：成功 ${j.ok} 步 / 失败 ${j.fail} 步`, j.fail?"warn":"ok", 3000); }
      else if(j.type==="error"){ toast(j.text||"宏执行失败","err",3000); }
    });
    if(early) return;   // macro_paused 提前停流：与原 return 同径（下方 finally 照样清理）
  }catch(e){
    if(String(e && e.name) !== "AbortError") toast(fetchErr(e,"重放宏"),"err");
    else toast("重放已停止","warn");
  }
  finally{ delete __macroCtl[id]; setBtn("▶ 重放", false); }
}
async function delMacro(id){
  confirmAsk("删除宏", "删除该宏？（不可恢复）", async ()=>{
    try{
      const d=await apiToast("/api/macro/del",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id})},"删除宏");
      if(d.ok){ toast("已删除","ok",1200); loadMacroList(); }
      else toast(d.error||"删除失败","err");
    }catch(_){}
  }, "删除");
}
async function openWebUI(btn){
  // v1.0.405：后端新增 /api/webui/open（系统默认浏览器打开本机服务地址）
  try{
    const d=await apiToast("/api/webui/open",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"},"打开网页版");
    toast("已在系统浏览器打开 "+d.url,"ok");
  }catch(_){}
}

// ---- 设置页（v1.0.306：界面偏好即时生效 + 系统信息） ----
// v1.0.373（体验C8 实装）：深色模式——body.dark 变量覆盖 + localStorage 记忆，启动即恢复
function applyDarkMode(on){
  document.body.classList.toggle("dark", !!on);
  // v1.0.615：主题在运行中被切换时，重建「↓ 回到最新」那颗 pill。
  // 实测：节点是浅色态下创建的（当时 display:none），切到深色后 Chrome 不重算它身上
  // var(--muted) 的依赖 → 仍是 #5a6572，落在深底 #1a1f28 上只有 2.79:1（AA 要 4.5）。
  // 开机就是深色的用户没这问题（创建时即取到 #9aa4b5，实测 6.57:1）。
  // 删掉让 __syncJump 的自愈分支重建即可，代价是一次 DOM 操作。
  try{
    ["opPane","setChatPane"].forEach(id=>{
      const sc=document.getElementById(id);
      if(sc && sc.__jump){ sc.__jump.remove(); sc.__jump=null; }
    });
  }catch(e){}
}
function toggleDarkMode(cb){
  applyDarkMode(cb.checked);
  lsSet("darkMode", cb.checked?"1":"0");
  toast(cb.checked?"🌙 已切换深色模式":"已切换浅色模式","ok",1200);
}
try{ applyDarkMode(localStorage.getItem("darkMode")==="1"); }catch(e){}
// v1.0.588：界面整体缩放（字号可读性）。用 html 的 zoom 统一放大，覆盖内联 px 字号，
// 与 Ctrl++ 等效；localStorage 记忆，启动即恢复（与深色模式同一套时序，先于 openView）。
const __UI_SCALES = {s:"0.9", m:"1", l:"1.15", xl:"1.3"};
function applyUiScale(key){
  document.documentElement.style.zoom = __UI_SCALES[key] || "1";
}
function toggleUiScale(sel){
  applyUiScale(sel.value);
  lsSet("uiScale", sel.value);
  toast("界面缩放已调整：" + (sel.selectedOptions[0] ? sel.selectedOptions[0].text : sel.value), "ok", 1400);
}
try{ applyUiScale(localStorage.getItem("uiScale") || "m"); }catch(e){}
function saveUiPref(){
  const st=$("opt_show_times");
  if(st){ lsSet("timesHidden", st.checked?"0":"1"); applyTimesPref(); }
  // v1.0.633：删除「自动任务分组默认收起」的读写——分组已收进侧栏「更多」，
  // 由「更多」自身折叠，这个开关再留着就是一个不再生效的界面承诺
  const dk=$("opt_dark_mode");
  if(dk){ applyDarkMode(dk.checked); lsSet("darkMode", dk.checked?"1":"0"); }
  toast("已保存","ok",1200);
}
async function loadUiPref(){
  const st=$("opt_show_times"); if(st) st.checked=!timesHiddenPref();
  const dk=$("opt_dark_mode");
  if(dk){ let dm=false; try{ dm=localStorage.getItem("darkMode")==="1"; }catch(e){} dk.checked=dm; applyDarkMode(dm); }
  const us=$("opt_ui_scale");
  if(us){ let k = lsGet("uiScale") || "m"; if(!__UI_SCALES[k]) k="m"; us.value=k; applyUiScale(k); }
  try{ const d=await getJson("/api/workspace"); const el=$("opt_wsroot"); if(el&&d.root) el.value=d.root; }catch(e){}
}
// ---- v1.0.373 管理员模式：状态显示 + UAC 提权重启 ----
async function loadAdminStatus(){
  const el=$("adminState"); if(!el) return;
  const btn=$("adminRestartBtn");
  try{
    const d=await getJson("/api/admin/status");
    if(d.is_admin){
      el.innerHTML='✅ 当前正以<b style="color:var(--brand-fg)">管理员模式</b>运行——系统级命令、保护目录写入、HKLM 注册表、系统级计划任务已解锁。';
      if(btn) btn.style.display="none";
    }else if(!d.elevatable){
      el.innerHTML='当前为普通权限运行。开发模式下请以管理员身份运行 IDE/终端来调试系统级能力。';
      if(btn) btn.style.display="none";
    }else{
      el.innerHTML='当前为普通权限运行。点击下方按钮会弹出 UAC 确认，确认后软件将以管理员模式重启。';
    }
  }catch(e){ el.textContent="管理员状态获取失败"; }
}
async function adminRestart(btn){
  if(btn && btn.disabled) return;
  if(btn){ btn.disabled=true; btn.textContent="⏳ 检查中…"; }
  let armed = false;
  try{
    const st = await getJson("/api/admin/status");   // v1.0.655 B6：task_status 并入 admin/status（armed 字段同形）
    armed = !!st.armed;
  }catch(e){}
  if(btn){ btn.disabled=false; btn.textContent="🛡 以管理员模式重启"; }
  if(!armed){
    // v1.0.542：未启用 → 引导一次性注册（弹 UAC 注册计划任务，之后零 UAC）
    confirmAsk("启用管理员重启支持",
      "首次使用需注册一个计划任务（弹一次 UAC 确认）。之后「管理员模式重启」不再弹 UAC、不再受安全软件拦截。现在启用吗？",
      async ()=>{
        if(btn){ btn.disabled=true; btn.textContent="⏳ 注册中…"; }
        try{
          const r=await fetch("/api/admin/arm_task",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
          const d=await r.json();
          if(!d.ok){ toast(d.error||"启用失败","err",6000); return; }
          toast("已启用——现在可以直接点「以管理员模式重启」","ok");
        }catch(e){ toast(fetchErr(e,"启用管理员重启支持"),"err"); }
        finally{ if(btn){ btn.disabled=false; btn.textContent="🛡 以管理员模式重启"; } }
      }, "启用");
    return;
  }
  confirmAsk("以管理员模式重启", "软件将退出并以管理员权限重新启动（计划任务预授权，无需 UAC）。未发送的输入会丢失，确认继续？", async ()=>{
    if(btn){ btn.disabled=true; btn.textContent="⏳ 正在提权重启…"; }
    try{
      const r=await fetch("/api/admin/restart",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
      const d=await r.json();
      if(!d.ok){
        if(d.need_arm){ toast("管理员重启支持未启用——请再次点击完成一次性启用","err",5000); }
        else { toast((d.error||"重启失败"),"err",6000); }
        if(btn){ btn.disabled=false; btn.textContent="🛡 以管理员模式重启"; }
        return;
      }
      toast(d.hint||"软件将以管理员模式重启","ok");
      document.title += "（管理员重启中…）";
    }catch(e){
      toast(fetchErr(e,"管理员重启"),"err");
      if(btn){ btn.disabled=false; btn.textContent="🛡 以管理员模式重启"; }
    }
  }, "重启");
}
// ---- v1.0.369/373 开机自启 + 启动后隐藏主窗口（HKCU Run 注册表 + --minimized 参数） ----
// v1.0.373（需求2）：管理员模式自启动（计划任务 /rl highest）——勾选弹 UAC 创建，取消弹 UAC 删除
async function loadAutostart(){
  const el=$("opt_autostart"); if(!el) return;
  const hd=$("opt_autostart_hide");
  const ad=$("opt_autostart_admin");
  try{
    const d=await getJson("/api/autostart");
    el.checked=!!d.enabled;
    if(ad){ ad.checked=(d.mode==="admin"); ad.disabled=!d.supported; }
    if(hd){ hd.checked=!!d.hide_main; hd.disabled=!d.supported || !d.enabled; }
    if(!d.supported){ el.disabled=true; el.parentElement.title="仅打包版（exe）支持开机自启；开发模式请手动配置"; }
    else el.disabled=false;
  }catch(e){ el.disabled=true; if(ad) ad.disabled=true; }
}
async function toggleAutostart(cb){
  const want=cb.checked;
  try{
    const hide=$("opt_autostart_hide");
    const r=await fetch("/api/autostart",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({enabled:want, hide_main:!!(hide&&hide.checked)})});
    const d=await r.json();
    if(!d.ok){ cb.checked=!want; toast(d.error||("HTTP "+r.status),"err"); return; }
    cb.checked=!!d.enabled;
    const ad=$("opt_autostart_admin");
    if(ad) ad.checked=(d.mode==="admin");
    if(hide){ hide.disabled=!d.enabled; hide.checked=!!d.hide_main; }
    toast(want?(d.mode==="admin"?"✅ 管理员模式自启保持开启":"✅ 已开启开机自启（下次开机自动运行）"):"已关闭开机自启","ok");
  }catch(e){ cb.checked=!want; toast(fetchErr(e,"开机自启设置"),"err"); }
}
// v1.0.373（需求2）：管理员模式自启动——勾选创建最高权限计划任务（UAC 确认），取消删除任务
async function toggleAutostartAdmin(cb){
  const want=cb.checked;
  cb.checked=!want;   // 先回滚，用户在弹层确认后再置位（confirmAsk 无取消回调）
  confirmAsk(want?"管理员模式自启动":"取消管理员模式自启动",
    want?"将创建一个最高权限的计划任务（UAC 弹窗需点「是」）：开机后软件免 UAC 直接以管理员模式运行，并替换现有普通自启。确认继续？"
        :"将删除管理员自启动的计划任务（UAC 弹窗需点「是」），开机不再自动运行软件。确认继续？",
    async ()=>{
      cb.checked=want;
      const en=$("opt_autostart");
      const hd=$("opt_autostart_hide");
      try{
        const r=await fetch("/api/autostart",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({enabled:want, admin:want, hide_main:!!(hd&&hd.checked)})});
        const d=await r.json();
        if(!d.ok){ cb.checked=!want; toast(d.error||("HTTP "+r.status),"err"); return; }
        if(en) en.checked=!!d.enabled;
        if(hd){ hd.disabled=!d.enabled; hd.checked=!!d.hide_main; }
        toast(want?"✅ 已设置管理员模式自启动（开机免 UAC 直接提权运行）":"已取消管理员模式自启动","ok");
      }catch(e){ cb.checked=!want; toast(fetchErr(e,"管理员自启动设置"),"err"); }
    }, want?"创建任务":"删除任务");
}
async function toggleAutostartHide(cb){
  const en=$("opt_autostart");
  if(!en || !en.checked){ cb.checked=false; toast("请先开启「开机自动启动」","warn"); return; }
  try{
    const r=await fetch("/api/autostart",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({enabled:true, hide_main:cb.checked})});
    const d=await r.json();
    if(!d.ok){ cb.checked=!cb.checked; toast(d.error||("HTTP "+r.status),"err"); return; }
    cb.checked=!!d.hide_main;
    toast(cb.checked?"已设置：开机启动后隐藏主窗口（从任务栏托盘恢复）":"已设置：开机启动后正常显示主窗口","ok");
  }catch(e){ cb.checked=!cb.checked; toast(fetchErr(e,"隐藏窗口设置"),"err"); }
}
async function openLogFolder(){
  try{ await postJson("/api/workspace/open",{dir:"logs"}); }catch(e){ toast(e.message||"打开失败","err"); }
}
// ---- 功能管理（v1.0.312）：总闸存 config.feature_flags，立即生效 ----
// v1.0.617：键名唯一事实源在 core/config_store.py::FEATURE_FLAGS。这份 JS 数组是浏览器里
// 不得不再留的一份，靠 tests/test_v617_flags.py 断言"三者集合相等"（Python 键表 / 这里 /
// index.html 的 opt_ff_* 复选框 id）——加开关不改锁就变红，不再靠人肉对齐。
const _FF_KEYS=["skills","connectors","team","memory","evolve","self_repair_daily","self_auto_fix"];
// 缺省即关的键（其余维持"缺省即开"的历史语义）；须等于 FEATURE_FLAG_OFF_BY_DEFAULT ∩ UI 键
const _FF_OFF_BY_DEFAULT=["self_auto_fix"];
async function loadFeatureFlags(){
  try{
    const d=await getJson("/api/config");
    const ff=d.feature_flags||{};
    // v1.0.357：self_auto_fix 是危险开关（会自动改源码），默认必须为关（未设置时不勾选）
    _FF_KEYS.forEach(k=>{ const el=$("opt_ff_"+k); if(el) el.checked = _FF_OFF_BY_DEFAULT.includes(k) ? ff[k]===true : ff[k]!==false; });
  }catch(e){}
}
async function saveFeatureFlags(){
  const ff={};
  _FF_KEYS.forEach(k=>{ const el=$("opt_ff_"+k); if(el) ff[k]=!!el.checked; });
  try{
    await apiToast("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({feature_flags:ff})},"保存功能开关");
    const off=[];
    if(!ff.skills) off.push("技能系统"); if(!ff.connectors) off.push("连接器");
    toast("功能管理已保存"+(off.length?("（已停用："+off.join("、")+"）"):""),"ok");
  }catch(_){}
}
// v1.0.354 体检进度渲染：百分比无法可靠预测（LLM 输出时长不可知），
// 用「阶段指示 + 已接收字符数 + 已耗时」如实展示——字符在涨就证明还在跑。
const _REPAIR_PHASES = ["采集数据", "模型分析", "应用修复", "完成"];
const _PHASE_IDX = {collect: 0, llm: 1, apply: 2, done: 3};
function renderRepairProgress(box, prog){
  const p=(prog&&prog.phase)||"collect";
  const idx=(_PHASE_IDX[p]!==undefined)?_PHASE_IDX[p]:0;
  let chips=_REPAIR_PHASES.map((ph,i)=>
    i<idx ? '<span style="color:var(--brand-fg)">✅ '+ph+'</span>'
    : i===idx ? '<span style="color:var(--brand-fg);font-weight:600">⏳ '+ph+(p==="llm"?"…":"")+'</span>'
    : '<span class="dim">⚪ '+ph+'</span>').join(' <span style="color:var(--muted)">›</span> ');
  let line="";
  if(p==="llm"){
    line='已接收 <b style="color:var(--brand-fg)">'+(prog.tokens||0)+'</b> 字符 · 已耗时 '
      +(prog.elapsed_s||0)+' 秒'+(prog.model?(' · 模型：'+escapeHtml(prog.model)):'');
  }else if(p==="collect"){ line="正在采集运行日志与经验库…"; }
  else if(p==="apply"){ line=escapeHtml(prog.note||"正在应用修复…"); }
  else if(p==="cancelled"){ line='<span style="color:var(--err-fg)">⏹ '+escapeHtml(prog.note||"已取消")+'</span>'; }
  else if(p==="failed"){ line='<span style="color:var(--err-fg)">'+escapeHtml(prog.note||"体检失败")+'</span>'; }
  box.innerHTML='<div style="font-size:var(--fs-sm);margin-bottom:4px">'+chips+'</div>'+
    (line?'<div class="sub" style="font-size:var(--fs-sm)">'+line+'</div>':'')+
    '<div style="height:3px;border-radius:2px;background:var(--border);overflow:hidden;margin-top:6px">'+
    '<div style="width:38%;height:100%;background:var(--brand);animation:repairSlide 1.6s ease-in-out infinite"></div></div>';
}
let _repairRunning = false;   // v1.0.356：体检运行态——按钮同位切换「⏹ 取消体检」
// v1.0.373 一键打包并重启（确认式）：自我修复/版本管理页触发——
// 程序 2s 后退出 → 延迟批处理接管（杀进程 → build --force 打包 → 自动启动新 exe）
async function rebuildNow(btn){
  if(btn && btn.disabled) return;
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("打包并重启", "用当前源码打包新版本并重启软件？程序将在 2 秒后退出（未发送的输入会丢失）；打包约 1-2 分钟期间窗口消失属正常；完成后自动启动新版本，打包失败则自动回到当前版本。", async ()=>{
    if(btn){ btn.disabled=true; btn.textContent="⏳ 正在退出并打包…"; }
    try{
      const r=await fetch("/api/repair/rebuild",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({note:"软件内一键打包"})});
      const d=await r.json();
      if(!d.ok){ toast(d.error||"触发失败","err"); if(btn){ btn.disabled=false; btn.textContent="📦 打包并重启"; } return; }
      toast(d.hint||"正在退出并打包，完成后自动重启","ok");
      document.title += "（打包中…）";
    }catch(e){
      toast(fetchErr(e,"一键打包"),"err");
      if(btn){ btn.disabled=false; btn.textContent="📦 打包并重启"; }
    }
  }, "打包并重启");
}
// v1.0.358 版本管理：版本列表与一键切换（重启软件生效）。
// 版数由后端 version_store._KEEP 决定，这里不写数字——写过一次，结果 _KEEP 降到 3 之后
// 这句还是旧的，直到 v1.0.615 才被 copy-truth 锁查出来（js/ 当时还不在扫描面内）。
async function loadVersionList(){
  const box=$("versionListBox"); if(!box) return;
  try{
    const d=await getJson("/api/versions");
    const cur=d.current||"";
    const vs=d.versions||[];
    // v1.0.373（体验B5）：软件本体更新提示——产物库里有比当前运行版本更新的构建
    //（版本管理切换走 staged copy，重启生效），置顶横幅一键切换
    const latest = vs[0];
    let banner = "";
    if(latest && latest.version && d.current && latest.version !== d.current){
      banner = '<div style="margin:0 0 8px;padding:9px 12px;border:1px solid var(--accent);background:var(--accent-soft);border-radius:var(--r-md);font-size:var(--fs-sm);display:flex;align-items:center;gap:8px">'+
        '<span>🆕 检测到新版本 <b>v'+escapeHtml(latest.version)+'</b> 已构建（当前运行 v'+escapeHtml(d.current)+'）</span>'+
        '<button class="primary smbtn" style="margin-left:auto" onclick="switchVersion(\''+jsStr(latest.version)+'\')">⏬ 切换到新版本</button></div>';
    }
    if(!vs.length){ box.innerHTML='<div class="sub">还没有版本记录——下次打包构建（build.py --yes）后自动登记。</div>'; return; }
    box.innerHTML=banner+'<div style="font-size:var(--fs-sm)">'+vs.map(v=>{
        const isCur=v.version===cur;
        const t=new Date((v.ts||0)*1000);
        const tm=isFinite(t.getTime())?((t.getMonth()+1)+"-"+t.getDate()+" "+String(t.getHours()).padStart(2,"0")+":"+String(t.getMinutes()).padStart(2,"0")):"";
        const name=isCur?'<b style="color:var(--brand-fg)">'+escapeHtml(v.version)+'（当前）</b>':escapeHtml(v.version);
        const btn=isCur?'':'<button class="ghost smbtn" onclick="switchVersion(\''+jsStr(v.version)+'\')">⏪ 切换到此版</button>';
        // v1.0.360：版本介绍——相对上一版的提交变更清单（最多 10 条）
        const chs=(v.changes&&v.changes.length)
          ? '<div style="margin:3px 0 2px;padding-left:6px;border-left:2px solid var(--border);color:var(--muted);font-size:var(--fs-xs);line-height:1.55">'+
            v.changes.map(c=>'• '+escapeHtml(c)).join('<br/>')+'</div>'
          : ((v.note&&(!v.changes||!v.changes.length))?'<div style="margin:3px 0 2px;padding-left:6px;border-left:2px solid var(--border);color:var(--muted);font-size:var(--fs-xs)">'+escapeHtml(v.note)+'</div>':'');
        return '<div style="padding:8px 4px;border-top:1px solid var(--border)">'+
          '<div style="display:flex;align-items:center;gap:8px"><span>'+name+'</span>'+
          '<span style="color:var(--muted);font-size:var(--fs-xs)">'+tm+'</span>'+
          '<span style="margin-left:auto">'+btn+'</span></div>'+chs+'</div>';
      }).join("")+'</div>';
  }catch(e){ box.innerHTML='<div class="sub">加载失败：'+escapeHtml(fetchErr(e,"版本列表"))+'</div>'; }
}
async function switchVersion(ver){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("切换版本", "确定切换到 " + ver + " ？当前版本会先自动快照（可随时切回）；源码与程序文件同步回退，重启软件后生效。", async ()=>{
    try{
      const d=await apiToast("/api/versions/switch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({version:ver})},"版本切换");
      toast("已切换到 "+ver+"（"+(d.detail||"")+"）","ok");
      loadVersionList();
    }catch(_){}
  }, "切换");
}
function repairBtnClick(){
  if(_repairRunning){ cancelSelfRepair(); } else { runSelfRepair(); }
}
async function cancelSelfRepair(){
  try{
    const d = await postJson("/api/repair/cancel",{});
    toast(d.was_running ? "已请求取消，正在中断模型调用…" : "当前没有正在运行的体检", d.was_running ? "ok" : "warn");
  }catch(e){ toast(fetchErr(e,"取消体检"),"err"); }
}
async function runSelfRepair(){
  const btn=$("repairRunBtn"); const box=$("repairResult");
  _repairRunning = true;
  window.__repairPollAborted = false;   // v1.0.471（UI#9）：新一轮体检复位轮询取消标志
  if(btn){ btn.disabled=false; btn.textContent="⏹ 取消体检"; btn.classList.remove("primary"); btn.classList.add("ghost"); }
  if(box){
    box.style.display="block";
    renderRepairProgress(box, {phase:"collect", tokens:0, elapsed_s:0});
  }
  try{
    const startTs=Math.floor(Date.now()/1000)-2;   // 容忍 2s 时钟偏差
    const r=await fetch("/api/repair/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({background:true})});
    const d=await r.json();
    if(!d.ok || !d.started) throw new Error(d.error||"启动失败");
    // 双通道轮询：progress.json（1.5s，实时字符数/阶段）+ last_run.json（完成/失败判定）。
    // 轮询窗 15 分钟——超时给非终态提示（后台线程会继续跑完落盘报告，可手动刷新）。
    // v1.0.471（UI#9）：轮询可取消——离开设置视图（cleanupViewPollers）或 DOM 已移除
    // 时停止空转，原实现循环固定空转到 15 分钟 deadline。
    const deadline=Date.now()+15*60*1000;
    let last=null;
    while(Date.now()<deadline){
      if(window.__repairPollAborted || (box && !box.isConnected)) break;
      await new Promise(res=>setTimeout(res,1500));
      if(window.__repairPollAborted) break;
      try{
        // v1.0.655 B6：last+progress 合并为 /repair/status 一次往返（原两趟轮询）
        const rs=await getJson("/api/repair/status?t="+Date.now());
        const lr=rs.last||{};
        if((lr.ts||0)>=startTs){ last=lr; break; }
        if(box && box.isConnected) renderRepairProgress(box, rs.progress||{});
      }catch(e){}
      if(btn && btn.textContent.indexOf("…")<0) btn.textContent="🩺 体检中…";
    }
    if(box){
      if(!last){
        box.innerHTML='<div class="note">⏳ 体检仍在后台进行（模型输出较慢，已等待 15 分钟——推理型模型属正常）。'+
          '后台会继续跑完并把结果写入报告，点「📁 查看/刷新结果」随时查看；期间不必重新点击体检。</div>'+
          '<div class="row" style="margin-top:6px"><button class="ghost smbtn" onclick="viewLatestRepairReport()">🔄 查看/刷新结果</button></div>';
      }else if(last.cancelled){
        box.innerHTML='<div class="note">⏹ 体检已取消（'+escapeHtml(last.error||"已被用户取消")+'）。可随时重新点「🩺 立即体检」。</div>';
      }else if(last.ok){
        const ld=(await getJson("/api/repair/status?t="+Date.now())).latest||{};   // v1.0.655 B6
        const fx=(last.autofix&&last.autofix.length)
          ? '<div style="margin-top:4px;font-size:var(--fs-sm)">'+last.autofix.map(a=>{
              const ic={committed:"✅",applied:"✅",reverted:"↩️",rejected:"⛔",error:"❌"}[a.status]||"•";
              return '<div>'+ic+' [自动修复·'+escapeHtml(a.status||"")+'] '+escapeHtml(a.finding||"")+(a.file?('（'+escapeHtml(a.file)+'）'):'')+'</div>';
            }).join("")+'</div>' : '';
        // v1.0.373（优化2）：自动修复已提交 → 修复的是源码，需打包重启才对运行的 exe 生效
        const fxDone=(last.autofix||[]).some(a=>a.status==="committed"||a.status==="applied");
        const rebuildTip=fxDone
          ? '<div style="margin-top:6px;padding:8px 10px;border:1px solid var(--accent);background:var(--accent-soft);border-radius:var(--r-md);font-size:var(--fs-sm);display:flex;align-items:center;gap:8px">'+
            '<span>🔧 修复已写入源码——需重新打包才能对运行中的软件生效</span>'+
            '<button class="primary smbtn" style="margin-left:auto" onclick="rebuildNow(this)">📦 打包并重启</button></div>' : '';
        box.innerHTML='<div class="note">✅ 体检完成：自动修复 '+(last.applied||0)+' 条，跳过 '
          +(last.skipped||0)+' 条，待人工确认 '+(last.manual||0)+' 条。'+(ld.name?("报告："+escapeHtml(ld.name)):"")+'</div>'+fx+rebuildTip+
          (ld.content?'<div style="border:1px solid var(--border);border-radius:var(--r-md);padding:8px 10px;margin-top:6px;font-size:var(--fs-sm);max-height:320px;overflow:auto">'+renderRich(ld.content)+'</div>':'');
      }else{
        box.innerHTML='<div class="note">❌ 体检未完成：'+escapeHtml(last.error||"未知原因")+
          '。<br/>常见原因：模型接口不可达/超时。请检查「模型管理」里的接口连通性后重试（会自动换备用模型）。</div>';
      }
    }
    toast(last?(last.cancelled?"体检已取消":(last.ok?"自我体检完成":"体检未完成（见详情）")):"体检后台仍在进行", last?(last.ok?"ok":"warn"):"ok");
  }catch(e){
    const msg=fetchErr(e,"自我体检");
    if(box){ box.style.display="block"; box.innerHTML='<div class="note">体检失败：'+escapeHtml(msg)+'</div>'; }
    toast(msg,"err");
  }finally{
    _repairRunning = false;
    if(btn){ btn.textContent="🩺 立即体检"; btn.classList.remove("ghost"); btn.classList.add("primary"); }
  }
}
async function viewLatestRepairReport(){
  const box=$("repairResult"); if(!box) return;
  box.style.display="block";
  try{
    const ld=(await getJson("/api/repair/status?t="+Date.now())).latest||{};   // v1.0.655 B6
    box.innerHTML=ld.content
      ? '<div class="note">最新报告：'+escapeHtml(ld.name||"")+'</div><div style="border:1px solid var(--border);border-radius:var(--r-md);padding:8px 10px;margin-top:6px;font-size:var(--fs-sm);max-height:320px;overflow:auto">'+renderRich(ld.content)+'</div>'
      : '<div class="sub">还没有体检报告。若刚点过体检，模型可能仍在输出——稍等片刻再点「🔄 查看/刷新结果」。</div>';
  }catch(e){ box.innerHTML='<div class="note">读取失败：'+escapeHtml(fetchErr(e,"报告"))+'</div>'; }
}
async function viewMemory(){
  const anchor=$("opt_ff_memory");
  let box=$("memViewBox");
  if(box){ box.remove(); return; }   // 再点收起
  try{
    const d=await getJson("/api/memory/list");
    box=document.createElement("div");
    box.id="memViewBox";
    box.style.cssText="margin-top:8px;max-height:300px;overflow:auto;background:var(--panel2);border:1px solid var(--border);border-radius:var(--r-md);padding:8px 10px;font-size:var(--fs-sm)";
    if(!(d.items||[]).length){
      box.textContent = "（记忆为空——对话完成后会自动沉淀值得记住的事实）";
    } else {
      d.items.forEach(it=>{
        const row=document.createElement("div");
        row.style.cssText="display:flex;align-items:flex-start;gap:6px;padding:4px 0;border-bottom:1px solid var(--border)";
        // ts 是后端从 MEMORY.md 用 `[^\]]+` 抓出来的自由文本（模型写什么就是什么），
        // 不转义就是残余注入面——同文件别处已统一 escapeHtml（v1.0.616 补齐）。
        const meta = (it.score!=null?`<span style="color:${it.score>=8?"var(--ok-fg)":"var(--warn-fg)"};flex:0 0 auto">${it.score}分</span>`:"")+
          (it.ts?`<span style="color:var(--muted);flex:0 0 auto">${escapeHtml(it.ts)}</span>`:"");
        const txt=document.createElement("span");
        txt.style.cssText="flex:1;overflow-wrap:anywhere";
        txt.textContent=it.text;
        const del=document.createElement("button");
        del.className="ghost"; del.textContent="删"; del.style.minHeight="20px"; del.style.padding="0 6px"; del.style.fontSize="var(--fs-xs)";
        del.onclick=()=>{
          // v1.0.471（UI#6）：原生 confirm → confirmAsk
          confirmAsk("删除记忆", "删除这条记忆？", async ()=>{
            try{
              const rr=await fetch("/api/memory/delete_line",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({idx:it.idx})});
              const dd=await rr.json();
              if(dd.ok){ toast("已删除","ok"); row.remove(); if(!dd.remaining){ box.textContent="（记忆已清空）"; } }
              else toast(dd.error||"删除失败","err");
            }catch(_){ toast("删除失败","err"); }
          }, "删除");
        };
        row.appendChild(meta.tagName?meta:document.createElement("span")).innerHTML=meta;
        row.appendChild(txt); row.appendChild(del);
        box.appendChild(row);
      });
    }
    if(anchor) anchor.closest("label.field").appendChild(box);
  }catch(e){ toast(fetchErr(e,"读取记忆"),"err"); }
}
async function clearMemory(){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("清空本地记忆", "清空全部本地记忆？此操作不可恢复（建议先查看确认）。", async ()=>{
    try{
      const d=await apiToast("/api/memory/clear",{method:"POST"},"清空记忆");
      toast(`已清空 ${d.removed} 条记忆`,"ok");
      const box=$("memViewBox"); if(box) box.remove();
    }catch(_){}
  }, "清空");
}
// ---- 用户固定规则（v1.0.313 三层记忆之用户级规则） ----
async function loadUserRules(){
  try{
    const d=await getJson("/api/config");
    const el=$("opt_user_rules"); if(el) el.value=d.user_rules||"";
  }catch(e){}
}
async function saveUserRules(btn){
  setBtnLoading(btn,true);
  try{
    await apiToast("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({user_rules:$("opt_user_rules").value})},"保存规则");
    toast("用户规则已保存（下次对话生效）","ok");
  }catch(_){}
  finally{ setBtnLoading(btn,false); }
}
// ---- 定时任务（v1.0.313）：CRUD + 立即跑 ----
async function loadSchedules(){
  const list=$("schedList"); if(!list) return;
  try{
    const d=await getJson("/api/schedules");
    const items=d.schedules||[];
    list.innerHTML="";
    if(!items.length){ list.innerHTML='<div class="sub">暂无定时任务——用下方表单添加。</div>'; return; }
    items.forEach(s=>{
      const row=document.createElement("div");
      row.style.cssText="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);flex-wrap:wrap";
      const dot=s.running?'<span class="pill wrn">执行中…</span>':(s.enabled?'<span class="pill ok">启用</span>':'<span class="pill">已停用</span>');
      // v1.0.614：上次结果的状态要看得见。超时中止 / 中途要确认，过去只写成一句文本、
      // 徽标一律"启用"，用户第二天以为都跑完了（判据落在 scheduler.last_status 上）。
      const _bad={timeout:"⚠️ 超时中止", blocked:"⚠️ 待确认未完成", error:"❌ 执行异常"}[s.last_status||""];
      const lastTag=_bad?` <span class="pill err">${_bad}</span>`:"";
      row.innerHTML =
        `<b>${escapeHtml(s.name)}</b><code>${escapeHtml(s.cron)}</code>${dot}${lastTag}`+
        `<span class="sub" style="flex:1;min-width:200px;font-size:var(--fs-xs)">${escapeHtml((s.prompt||"").slice(0,60))}${s.last_run?`<br/>上次：${escapeHtml(s.last_run)} · ${escapeHtml((s.last_result||"").slice(0,60))}`:""}</span>`;
      const mkBtn=(txt,fn,primary)=>{ const b=document.createElement("button"); b.className=primary?"primary":"ghost"; b.style.padding="3px 10px"; b.style.fontSize="var(--fs-xs)"; b.textContent=txt; b.onclick=fn; return b; };
      row.appendChild(mkBtn(s.enabled?"停用":"启用",()=>toggleSchedule(s.id,!s.enabled)));
      row.appendChild(mkBtn("▶ 立即跑",()=>runScheduleNow(s.id)));
      row.appendChild(mkBtn("🗑",()=>delSchedule(s.id)));
      list.appendChild(row);
    });
  }catch(e){ list.innerHTML='<div class="sub">加载失败：'+escapeHtml(e.message||"")+'</div>'; }
}
async function saveSchedule(btn){
  const name=$("sched_name").value.trim(), prompt=$("sched_prompt").value.trim();
  if(!name||!prompt){ toast("任务名与提示词必填","warn"); return; }
  setBtnLoading(btn,true);
  try{
    await apiToast("/api/schedules",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name,prompt,cron:$("sched_cron").value,enabled:true})},"添加定时任务");
    toast("定时任务已添加","ok");
    $("sched_name").value=""; $("sched_prompt").value="";
    loadSchedules();
  }catch(_){}
  finally{ setBtnLoading(btn,false); }
}
// v1.0.471（UI#13）：启停/删除/立即跑补错误处理（原 await fetch 裸奔，失败静默）
async function toggleSchedule(id,enabled){
  try{
    await apiToast(`/api/schedules/${id}/enabled`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled})},"切换定时任务");
    loadSchedules();
  }catch(_){}
}
async function delSchedule(id){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk（统一弹窗风格）
  confirmAsk("删除定时任务", "删除后该任务不再执行且不可恢复。", async ()=>{
    try{
      await apiToast(`/api/schedules/${id}`,{method:"DELETE"},"删除定时任务");
      loadSchedules(); toast("定时任务已删除","ok");
    }catch(_){}
  }, "删除");
}
async function runScheduleNow(id){
  try{
    await apiToast(`/api/schedules/${id}/run`,{method:"POST"},"立即执行定时任务");
    toast("已触发后台执行，结果稍后见 产物库/产出/","ok");
    setTimeout(loadSchedules, 2500);
  }catch(_){}
}
// ---- OCR 组件按需在线安装（v1.0.307）：状态展示 + 手动触发 + 下载中轮询 ----
var __ocrPolling = false;
// v1.0.473 组件体检：外部组件状态汇总（/api/components/status；v1.0.611 起含语音组件，共七类）
// v1.0.473（用户反馈：体检后没有具体方案）——渲染结构化 actions：
//   api  → POST /api/components/fix（软件内一键完成，如 OCR 安装/dsh 安装/OCR 升级）
//   link → 系统浏览器打开下载页（Git/Node 等系统级组件）
//   cmd  → 复制命令到剪贴板（用户在终端执行后重新体检）
var __compPoll = null;   // v1.0.606：有组件在「安装中」时的 3s 自动刷新定时器（切视图即停）
async function loadComponents(btn){
  const box=$("compList"); if(!box) return;
  if(__compPoll){ clearTimeout(__compPoll); __compPoll=null; }
  if(btn){ setBtnLoading(btn,true); }
  try{
    const d=await api("/api/components/status");
    window.__compData=d;   // v1.0.607：一键安装要从同一份数据里挑「软件内可自动装」的项
    const need=(d.components||[]).filter(c=>c.status!=="ok"&&c.status!=="na").length;   // v1.0.619：na=本机不适用，不算待处理
    // v1.0.650 U6：汇总行以前说「全部就绪」，行内同一条目又叫「可用」——同一页两口径。
    $("comp_summary").textContent=`${d.ok_n}/${d.total} 项${STATUS_WORD.ok}${need?` · ${need} 项待处理`:` · 全部${STATUS_WORD.ok}`}${d.na_n?` · ${d.na_n} 项${STATUS_WORD.na}`:""} · ${d.checked_at}`;
    const ic=STATUS_ICON, st=STATUS_WORD;   // v1.0.508 规范 → v1.0.650 收敛到 js/01-core.js 的单一事实源
    box.innerHTML=d.components.map(c=>{
      const acts=(c.actions||[]).map((a,ai)=>{
        if(a.type==="api") return `<button class="primary smbtn" data-ck="${escapeHtml(c.key)}" data-ai="${ai}">${escapeHtml(a.label)}</button>`;
        if(a.type==="link") return `<button class="ghost smbtn" data-cl="${escapeHtml(a.url)}">${escapeHtml(a.label)}</button>`;
        if(a.type==="cmd") return `<button class="ghost smbtn" data-cc="${escapeHtml(a.cmd)}" title="${escapeHtml(a.cmd)}">${escapeHtml(a.label)}</button>`;
        return "";
      }).join(" ");
      // v1.0.607 清爽化：就绪项就一行（图标 + 名称 + 版本），不再铺一段"它能干什么"的说明；
      // 只有待处理项才展开描述/提示/按钮——需要看的注意力才给视觉空间。
      const quiet = c.status==="ok" && !acts;
      return `
      <div class="comp-item${quiet?" comp-quiet":""}">
        <span style="flex:0 0 auto">${ic[c.status]||"⚪"}</span>
        <div class="comp-main">
          <div><b class="comp-name">${escapeHtml(c.name)}</b>
            <span class="tag ${c.status==="ok"?"def":(c.status==="outdated"?"judge":"dis")}">${st[c.status]||c.status}</span>
            ${c.version?`<span class="comp-ver">${escapeHtml(String(c.version).slice(0,60))}</span>`:""}
          </div>
          ${quiet?"":`<div class="comp-desc" title="${escapeHtml([c.desc,c.hint].filter(Boolean).join(' · '))}">${escapeHtml(c.desc||"")}${c.hint?`<span class="comp-sep">·</span>${escapeHtml(c.hint)}`:""}</div>`}
          ${acts?`<div class="comp-acts">${acts}</div>`:""}
        </div>
      </div>`;}).join("");
    // 事件委托：api/link/cmd 三类动作
    box.querySelectorAll("[data-ck]").forEach(b=>{
      b.onclick=async()=>{
        const comp=d.components.find(x=>x.key===b.dataset.ck);
        const a=comp && (comp.actions||[])[parseInt(b.dataset.ai,10)];
        if(!a) return;
        if(a.confirm && !confirmAsk("确认操作", a.confirm, ()=>_compFix(b, comp, a), "继续")) return;
        if(!a.confirm) _compFix(b, comp, a);
      };
    });
    box.querySelectorAll("[data-cl]").forEach(b=>{
      b.onclick=()=>{ try{ window.pywebview.api.open_external(b.dataset.cl); }catch(e){ window.open(b.dataset.cl, "_blank"); } };
    });
    box.querySelectorAll("[data-cc]").forEach(b=>{
      b.onclick=async()=>{ try{ await navigator.clipboard.writeText(b.dataset.cc); toast("命令已复制——在构建环境终端执行后重新体检","ok",3500); }catch(e){ toast("复制失败，请手动选择命令文本","err"); } };
    });
    // v1.0.606：dsh / OCR 的安装是后台任务——列表里有「安装中」就 3 秒后自己再取一次，
    // 用户不必反复点体检；全部落定就停轮询（切走视图由 cleanupViewPollers 兜底清）
    if((d.components||[]).some(c=>c.status==="installing")){
      __compPoll=setTimeout(()=>{ loadComponents(); }, 3000);
    }
  }catch(e){
    box.innerHTML='<div style="color:var(--err-fg);font-size:var(--fs-sm)">体检失败：'+escapeHtml(fetchErr(e,"组件体检"))+'<br/><span style="color:var(--muted);font-size:var(--fs-xs)">稍后重试；持续失败请到「设置 → 打开日志文件夹」查看 companion.log</span></div>';
  }
  finally{ if(btn) setBtnLoading(btn,false); }
}
async function _compFix(btn, comp, a){
  setBtnLoading(btn,true);
  try{
    const d=await api(a.api,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(a.body||{})});
    if(comp.key==="ocr"){
      toast(d.msg||"OCR 组件安装已开始","ok",3000); ocrInstallPollProgress();
    } else if(d.ok===false){
      toast(d.error||"操作失败","err",4000);
    } else {
      toast(d.msg||"操作完成——点击「立即体检」刷新状态","ok",3500);
    }
    if(comp.key!=="ocr" && !d.already_installed){ setTimeout(()=>loadComponents(), 1500); }
  }catch(e){ toast(fetchErr(e,"组件修复"),"err"); }
  finally{ setBtnLoading(btn,false); }
}
// v1.0.607 一键安装：把「软件内能自动完成」的组件一次跑完。判据用后端给的 actions——
// 只认 `type==="api" && kind==="install"`（OCR 按需下载 / dsh npm 安装 / 打包环境 pip 安装）；
// kind=update（更新到最新版，实验性）与 kind=check（自检）**不参与一键**，Git、Node 这类
// 系统安装器（type=link/cmd）更不混进来假装能装，只在确认文案里如实列出仍需手动的项。
function _instAction(c){
  return ((c && c.actions) || []).find(a => a.type === "api" && a.kind === "install") || null;
}
async function installAllComponents(btn){
  const comps=((window.__compData||{}).components)||[];
  if(!comps.length){ toast("还没有体检结果，先点「重新体检」","warn",3000); return; }
  const _todo=(c)=>c.status!=="ok"&&c.status!=="na"&&_instAction(c);      // v1.0.619：na 不参与一键安装
  const _manual=(c)=>c.status!=="ok"&&c.status!=="na"&&!_instAction(c);
  const todo=comps.filter(_todo);
  const manual=comps.filter(_manual);
  if(!todo.length){
    toast(manual.length ? ("没有可自动安装的组件；还需手动处理：" + manual.map(c=>c.name).join("、"))
                        : "组件都已就绪，无需安装", "warn", 4500);
    return;
  }
  const _oc = comps.find(c=>c.key==="ocr") || {};
  // 体积数字来自后端 size_hint（core/ocr.py 实测口径），前端不再自抄一份——抄过就漏改过。
  const _sz = (_oc.size_hint && todo.some(c=>c.key==="ocr")) ? "。OCR " + _oc.size_hint : "";
  const _pf = todo.some(c=>c.key==="node"||c.key==="git") ? "；Node/Git 装软件内便携版（不写系统 PATH）" : "";
  confirmAsk("一键安装运行组件",
    "将依次自动安装：" + todo.map(c=>c.name).join("、") + _sz + _pf +
    "，每项约 1-10 分钟，进度在本页下方逐条显示，期间请不要关闭软件。" +
    (manual.length ? ("这几项软件装不了、仍需手动：" + manual.map(c=>c.name).join("、") + "。")
                   : ("前置条件也一并自动装好，不需要你先去装任何东西。")),
    ()=>_compInstallRun(btn, todo), "开始安装");
}
async function _compInstallRun(btn, todo){
  const box=$("compProgress");
  if(box){ box.style.display="block"; box.innerHTML='<span class="pp-run">⏳ 准备中…</span>'; }
  if(btn) btn.disabled=true;
  const lines=[]; let okN=0, errN=0;
  for(let i=0;i<todo.length;i++){
    const c=todo[i];
    const a=_instAction(c)||{};
    lines.push('<span class="pp-run">⏳ '+(i+1)+'/'+todo.length+'　' + escapeHtml(c.name) + ' 处理中…</span>');
    if(box) box.innerHTML=lines.join("<br/>");
    let res={};
    try{
      res=await api(a.api||"/api/components/fix",{method:"POST",headers:{"Content-Type":"application/json"},
                                                 body:JSON.stringify(a.body||{key:c.key})});
    }catch(e){ res={ok:false, error:fetchErr(e,c.name)}; }
    lines.pop();
    const good = res.ok!==false && !res.__error__;
    if(good){ okN++; lines.push('✅ ' + escapeHtml(c.name) + '：' + escapeHtml(String(res.msg||res.note||"已开始/已完成").slice(0,70)));
      if(c.key==="ocr" && typeof ocrInstallPollProgress==="function"){ try{ ocrInstallPollProgress(); }catch(e){} } }
    else { errN++; lines.push('<span class="pp-err">❌ ' + escapeHtml(c.name) + '：' +
      escapeHtml(String(res.error||res.__error__||res.reason||"失败").slice(0,90)) + '</span>'); }
    if(box) box.innerHTML=lines.join("<br/>");
  }
  if(box) box.innerHTML=lines.join("<br/>") + '<br/>合计：成功 ' + okN + '，失败 ' + errN + '。列表稍后自动刷新。';
  if(btn) btn.disabled=false;
  toast("一键安装跑完：成功 " + okN + " / 失败 " + errN, errN?"warn":"ok", 4500);
  setTimeout(()=>loadComponents(), 1200);
}
function ocrInstallPollProgress(){
  // 复用 OCR 区域的 2s 状态轮询（_ocrPolling 已有），这里确保启动
  if(typeof __ocrPolling!=="undefined" && !__ocrPolling){ ocrPollTick(); }
}
function renderOcrStatus(d){
  const el=$("opt_ocr_status"); if(!el) return;
  const btn=$("optOcrBtn");
  let txt="";
  if(d.state==="running"){ txt=`${STATUS_ICON.installing} ${d.msg}（${d.progress}%）`; }
  else if(d.state==="ready" || d.installed){ txt=`${STATUS_ICON.ok} ${STATUS_WORD.ok}（能不能真识别以下方自检为准）`; }
  else if(d.state==="failed"){ txt=`${STATUS_ICON.err} 安装失败：`+(d.error||"未知错误"); }
  else { txt=`${STATUS_ICON.off} ${STATUS_WORD.off}（碰到 OCR 事务时将自动联网下载，约 81MB / 占用约 226MB）`; }
  el.classList.toggle("st-run", d.state==="running");
  el.classList.toggle("st-err", d.state==="failed");
  // v1.0.607：状态行从「灰底 input」换成纯文本（视图清爽），同时兼容仍是 INPUT 的老节点；
  // 颜色改走 CSS 类（.st-run/.st-err），不再往元素上写 inline style——inline 色会绕过
  // 深色模式与对比度台子的判据。
  if(el.tagName === "INPUT") el.value = txt; else el.textContent = txt;
  if(btn) btn.disabled = (d.state==="running");
  // 下载中 2s 轮询，结束自动停
  if(d.state==="running" && !__ocrPolling){ __ocrPolling=true; pollOcrStatus(); }
  if(d.state!=="running") __ocrPolling=false;
  if(d.selfcheck) renderOcrSelfCheck(d.selfcheck);   // v1.0.608：状态接口顺带带来自检结论
}
function renderOcrSelfCheck(sc){
  const el=$("opt_ocr_sc"); if(!el) return;
  const secs=(ms)=> ms? (ms/1000).toFixed(1)+" 秒" : "—";
  if(!sc || sc.state==="idle" || sc.state==="running"){
    el.className="comp-ocr-status";
    el.textContent="真实识别自检：未运行（开机 20 秒后自动跑一次，或点「🔁 立即自检」）";
    return;
  }
  if(sc.ok){
    el.className="comp-ocr-status";
    el.textContent="真实识别自检 ✅ 通过（"+secs(sc.ms)+"，读到「"+String(sc.saw||"").replace(/\s+/g," ").slice(0,24)+"」）";
  } else {
    el.className="comp-ocr-status st-err";
    el.textContent="真实识别自检 ❌ 未通过（"+secs(sc.ms)+"）："+String(sc.error||"未知原因");
  }
}
async function ocrSelfCheckNow(btn){
  if(btn){ btn.disabled=true; btn.textContent="自检中…"; }
  try{
    const d=await (await fetch("/api/ocr/selfcheck",{method:"POST"})).json();
    renderOcrSelfCheck(d);
    toast(d.ok ? ("OCR 自检通过（"+((d.ms||0)/1000).toFixed(1)+" 秒）") : ("OCR 自检未通过："+(d.error||"未知原因")), d.ok?"ok":"warn", 4500);
    try{ loadComponents(); }catch(e){}
  }catch(e){ toast("自检请求失败："+(e.message||e),"err"); }
  finally{ if(btn){ btn.disabled=false; btn.textContent="🔁 立即自检"; } }
}
async function pollOcrStatus(){
  try{
    const d=await getJson("/api/ocr/status");
    renderOcrStatus(d);
    if(d.state==="running") setTimeout(pollOcrStatus, 2000);
    else __ocrPolling=false;
  }catch(e){ __ocrPolling=false; }
}
async function loadOcrStatus(){
  try{ renderOcrStatus(await getJson("/api/ocr/status")); }catch(e){}
}
async function ocrInstall(){
  const btn=$("optOcrBtn"); if(btn){ btn.disabled=true; btn.textContent="下载中…"; }
  try{ renderOcrStatus(await api("/api/ocr/install",{method:"POST"})); }catch(e){ toast("触发安装失败","err"); }
  pollOcrStatus();
  if(btn) btn.textContent="立即安装 / 重装";
}
// ---- OCR 组件持续更新（v1.0.308）：检查更新 / 更新到最新（实验性）/ 恢复推荐版本 ----
async function ocrCheckUpdates(btn){
  if(btn){ btn.disabled=true; btn.textContent="检查中…"; }
  const box=$("opt_ocr_updates");
  try{
    const d=await getJson("/api/ocr/updates");
    if(box){
      box.style.display="block";
      if(!d.packages || !d.packages.length){ box.textContent="⚠️ 检查失败：无法访问镜像"; }
      else{
        box.innerHTML = d.packages.map(function(p){
          const cur = p.installed || (p.state==="missing" ? "未安装" : "未知");
          const latest = p.update_available ? `<b style="color:var(--accent-fg)">${p.latest||"?"}</b>` : (p.latest || "—");
          const mark = p.update_available ? "⬆" : "✅";
          return `${mark} ${p.pkg}：${cur} → ${latest}`;
        }).join("<br/>") + `<br/><b>${d.updates_available ? `共 ${d.updates_available} 个组件可更新` : "全部组件已是最新"}</b>`;
      }
    }
  }catch(e){ toast("检查更新失败——确认网络可访问更新源后重试","err"); }
  if(btn){ btn.disabled=false; btn.textContent="检查更新"; }
}
async function ocrUpdate(mode){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  if(mode==="latest"){
    confirmAsk("更新到最新版", "「更新到最新版」为实验性功能：新版本组合未经本软件回归验证，可能出现不兼容。出问题可用「恢复推荐版本」一键回退到验证过的组合。", ()=>{
      _ocrUpdateGo(mode);
    }, "继续更新");
    return;
  }
  _ocrUpdateGo(mode);
}
async function _ocrUpdateGo(mode){
  try{
    const r=await fetch("/api/ocr/update",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:mode})});
    const d=await r.json();
    if(d.error){ toast("更新失败："+d.error,"err"); return; }
    toast(d.msg||"已开始更新","ok");
    pollOcrStatus();
  }catch(e){ toast("更新请求失败——确认网络后重试；也可用「恢复推荐版本」回退","err"); }
}

// ---- 知识库（v1.0.294：课件/题库/制度 三分类，独立于产物库扁平树） ----
// v1.0.477：企微聊天文件静默分析（知识库/企微文件/）
// v1.0.498：openWorkspace/openKnowledge 孤儿入口已删——关闭文件预览与企微文件刷新由 openView 直接管
let __warcPoll = null;
async function loadWecomArchive(){
  const box=$("warcStatus"); if(!box) return;
  try{
    const d=await api("/api/wecom_archive/status");
    const bits=[
      `📂 账号 ${d.accounts.length} 个`,
      `📄 源文件 ${d.source_files}`,
      `✅ 已入库 ${d.archived}`,
      d.needs_ocr?`⏳ 待 OCR ${d.needs_ocr}`:"",
      d.last_scan?`🕐 上次扫描 ${d.last_scan}`:"从未扫描",
    ].filter(Boolean);
    box.innerHTML=bits.map(b=>`<span>${escapeHtml(b)}</span>`).join("");
    $("warcNote").textContent=d.note||"";
    // v1.0.588 合规闸：未授权时该功能一律不运行（daemon 与手动扫描都拦），先要一句明确同意
    const btn = $("warcScanBtn");
    const cbox = $("warcConsent");
    const consented = !!d.consent;
    if(cbox){
      cbox.innerHTML = consented
        ? `<span style="color:var(--brand-fg)">✅ 已授权解析本机企微文件</span>` +
          `<button class="ghost smbtn" style="margin-left:8px" onclick="wecomArchiveConsent(false)">撤回授权</button>`
        : `<span style="color:var(--warn-fg)">⚠️ 需要授权：本功能会把聊天附件<b>正文内容</b>复制进本机知识库；` +
          `只读明文文件，不解析企微加密的消息文本库。</span>` +
          `<button class="primary smbtn" style="margin-left:8px" onclick="wecomArchiveConsent(true)">我已了解，授权并扫描</button>`;
    }
    if(btn){
      btn.disabled = !consented || !!d.running;
      btn.textContent = !consented ? "🔒 未授权" : (d.running ? "⏳ 分析中…" : "🔍 立即扫描入库");
    }
    if(d.running){
      if(!__warcPoll) __warcPoll=setTimeout(()=>{ __warcPoll=null; loadWecomArchive(); }, 3000);
    } else if(__warcPoll){ clearTimeout(__warcPoll); __warcPoll=null; }
  }catch(e){
    box.innerHTML='<span style="color:var(--err-fg)">状态获取失败：'+escapeHtml(fetchErr(e,"企微文件分析"))+"</span>";
  }
}
async function wecomArchiveScan(){
  const btn=$("warcScanBtn"); if(btn){ btn.disabled=true; btn.textContent="⏳ 分析中…"; }
  try{
    await api("/api/wecom_archive/scan",{method:"POST"});
    toast("静默分析已启动（后台执行，完成后自动刷新）","ok");
    setTimeout(loadWecomArchive, 2500);
  }catch(e){ toast(fetchErr(e,"启动分析"),"err"); if(btn){ btn.disabled=false; btn.textContent="🔍 立即扫描入库"; } }
}
// v1.0.588 合规闸：授权/撤回「解析本机企微聊天文件」
async function wecomArchiveConsent(value){
  try{
    await api("/api/wecom_archive/consent",{method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({consent: !!value})});
    toast(value ? "已授权，正在后台扫描" : "已撤回授权——今后不再读取企微文件（已入库内容保留）", "ok", 2600);
    if(value){ api("/api/wecom_archive/scan",{method:"POST"}).catch(()=>{}); }
    setTimeout(loadWecomArchive, value ? 1500 : 300);
  }catch(e){ toast(fetchErr(e,"授权操作"),"err"); }
}
async function loadKnowledge(){
  loadKbOps();
  const tree=$("kbTree"); showLoading(tree,"加载知识库中…");
  try{
    const d=await getJson("/api/knowledge");
    if(d.kb_root) $("kbRoot").textContent="保存位置："+d.kb_root;
    if(!d.categories || !d.categories.length){ showEmpty(tree,"📚 知识库为空<br/>课程课件 OCR 与答题记录会归档到这里"); return; }
    const icons={课件:"📖",题库:"📝",制度:"📜",消化:"✨"};
    const html=d.categories.map(g=>{
      const files=g.files.length? g.files.map(f=>{
        const kb=(f.size/1024).toFixed(1);
        const enc=encodeURIComponent(f.name);
        // v1.0.361 审查修复（P17）：同产物库——文件名转义 + onclick 引号加固
        const safeName=escapeHtml(f.name);
        const encQ=enc.replace(/'/g,"%27");
        return `<div class="wrow"><span class="wname" onclick="viewKbFile('${g.cat}','${encQ}')">${safeName}</span>`
          + `<span class="wmeta">${kb}KB · ${f.mtime}</span></div>`;
      }).join("") : `<div class="wmeta" style="padding:4px 0">（空）</div>`;
      return `<div class="wgroup"><h4>${icons[g.cat]||"📁"} ${g.cat}（${g.files.length}）</h4>${files}</div>`;
    }).join("");
    $("kbTree").innerHTML=html;
  }catch(e){ showEmpty(tree,"⚠️ 加载失败<br/>"+escapeHtml(e.message||"网络错误")); }
}
async function viewKbFile(cat, name){
  try{
    const d=await getJson("/api/knowledge/file?cat="+encodeURIComponent(cat)+"&name="+name);
    $("kbFileName").textContent=d.name;
    $("kbFileContent").textContent=d.content;
    $("kbTree").style.display="none"; $("kbFileView").style.display="block";
  }catch(e){ toast(e.message||"读取失败","err"); }
}
function closeKbFile(){ $("kbTree").style.display="block"; $("kbFileView").style.display="none"; }
async function openKbFolder(){
  try{ await postJson("/api/knowledge/open",{}); }catch(e){ toast(e.message||"打开失败","err"); }
}

// ---- 知识库后台维护（v1.0.301：空闲自动整理 + 每日 AI 消化，daemon 60s 轮询） ----
let kbOpsPoll=null;
function _kbOpsFmtTime(ts){
  if(!ts) return "尚未";
  return new Date(ts*1000).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
}
var __kbWasBusy=false;
async function loadKbOps(){
  const el=$("kbOpsStatus"); if(!el) return;
  let d;
  try{ d=await (await fetch("/api/knowledge/maintenance/status")).json(); }
  catch(e){ el.textContent="⚠️ 维护状态不可用（后端未启动新版？）"; return; }
  if(!d.ok){ el.textContent="⚠️ "+(d.error||"获取状态失败"); return; }
  // v1.0.479：执行进度（当前动作+秒数）与上次结果——手动整理/消化不再"看不到进度"
  if(d.busy && d.action){
    const act=d.action==="organize"?"整理":"消化";
    el.innerHTML="⏳ 正在"+act+"…（已 <b>"+(d.elapsed||0)+"</b>s"+(d.action==="digest"?"，LLM 生成中":"")+"）";
  } else {
    el.textContent=(d.running?"🟢 自动维护 开":"⚪ 自动维护 未启动")+" · "+(d.idle?"空闲中":"忙碌中");
  }
  const det=$("kbOpsDetail");
  if(det){
    const lr=d.last_result;
    det.textContent="上次整理 "+_kbOpsFmtTime(d.last_organize)
      +" · 今日消化 "+(d.today_digest?(d.today_digest_empty?"无需（24h 无新增）":"已完成"):"未完成")
      +(d.latest_digest&&!d.today_digest_empty?("（最近 "+d.latest_digest+"）"):"")
      +(d.digest_fails_today?(" · 今日消化失败 "+d.digest_fails_today):"")
      +(lr?(" · 上次手动"+(lr.action==="organize"?"整理":"消化")+"："+(lr.ok?("✅ "+(lr.detail||"完成")+" "+lr.ts):("❌ "+(lr.detail||"失败")))):"")
      +(d.busy?" · 维护任务进行中":"");
  }
  // v1.0.479：自动触发条件透出——"自动维护开了为什么没执行"的答案（等空闲 10 分钟）
  const hint=$("kbOpsHint");
  if(hint && !d.busy){
    hint.textContent=d.idle?"":"自动任务等你停止使用软件 10 分钟后执行（避免抢占 agent）";
  }
  // busy→idle 跳变：弹结果通知（手动任务完成/失败即时可见）
  if(__kbWasBusy && !d.busy && d.last_result){
    const lr=d.last_result;
    if(lr.manual && lr.ok) toast((lr.action==="organize"?"整理":"消化")+"完成 ✅ "+(lr.detail||""),"ok",3500);
    else if(lr.manual && !lr.ok) toast((lr.action==="organize"?"整理":"消化")+"失败："+(lr.detail||"未知原因"),"err",5000);
  }
  __kbWasBusy=!!d.busy;
  if(d.busy){ if(!kbOpsPoll) kbOpsPoll=setInterval(loadKbOps,2000); }
  else if(kbOpsPoll){ clearInterval(kbOpsPoll); kbOpsPoll=null; }
}
async function kbRun(action){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  if(action==="organize"){
    confirmAsk("立即整理", "生成 INDEX 索引 + 重复文件告警。不会移动/删除/重命名任何文件。", ()=>{ _kbRunGo(action); }, "开始整理");
  } else {
    confirmAsk("立即消化", "AI 将读取最近 24h 更新的文件生成今日消化。若今日已有消化文件将被覆盖（1-2 分钟）。", ()=>{ _kbRunGo(action); }, "开始消化");
  }
}
async function _kbRunGo(action){
  try{
    const r=await fetch("/api/knowledge/maintenance/run",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({action:action,force:action==="digest"})});
    const d=await r.json();
    if(!d.ok) toast(d.error||"触发失败","err");
    else{
      // v1.0.479：状态条立即切执行中 + 高频轮询（完成后自动弹结果通知）
      const el=$("kbOpsStatus");
      if(el) el.innerHTML="⏳ 正在"+(action==="organize"?"整理":"消化")+"…（已 <b>0</b>s）";
      __kbWasBusy=true;
      if(!kbOpsPoll) kbOpsPoll=setInterval(loadKbOps,2000);
      loadKbOps();
      return;
    }
  }catch(e){ toast("触发请求失败："+(e.message||"网络错误"),"err"); }
  loadKbOps();
}

// ---- 学习中心（内置跑课引擎，v1.0.276 起不再依赖外部 study_driver.py） ----
// v1.0.361 审查修复（P17）：离开视图时清理轮询定时器（原只建不拆，切页后每 3s 空打后端）
// v1.0.471（UI#9）：置位 __repairPollAborted——体检 progress 轮询循环随之终止
function cleanupViewPollers(){
  if(studyPollTimer){ clearInterval(studyPollTimer); studyPollTimer=null; }
  if(kbOpsPoll){ clearInterval(kbOpsPoll); kbOpsPoll=null; }
  // v1.0.606：运行组件的「安装中」自动刷新——切走本页要停，否则留个 3s 轮询在后台打接口
  if(__compPoll){ clearTimeout(__compPoll); __compPoll=null; }
  window.__repairPollAborted = true;
}
let studyPollTimer=null;
function _studyStateLabel(s){
  return {running:"运行中",waiting_login:"等待登录",waiting_window:"等待学习窗口",cooldown:"冷却重试中",idle:"未运行"}[s]||s||"未运行";
}
async function refreshStudy(){
  let d;
  try{ d=await getJson("/api/study/status"); }
  catch(e){ const c=$("studyStateChip"); if(c){ c.textContent="后端离线"; c.className="tag dis"; } return; }
  const chip=$("studyStateChip");
  if(chip){
    chip.textContent=_studyStateLabel(d.state);
    chip.style.background=""; chip.style.color="";
    if(d.running){ chip.className="tag def"; }
    else if(d.state==="idle"){ chip.className="tag"; }
    else { chip.className="tag wrn"; }              // v1.0.616：警示档改令牌，不再内联 hex
  }
  const cur=$("studyCurrent"); if(cur) cur.textContent=d.current||"—";
  const cn=$("studyCourseN"); if(cn) cn.textContent=(d.course_n||0)+" 门";
  const ul=$("studyUnlearned"); if(ul) ul.textContent=(typeof d.unlearned==="number")?d.unlearned:"—";
  const sk=$("studySkips"); if(sk) sk.textContent=(d.skips||0);
  const dd=$("studyDead"); if(dd) dd.textContent=(d.dead||0);
  const el=$("studyElapsed"); if(el) el.textContent=(d.elapsed_min||0)+" min";
  const sb=$("studyStartBtn"), stb=$("studyStopBtn");
  if(sb) sb.disabled=!!d.running;
  if(stb) stb.disabled=!d.running;
  const hint=$("studyHint"); if(hint) hint.textContent=d.last_result?("最近结果："+d.last_result):"";
  const logs=$("studyLogs"); if(logs) logs.textContent=(d.logs&&d.logs.length)?d.logs.join("\n"):"（无日志）";
}
function loadStudy(){
  refreshStudy();
  if(!studyPollTimer){ studyPollTimer=setInterval(refreshStudy,3000); }
}
async function studyStart(){
  const btn=$("studyStartBtn"); if(btn) btn.disabled=true;
  try{
    const r=await fetch("/api/study/start",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    const d=await r.json();
    if(!d.ok) toast((d.error||"启动失败")+"——请先确认课程与浏览器路径已配置好","err",5000); else toast("跑课引擎已启动","ok");
  }catch(e){ toast("启动请求失败："+(e.message||"网络错误")+"——请确认课程与浏览器路径已配置","err",5000); }
  await refreshStudy();
}
async function studyStop(){
  try{
    const r=await fetch("/api/study/stop",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    const d=await r.json();
    if(!d.ok) toast(d.error||"停止失败","err"); else toast("已请求停止","ok");
  }catch(e){ toast("停止请求失败："+(e.message||"网络错误"),"err"); }
  await refreshStudy();
}

function toggleLivePane(){
  const card=$("liveCard"); if(!card) return;
  const collapsed=card.classList.toggle("collapsed");
  // v1.0.561：同步 .folded 状态类——原折叠样式依赖 .work-grid:has(.livecard.collapsed)
  // + CSS 嵌套，旧版 WebView2 不支持时整块失效 → 折叠条被 stretch 拉满全高、
  // 下方大片空白（用户截图实锤）。JS 切类零兼容问题。
  const grid=card.closest(".work-grid"); if(grid){ grid.classList.toggle("folded", collapsed); }
  const btn=$("liveFoldBtn");
  if(btn){ setFold(btn, !collapsed); btn.title=collapsed?"展开窗口栏":"折叠窗口栏"; }   // v1.0.635：统一折叠原语
  lsSet("liveFold", collapsed?"1":"0");
  if(!collapsed){ setTimeout(()=>{ const s=card.querySelector(".scroll"); if(s){ s.scrollTop=s.scrollHeight; } },60); }
}
function setOpTarget(txt, ok){
  const el=$("opTargetHint"); if(!el) return;
  el.textContent=txt; el.className="sub op-target "+(ok?"ok":"none")+(el.dataset.min==="1"?" min":"");
}
// v1.0.562：运行状态条可收起——折叠后压成细线（agent 状态更新保持收起不打扰），再点还原
function toggleOpTargetMin(){
  const el=$("opTargetHint"); if(!el) return;
  const min=el.dataset.min==="1"?"0":"1";
  el.dataset.min=min;
  el.classList.toggle("min", min==="1");
  lsSet("opTargetMin", min);
}


// ---- 安全中心（v1.0.320）：配置/审计 ----
async function loadSecurity(){
  try{
    const d=await getJson("/api/security/config");
    const s=d.security||{};
    $("sec_mode").value=s.command_mode||"block";
    const _pl=$("sec_perm_level"); if(_pl) _pl.value=s.permission_level||"auto";   // v1.0.590
    $("sec_del_limit").value=s.delete_confirm_limit??10;
    $("sec_whitelist").value=(s.cmd_whitelist||[]).join("\n");
    $("sec_blacklist").value=(s.cmd_blacklist||[]).join("\n");
    $("sec_paths").value=(s.protected_paths||[]).join("\n");
    $("sec_backup").checked=s.auto_backup!==false;
    $("sec_backup_mb").value=s.backup_limit_mb??500;
    const list=$("auditList"); if(list){
      const items=d.audit||[];
      list.innerHTML=items.length?items.map(a=>
        `<div>${a.allowed?"<b style='color:var(--brand-fg)'>放行</b>":"<b style='color:var(--err-fg)'>拦截</b>"} ${escapeHtml(a.tool)}：${escapeHtml((a.summary||"").slice(0,70))} <span class="dim">${escapeHtml(a.ts)}</span></div>`
      ).join(""):"暂无审计记录——agent 执行命令/删除/写文件等敏感操作后，这里会列出每一条放行与拦截明细。";
    }
  }catch(e){}
}
async function saveSecurity(btn){
  setBtnLoading(btn,true);
  try{
    const sec={
      command_mode:$("sec_mode").value,
      permission_level:($("sec_perm_level")||{}).value||"auto",   // v1.0.590 全局默认权限档
      delete_confirm_limit:parseInt($("sec_del_limit").value)||10,
      cmd_whitelist:$("sec_whitelist").value.split("\n").map(x=>x.trim()).filter(Boolean),
      cmd_blacklist:$("sec_blacklist").value.split("\n").map(x=>x.trim()).filter(Boolean),
      protected_paths:$("sec_paths").value.split("\n").map(x=>x.trim()).filter(Boolean),
      auto_backup:$("sec_backup").checked,
      backup_limit_mb:parseInt($("sec_backup_mb").value)||500,
    };
    await apiToast("/api/security/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({security:sec})},"保存安全设置");
    toast("安全设置已保存（立即生效）","ok");
    loadSecurity();
    // v1.0.590：全局权限档改了要让 composer 上的权限下拉立刻反映（未单独选过档的任务）
    if(typeof loadPermDefault==="function") loadPermDefault();
  }catch(_){}
  finally{ setBtnLoading(btn,false); }
}
async function exportAudit(){
  try{
    const d=await apiToast("/api/security/audit/export",{method:"POST"},"导出审计");
    toast("已导出："+d.path,"ok",3000);
  }catch(_){}
}
async function clearAudit(){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk + 错误处理
  confirmAsk("清空审计", "清空全部安全审计记录？", async ()=>{
    try{
      await apiToast("/api/security/audit/clear",{method:"POST"},"清空审计");
      toast("审计已清空","ok"); loadSecurity();
    }catch(_){}
  }, "清空");
}

// ===== v1.0.614（审查 ㉙㉚㉛）报销个人信息 · dsh 凭据 · 后台自动任务开关 =====
// 这三组值此前只存在于 config.json，界面无处可填，而快捷入口的提示词写着「个人信息在
// 设置里」；凭据只写不回显（后端 GET 只给「有没有 + 尾号」），留空保存＝保留原值。
// _persLoaded：没成功读到当前值就不许保存——输入框空着写回 config 等于把真实姓名/银行
// 账号清掉（GET 失败但 POST 成功是可能发生的，例如上一次读的时候后端刚重启）。
let _persLoaded=false;
async function postPersonal(body,failLabel){
  const r=await fetch("/api/personal/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d=await r.json().catch(()=>({}));
  if(!r.ok||!d.ok) throw new Error(d.error||(failLabel+"失败（HTTP "+r.status+"）"));
  return d;
}
// v1.0.650（F4）：任务软预算四项。键名与后端 core.config_store.AGENT_SOFT_BUDGET_DEFAULTS
// 一一对应，输入框 id 恒为 "bg_"+键名——名单只在这里写一次，读/存/恢复默认三处共用。
const SOFT_BUDGET_KEYS = ["soft_budget_tokens", "soft_budget_minutes",
                          "soft_budget_tokens_per_item", "soft_budget_minutes_per_item"];
const SOFT_BUDGET_DEFAULTS = { soft_budget_tokens: 400000, soft_budget_minutes: 120,
                               soft_budget_tokens_per_item: 200000, soft_budget_minutes_per_item: 15 };
function _budgetNum(k){
  const v = parseInt(($("bg_" + k) || {}).value, 10);
  return isNaN(v) ? 0 : Math.max(0, v);       // 0 是合法档（=该项关闭），不能回落成默认
}
function resetBudgetForm(btn){
  SOFT_BUDGET_KEYS.forEach(k=>{ const el=$("bg_"+k); if(el) el.value = SOFT_BUDGET_DEFAULTS[k]; });
  toast("输入框已恢复出厂默认，尚未保存——点「保存任务预算」才生效", "info", 4200);
}
async function saveBudget(btn){
  if(!_persLoaded){ toast("任务预算还没加载成功，先别保存——现在保存会把实际生效的额度写成界面默认值", "err"); return; }
  setBtnLoading(btn,true);
  try{
    const budget={};
    SOFT_BUDGET_KEYS.forEach(k=>{ budget[k]=_budgetNum(k); });
    await postPersonal({ budget },"保存任务预算");
    toast("任务预算已保存（下一个任务起生效，正在跑的这一轮沿用开始时的额度）","ok",3600);
    loadPersonal();
  }catch(e){ toast(fetchErr(e,"保存任务预算"),"err"); }
  finally{ setBtnLoading(btn,false); }
}
async function loadPersonal(){
  const set=(id,v)=>{ const el=$(id); if(el) el.value=(v==null?"":v); };
  const chk=(id,v)=>{ const el=$(id); if(el) el.checked=!!v; };
  _persLoaded=false;
  try{
    const d=await getJson("/api/personal/config");
    if(!d||!d.ok) throw new Error((d&&d.error)||"接口没返回配置");
    const r=d.reimburse||{}, k=d.dsh||{}, b=d.background||{};
    set("pii_name",r.name); set("pii_account",r.bank_account); set("pii_bank",r.bank_name);
    chk("pii_mask",r.mask_for_llm);
    set("dsh_base_url",k.base_url); set("dsh_cwd",k.cwd); set("dsh_extra_args",k.extra_args);
    const keyEl=$("dsh_api_key"); if(keyEl) keyEl.value="";     // 永远不回灌密文/明文
    const has=!!k.api_key_set;
    const ck=$("dshClearKey"); if(ck) ck.style.display=has?"":"none";
    const st=$("personalState");
    let note=has?("dsh 凭据已设置 · 尾号 "+(k.api_key_tail||"")):"dsh 凭据未设置（可选）";
    // DPAPI 绑定 Windows 用户+机器：配置从别的机器/账户搬过来时那几项必然解不开。
    // 后端此时下发空值（密文不进 DOM），这里必须说清"留空保存不会清掉它"，
    // 否则用户以为已经清空、其实旧的不可读密文还躺在 config 里。
    const enc=r.encrypted_fields||[];
    if(enc.length) note="⚠ "+enc.map(f=>({name:"姓名",bank_account:"银行账号",bank_name:"开户行"}[f]||f)).join("、")
      +" 是加密存储但本机解不开（配置来自别的电脑或 Windows 账户）——留空保存不会清掉它，填新值即覆盖。"+note;
    else if(k.api_key_unreadable) note="⚠ dsh 凭据是解不开的密文（同上一句），填新值即覆盖。"+note;
    if(st) st.textContent=note;
    chk("bg_digests_daily",b.digests_daily); set("bg_digests_time",b.digests_time||"18:30");
    chk("bg_kb_organize",b.kb_organize); chk("bg_kb_digest",b.kb_digest); chk("bg_todo_watch",b.todo_watch);
    set("bg_todo_interval", b.todo_interval || 10);
    const bu=d.budget||{};
    SOFT_BUDGET_KEYS.forEach(k=>set("bg_"+k, bu[k]));
    _persLoaded=true;
  }catch(e){
    const st=$("personalState");
    if(st) st.textContent="⚠ 没读到当前配置，先别点保存（否则会把已有的姓名/银行账号存成空）";
  }
}
async function savePersonal(btn){
  if(!_persLoaded){ toast("个人信息还没加载成功，先别保存——现在保存会把已有的姓名/银行账号写成空值","err"); return; }
  setBtnLoading(btn,true);
  try{
    const body={ reimburse:{ name:$("pii_name").value.trim(), bank_account:$("pii_account").value.trim(),
                             bank_name:$("pii_bank").value.trim(), mask_for_llm:$("pii_mask").checked },
                 dsh:{ base_url:$("dsh_base_url").value.trim(),
                       cwd:($("dsh_cwd")||{}).value||"", extra_args:($("dsh_extra_args")||{}).value||"" } };
    const nk=$("dsh_api_key").value.trim();
    if(nk) body.dsh.api_key=nk;                                   // 留空＝不动原凭据
    await postPersonal(body,"保存个人信息");
    toast("个人信息已保存（姓名/银行账号/凭据按 DPAPI 加密落盘）","ok");
    loadPersonal();
  }catch(e){ toast(fetchErr(e,"保存个人信息"),"err"); }
  finally{ setBtnLoading(btn,false); }
}
async function saveBackground(btn){
  if(!_persLoaded){ toast("后台开关还没加载成功，先别保存——现在保存会把三项默认开关全部关掉","err"); return; }
  setBtnLoading(btn,true);
  try{
    await postPersonal({ background:{ digests_daily:$("bg_digests_daily").checked,
                                      digests_time:$("bg_digests_time").value||"18:30",
                                      kb_organize:$("bg_kb_organize").checked,
                                      kb_digest:$("bg_kb_digest").checked,
                                      todo_watch:$("bg_todo_watch").checked,
                                      todo_interval:parseInt(($("bg_todo_interval")||{}).value,10)||10 } },"保存后台开关");
    toast("后台开关已保存（立即生效，无需重启）","ok");
    loadPersonal();
  }catch(e){ toast(fetchErr(e,"保存后台开关"),"err"); }
  finally{ setBtnLoading(btn,false); }
}
function clearDshKey(btn){
  confirmAsk("清除 dsh 凭据","清除后「让 AI 操作这台电脑」要重新填凭据才能用。确定清除？",async ()=>{
    setBtnLoading(btn,true);
    try{
      await postPersonal({ dsh:{ clear_key:true } },"清除 dsh 凭据");
      toast("dsh 凭据已清除","ok"); loadPersonal();
    }catch(e){ toast(fetchErr(e,"清除 dsh 凭据"),"err"); }
    finally{ setBtnLoading(btn,false); }
  },"清除");
}

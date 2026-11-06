// 13-dashboard.js — 任务运行看板（v1.0.523-2）
// 工作台顶部轻量四格：今日 Token / 定时任务 / 会话 / 知识库。
// 数据来自 /api/dashboard（后端 60s 缓存）；打开工作台时刷新，不做实时轮询。

function fmtDashNum(n){ return fmtNum(n); }   // 与 07 的 fmtTok 同款实现收敛到 01-core 的 fmtNum，保留旧函数名

async function loadDashboard(){
  const strip = $("dashStrip");
  if(!strip) return;
  try{
    const d = await getJson("/api/dashboard");
    const cells = [];
    // ① 今日 Token
    const u = d.usage_today || {};
    if(!u.error){
      cells.push({t:"今日 Token", v:fmtDashNum(u.total||0),
        s:`${u.requests||0} 次调用 · 输出 ${fmtDashNum(u.completion||0)}`,
        tip:"点击查看用量趋势（模型页）", go:"models"});
    }
    // ② 定时任务
    const sc = d.schedules || {};
    if(!sc.error && sc.total !== undefined){
      const failTip = (sc.items||[]).filter(x=>/异常|失败/.test(x.last_result||""))
        .slice(0,3).map(x=>`${x.name}：${x.last_result}`).join("\n") || "近期无失败";
      cells.push({t:"定时任务", v:`${sc.enabled}/${sc.total}`,
        s:`今日已跑 ${sc.ran_today||0} · 异常 ${sc.failed_last||0}`,
        tip:`启用/总数 · 点击进入定时任务管理\n${failTip}`, go:"schedules", warn:(sc.failed_last||0)>0});
    }
    // ③ 会话（任务）
    const cv = d.conversations || {};
    if(!cv.error){
      cells.push({t:"任务会话", v:fmtDashNum(cv.total||0),
        s:`今日新建 ${cv.today||0}`, tip:"累计任务会话数 / 今日新建", go:null});
    }
    // ④ 知识库
    const kb = d.kb || {};
    if(!kb.error){
      const by = Object.entries(kb.by_dir||{}).sort((a,b)=>b[1]-a[1]).slice(0,3)
        .map(([k,v])=>`${k} ${v}`).join(" · ");
      cells.push({t:"知识库", v:fmtDashNum(kb.total||0),
        s:by || "暂无文件", tip:"可检索文件数（按分类）", go:"knowledge"});
    }
    // ⑤ 后台批审进度（运行中显示；完成后保留 3 分钟的结论格）
    try{
      const ap = await getJson("/api/audit/progress");
      // v1.0.599（用户反馈"说跑完了但界面没有结果"）：running→finished 的那一次翻转，
      // 把服务端刚落地的结果消息拉回发起它的对话。Toast 会被系统勿扰静默吞掉，
      // 而看板格在旧逻辑里跑完就消失，用户看到的最后一句还停在"预计耗时 3-6 分钟"。
      if(__auditWasRunning && !ap.running && ap.finished){
        try{ if(ap.pane && typeof reloadPaneFromStore === "function") reloadPaneFromStore(ap.pane); }catch(e){}
        __auditDoneAt = Date.now();
      }
      __auditWasRunning = !!ap.running;
      if(ap.running){
        const pct = ap.total ? Math.round(ap.done/ap.total*100) : 0;
        const aborting = ap.abort_requested;
        cells.push({t: aborting ? `批审中止中… ${pct}%` : `批审进行中 ${pct}%`,
          v:`${ap.done}/${ap.total||"?"}`,
          s:`✅${ap.green||0} ❌${ap.red||0} ⏳${ap.pending||0} · ${ap.current||""}`,
          tip: aborting ? "正在等当前单据跑完即停止（已处理的出报告，剩余可续跑）"
                        : "后台批审进行中——可继续对话，完成后结果会写回发起的对话", go:null, cls:"dash-run",
          abortable: !aborting});
      } else if (ap.finished && ap.ok && (Date.now() - __auditDoneAt) < 180000){
        cells.push({t:"批审完成", v:`${ap.done||0}/${ap.total||0}`,
          s:`✅${ap.green||0} ❌${ap.red||0} ⏳${ap.pending||0}`,
          tip:(ap.summary||"") + (ap.report_path?("\n报告：产物库/"+ap.report_path):""),
          go:"work", cls:"dash-ok"});
      } else if (ap.finished && !ap.ok && (Date.now() - __auditDoneAt) < 180000){
        cells.push({t:"批审失败", v:"⚠", s:(ap.summary||"").slice(0,40),
          tip:ap.summary||"", go:"work", cls:"dash-warn"});
      } else if (ap.resumable) {
        // v1.0.540：有断点 → 显示「续跑剩余」（上次中止/超时的批审从断点接续）
        cells.push({t:"批审待续跑", v:"↻",
          s:"上次未跑完，点击续跑剩余单据",
          tip:"从断点接续（已完成项自动跳过）", go:null, cls:"dash-warn", resume:true});
      }
      if(ap.running) scheduleAuditPoll();
    }catch(e){ /* 进度接口失败不影响看板 */ }

    strip.innerHTML = "";
    cells.forEach(c=>{
      const el = document.createElement("div");
      // v1.0.594：样式全部交给 .dash-cell 类（看板条也从 work-grid 首行搬进了顶部状态行）。
      // 原来这里用 el.style.cssText 内联一整套，还带 var(--border,#e4e8ef) 这类回退色——
      // 既绕开主题令牌，又让"搬位置只改 CSS"变成"必须回来改 JS"。
      el.className = "dash-cell" + (c.warn ? " dash-warn" : "") + (c.cls ? " "+c.cls : "")
                   + (c.go ? " dash-jump" : "");
      el.title = c.tip || "";
      // v1.0.614：看板的标题/数值/副文本全部来自后端（含单据名、任务名），必须转义——
      // 名字里一个 "<" 就能吞掉后面整块 DOM（此前是原样拼进 innerHTML）。
      el.innerHTML = `<div class="dc-t">${escapeHtml(c.t||"")}</div>`+
        `<div class="dc-v">${escapeHtml(String(c.v==null?"":c.v))}</div>`+
        `<div class="dc-s">${escapeHtml(c.s||"")}</div>`;
      if(c.go) el.onclick = ()=>{ try{ openView(c.go); }catch(e){} };
      // v1.0.537：批审中止按钮（运行中可停止——当前单据跑完即停，断点可续跑）
      if(c.abortable){
        const ab = document.createElement("button");
        ab.className = "ghost smbtn"; ab.textContent = "⏹ 停止";
        ab.title = "中止批审：当前单据跑完后停止，已处理结果自动出报告，剩余可稍后续跑";
        ab.style.marginLeft = "8px";
        ab.onclick = async (ev)=>{
          ev.stopPropagation();
          ab.disabled = true; ab.textContent = "停止中…";
          try{ await api("/api/audit/abort", {method:"POST"}); }catch(e){}
          loadDashboard();
        };
        el.appendChild(ab);
      }
      // v1.0.540：断点续跑按钮
      if(c.resume){
        el.onclick = async ()=>{
          el.style.opacity = ".5";
          try{ await api("/api/audit/resume", {method:"POST"}); }catch(e){}
          loadDashboard(); scheduleAuditPoll();
        };
      }
      // v1.0.540：批审历史趋势（hover 显示最近 5 轮判定摘要）
      if(d.audit_history && d.audit_history.length && c.t && (c.t.indexOf("批审") === 0 || c.t === "待续跑")){
        const hist = d.audit_history.map(h =>
          `${h.ts||"?"}：✅${h.green} ❌${h.red} ⏳${h.pending} / 共${h.total}`).join("\n");
        el.title = (el.title ? el.title + "\n——\n" : "") + "最近批审：\n" + hist;
      }
      strip.appendChild(el);
    });
    strip.style.display = cells.length ? "flex" : "none";
  }catch(e){ /* 看板失败不影响工作台 */ }
}

// 批审运行期间 5s 轮询进度（结束后自动停，回到正常低频）
let __auditPollTimer = null;
// v1.0.599：批审 running→finished 翻转检测 + 结论格保留窗口（3 分钟）
let __auditWasRunning = false;
let __auditDoneAt = 0;
function scheduleAuditPoll(){
  if(__auditPollTimer) return;
  __auditPollTimer = setInterval(async ()=>{
    try{
      const ap = await getJson("/api/audit/progress");
      if(!ap.running){
        clearInterval(__auditPollTimer); __auditPollTimer = null;
      }
      loadDashboard();
    }catch(e){}
  }, 5000);
}

// 打开工作台时刷新一次（12-init 首次 + 每次 openView('work')）
if(typeof __dashHooked === "undefined"){
  var __dashHooked = true;
  const _origOpenView = window.openView;
  if(typeof _origOpenView === "function"){
    window.openView = function(name){
      const r = _origOpenView.apply(this, arguments);
      if(name === "work") { try{ loadDashboard(); }catch(e){} }
      return r;
    };
  }
}

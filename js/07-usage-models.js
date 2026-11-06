async function scanModels(){
  const panel=$("scanPanel"), box=$("scanResults");
  panel.style.display="block"; box.innerHTML='<div style="color:var(--muted)">正在探测本机 LM Studio / Ollama…</div>';
  $("scanHint").textContent=""; $("scanImportBtn").style.display="none";
  let d;
  try{ d = await (await fetch("/api/models/scan",{method:"POST"})).json(); }
  catch(e){ box.innerHTML=`<div style="color:var(--err-fg)">扫描失败：${escapeHtml(fetchErr(e,"扫描"))}<br/><span style="color:var(--muted);font-size:var(--fs-sm)">确认 LM Studio（端口 1234）/ Ollama（端口 11434）已启动后重试；组件缺失看「设置 → 运行组件」</span></div>`; return; }
  if (!d.ok){ box.innerHTML=`<div style="color:var(--err-fg)">扫描失败：${escapeHtml(d.error||"未知错误")}<br/><span style="color:var(--muted);font-size:var(--fs-sm)">确认本地模型服务已启动后重试</span></div>`; return; }
  const found = (d.results||[]).filter(r=>r.reachable);
  if (!found.length){
    const hints = (d.results||[]).filter(r=>!r.reachable && r.error)
      .map(r=>`${escapeHtml(r.label)}（${escapeHtml(r.base_url)}）：${escapeHtml(r.error)}`);
    // v1.0.471（UI#12）：硬编码浅色（#888/#c77/#e2e2e2/#ccc）→ CSS 变量，暗色模式不再刺眼
    box.innerHTML = hints.length
      ? hints.map(h=>`<div style="color:var(--warn-fg); font-size:var(--fs-md); margin-bottom:4px">• ${h}</div>`).join("") + '<div style="color:var(--muted); margin-top:4px">未发现可直接导入的本地模型服务。请确认已启动 LM Studio（默认端口 1234，若已开启 API Key 需手动添加）或 Ollama（默认端口 11434）后重试。</div>'
      : '<div style="color:var(--muted)">未发现本地模型服务。请确认已启动 LM Studio（默认端口 1234）或 Ollama（默认端口 11434）后重试。</div>';
    return;
  }
  $("scanHint").textContent = `共发现 ${found.length} 个服务（耗时 ${d.elapsed||0}s），勾选后点「导入选中模型」`;
  box.innerHTML = found.map((p,pi)=>`
    <div style="border:1px solid var(--border); border-radius:var(--r-md); padding:10px; margin-bottom:8px">
      <div class="row" style="align-items:center; gap:8px">
        <b>${escapeHtml(p.label)}</b>
        <span style="color:var(--muted); font-size:var(--fs-sm)">${escapeHtml(p.base_url)}</span>
        <button class="ghost smbtn" style="margin-left:auto" onclick="checkAllScan(${pi})">全选</button>
      </div>
      <div style="margin-top:8px; display:flex; flex-wrap:wrap; gap:6px" id="scanRow${pi}">
        ${(p.models||[]).map(m=>`
          <label style="display:flex; align-items:center; gap:4px; border:1px solid var(--border); border-radius:var(--r-lg); padding:3px 10px; cursor:pointer; font-size:var(--fs-md)">
            <input type="checkbox" value="${escapeHtml(m.id)}" data-base="${escapeHtml(p.base_url)}" data-label="${escapeHtml(p.label)}" checked/>
            ${escapeHtml(m.name)}
          </label>`).join("")}
      </div>
    </div>`).join("");
  $("scanImportBtn").style.display="inline-block";
}
function checkAllScan(pi){
  const row=$("scanRow"+pi); if(!row) return;
  const cbs=row.querySelectorAll("input[type=checkbox]");
  const allChecked=[...cbs].every(cb=>cb.checked);
  cbs.forEach(cb=>cb.checked=!allChecked);
  const btn=row.parentElement.querySelector('button[onclick^="checkAllScan"]');
  if(btn) btn.textContent = allChecked ? "全选" : "取消";
}
function checkAllScanPanel(){
  document.querySelectorAll("#scanResults input[type=checkbox]").forEach(cb=>cb.checked=true);
}
function invertScanPanel(){
  document.querySelectorAll("#scanResults input[type=checkbox]").forEach(cb=>cb.checked=!cb.checked);
}
async function importScanned(){
  const cbs=[...document.querySelectorAll("#scanResults input[type=checkbox]:checked")];
  if(!cbs.length){ toast("请先勾选要导入的模型","err"); return; }
  const exists=new Set(models.map(m=>`${m.base_url}|${m.model}`));
  const needDefault=!models.some(m=>m.enabled && m.is_default);
  let added=0, dup=0;
  cbs.forEach(cb=>{
    const rec={ name: cb.value,  // v1.0.233：列表名称默认同模型参数值，导入后可编辑改名
      base_url:cb.dataset.base, model:cb.value, api_key:"",
      enabled:true, is_default:needDefault && added===0 };
    const key=`${rec.base_url}|${rec.model}`;
    if(exists.has(key)){ dup++; return; }
    models.push(rec); exists.add(key); added++;
  });
  if(!added){ toast("所选模型均已存在，无需重复导入","err"); return; }
  try{
    const r=await fetch("/api/models",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(models)});
    const d=await r.json();
    if(!r.ok || d.error){ toast("导入失败："+(d.error||"HTTP "+r.status),"err"); return; }
    $("scanPanel").style.display="none";
    await loadModels(); refreshStatus();
    toast(`已导入 ${added} 个本地模型${dup?`，跳过 ${dup} 个重复`:""}`);
  }catch(e){ toast(fetchErr(e,"导入模型"),"err"); }
}
let currentJudgeId = "";
// ---- Token 用量统计（v1.0.252，模型页）----
let __usageDays = 14;
function fmtTok(n){ return fmtNum(n); }   // 与 13-dashboard 的 fmtDashNum 同款实现收敛到 01-core 的 fmtNum，保留旧函数名
async function loadUsageStats(days){
  if(days) __usageDays = days;
  const cards=$("usageCards"), chart=$("usageChart"), chartX=$("usageChartX"), mw=$("usageModelsWrap");
  if(!cards) return;
  try{
    const d = await getJson(`/api/usage/summary?days=${__usageDays}`);
    const rl = $("usageRangeLabel"); if(rl) rl.textContent = `近 ${d.days} 天`;
    document.querySelectorAll("#usageRangeBtns [data-days]").forEach(b=>{ b.style.color = (+b.dataset.days===d.days) ? "var(--brand-fg)" : ""; b.style.fontWeight = (+b.dataset.days===d.days) ? "700" : ""; });
    // 汇总卡
    cards.innerHTML = "";
    const mk = (label, value, sub)=>{
      const el=document.createElement("div"); el.className="usage-stat";
      el.innerHTML = `<div class="us-label">${label}</div><div class="us-value">${value}</div><div class="us-sub">${sub||""}</div>`;
      cards.appendChild(el);
    };
    mk("今日 Tokens", fmtTok(d.today.total), `输入 ${fmtTok(d.today.prompt)} · 输出 ${fmtTok(d.today.completion)}`);
    mk("今日请求", d.today.requests, d.today.requests ? "次调用" : "今天还没有调用");
    mk(`${d.days} 天总计`, fmtTok(d.totals.total), `共 ${d.totals.requests} 次调用`);
    mk("日均用量", fmtTok(d.avg_daily_total), "tokens / 天");
    // 柱状图（stacked：输入绿在下、输出蓝在上）
    chart.innerHTML=""; chartX.innerHTML="";
    const max = Math.max(1, ...d.day_series.map(x=>x.total));
    const hasData = d.totals.total > 0;
    if(!hasData){
      chart.innerHTML = `<div style="flex:1;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:var(--fs-sm)">暂无数据——发起任意对话/任务后这里会出现用量柱</div>`;
    } else {
      d.day_series.forEach(x=>{
        const col=document.createElement("div"); col.className="uc-col";
        col.dataset.tip = `${x.date.slice(5)}｜总 ${fmtTok(x.total)}（入 ${fmtTok(x.prompt)} / 出 ${fmtTok(x.completion)}）｜${x.requests} 次`;
        const pt=document.createElement("div"); pt.className="uc-pt";
        pt.style.height = x.prompt/max*130 +"px";
        const ct=document.createElement("div"); ct.className="uc-ct";
        ct.style.height = x.completion/max*130 +"px";
        if(x.total>0){ pt.style.minHeight="2px"; ct.style.minHeight = x.completion>0?"2px":"0"; }
        col.appendChild(pt); col.appendChild(ct); chart.appendChild(col);
        const xl=document.createElement("span"); xl.textContent = x.date.slice(8); chartX.appendChild(xl);
      });
    }
    // 模型分布
    if(d.models && d.models.length){
      mw.style.display="";
      const box=$("usageModels"); box.innerHTML="";
      const mmax = Math.max(1, d.models[0].total);
      d.models.forEach(m=>{
        const row=document.createElement("div"); row.className="usage-mrow";
        row.title = `输入 ${m.prompt} · 输出 ${m.completion} · ${m.requests} 次调用`;
        row.innerHTML = `<span class="um-name">${escapeHtml(m.model)}</span><div class="um-bar"><div style="width:${Math.max(2, m.total/mmax*100)}%"></div></div><span class="um-val">${fmtTok(m.total)} tok · ${m.requests}次</span>`;
        box.appendChild(row);
      });
    } else { mw.style.display="none"; }
  }catch(e){
    cards.innerHTML = `<div class="usage-stat" style="grid-column:1/-1"><div class="us-label">用量加载失败</div><div class="us-sub">${escapeHtml(String(e&&e.message||e))}</div></div>`;
  }
}
// 清空 Token 用量统计（v1.0.270）：删除全部月度存档
async function clearUsageStats(){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("清空用量统计", "将永久删除 dist/logs/usage_*.jsonl 全部存档记录，不可恢复。", async ()=>{
    try{
      const d = await apiToast("/api/usage/clear", {method:"DELETE"}, "清空用量统计");
      toast(`已清空：${d.records} 条记录 / ${d.removed} 个存档文件`);
      loadUsageStats();
    }catch(_){}
  }, "清空");
}
async function loadModels(){
  try { const c = await getJson("/api/config"); currentJudgeId = c.judge_model_id || ""; } catch(e){ currentJudgeId = ""; }
  // v1.0.616（审查 P2）：这里此前不兜错——接口挂了整页停在上一版数据、非数组返回直接抛
  // forEach 异常（openView 链上表现为"模型页空白像没配模型"）。
  const tb = $("modelTable") && $("modelTable").querySelector("tbody"); if(!tb) return;
  try{
    const d = await getJson("/api/models");
    if(!Array.isArray(d)) throw new Error((d && d.error) || "接口返回结构异常");
    models = d;
  }catch(e){
    tb.innerHTML = `<tr><td colspan="6" class="sub">⚠️ 模型列表读取失败：${escapeHtml(String((e&&e.message)||e))} —— 换个动作再回来会自动重试</td></tr>`;
    return;
  }
  tb.innerHTML="";
  models.forEach(m=>{
    const tr=document.createElement("tr");
    if (m.id===currentJudgeId) tr.classList.add("judge-row");   // 判断大模型行高亮
    const tags = (m.is_default?'<span class="tag def">默认</span>':'') + (m.enabled?'':'<span class="tag dis">禁用</span>')
      + (m.id===currentJudgeId?'<span class="tag judge">判断</span>':'');
    // 推理参数徽标（v1.0.186）：思考强度/思考开关/思考预算/自定义扩展
    const rtags = (m.reasoning_effort?`<span class="tag" title="推理强度">🧠${m.reasoning_effort}</span>`:'')
      + (m.enable_thinking===true||m.enable_thinking===false?`<span class="tag" title="思考开关">💭${m.enable_thinking?'开':'关'}</span>`:'')
      + (m.thinking_budget?`<span class="tag" title="思考预算 token">⏱${m.thinking_budget}</span>`:'')
      + (m.extra_params?`<span class="tag" title="自定义扩展参数">🧩扩展</span>`:'');
    // 协议与能力徽标（v1.0.187）
    const protoLabel = ({openai:'OpenAI',anthropic:'Anthropic',custom:'自定义'})[m.protocol||'openai'];
    const ptags = `<span class="tag caps" title="协议">${protoLabel}</span>`
      + (m.max_input_tokens?`<span class="tag caps" title="上下文窗口">📥${m.max_input_tokens>=1024?(m.max_input_tokens/1024|0)+'K':m.max_input_tokens}</span>`:'')
      + (m.max_output_tokens?`<span class="tag caps" title="输出上限">📤${m.max_output_tokens>=1024?(m.max_output_tokens/1024|0)+'K':m.max_output_tokens}</span>`:'');
    tr.innerHTML=`<td>${escapeHtml(m.name)}${tags}</td><td>${ptags}</td><td>${escapeHtml(m.model)}${rtags}</td>`+
      `<td>${m.api_key_masked?('🔑 '+m.api_key_masked):'未填'}</td>`+
      // v1.0.471（UI#7）：内联 onclick 字符串拼接 → data-* + 容器事件委托
      //（消灭 jsStr 转义拼接，id 只出现在 dataset，无注入面）
      `<td class="ops"><button class="ghost smbtn" data-act="edit" data-id="${escapeHtml(m.id)}">编辑</button>`+
      `<button class="ghost smbtn" data-act="bench" data-id="${escapeHtml(m.id)}">🧪 评测</button>`+
      `<button class="ghost smbtn" data-act="default" data-id="${escapeHtml(m.id)}">默认</button>`+
      `<button class="ghost smbtn" data-act="judge" data-id="${escapeHtml(m.id)}">${m.id===currentJudgeId?'取消判断':'判断'}</button>`+
      `<span class="btnsep" aria-hidden="true"></span>`+
      `<button class="danger smbtn" data-act="del" data-id="${escapeHtml(m.id)}">删</button></td>`;
    tb.appendChild(tr);
  });
  const sel=$("t_model"); if(sel) fillModelSelect(sel, models);   // 拼串本体收敛到 fillModelSelect（01-core），与 02/08 同款下拉共用
  if(typeof fillModelSels==="function") fillModelSels();   // v1.0.486：模型增删后同步任务选择器
  // v1.0.588：双模型分工的实际状态一眼可见（未配置时卡点自动介入/ask_judge 全部不生效，
  // 此前只有读帮助文档才知道）。value 补 escapeHtml——同款下拉在 02/08 都已转义，此处漏了一处
  const js1=$("judgeState");
  if(js1){
    const jm = models.find(m=>m.id===currentJudgeId);
    js1.innerHTML = jm
      ? `<span style="color:var(--brand-fg)">✅ 双模型分工已启用</span>：判断大模型 <b>${escapeHtml(jm.name)}</b>`+
        `（小模型跑工具循环，卡住/交卷被拦/ask_judge 时自动拉它给建议）`
      : `<span style="color:var(--warn-fg)">⚠️ 当前是单模型</span>：未设判断大模型，「卡点自动介入 / ask_judge / 团队规划审查」<b>不会生效</b>`+
        `（任务照常跑，只是没有大模型兜底）。想启用请在上面任一模型行点「判断」。`;
  }
}
// v1.0.471（UI#7）：模型表操作按钮事件委托（一次绑定，动态行无需重复挂）
document.addEventListener("click", function(e){
  const btn = e.target.closest && e.target.closest("#modelTable td.ops [data-act]");
  if(!btn) return;
  const id = btn.dataset.id, act = btn.dataset.act;
  if(act==="edit") editModel(id);
  else if(act==="bench") runModelBench(id);
  else if(act==="default") setDefaultModel(id);
  else if(act==="judge") setJudgeModel(id);
  else if(act==="del") delModel(id);
});
function showModelForm(){
  $("modelForm").style.display="block";
  // 基础字段
  $("m_name").value=""; $("m_model").value=""; $("m_base").value=""; $("m_key").value="";
  // v1.0.233：名称默认跟随模型参数值（未自定义时联动）
  window.__nameCustom = false;
  $("m_enabled").checked=true; $("m_default").checked=false;
  $("m_key").placeholder="sk-...（留空=保留已有）";
  // 协议
  $("m_protocol").value="openai"; onProtocolChange();
  // 高级配置 caps（全开默认）
  $("m_cap_tool").checked=true; $("m_cap_vision").checked=false;
  $("m_cap_thinking").checked=false; $("m_cap_only").checked=false;
  $("m_cap_off").checked=true; $("m_cap_custom").checked=false;
  // 思考强度
  $("m_effort").value="";
  document.querySelectorAll(".m_lv").forEach(cb=>cb.checked=false);
  // token 档位
  $("m_in_tok").value=""; $("m_out_tok").value="";
  // 兼容旧字段
  $("m_budget").value=""; $("m_extra").value="";
  window.__editId=null;
}

function onProtocolChange(){
  const v = $("m_protocol").value;
  // 自定义协议时：API Base URL 是完整 endpoint；OpenAI / Anthropic 时是 base 地址，自动补 /chat/completions
  const isCustom = v === "custom";
  // m_endpoint_label 绑的是 API Base URL 输入框（v1.0.193 重排后），按协议切换 label + placeholder
  $("m_endpoint_label").textContent = isCustom
    ? "API Base URL（完整 endpoint，自定义协议）"
    : "API Base URL（自动追加 /chat/completions）";
  $("m_base").placeholder = isCustom
    ? "https://api.example.com/v1/chat/completions"
    : "https://api.example.com/v1";
  // 选自定义时自动勾上 caps.custom_protocol（m_base_label 是模型参数值标签，不动）
  if (isCustom) $("m_cap_custom").checked = true;
}

// ===== v1.0.233 模型名称默认跟随模型参数值 =====
// 未自定义时：改模型参数值/点选检测出的模型 → 名称自动同步同值；
// 用户改过名称（与参数值不同）→ 视为自定义，不再联动；清空或改回同值 → 恢复联动。
function onModelValInput(){
  if (!window.__nameCustom) $("m_name").value = $("m_model").value;
}
function onNameInput(){
  const n = $("m_name").value.trim(), mv = $("m_model").value.trim();
  window.__nameCustom = !!(n && n !== mv);
}

function pickToken(inputId, value){
  $(inputId).value = value;
  // 视觉反馈：按钮短暂高亮
  const ev = event || window.event;
  if (ev && ev.target){
    ev.target.style.background = "#e8f7ee";
    ev.target.style.borderColor = "var(--brand)";
    setTimeout(()=>{ try{ ev.target.style.background=""; ev.target.style.borderColor=""; }catch(e){} }, 400);
  }
}

// ===== 远程模型探测（v1.0.188）=====
async function probeModels(){
  const base = $("m_base").value.trim();
  const key = $("m_key").value.trim();
  if (!base){ toast("请先填接口地址", "err"); return; }
  const panel = $("probePanel");
  const btn = $("probeBtn");
  btn.disabled = true; btn.textContent = "检测中…";
  panel.style.display = "block";
  panel.innerHTML = `<div class="wmeta" style="padding:6px 2px">⏳ 正在拉取 <b>${escapeHtml(base)}</b> 的模型列表…</div>`;
  try{
    const r = await fetch("/api/models/probe", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({base_url: base, api_key: key})});
    const d = await r.json();
    if (!r.ok || !d.ok){ panel.innerHTML = `<div class="err-note">⚠️ ${escapeHtml(d.error||("HTTP "+r.status))}</div>`; return; }
    // effort 试探结果提示
    let hint = "";
    if (d.effort_probe === "supported") hint = `<div class="probe-ok">✅ 已确认该服务接受 reasoning_effort 参数（思考强度真生效）</div>`;
    else if (d.effort_probe === "rejected") hint = `<div class="probe-warn">ℹ️ 该服务拒绝 reasoning_effort 参数——思考强度档位不可用，请用「思考开关/自定义参数」控制</div>`;
    else if (d.effort_probe && String(d.effort_probe).startsWith("error")) hint = `<div class="probe-warn">ℹ️ reasoning_effort 试探异常（${escapeHtml(d.effort_probe)}），以模型名推断为准</div>`;
    const items = (d.models||[]).map(m=>{
      const c = m.caps || {};
      const lv = (m.levels||[]).map(x=>({low:"低",medium:"中",high:"高",xhigh:"超高",max:"极致"})[x]||x).join("·");
      const capsTxt = [
        c.tool_call ? "🔧工具" : "", c.vision ? "👁图片" : "",
        c.thinking ? "💭思考" : "", c.thinking_only ? "🔒仅思考" : "",
        c.can_disable_thinking===false ? "" : ""
      ].filter(Boolean).join(" ");
      return `<div class="probe-item" onclick="pickProbedModel(this)" data-id="${jsStr(m.id)}" data-caps='${JSON.stringify(c).replace(/'/g,"&#39;")}' data-levels='${JSON.stringify(m.levels||[]).replace(/'/g,"&#39;")}'>
        <span class="probe-id">${escapeHtml(m.id)}</span>
        <span class="probe-caps">${capsTxt}${lv?` · 强度:${escapeHtml(lv)}`:""}</span>
        <span class="probe-pick">点选填入 →</span>
      </div>`;
    }).join("");
    panel.innerHTML = `<div style="font-size:var(--fs-sm); color:var(--muted); margin-bottom:6px">共 ${d.models.length} 个可用模型（点击填入表单，能力按模型名自动推断，可手动微调）</div>${hint}<div style="max-height:220px; overflow:auto">${items}</div>`;
  }catch(e){
    panel.innerHTML = `<div class="err-note">⚠️ ${escapeHtml(e.message||"网络错误")}</div>`;
  }finally{
    btn.disabled = false; btn.textContent = "🔍 检测模型";
  }
}
// 能力勾选回填（pickProbedModel / editModel 两处 7 行同款）：caps 缺省从 obj.caps 取，
// 勾选口径固定——tool_call/can_disable_thinking 为「!== false 即勾」，其余为布尔直取。
function fillCapChecks(obj, caps){
  const c = caps || (obj && obj.caps) || {};
  $("m_cap_tool").checked = c.tool_call !== false;
  $("m_cap_vision").checked = !!c.vision;
  $("m_cap_thinking").checked = !!c.thinking;
  $("m_cap_only").checked = !!c.thinking_only;
  $("m_cap_off").checked = c.can_disable_thinking !== false;
  $("m_cap_custom").checked = !!c.custom_protocol;
}
function pickProbedModel(el){
  const id = el.dataset.id;
  $("m_model").value = id;
  // v1.0.233：名称未自定义时同步为模型参数值
  if (!window.__nameCustom) $("m_name").value = id;
  // 回填 caps（7 行同款收敛到 fillCapChecks）
  try{
    fillCapChecks(el, JSON.parse(el.dataset.caps||"{}"));
  }catch(e){}
  // 回填思考强度档位
  try{
    const lv = JSON.parse(el.dataset.levels||"[]");
    document.querySelectorAll(".m_lv").forEach(cb=>cb.checked = lv.includes(cb.value));
  }catch(e){}
  // 视觉高亮选中项
  document.querySelectorAll(".probe-item").forEach(x=>x.classList.remove("sel"));
  el.classList.add("sel");
  toast("已填入模型 " + id);
}

async function saveModel(btn){
  // caps 收集
  const caps = {
    tool_call: $("m_cap_tool").checked,
    vision: $("m_cap_vision").checked,
    thinking: $("m_cap_thinking").checked,
    thinking_only: $("m_cap_only").checked,
    can_disable_thinking: $("m_cap_off").checked,
    custom_protocol: $("m_cap_custom").checked,
  };
  const levels = [...document.querySelectorAll(".m_lv:checked")].map(cb=>cb.value);
  // 思考开关：选了 caps.thinking 且用户没显式给 enable_thinking 时，按「推理模式开 = 思考开」推一个；
  // 保持与 v1.0.186 兼容（v1.0.186 字段为单选下拉），这里把「推理模式」与「enable_thinking」联动：
  // caps.thinking = true  → enable_thinking 默认 true（除非用户没勾「允许关闭思考」）
  // caps.thinking = false → enable_thinking 不设置（跟随模型默认）
  const enable_thinking = caps.thinking ? true : null;

  const rec = {
    name: $("m_name").value.trim() || $("m_model").value.trim(),  // v1.0.233：留空默认同模型参数值
    model: $("m_model").value.trim(),
    base_url: $("m_base").value.trim(),
    api_key: $("m_key").value.trim(),
    enabled: $("m_enabled").checked,
    is_default: $("m_default").checked,
    protocol: $("m_protocol").value,
    caps, thinking_supported_levels: levels,
    default_thinking_effort: $("m_effort").value || null,
    reasoning_effort: $("m_effort").value || null,  // 兼容 v1.0.186 旧逻辑（OpenAI 用）
    enable_thinking,
    thinking_budget: null, extra_params: null,
  };
  const _b = parseInt($("m_budget").value, 10);
  if ($("m_budget").value.trim() && _b>0 && _b<200000) rec.thinking_budget = _b;
  const _in = parseInt($("m_in_tok").value, 10);
  if ($("m_in_tok").value.trim() && _in>0) rec.max_input_tokens = _in;
  const _out = parseInt($("m_out_tok").value, 10);
  if ($("m_out_tok").value.trim() && _out>0) rec.max_output_tokens = _out;
  const _ep = $("m_extra").value.trim();
  if (_ep){ try{ rec.extra_params = JSON.parse(_ep); }catch(e){ toast("自定义参数不是合法 JSON："+e.message, "err"); return; } }
  if (!rec.model || !rec.base_url){ toast("模型参数值和接口地址不能为空", "err"); return; }
  if (window.__editId){ rec.id = window.__editId; }
  // 默认唯一性（v1.0.188 修复）：勾了「设为默认」就把其它模型默认位清掉，防双默认
  if (rec.is_default){
    models.forEach(m=>{ m.is_default = false; });
  }
  if (!window.__editId) models.push(rec);
  else { const idx = models.findIndex(x=>x.id===window.__editId); if(idx>=0) models[idx]=rec; else models.push(rec); }
  setBtnLoading(btn||document.querySelector("#modelForm .primary"), true);
  try {
    const r = await fetch("/api/models",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(models)});
    const d = await r.json();
    if (!r.ok || d.error){ toast("保存失败："+(d.error||"HTTP "+r.status), "err"); return; }
    const isNew = !window.__editId;
    $("modelForm").style.display="none"; window.__editId=null; await loadModels(); refreshStatus();
    // v1.0.626（用户要求取消）：v1.0.319 起"新保存的模型自动跑一轮综合评测"，实测打扰大于价值
    // ——保存动作当场打真实接口、烧额度、还会撞上"已有评测在进行"；用户只是想把模型存下来。
    // 评测能力一项没少：列表右侧「🧪 评测」和勾选后的综合评测都在，只是改回**由用户主动点**。
    toast(isNew ? "模型已保存｜要验证连通/能力，点它右侧的「🧪 评测」（保存不再自动跑）" : "模型已保存");
  } catch(e){ toast(fetchErr(e,"保存模型"), "err"); }
  finally { setBtnLoading(btn||document.querySelector("#modelForm .primary"), false); }
}
document.addEventListener("keydown", e=>{
  if (e.key==="Enter" && $("modelForm").style.display!=="none" && ["m_name","m_model","m_base","m_key"].includes(e.target.id)){
    e.preventDefault(); saveModel();
  }
});
function editModel(id){
  const m=models.find(x=>x.id===id); if(!m) return;
  showModelForm();
  $("m_name").value=m.name;$("m_model").value=m.model;$("m_base").value=m.base_url;
  $("m_key").placeholder="已保存："+(m.api_key_masked||"")+"（留空不改）";
  $("m_enabled").checked=m.enabled;$("m_default").checked=m.is_default;
  // 协议（v1.0.187）
  $("m_protocol").value = m.protocol || "openai"; onProtocolChange();
  // caps 回填（缺字段用合理默认；7 行同款收敛到 fillCapChecks）
  fillCapChecks(m, m.caps);
  // 思考强度：默认强度 + 支持档位多选回填
  $("m_effort").value = m.default_thinking_effort || m.reasoning_effort || "";
  document.querySelectorAll(".m_lv").forEach(cb=>{
    cb.checked = (m.thinking_supported_levels||[]).includes(cb.value);
  });
  // token 档位回填
  $("m_in_tok").value = m.max_input_tokens || "";
  $("m_out_tok").value = m.max_output_tokens || "";
  // 兼容旧字段回填
  $("m_budget").value = m.thinking_budget ? String(m.thinking_budget) : "";
  $("m_extra").value = m.extra_params ? JSON.stringify(m.extra_params) : "";
  // v1.0.233：编辑时名称与参数值不同 → 已自定义（不联动）；相同 → 保持联动
  window.__nameCustom = (m.name||"").trim() !== (m.model||"").trim();
  window.__editId = id;
}
function cancelModelForm(){ $("modelForm").style.display="none"; window.__editId=null; }
async function setDefaultModel(id){
  models.forEach(m=>m.is_default=(m.id===id));
  try {
    const r = await fetch("/api/models",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(models)});
    const d = await r.json(); if (!r.ok || d.error){ toast("设置默认失败："+(d.error||"HTTP "+r.status), "err"); return; }
    await loadModels(); refreshStatus(); toast("已设为默认");
  } catch(e){ toast(fetchErr(e,"设置默认"), "err"); }
}
async function delModel(id){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("删除模型", "删除该模型？", async ()=>{
    models = models.filter(m=>m.id!==id);
    try {
      const r = await fetch("/api/models",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(models)});
      const d = await r.json(); if (!r.ok || d.error){ toast("删除失败："+(d.error||"HTTP "+r.status), "err"); return; }
      await loadModels(); refreshStatus(); toast("已删除");
    } catch(e){ toast(fetchErr(e,"删除模型"), "err"); }
  }, "删除");
}
// 双模型分工（v1.0.234）：设置/取消「判断模型」。再点一次已设为判断的模型 → 取消（退回单模型）。
async function setJudgeModel(id){
  const newId = (currentJudgeId === id) ? "" : id;
  try {
    const r = await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({judge_model_id:newId})});
    const d = await r.json(); if (!r.ok || d.error){ toast("设置判断模型失败："+(d.error||"HTTP "+r.status), "err"); return; }
    currentJudgeId = newId;
    await loadModels(); refreshStatus();
    toast(newId ? "已设为判断模型（双模型分工：大模型判断 + 小模型工具）" : "已取消判断模型（单模型）");
  } catch(e){ toast(fetchErr(e,"设置判断模型"), "err"); }
}

// —— 模型配置一键导出/导入（v1.0.238；v1.0.270 导出改为后端落盘——
//    pywebview 下 blob+a.download 点击不触发浏览器下载，旧版只弹 toast 没有文件）——
async function exportModelCfg(){
  try{
    const d = await apiToast("/api/models/export/save", {method:"POST"}, "导出模型配置");
    toast("模型配置已导出（"+d.count+" 个模型）：\n" + d.path, "ok", 8000);
  }catch(_){}
}
async function importModelCfg(inp){
  const f = inp.files && inp.files[0];
  inp.value = "";
  if(!f) return;
  try{
    const text = await f.text();
    const d = await apiToast("/api/models/import",{method:"POST",headers:{"Content-Type":"application/json"},body:text}, "导入模型配置");
    toast(`已导入 ${d.count} 个模型${d.judge_model_id ? "，判断大模型已恢复" : "（未含判断大模型）"}`);
    await loadModels(); refreshStatus();
  }catch(_){}
}

// ---- 模板 ----


// ---- 模型综合评测（v1.0.319）：7 项打分制，SSE 逐项实时 + 总分 ----
// v1.0.342 修复：旧版出错只 toast、弹窗残留在 DOM → 守卫把后续所有打开都拦成
// 「已有评测在进行」，用户看到的是"第一次弹出后再也不显示"。现在：
//   ① 弹窗带 dataset.running 运行态标记，守卫只拦"真在跑"，残壳自动清掉重开；
//   ② 全链 AbortController，弹窗内可「⏹ 停止」（关闭按钮在运行中也会先停再关）；
//   ③ 失败原因直接显示在弹窗里（不再只有一闪而过的 toast）。
let __benchModalCtrl = null;   // 弹窗版评测的中断控制器
function closeBenchModal(){
  const m=document.getElementById("benchModal");
  if(m){
    if(m.dataset.running==="1" && __benchModalCtrl){ try{__benchModalCtrl.abort();}catch(e){} }
    m.remove();
  }
  __benchModalCtrl=null;
}
function stopBenchModal(){ if(__benchModalCtrl){ try{__benchModalCtrl.abort();}catch(e){} } }
async function runModelBench(modelId, nameHint){
  const exist=document.getElementById("benchModal");
  if(exist){
    if(exist.dataset.running==="1"){ toast("已有评测在进行，可点弹窗里的「⏹ 停止」","warn"); return; }
    exist.remove();   // 上次异常残留的空壳：清掉重开
  }
  const name = nameHint || modelId;
  const ov=document.createElement("div");
  ov.id="benchModal";
  ov.dataset.running="1";
  ov.className="modal-veil";
  ov.innerHTML=
    `<div class="modal-card" style="--mw:520px">`+
      `<div style="font-weight:500;font-size:var(--fs-lg);margin-bottom:4px">🧪 模型综合评测 — ${escapeHtml(name)}</div>`+
      `<div class="sub" style="font-size:var(--fs-xs);margin-bottom:10px">7 项常用能力（基础响应/指令遵循/中文生成/逻辑推理/工具调用意图/长文提取/响应速度），加权总分 100。预计 1-2 分钟。</div>`+
      `<div id="benchRows"></div>`+
      `<div id="benchTotal" style="display:none;margin-top:10px;padding:10px;border-radius:var(--r-md);background:var(--panel2,#f6f8fa);text-align:center"></div>`+
      `<div class="row" style="justify-content:flex-end;margin-top:10px">`+
        `<button class="ghost" onclick="stopBenchModal()" title="中断本次评测（服务端在当前项结束后停止）">⏹ 停止</button>`+
        `<button class="ghost" onclick="closeBenchModal()">关闭</button>`+
      `</div>`+
    `</div>`;
  document.body.appendChild(ov);
  const rows=ov.querySelector("#benchRows");
  const addRow=(label)=>{ const d=document.createElement("div"); d.style.cssText="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border,#e4e8ef);font-size:var(--fs-sm)";
    d.innerHTML=`<b style="min-width:110px">${escapeHtml(label)}</b><span style="flex:1;color:var(--muted,#8792a3)">测试中…</span>`;
    rows.appendChild(d); return d; };
  const ctrl=new AbortController(); __benchModalCtrl=ctrl;
  let settled=false;
  try{
    const resp=await fetch("/api/models/bench7",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model_id:modelId}),signal:ctrl.signal});
    if(!resp.ok){ const e=await resp.json().catch(()=>({})); throw new Error(e.error||("HTTP "+resp.status)); }
    // v1.0.471（UI#15）：手写 read 循环改用公共 consumeSSE（01-core）
    await consumeSSE(resp.body.getReader(), (j)=>{
      if(j.type==="bench_item"){
        let d=[...rows.children].find(x=>x.dataset.key===j.key);
        if(!d){ d=addRow(j.name); d.dataset.key=j.key; }
        const color=j.score>=80?"var(--brand-fg)":j.score>=50?"var(--warn-fg)":"var(--err-fg)";
        d.lastElementChild.innerHTML=`<b style="color:${color}">${j.score} 分</b> · ${escapeHtml(j.detail||"")} · ${(j.cost_ms/1000).toFixed(1)}s`;
      } else if(j.type==="bench_done"){
        settled=true;
        const tt=ov.querySelector("#benchTotal");
        tt.style.display="block";
        const color=j.total>=85?"var(--brand-fg)":j.total>=70?"var(--warn-fg)":j.total>=50?"var(--warn-fg)":"var(--err-fg)";
        tt.innerHTML=`<b style="font-size:var(--fs-2xl);color:${color}">${j.total} 分</b> <span class="pill">${j.grade}</span>`;
      }
    });
  }catch(e){
    if(e && (e.name==="AbortError")){
      rows.innerHTML+='<div style="color:var(--warn-fg);padding:6px 0">⏹ 已停止评测</div>';
    }else{
      const msg=fetchErr(e,"模型评测");
      toast(msg,"err");
      rows.innerHTML+='<div style="color:var(--err-fg);padding:6px 0">评测失败：'+escapeHtml(msg)+'（检查模型接口地址/网络后可重新评测）</div>';
    }
  }finally{
    settled=true;
    ov.dataset.running="0";
    __benchModalCtrl=null;
  }
}

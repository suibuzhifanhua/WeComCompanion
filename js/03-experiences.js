let __exps = [];
let __skillOptions = [];
async function loadExperiences(){
  const list = $("expList");
  list.innerHTML = '<div class="empty">加载中…</div>';
  try{
    // 并行拉取经验列表 + 技能清单（模块下拉选项）
    const [er, sr] = await Promise.all([
      getJson("/api/experiences"),
      getJson("/api/skills").catch(()=>({skills:[]}))
    ]);
    __exps = er.items || [];
    __skillOptions = sr.skills || [];
    renderExpModuleOptions();
    renderExps();
  }catch(e){
    list.innerHTML = '<div class="empty">加载失败：'+escapeHtml(String(e))+'</div>';
  }
}
function renderExpModuleOptions(){
  const sel = $("expModule");
  const opts = ['<option value="">（通用/未分类）</option>'];
  for(const s of __skillOptions){
    if(s && s.id) opts.push('<option value="'+escapeHtml(s.id)+'">'+escapeHtml((s.icon||"")+ " "+ (s.name||s.id))+'</option>');
  }
  sel.innerHTML = opts.join("");
}
function renderExps(){
  const list = $("expList");
  if(!__exps.length){
    list.innerHTML = '<div class="empty">暂无经验记录。点「＋ 新增经验」把各模块踩过的坑和解决方法记下来。</div>';
    return;
  }
  // 按模块分组
  const groups = {};
  for(const e of __exps){
    const k = e.module || "（通用）";
    (groups[k] = groups[k] || []).push(e);
  }
  let html = "";
  for(const [mod, items] of Object.entries(groups)){
    html += '<div style="margin-bottom:16px">';
    html += '<div style="font-size:var(--fs-md);font-weight:600;color:var(--muted);margin-bottom:8px">📁 '+escapeHtml(mod)+'（'+items.length+' 条）</div>';
    for(const e of items){
      html += '<div class="exp-card" style="border:1px solid var(--border);border-radius:var(--r-md);padding:10px 12px;margin-bottom:8px;background:var(--panel2)">';
      html += '<div style="font-size:var(--fs-md);margin-bottom:6px"><b>问题：</b>'+escapeHtml(e.problem||"（未填写）")+'</div>';
      html += '<div style="font-size:var(--fs-md);margin-bottom:8px"><b style="color:var(--brand-fg)">解决：</b>'+escapeHtml(e.solution||"（未填写）")+'</div>';
      html += '<div class="row" style="justify-content:space-between;gap:8px">';
      html += '<span style="font-size:var(--fs-xs);color:var(--muted)">更新：'+escapeHtml(e.updated||"")+'</span>';
      html += '<span class="row" style="gap:6px">';
      html += '<button class="ghost" onclick="openExpForm(\''+jsStr(e.id)+'\')">编辑</button>';
      html += '<button class="ghost" style="color:var(--err-fg)" onclick="delExp(\''+jsStr(e.id)+'\')">删除</button>';
      html += '</span></div></div>';
    }
    html += '</div>';
  }
  list.innerHTML = html;
}
function openExpForm(id){
  $("expForm").style.display = "block";
  $("expId").value = id || "";
  const e = id ? (__exps.find(x=>x.id===id) || null) : null;
  $("expModule").value = e ? (e.module||"") : "";
  $("expProblem").value = e ? (e.problem||"") : "";
  $("expSolution").value = e ? (e.solution||"") : "";
}
function closeExpForm(){
  $("expForm").style.display = "none";
  $("expId").value = "";
}
async function saveExp(btn){
  const id = $("expId").value;
  const payload = {module: $("expModule").value, problem: $("expProblem").value.trim(), solution: $("expSolution").value.trim()};
  if(!payload.problem && !payload.solution){ toast("问题与解决方法至少填一项","warn"); return; }
  btn.disabled = true;
  try{
    const url = id ? "/api/experiences/"+id : "/api/experiences";
    const method = id ? "PUT" : "POST";
    const r = await fetch(url, {method, headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
    const j = await r.json();
    if(!j.ok){ toast("保存失败：" + (j.error||""),"err"); return; }
    closeExpForm();
    loadExperiences();
  }catch(e){ toast("保存失败："+String(e),"err"); }
  finally{ btn.disabled = false; }   // v1.0.616：原来写在 try 之后——服务端回 ok:false 时
                                     // 上面那条 return 会跳过复位，保存按钮从此永久禁用
}
async function delExp(id){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("删除经验", "确定删除这条经验？", async ()=>{
    try{
      const r = await fetch("/api/experiences/"+id, {method:"DELETE"});
      const j = await r.json();
      if(!j.ok){ toast("删除失败：" + (j.error||""),"err"); return; }
      loadExperiences();
    }catch(e){ toast("删除失败："+String(e),"err"); }
  }, "删除");
}


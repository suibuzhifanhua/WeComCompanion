let connSchemas = {};
async function loadSkills(){
  const list = $("skillList"); showLoading(list,"加载技能中…");
  try{
    const d = await getJson("/api/skills");
    list.innerHTML="";
    const skills=(d.skills||[]);
    const modeEl=$("skillMode");
    if(modeEl){
      modeEl.textContent = (d.enabled_skills===null||d.enabled_skills===undefined)
        ? "当前：全部启用（默认）"
        : `当前：${skills.filter(s=>s.enabled).length}/${skills.length} 个启用`;
    }
    if(!skills.length){ showEmpty(list,"🧩 暂无技能<br/>刷新页面重试；仍为空请到「打开日志文件夹」查看 skills 加载报错"); return; }
    skills.forEach(s=>{
      const card=document.createElement("div"); card.className="skill-card";
      const nTools=(s.tools||[]).length;
      // v1.0.373（用户需求6）：工具标签默认折叠为「🧰 N 个工具 ▸」，点击展开
      const tools=(s.tools||[]).map(t=>`<code>${escapeHtml(t)}</code>`).join("");
      const toolsBlock=nTools?`<button class="ghost smbtn skill-tools-toggle" onclick="toggleSkillTools(this)" title="展开/收起工具清单" aria-expanded="false">🧰 ${nTools} 个工具 <span class="fold-caret" aria-hidden="true"></span></button><div class="skill-tools" style="display:none">${tools}</div>`:"";
      const badge=s.overridden?'<span class="pill wrn">已优化</span>':'';
      // v1.0.303：自定义技能徽标 + 删除按钮（内置技能无删除）
      // v1.0.649（用户："聊天里安装的技能要在技能中心可管理"）：从技能库/技能源装进来的
      // 条目原先和手工自建一样只挂「自定义」，用户在列表里认不出"这就是我装的那个"——
      // 按 hub_ 前缀单独挂「技能库」，并把删除位一并给出（就是卸载）。
      const customBadge=s.custom?(s.auto?'<span class="pill inf">🧬 自学</span>'
                          :(String(s.id||"").startsWith("hub_")?'<span class="pill ok">📚 技能库</span>'
                                                                :'<span class="pill ok">自定义</span>')):'';
      const delBtn=s.custom?`<button class="ghost" onclick="deleteCustomSkill('${jsStr(s.id)}')">🗑 删除</button>`:'';
      // v1.0.349：版本历史与回滚入口
      const verBtn=`<button class="ghost" onclick="openSkillVersions('${jsStr(s.id)}','${jsStr(s.name)}')" title="提示词版本历史与回滚">⏱ 版本</button>`;
      card.innerHTML =
        // v1.0.364 审查修复（H2）：icon 来自远程技能库/自定义输入（页面可控），
        // 原样进 innerHTML 存在 XSS 注入面——统一转义；id 同样加固
        `<div class="skill-ic">${escapeHtml(s.icon||"🧩")}</div>`+
        `<div class="skill-main">`+
          `<div class="skill-title">${escapeHtml(s.name)} ${badge}${customBadge}<span class="pill">${(s.tools||[]).length} 工具</span></div>`+
          `<div class="skill-desc">${escapeHtml(s.desc||"")}</div>`+
          toolsBlock+
          `<div class="row" style="margin-top:8px"><button class="ghost" onclick="openSkillEditor('${jsStr(s.id)}')">🔧 ${s.custom?'编辑/优化':'专项优化'}</button>${verBtn}${delBtn}</div>`+
        `</div>`+
        `<label class="switch"><input type="checkbox" id="skill_${jsStr(s.id)}" ${s.enabled?'checked':''}/><span class="slider"></span></label>`;
      list.appendChild(card);
    });
  }catch(e){ showEmpty(list,"⚠️ 加载失败<br/>"+escapeHtml(e.message||"网络错误")); toast(fetchErr(e,"加载技能"),"err"); }
}
// ---- v1.0.349 技能版本历史与回滚 ----
const _VER_SRC_LABEL = {manual_edit: "手动编辑", self_repair: "自修复沉淀",
                        override_clear: "清除覆盖", delete: "删除前快照",
                        rollback_pre: "回滚前快照"};
async function openSkillVersions(sid, name){
  let ov=document.getElementById("skillVerModal"); if(ov) ov.remove();
  ov=document.createElement("div"); ov.id="skillVerModal";
  ov.className="modal-veil";
  ov.innerHTML='<div class="modal-card" style="--mw:660px">'+
    '<div style="font-weight:600;font-size:var(--fs-lg);margin-bottom:4px">⏱ 技能版本与回滚 — '+escapeHtml(name||sid)+'</div>'+
    '<div class="sub" style="font-size:var(--fs-xs);margin-bottom:10px">每次编辑/自修复/清除覆盖/回滚前都会自动保存一份完整提示词快照（保留最近 20 份），可随时回滚；回滚前当前状态也会先存一份，回滚本身可再撤销。</div>'+
    '<div id="skillVerList">加载中…</div>'+
    '<div class="row" style="justify-content:flex-end;margin-top:10px"><button class="ghost" onclick="document.getElementById(\'skillVerModal\').remove()">关闭</button></div></div>';
  document.body.appendChild(ov);
  try{
    const d=await getJson("/api/skills/"+encodeURIComponent(sid)+"/versions");
    const vs=d.versions||[];
    const box=ov.querySelector("#skillVerList");
    if(!vs.length){
      box.innerHTML='<div class="sub">该技能还没有版本历史。第一次编辑/自修复/回滚后会自动开始记录。</div>';
      return;
    }
    box.innerHTML=vs.map(v=>
      '<div style="border:1px solid var(--border);border-radius:var(--r-md);padding:8px 10px;margin-bottom:6px">'+
      '<div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap">'+
      '<span style="font-size:var(--fs-sm)"><b>'+escapeHtml(new Date(v.ts*1000).toLocaleString("zh-CN",{hour12:false}))+'</b> · '
      +escapeHtml(_VER_SRC_LABEL[v.source]||v.source)+' · '+v.len+' 字</span>'+
      '<button class="ghost smbtn" onclick="rollbackSkillVersion(\''+jsStr(sid)+'\','+v.ts+')">⏪ 回滚到此版</button></div>'+
      '<div style="color:var(--muted);font-size:var(--fs-xs);margin-top:4px;white-space:pre-wrap;word-break:break-word">'+escapeHtml(v.head)+'…</div>'+
      (v.note?'<div style="color:var(--muted);font-size:var(--fs-xs);margin-top:2px">'+escapeHtml(v.note)+'</div>':'')+
      '</div>').join("");
  }catch(e){
    const box2=ov.querySelector("#skillVerList");
    if(box2) box2.innerHTML='<div class="sub">加载失败：'+escapeHtml(fetchErr(e,"版本历史"))+'</div>';
  }
}
async function rollbackSkillVersion(sid, ts){
  // v1.0.471（UI#6）：原生 confirm → confirmAsk
  confirmAsk("回滚技能版本", "回滚该技能提示词到所选版本？当前状态会先自动存一份快照（可再次回滚撤销）。", async ()=>{
    try{
      await apiToast("/api/skills/"+encodeURIComponent(sid)+"/rollback",
        {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ts:ts})},"回滚");
      toast("已回滚（当前状态已存为快照）","ok");
      const m=document.getElementById("skillVerModal"); if(m) m.remove();
      loadSkills();
    }catch(_){}
  }, "回滚");
}
async function saveSkills(btn){
  const ids=[]; document.querySelectorAll('[id^="skill_"]').forEach(c=>{ if(c.checked) ids.push(c.id.replace("skill_","")); });
  setBtnLoading(btn,true);
  try{
    await apiToast("/api/skills",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled_skills:ids})},"保存");
    toast("已保存技能启用设置","ok");
  }catch(_){}
  finally{ setBtnLoading(btn,false); }
}
// ---- 公共技能库（v1.0.309）：内置快照 + 远程源，一键安装为自定义技能（hub_ 前缀） ----
function toggleHubPanel(){
  const p=$("hubPanel");
  if(!p) return;
  const show = p.style.display==="none";
  p.style.display = show ? "block" : "none";
  if(show && !$("hubList").dataset.loaded) loadHub();
}
async function loadHub(){
  const list=$("hubList"), meta=$("hubMeta");
  if(!list) return;
  showLoading(list,"加载公共技能库…");
  try{
    const d = await getJson("/api/skills/hub");
    list.dataset.loaded="1";
    const skills=d.skills||[];
    if(meta) meta.textContent = `（${skills.length} 个 · 源：${d.source==="remote"?"远程":"内置快照"}${d.version?" · "+d.version:""}${d.note?" · "+d.note:""}）`;
    list.innerHTML="";
    if(!skills.length){ showEmpty(list,"技能库为空——点右上角「添加技能源」填入仓库地址，即可浏览并一键安装"); return; }
    // 按分类分组渲染
    const byCat={}; skills.forEach(s=>{ (byCat[s.category||"其他"]=byCat[s.category||"其他"]||[]).push(s); });
    Object.keys(byCat).forEach(cat=>{
      const hd=document.createElement("div");
      hd.style.cssText="grid-column:1/-1;font-size:var(--fs-sm);font-weight:600;color:var(--muted);padding:4px 2px";
      hd.textContent="— "+cat+" —";
      list.appendChild(hd);
      byCat[cat].forEach(s=>{
        const card=document.createElement("div"); card.className="skill-card";
        const nTools=(s.tools||[]).length;
        // v1.0.373（用户需求6）：工具标签默认折叠，点击展开
        const tools=(s.tools||[]).map(t=>`<code>${escapeHtml(t)}</code>`).join("");
        const toolsBlock=nTools?`<button class="ghost smbtn skill-tools-toggle" onclick="toggleSkillTools(this)" title="展开/收起工具清单" aria-expanded="false">🧰 ${nTools} 个工具 <span class="fold-caret" aria-hidden="true"></span></button><div class="skill-tools" style="display:none">${tools}</div>`:"";
        // v1.0.316：已装且同版本 → 只显示「已安装」徽标不显示按钮；仅源有更新（版本不一致）才出「更新」
        let action;
        if (s.update_available) {
          action=`<button class="ghost" onclick="installHubSkill('${jsStr(s.id)}', this)">♻️ 更新</button>`;
        } else if (s.installed) {
          action=`<span class="sub" style="font-size:var(--fs-xs)">✅ 已是最新</span>`;
        } else {
          action=`<button class="ghost" onclick="installHubSkill('${jsStr(s.id)}', this)">⬇️ 安装</button>`;
        }
        const badge=s.update_available?'<span class="pill wrn">有更新</span>':(s.installed?'<span class="pill ok">已安装</span>':'');
        card.innerHTML =
          `<div class="skill-ic">${escapeHtml(s.icon||"🧩")}</div>`+
          `<div class="skill-main">`+
            `<div class="skill-title">${escapeHtml(s.name)} <span class="pill">${(s.tools||[]).length} 工具</span>${badge}</div>`+
            `<div class="skill-desc">${escapeHtml(s.desc||"")}</div>`+
            toolsBlock+
            `<div class="row" style="margin-top:8px">${action}</div>`+
          `</div>`;
        list.appendChild(card);
      });
    });
  }catch(e){ showEmpty(list,"⚠️ 加载失败<br/>"+escapeHtml(e.message||"网络错误")); toast(fetchErr(e,"加载公共技能库"),"err"); }
}
// v1.0.373（用户需求6）：展开/收起工具标签
function toggleSkillTools(btn){
  const box=btn.nextElementSibling; if(!box) return;
  const showing=box.style.display!=="none";
  box.style.display=showing?"none":"";
  // v1.0.635：原来靠 textContent 换 ▴/▾（全软件唯一用 ▴ 的地方），现在统一走 aria-expanded
  setFold(btn, !showing);
}
async function installHubSkill(hid, btn){
  setBtnLoading(btn,true);
  try{
    const d=await apiToast("/api/skills/hub/install",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:hid})},"安装技能");
    toast(`已安装技能「${d.name}」并启用（可在上方列表编辑）`,"ok");
    loadHub(); loadSkills();
  }catch(_){}
  finally{ setBtnLoading(btn,false); }
}
function toggleHubUrlForm(){ const f=$("hubUrlForm"); if(f) f.style.display = f.style.display==="none" ? "block" : "none"; }
async function saveHubUrl(btn){
  const url=$("hubUrl").value.trim();
  setBtnLoading(btn,true);
  try{
    await apiToast("/api/skills/hub/url",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})},"保存源");
    toast(url?"已保存远程源":"已恢复内置快照","ok");
    $("hubUrlForm").style.display="none";
    $("hubList").dataset.loaded=""; loadHub();
  }catch(_){}
  finally{ setBtnLoading(btn,false); }
}
// 全选 / 反选：只改勾选状态，点「保存启用设置」后生效
function setAllSkills(mode){
  const cbs=document.querySelectorAll('[id^="skill_"]');
  cbs.forEach(c=>{ c.checked = (mode==='toggle') ? !c.checked : !!mode; });
}
// 全部启用（默认）：POST null 表示恢复默认全开，立即生效并刷新勾选
async function enableAllSkills(btn){
  setBtnLoading(btn,true);
  try{
    await apiToast("/api/skills",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled_skills:null})},"保存");
    setAllSkills(true);
    const modeEl=$("skillMode"); if(modeEl) modeEl.textContent="当前：全部启用（默认）";
    toast("已恢复默认：全部技能启用","ok");
  }catch(_){}
  finally{ setBtnLoading(btn,false); }
}

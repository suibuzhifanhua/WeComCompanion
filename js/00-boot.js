// 启动进度遮罩：纯前端、窗口一出现即可见；按 /api/status 真实状态推进，就绪后淡出（绝不无限阻塞）
(function(){
  var ov=document.getElementById("bootOverlay"), bar=document.getElementById("bootBar"),
      pct=document.getElementById("bootPct"), st=document.getElementById("bootStatus"),
      foot=document.getElementById("bootFoot");
  if(!ov) return;
  var cur=6, target=20, done=false, tries=0;
  function setPct(p){ p=Math.max(0,Math.min(100,Math.round(p))); pct.textContent=p+"%"; bar.style.width=p+"%"; }
  function setMsg(m,err){ st.textContent=m; st.className="bo-status"+(err?" err":""); }
  function finish(){
    if(done) return; done=true; setPct(100); ov.classList.add("done");
    setTimeout(function(){ ov.classList.add("hide"); setTimeout(function(){ if(ov.parentNode) ov.parentNode.removeChild(ov); },520); }, 320);
  }
  (function tick(){ if(done) return; if(cur<target){ cur+=Math.max(.3,(target-cur)*.14); setPct(cur); } setTimeout(tick,80); })();
  function poll(){
    if(done) return; tries++;
    api("/api/status",{cache:"no-store"}).then(function(d){
      var w=d.wecom||{};
      if(foot && d.version && !foot.textContent) foot.textContent="v"+d.version;
      if(d.ready && w.bound && w.windows>0){ target=100; setMsg("就绪 · "+w.windows+" 个窗口"); setTimeout(finish,340); }
      else if(w.needs_kill){ target=93; setMsg("已进入 · 请在「企微」页确认重启并绑定"); setTimeout(finish,1700); }
      else if(w.bound){ target=96; setMsg("已进入 · 后台连接企微中…"); setTimeout(finish,1200); }
      else if(d.ready){ target=88; setMsg("已进入 · AI 助手就绪（企业微信未绑定也能直接问）"); setTimeout(finish,1200); }
      else { target=80; setMsg("服务就绪 · 后台初始化中…"); setTimeout(finish,1700); }
    }).catch(function(){ /* 服务未就绪：靠 18s 硬超时兜底 */ });
    if(!done) setTimeout(poll,350);
  }
  setMsg("正在启动…");
  setTimeout(poll,150);
  setTimeout(function(){ if(!done) finish(); }, 18000);  // 硬兜底：无论如何淡出，避免遮罩卡死界面
})();

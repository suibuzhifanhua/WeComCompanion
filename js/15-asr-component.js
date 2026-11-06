// 15-asr-component.js — 语音转写组件卡（v1.0.536）
// 设置页：状态显示（可用/未安装/安装中，用词取自 js/01-core.js 的 STATUS_*）+ 一键安装 + 进度条轮询。
// 对话框 🎤 按钮未装组件时引导跳转本页。

function renderAsrComponent(){
  // v1.0.538：AbortController 8s 超时——后端请求挂起时 UI 不再永久卡死
  const ctl = new AbortController(); const to = setTimeout(() => ctl.abort(), 8000);
  fetch("/api/asr/status", {signal: ctl.signal}).then(r => r.json()).then(d => {
    clearTimeout(to);
    const st = $("asrCompStatus"), btn = $("asrInstallBtn"), bar = $("asrInstallBar");
    if (!st) return;
    if (d.available === "installed") {
      // v1.0.650 U6：用词与图标统一到 js/01-core.js 的状态口径——以前这里叫「已安装」、
      // 组件视图同一条叫「可用」、汇总行又叫「全部就绪」；「安装中」这里是黄点（规范里
      // 黄=降级/注意，进行中=⏳），组件视图是 ⏳。圆点本身合规，漂的是用词与颜色档。
      st.innerHTML = STATUS_ICON.ok + ' ' + STATUS_WORD.ok + '——对话输入框的 🎤 可用（模型首次使用时自动下载）';
      btn.style.display = ""; btn.disabled = false; btn.textContent = "↻ 重装/修复（组件异常时使用）";
      bar.style.display = "none";
    } else if (d.available === "missing") {
      const p = d.progress || {};
      if (p.running) {
        st.innerHTML = STATUS_ICON.installing + ' ' + STATUS_WORD.installing + '…（可离开本页，后台继续）';
        btn.style.display = "none"; btn.disabled = false; btn.textContent = "⬇ 一键安装（约 75MB，从 PyPI 下载，仅一次）";
        bar.style.display = "";
        $("asrInstallFill").style.width = (p.pct || 0) + "%";
        $("asrInstallPhase").textContent = (p.phase || "") + " " + (p.pct || 0) + "%";
        setTimeout(renderAsrComponent, 1200);   // 自轮询直到终态
      } else if (p.ok === false) {
        st.innerHTML = STATUS_ICON.err + ' 安装失败：' + escapeHtml(p.error || "未知错误");
        btn.style.display = ""; btn.disabled = false; btn.textContent = "↻ 重试安装"; bar.style.display = "none";
      } else {
        st.innerHTML = STATUS_ICON.off + ' ' + STATUS_WORD.off + '——对话输入框的 🎤 不可用';
        btn.style.display = ""; btn.disabled = false; btn.textContent = "⬇ 一键安装（约 75MB，从 PyPI 下载，仅一次）"; bar.style.display = "none";
      }
    } else {
      st.innerHTML = STATUS_ICON.na + ' 当前平台不支持语音转写';
      btn.style.display = "none"; bar.style.display = "none";
    }
  }).catch(e => {
    clearTimeout(to);
    const st = $("asrCompStatus");
    if (st) st.innerHTML = '<span class="dot err" style="display:inline-block"></span> 状态获取超时——后端忙碌，稍后自动重试';
    setTimeout(renderAsrComponent, 3000);   // 超时自动重试
  });
}

function installAsrComponent(){
  const btn = $("asrInstallBtn");
  if (btn) { btn.disabled = true; btn.textContent = "准备安装…"; }
  // v1.0.538：立即切进度条视图（乐观态）——不等后端返回，进度条必现
  const bar = $("asrInstallBar");
  if (bar) { bar.style.display = ""; $("asrInstallFill").style.width = "2%";
             $("asrInstallPhase").textContent = "正在启动安装…"; }
  const ctl = new AbortController(); const to = setTimeout(() => ctl.abort(), 10000);
  fetch("/api/asr/install", {method:"POST", headers:{"Content-Type":"application/json"},
                             body:"{}", signal: ctl.signal})
    .then(r => r.json()).then(d => {
      clearTimeout(to);
      if (!d.ok) { toast(d.error || "安装启动失败", "err"); }
      else { toast("组件安装已开始（后台下载中，约 5-10 分钟）", "ok"); }
    }).catch(e => {
      clearTimeout(to);
      toast("安装请求超时——后台可能仍在进行，请稍后刷新本页查看", "err");
    }).finally(() => {
      if (btn) { btn.disabled = false; }
      renderAsrComponent();   // 统一交回状态机渲染（含轮询）
    });
}

// 设置页打开时刷新组件状态
if (typeof __asrHooked === "undefined") {
  var __asrHooked = true;
  const _origOpenViewAsr = window.openView;
  if (typeof _origOpenViewAsr === "function") {
    window.openView = function(name){
      const r = _origOpenViewAsr.apply(this, arguments);
      if (name === "settings") { try { renderAsrComponent(); } catch(e){} }
      return r;
    };
  }
}

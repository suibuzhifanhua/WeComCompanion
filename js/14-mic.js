// 14-mic.js — 语音输入（v1.0.523-5）
// 🎤 点击开始 → 再点停止 → 录音 POST /api/asr/transcribe（本地 faster-whisper 转写，
// 音频不外发）→ 文本填入输入框（用户可修改后再发送）。
// 引擎未安装时按钮自动降级为提示（/api/asr/status）。

let __micRec = null;          // MediaRecorder 实例
let __micChunks = [];
let __micBtnId = null;        // 当前录音的按钮 id
let __micStream = null;
let __micStatusTimer = null;
let __micT0 = 0;              // v1.0.595：录音起始时刻，用于按【真实时长】判太短

async function toggleMic(inputId, btnId){
  // v1.0.616（审查 P2）：录音态原来只有一个全局开关——在工作台点着 🎤 时去助手输入框
  // 再点 🎤，会**停掉工作台那段录音**而不是开始新的（用户体感"点了没反应")。
  // 现在：不同按钮 → 先停旧的（旧录音照常送去转写，结果仍回它自己的输入框）再开新的；
  // 同一按钮 → 停止即完成。
  if(__micRec){
    const other = __micBtnId && __micBtnId !== btnId;
    await _micStop();
    if(!other) return;
    toast("已停止另一个输入框的录音（转写照常进行），开始这里的新录音", "warn", 2600);
  }
  const btn = $(btnId);
  if(!navigator.mediaDevices || !window.MediaRecorder){
    toast("当前环境不支持录音（WebView 未授权麦克风）", "err"); return;
  }
  try{
    // 首次点按先探引擎（避免录完才发现不可用）
    if(!window.__asrAvailable){
      const st = await getJson("/api/asr/status");
      if(st.available !== "installed"){
        toast("语音组件未安装——设置页「语音转写组件」可一键安装", "err", 4000);
        try { openView("settings"); } catch(e) {}
        return;
      }
      window.__asrAvailable = true;
    }
    __micStream = await navigator.mediaDevices.getUserMedia({audio:true});
  }catch(e){
    toast("麦克风不可用：" + (e && e.message || e), "err"); return;
  }
  try{
    __micChunks = [];
    __micRec = new MediaRecorder(__micStream);
    __micRec.ondataavailable = ev => { if(ev.data && ev.data.size) __micChunks.push(ev.data); };
    __micRec.onstop = () => _micTranscribe(inputId, btnId);
    __micRec.start();
    __micT0 = Date.now();
    __micBtnId = btnId;
    if(btn){ btn.textContent = "⏺"; btn.style.color = "var(--err-fg)"; btn.classList.add("mic-rec"); }
    let sec = 0;
    // v1.0.614：录音计时原来只写进 title（tooltip）——最长 120 秒的等待期间界面上
    // 没有任何可读状态，用户不知道是在录、在传还是死了。改成按钮上直接显示秒数。
    __micStatusTimer = setInterval(()=>{
      sec++;
      if(btn){ btn.textContent = "⏺" + sec + "s"; btn.title = "录音中 " + sec + "s — 再次点击结束"; }
    }, 1000);
  }catch(e){
    _micCleanup();
    toast("录音启动失败：" + (e && e.message || e), "err");
  }
}

async function _micStop(){
  if(__micStatusTimer){ clearInterval(__micStatusTimer); __micStatusTimer = null; }
  try{ __micRec.stop(); }catch(e){}
  (__micStream||[]).getTracks && (__micStream||{}).getTracks && (__micStream||{}).getTracks();
  try{ (__micStream || {getTracks:()=>[]}).getTracks().forEach(t=>t.stop()); }catch(e){}
}

async function _micTranscribe(inputId, btnId){
  const btn = $(btnId);
  const blob = new Blob(__micChunks, {type: __micRec && __micRec.mimeType || "audio/webm"});
  __micChunks = []; __micRec = null; __micStream = null; __micBtnId = null;
  _micResetBtn(btn);
  // v1.0.595：判「太短」改用真实录音时长。原来只看 blob.size < 2000——webm/opus 的容器头
  // 本身就有 ~1KB 量级、码率又随静音浮动，正常的一两个词短语（"打开报销"）会被误判成
  // "录音太短"直接丢弃，用户体感就是"短录音不支持"。0.4s 以下 whisper 也确实给不出结果。
  const secs = __micT0 ? (Date.now() - __micT0) / 1000 : 0;
  __micT0 = 0;
  if((secs && secs < 0.4) || blob.size < 512){
    toast("录音太短（不到 0.4 秒）——再说长一点", "err"); return;
  }
  if(btn){ btn.textContent = "⏳转写"; btn.title = "转写中…（本地模型推理，音频不外发）"; }
  try{
    const fd = new FormData();
    fd.append("audio", blob, "voice.webm");
    const ctrl = new AbortController(); const to = setTimeout(()=>ctrl.abort(), 120000);
    const r = await fetch("/api/asr/transcribe", {method:"POST", body:fd, signal:ctrl.signal});
    clearTimeout(to);
    const d = await r.json();
    if(!d.ok){ toast("转写失败：" + (d.error || r.status), "err"); return; }
    // v1.0.595：识别结果为空不算成功——原来会走到下面填一个空串并 toast「已填入（0 字）」，
    // 用户看到"成功"却没字，只能反复重录。
    if(!d.text || !d.text.trim()){
      toast("没听清——靠近麦克风、说得稍长一点再试", "warn", 3500); return;
    }
    const inp = $(inputId);
    if(inp){
      inp.value = (inp.value ? inp.value.replace(/\s+$/,"") + " " : "") + d.text;
      inp.dispatchEvent(new Event("input"));
      inp.focus();
      if(typeof autoResize === "function") autoResize(inp);
    }
    toast(`已填入（${d.text.length} 字）`, "ok", 2000);
  }catch(e){
    toast("转写请求失败：" + (e && e.message || e), "err");
  }finally{
    _micResetBtn(btn);
  }
}

function _micResetBtn(btn){
  if(btn){ btn.textContent = "🎤"; btn.style.color = ""; btn.classList.remove("mic-rec"); btn.title = "语音输入：点击开始录音，再点结束并填入（本地转写，不外发）"; }
}

function _micCleanup(){
  try{ (__micStream || {getTracks:()=>[]}).getTracks().forEach(t=>t.stop()); }catch(e){}
  __micRec = null; __micStream = null; __micChunks = [];
  _micResetBtn(__micBtnId ? $(__micBtnId) : null);
}

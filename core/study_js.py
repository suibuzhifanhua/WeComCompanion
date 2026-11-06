# -*- coding: utf-8 -*-
"""学习中心视频任务的「静音三件套」表达式——唯一事实源（v1.0.623）。

为什么要有这个文件：同一段 JS 此前活着三份（跑课引擎 prompt / study-solver 技能 /
快捷入口提示词），后两份停在只有 __muteLoop 的旧版——播放器会把 muted 重置回 false，
光靠 400ms 循环反制不掉；而裸媒体 iframe（src 直指 .mp4/m3u8 或 learn/app/filehub）
里根本没有 <video> 可拖，学习不计数。引擎先修成「属性锁 + 循环 + iframe 中和」，
另两份一路没跟上，快捷入口还把旧版抄进了用户可见的提示词。现引擎与技能都从这里取，
漂移由 tests/test_v623_mute_sync.py 判红。

三段各自挡什么：
  __muteLock  把 HTMLMediaElement.prototype.muted 的 setter 改写成「只能置真」，
              播放器自己调 muted=false 也无效（v1.0.266 那版循环只是补丁）；
  __muteLoop  对页面上已有/后插的 video、audio 持续兜底；
  iframe 中和 裸媒体 iframe 改写成 data: 页面里的 <video>，这样才查得到 v、才拖得动
              结尾；页面已有 <video> 时把 iframe 置 about:blank，避免双份计数。
"""

STUDY_MUTE_EXPR = (
    "if(!window.__muteLock){try{const d=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'muted');Object.defineProperty(HTMLMediaElement.prototype,'muted',{get:function(){return d.get.call(this);},set:function(){d.set.call(this,true);},configurable:true});}catch(e){}} if(!window.__muteLoop)"
    "{window.__muteLoop=setInterval(()=>{document.querySelectorAll('video,audio').forEach(m=>{try{m.muted=true;m.volume=0;}catch(e){}});},400);} document.querySelectorAll('iframe')"
    '.forEach(f=>{const s=f.src||\'\';if(!/\\.(mp4|m3u8|flv|mov|webm)([?#]|$)/i.test(s)&&s.indexOf(\'learn/app/filehub\')<0)return;if(s.indexOf(\'data:\')===0||s.indexOf(\'about:blank\')===0)return;if(document.querySelector(\'video\')){f.src=\'about:blank\';return;}const w="<html><body style=margin:0;overflow:hidden><video src="+JSON.stringify(s)+" autoplay muted playsinline style=width:100vw;height:100vh></video></body></html>";f.src=\'data:text/html;charset=utf-8,\'+encodeURIComponent(w);}); const v=document.querySelector(\'video\'); if(!v) return \'no video\'; try{v.play();}catch(e){} v.currentTime=v.duration-3; return \'mute lock+loop on\';'
)

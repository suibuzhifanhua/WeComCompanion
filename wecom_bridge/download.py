"""浏览器真实下载（带登录态）与文件上传。"""

import os
import time
import json
import base64
import re
from urllib.parse import urlparse, unquote

from core_utils import _lazy_requests as requests

from .cdp_client import (
    DEFAULT_PORT, CDP_UPLOAD_TIMEOUT, _logw, _open_ws, _recv_until,
    _cdp_eval, _cdp_eval_retry, _cdp_command,
)
from .windows import _page_ws, _require_page
from .read import click_element
from core.result import is_err, err_msg

# 浏览器真实下载（带登录态）相关常量
DOWNLOAD_WAIT_TIMEOUT = 90   # 等待浏览器下载完成的总超时（秒）
DOWNLOAD_POLL_STEP = 1.0     # 轮询下载目录的间隔（秒）


def _browser_ws_url(port=DEFAULT_PORT):
    """取 browser 级 WebSocket 端点（v1.0.412 免弹框下载的关键）。
    Browser.setDownloadBehavior 必须发到 browser target（/json/version 的
    webSocketDebuggerUrl）才对全部页面生效；发到 page 端点时新版 Chromium 直接
    报错/忽略 → 下载行为退回宿主默认 → 企微 Electron 弹「另存为」对话框挂住任务。
    取不到（CDP 未就绪/结构变化）返回 None。"""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return (r.json() or {}).get("webSocketDebuggerUrl") or None
    except Exception:  # noqa: BLE001
        return None


def set_download_behavior(download_dir, pick=None, port=DEFAULT_PORT):
    """把企微的浏览器下载行为指到 download_dir，使点击附件时自动落盘不弹框。
    企微窗口内是带登录态的真实浏览器环境，动态接口/OA 附件经此可下载。

    v1.0.412（用户反馈"抓取文件弹框确认"）：原实现把 Browser.setDownloadBehavior
    发到 page 级 ws_url——该命令只在 browser target 生效，发错层等于没设，企微
    Electron 默认下载流程接管弹「另存为」。现三级尝试：
      ①browser target + Browser 域（正确姿势，全局生效）
      ②page target + Browser 域（旧企微版本可能接受）
      ③page target + Page 域（老 Chromium 兜底）
    任一成功即返回；全失败才报错（调用方降级为点击下载+轮询）。"""
    try:
        os.makedirs(download_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"下载目录创建失败：{e}"}
    params = {"behavior": "allow", "downloadPath": download_dir, "eventsEnabled": True}
    errs = []
    # ① browser target（全局生效，弹框根治的正解）
    bws = _browser_ws_url(port)
    if bws:
        r = _cdp_command(bws, "Browser.setDownloadBehavior", params)
        if not is_err(r):
            return {"ok": True, "download_dir": download_dir, "via": "browser"}
        errs.append(f"browser:{err_msg(r)}")
    else:
        errs.append("browser ws 不可用")
    # ② page target + Browser 域
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    r = _cdp_command(ws_url, "Browser.setDownloadBehavior", params)
    if not is_err(r):
        return {"ok": True, "download_dir": download_dir, "via": "page-browser"}
    errs.append(f"page-browser:{err_msg(r)}")
    # ③ page target + Page 域（老 Chromium 兜底）
    r = _cdp_command(ws_url, "Page.setDownloadBehavior",
                     {"behavior": "allow", "downloadPath": download_dir})
    if not is_err(r):
        return {"ok": True, "download_dir": download_dir, "via": "page-legacy"}
    errs.append(f"page-legacy:{err_msg(r)}")
    return {"ok": False, "error": f"设置浏览器下载目录失败（三种通道均失败）：{'；'.join(errs)}"}


def _decode_name(raw):
    """把服务端给的附件名规范化成能落盘的本地名（v1.0.605，本地实测挖出来的坑）：
    ①RFC 5987 百分号形式 `filename*=UTF-8''%E8%AE%BE…`（实测 OA 就是这个形式，
      只做 latin-1 修复会得到一串 %xx，落到 产物库/下载 就是不可读文件名，
      还会让「同名预删」和跨张的前缀模糊命中口径失准）；
    ②老服务器把中文名按 latin-1 塞进头部 → latin-1 修 utf-8；
    ③只取 basename、去掉 Windows 不允许的冒号（防路径穿越与非法名）。"""
    s = str(raw or "").strip().strip('"').strip("'").strip()
    if not s:
        return ""
    if "%" in s:
        try:
            dec = unquote(s, "utf-8")
            if dec != s:
                s = dec
        except Exception:  # noqa: BLE001
            pass
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        if "\ufffd" not in fixed:
            s = fixed
    except Exception:  # noqa: BLE001
        pass
    return os.path.basename(s.replace("\\", "/")).replace(":", "").strip()


def _name_from_cd(cd):
    """从 Content-Disposition 头部取附件名（已规范化），取不到返回 ""。"""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", str(cd or ""), re.I)
    return _decode_name(m.group(1)) if m else ""


def download_attachment(pick, target, download_dir, port=DEFAULT_PORT, timeout=DOWNLOAD_WAIT_TIMEOUT,
                        allow_click=False):
    """下载窗口内 OA 附件到 download_dir。

    采用「页内 fetch + base64 回传写盘」方案：在带登录态的企微页面上定位附件行
    （OA KM 的 .upload_list_tr，行 id 即附件 fdId），直接 fetch 其 download 接口
    拿到二进制流回传本地写盘——绕开企微对浏览器下载的接管，可靠落盘。

    v1.0.604（用户口径「下载不要弹窗」）：**默认不再回退点击下载**。企微 Electron
    自管下载且无视 CDP 的 Browser/Page.setDownloadBehavior（实测命令返回成功仍弹原生
    「另存为」挂住任务），原靠 save_dialog_auto 事后自动应答，本质仍是"窗口在用户眼前
    闪一遍 + 替用户点保存"。现在页内 fetch 取不到就直接返回结构化错误（silent_only=True），
    由调用方记疑问项；确有需要恢复点击行为时显式传 allow_click=True。"""
    # 0. 准备下载目录
    try:
        os.makedirs(download_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"下载目录创建失败：{e}"}
    # 1. 页内 fetch 下载（首选）——v1.0.425：download_dir 必须下传（http-cookies
    #    通道在 _fetch_download 内部直接落盘；漏传会让 http 通道目录创建失败静默回退）
    fk = _fetch_download(pick, target, port, timeout, download_dir=download_dir)
    if isinstance(fk, dict) and fk.get("ok"):
        # v1.0.361 审查修复（S8）：文件名来自 content-disposition/页面属性（页面可控数据），
        # 含 ..\ 或盘符时可路径穿越写任意文件——统一取 basename 并拒绝空名。
        fname = os.path.basename(str(fk.get("filename") or "").replace("\\", "/")).strip()
        if not fname or fname in (".", ".."):
            fname = "download_%d.bin" % int(time.time())
        # v1.0.425：http-cookies 通道返回时文件已直接落盘（fk["path"]，无 data 键）——
        # 归位到 download_dir/<fname> 即返回；旧 eval 通道仍是内存 data 写盘。
        if fk.get("path") and not fk.get("data"):
            want = os.path.join(download_dir, fname)
            try:
                if os.path.abspath(str(fk["path"])) != os.path.abspath(want):
                    os.replace(fk["path"], want)
                return {"ok": True, "path": want, "filename": fname,
                        "bytes": os.path.getsize(want), "source": fk.get("source", "http-cookies")}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"附件归位失败：{e}", "download_dir": download_dir}
        p = os.path.join(download_dir, fname)
        # v1.0.470（P1-17）：先写 .part 临时文件再原子替换——原直接 open("wb") 写最终
        # 路径，中途崩溃/超时留下截断的半截文件且与旧文件同名无法分辨；重名旧文件
        # 先备份（配合 save_dialog_auto 的备份策略，覆盖不再销毁数据）。
        _bak = None
        try:
            if os.path.isfile(p):
                b, ext = os.path.splitext(p)
                _bak = f"{b}.bak_{int(time.time())}{ext}"
                os.rename(p, _bak)
            tmp = p + f".{os.getpid()}.part"
            with open(tmp, "wb") as f:
                f.write(fk["data"])
            os.replace(tmp, p)
        except Exception as e:  # noqa: BLE001
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "error": f"附件写盘失败：{e}", "download_dir": download_dir}
        return {"ok": True, "path": p, "filename": fname,
                "bytes": os.path.getsize(p), "source": "fetch",
                **({"replaced_backup": _bak} if _bak else {})}
    if is_err(fk):
        _logw(f"[wecom] fetch 下载不可用（{err_msg(fk)}），回退点击下载")
    if not allow_click:
        # v1.0.604：默认止步于静默通道——见函数 docstring（不弹原生「另存为」）
        _why = ""
        if isinstance(fk, dict):
            _why = err_msg(fk)
        _why = _why or "页内 fetch 未取到附件（页面非 OA KM 结构或未找到附件行）"
        _logw(f"[wecom] 附件静默获取失败（{_why[:70]}）——按「不弹窗」策略放弃点击下载回退")
        return {"ok": False, "error": f"附件无法静默获取：{_why[:90]}", "silent_only": True,
                "download_dir": download_dir,
                "hint": "已停用「点击下载」回退（企微必弹原生另存为、打断前台）。"
                        "可 read_wecom_page 取附件直链后用 download_file，或请用户手动下载。"}
    # 2. 回退：设置下载目录 + 点击触发浏览器真实下载
    sb = set_download_behavior(download_dir, pick=pick, port=port)
    if is_err(sb):
        return sb
    try:
        before = set(os.listdir(download_dir))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"下载目录读取失败：{e}"}
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    t = json.dumps(target)
    dl_js = (
        "(() => {"
        "  const t = " + t + ";"
        "  const rows = Array.from(document.querySelectorAll('.upload_list_tr'));"
        "  let row = rows.find(r => (r.textContent||'').includes(t));"
        "  if (!row) return JSON.stringify({ok:false, error:'未找到附件行: '+t});"
        "  const dl = row.querySelector('.upload_opt_down')"
        "            || Array.from(row.querySelectorAll('*')).find(e => (e.textContent||'').trim()==='下载');"
        "  if (!dl) return JSON.stringify({ok:false, error:'该附件行未找到下载按钮'});"
        "  try { dl.scrollIntoView({block:'center'}); } catch(e){}"
        "  dl.click();"
        "  return JSON.stringify({ok:true, filename:t});"
        "})()"
    )
    raw = _cdp_eval_retry(ws_url, dl_js)
    if isinstance(raw, str):
        try:
            dl_res = json.loads(raw)
        except Exception:  # noqa: BLE001
            dl_res = {"ok": False, "error": "附件行定位响应异常"}
    elif is_err(raw):
        dl_res = raw
    else:
        dl_res = {"ok": False, "error": "附件行定位无响应"}
    if isinstance(dl_res, dict) and dl_res.get("ok") is not True:
        clk = click_element(pick, target, port)
        if isinstance(clk, dict):
            if is_err(clk):
                return {"ok": False, "error": f"点击附件失败：{err_msg(clk)}", "download_dir": download_dir,
                        "detail": dl_res.get("error")}
            if clk.get("ok") is False:
                return {"ok": False, "error": f"点击附件失败：{clk.get('error')}", "download_dir": download_dir,
                        "detail": dl_res.get("error")}
    # 3. 轮询等待新文件出现且大小停止增长（下载完成）
    deadline = time.time() + timeout
    last_sizes = None
    while time.time() < deadline:
        try:
            cur = set(os.listdir(download_dir))
        except Exception:  # noqa: BLE001
            cur = set()
        new_files = [f for f in (cur - before) if not f.endswith((".crdownload", ".tmp", ".part"))]
        if new_files:
            sizes = {}
            for f in new_files:
                try:
                    sizes[f] = os.path.getsize(os.path.join(download_dir, f))
                except Exception:  # noqa: BLE001
                    sizes[f] = -1
            if last_sizes == sizes:
                newest = sorted(new_files,
                                key=lambda f: os.path.getmtime(os.path.join(download_dir, f)),
                                reverse=True)[0]
                p = os.path.join(download_dir, newest)
                return {"ok": True, "path": p, "filename": os.path.basename(p),
                        "bytes": os.path.getsize(p),
                        "all": [os.path.join(download_dir, f) for f in sorted(new_files)]}
            last_sizes = sizes
        time.sleep(DOWNLOAD_POLL_STEP)
    return {"ok": False, "error": f"等待下载超时（{timeout}s），未发现新文件落盘。可能附件是预览/在线打开而非直接下载，"
                         "或需点击具体的「下载」按钮", "download_dir": download_dir}


def _fetch_download(pick, target, port=DEFAULT_PORT, timeout=DOWNLOAD_WAIT_TIMEOUT, download_dir=None):
    """页内 fetch 下载 OA 附件（带登录态）：定位附件行 → 拿行 id（fdId）→ fetch download 接口 → base64 回传。

    仅适用于 OA KM 类（附件行含 .upload_list_tr 且行 id 为 fdId）。
    返回 {"ok":True,"data":bytes,"filename":str} 或 {"ok": False, "error":str}。"""
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    t = json.dumps(target)
    # 定位附件行拿 fdId（行 id 即附件唯一标识）
    find_js = (
        "(() => {"
        "  const t = " + t + ";"
        "  const rows = Array.from(document.querySelectorAll('.upload_list_tr'));"
        "  const row = rows.find(r => (r.textContent||'').includes(t));"
        "  if (!row) return JSON.stringify({ok:false, error:'未找到附件行: '+t});"
        "  return JSON.stringify({ok:true, fdId: row.id, name: (row.getAttribute('title')||'') "
        "    || (row.querySelector('.upload_list_filename_title')||{}).textContent || ''});"
        "})()"
    )
    raw = _cdp_eval_retry(ws_url, find_js)
    if is_err(raw):
        return raw
    if isinstance(raw, str):
        try:
            info = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "附件行定位响应异常"}
    else:
        return {"ok": False, "error": "附件行定位无响应"}
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error", "未找到附件行")}
    fd_id = info.get("fdId")
    if not fd_id:
        return {"ok": False, "error": "附件行无 fdId"}
    # v1.0.425：origin 必须取 **OA 页面自身** 的 URL（page dict 的 url 字段）——
    # 之前从 ws_url（ws://127.0.0.1:9222/devtools/...）推导得到的是本地 CDP 端口，
    # real_url 指向 127.0.0.1:9222 必失败且 cookie 匹配也错。
    # v1.0.468：http-cookies 直下通道**摘除**——企微 Electron 的 cookie 存在 CDP
    # 不可见的分区会话（page/browser 级 getCookies 实测均 0），此通道 100% 失败
    # 白耗 ~1.5s/张。分块页内直传为主通道。fetch_via_http 保留（非企微场景可用）。
    # v1.0.425：分块页内直传（替代整段长 JS——frozen exe 里 >1000 字符的 eval
    # 实测偶发 SyntaxError 截断，外部同码正常）。三步全部短 eval：
    #   ① 发起 fetch，字节缓冲挂 window（返回字节数 + content-disposition）
    #   ② 循环每 64KB 出一段 base64（段 JS <250 字符）
    #   ③ 清理 window 缓冲
    # 50MB 上限沿用（O10 内存口径）。
    buf_key = "__wecdl_" + format(int(time.time() * 1000) & 0xFFFFFF, "x") + "_" + format(abs(hash(str(fd_id))) % 9999, "04d")
    start_js = (
        "(async () => {"
        "  try {"
        "    const u = location.origin + '/ekp/sys/attachment/sys_att_main/sysAttMain.do?method=download&fdId=' + encodeURIComponent(" + json.dumps(str(fd_id)) + ");"
        "    const r = await fetch(u, {method:'GET', credentials:'include'});"
        "    if (!r.ok) return JSON.stringify({ok:false, error:'HTTP ' + r.status});"
        "    const cd = r.headers.get('content-disposition') || '';"
        "    const b = await r.arrayBuffer();"
        "    if (b.byteLength > 50*1024*1024) return JSON.stringify({ok:false, error:'附件超过 50MB，页内回传通道不支持'});"
        "    window." + buf_key + " = b;"
        "    return JSON.stringify({ok:true, size:b.byteLength, cd:cd.slice(0,220)});"
        "  } catch(e) { return JSON.stringify({ok:false, error:String(e).slice(0,120)}); }"
        "})()"
    )

    def _buf_cleanup():
        try:
            _cdp_eval(ws_url, "try{delete window." + buf_key + "}catch(e){}; 'ok'", timeout=5)
        except Exception:  # noqa: BLE001
            pass

    res = _cdp_eval(ws_url, start_js, timeout=min(timeout, 30), await_promise=True)
    if is_err(res):
        return res
    if not isinstance(res, str):
        return {"ok": False, "error": "附件下载无响应"}
    try:
        d = json.loads(res)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "附件下载响应异常"}
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error", "附件下载失败")}
    total = int(d.get("size") or 0)
    if total <= 0:
        return {"ok": False, "error": "响应体为空"}
    CH = 65536
    b64_parts = []
    off = 0
    # v1.0.470（P1-17）：分块循环加总超时预算——原实现每块 15s 无总 deadline，
    # 50MB≈800 块串行 eval，页面繁忙时可阻塞小时级。总预算 = timeout 参数（秒）。
    _dl_deadline = time.time() + max(30, min(int(timeout or 90), 300))
    while off < total:
        if time.time() > _dl_deadline:
            _buf_cleanup()
            return {"ok": False, "error": f"附件分块传输超时（已传 {off}/{total} 字节，预算 {int(max(30, min(int(timeout or 90), 300)))}s）——请重试"}
        n = min(CH, total - off)
        chunk_js = (
            "(() => { const u8 = new Uint8Array(window." + buf_key + ", " + str(off) + ", " + str(n) + ");"
            " let s=''; for (let i=0;i<u8.length;i++) s+=String.fromCharCode(u8[i]);"
            " return btoa(s); })()"
        )
        cres = _cdp_eval(ws_url, chunk_js, timeout=15)
        if is_err(cres):
            _buf_cleanup()
            return cres
        if not isinstance(cres, str) or not cres.strip().strip('"'):
            _buf_cleanup()
            return {"ok": False, "error": "分块读取失败"}
        b64_parts.append(cres.strip().strip('"'))
        off += n
    _buf_cleanup()
    try:
        data = base64.b64decode("".join(b64_parts))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"附件解码失败：{e}"}
    cd = d.get("cd") or ""
    # v1.0.605：CD 名字解析收敛到 _name_from_cd（百分号形式 + latin-1 修复 + basename），
    # 原实现只走 latin-1 修复，实测 EKP 的 filename*=UTF-8''%E8%AE%BE… 会得到不可读名
    filename = _name_from_cd(cd) or str(info.get("name") or target or "").strip() or "attachment.bin"
    filename = os.path.basename(filename.replace("\\", "/")).replace(":", "") or "attachment.bin"
    return {"ok": True, "data": data, "filename": filename}


def page_fetch_to_disk(port, pick, url, download_dir, filename=None, timeout=DOWNLOAD_WAIT_TIMEOUT):
    """v1.0.413（免弹框终极方案）：在带登录态的企微页面内 fetch 任意 URL，
    base64 回传写盘——**完全不触发浏览器/Electron 下载流**，从根上消灭「另存为」弹框。

    背景：实测 Browser.setDownloadBehavior 发到 browser 端点返回成功但企微 Electron
    主进程仍自管下载弹框（CDP 管不到主进程 session）。页内 fetch 与页面同源同登录态，
    拿到的就是文件字节流，与"另存为"流程零交集。

    返回 {"ok":True,"path","filename","bytes"} 或 {"ok": False, "error": ...}。50MB 上限（base64
    回传内存峰值 ×5-6，与 _fetch_download 同口径）。"""
    try:
        os.makedirs(download_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"下载目录创建失败：{e}"}
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    u = json.dumps(url)
    fn = json.dumps(filename or "")
    dl_js = (
        "(async () => {"
        "  const u = " + u + ";"
        "  const want = " + fn + ";"
        "  try {"
        "    const r = await fetch(u, {method:'GET', credentials:'include'});"
        "    if (!r.ok) return JSON.stringify({ok:false, error:'HTTP ' + r.status});"
        "    const ct = r.headers.get('content-type') || '';"
        "    if (/text\\/html/i.test(ct)) return JSON.stringify({ok:false, error:'返回的是 HTML 页面而非文件（登录墙或链接错误）'});"
        "    const cd = r.headers.get('content-disposition') || '';"
        "    let fn = '';"
        "    const m = cd.match(/filename\\*?=(?:UTF-8'')?\"?([^\";]+)/i);"
        "    if (m) { try { fn = decodeURIComponent(escape(m[1])); } catch(e){ fn = m[1]; } }"
        "    if (!fn && want) fn = want;"
        "    if (!fn) { try { fn = decodeURIComponent((new URL(u)).pathname.split('/').pop()) || 'download.bin'; } catch(e){ fn = 'download.bin'; } }"
        "    const buf = await r.arrayBuffer();"
        "    const bytes = new Uint8Array(buf);"
        "    if (bytes.length > 50*1024*1024) return JSON.stringify({ok:false, error:'文件超过 50MB，页内回传通道不支持'});"
        "    if (bytes.length < 8) return JSON.stringify({ok:false, error:'响应体过小（' + bytes.length + 'B），疑似空响应'});"
        "    let bin = '';"
        "    const CH = 0x8000;"
        "    for (let i=0;i<bytes.length;i+=CH) bin += String.fromCharCode.apply(null, bytes.subarray(i, i+CH));"
        "    return JSON.stringify({ok:true, b64: btoa(bin), fn: fn, size: bytes.length});"
        "  } catch(e) { return JSON.stringify({ok:false, error: String(e).slice(0,200)}); }"
        "})()"
    )
    res = _cdp_eval(ws_url, dl_js, timeout=timeout, await_promise=True)
    if is_err(res):
        return res
    if not isinstance(res, str):
        return {"ok": False, "error": "页内 fetch 无响应"}
    try:
        d = json.loads(res)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "页内 fetch 响应异常"}
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error", "页内 fetch 失败")}
    try:
        data = base64.b64decode(d.get("b64", ""))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"附件解码失败：{e}"}
    # v1.0.605：调用方显式给的文件名**优先**于服务端 Content-Disposition——批审按正文
    # 抓到的名字做「同名预删」与跨张的前缀匹配，被头部名盖掉就会口径失准（实测 EKP 头部
    # 是 filename*=UTF-8''%E8%AE%BE… 百分号形式，直接用会落成不可读名）。两条都过一遍规范化。
    fname = _decode_name(filename) or _decode_name(d.get("fn"))
    if not fname or fname in (".", ".."):
        fname = "download_%d.bin" % int(time.time())
    p = os.path.join(download_dir, fname)
    try:
        with open(p, "wb") as f:
            f.write(data)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"写盘失败：{e}"}
    return {"ok": True, "path": p, "filename": fname, "bytes": os.path.getsize(p), "source": "page-fetch"}


def find_attachment_url(port, pick, filename_hint):
    """v1.0.413：在页面 DOM 里按文件名提示找附件的真实下载 href（供 page_fetch_to_disk 用）。
    匹配 a[href] 的 text/title/download/href 含提示名或附件扩展名。返回绝对 URL 或 None。"""
    ws_url, page = _page_ws(pick, port)
    if is_err(page) or not ws_url:
        return None
    t = json.dumps(filename_hint or "")
    js = (
        "(() => {"
        "  const hint = " + t + ";"
        "  const ext = /\\.(xlsx|xls|doc|docx|pdf|zip|rar|7z|pptx?)(\\?|$)/i;"
        "  const as = Array.from(document.querySelectorAll('a[href]'));"
        "  let best = null;"
        "  const norm = s => (s||'').replace(/\\s+/g,'');"
        "  for (const a of as) {"
        "    const h = a.href || '';"
        "    if (!h || h === '#' || /^javascript:/i.test(h)) continue;"
        "    const txt = norm(a.innerText || a.textContent);"
        "    const ttl = norm(a.title || a.getAttribute('download') || '');"
        "    const nh = norm(h);"
        "    const hitName = hint && (txt.includes(norm(hint)) || ttl.includes(norm(hint)) || nh.includes(norm(hint)));"
        "    const hitExt = ext.test(h) || ext.test(txt);"
        "    if (hitName && hitExt) return JSON.stringify({ok:true, url:h, score:3});"
        "    if (hitName) best = best || JSON.stringify({ok:true, url:h, score:2});"
        "    if (!hint && hitExt && !best) best = JSON.stringify({ok:true, url:h, score:1});"
        "  }"
        "  return best || JSON.stringify({ok:false, error:'页面无匹配的附件链接'});"
        "})()"
    )
    try:
        raw = _cdp_eval_retry(ws_url, js)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, str):
        return None
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not d.get("ok"):
        return None
    url = d.get("url") or ""
    if url.startswith("//"):
        url = "https:" + url
    return url or None


def _get_page_cookies(port, pick, url):
    """v1.0.425：CDP Network.getCookies 取指定 URL 的登录态 cookie 字典。
    用于 Python 侧直接下载 OA 附件（绕开页内 eval 的长 JS 限制——frozen exe
    里长 expression 实测偶发 SyntaxError 截断）。返回 {name: value} 或 {}。"""
    ws_url, page = _page_ws(pick, port)
    if not ws_url:
        return {}
    try:
        ws = _open_ws(ws_url, 8)
        if isinstance(ws, dict):
            return {}
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))
            ws.send(json.dumps({"id": 2, "method": "Network.getCookies",
                                "params": {"urls": [url]}}))
            deadline = time.time() + 8
            while time.time() < deadline:
                ws.settimeout(max(0.5, deadline - time.time()))
                msg = json.loads(ws.recv())
                if msg.get("id") == 2:
                    out = {}
                    for c in (msg.get("result", {}) or {}).get("cookies", []):
                        out[c.get("name", "")] = c.get("value", "")
                    return out
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        _logw(f"[wecom] 取 cookie 失败：{e}")
    return {}


def fetch_via_http(port, pick, url, download_dir, filename=None, timeout=DOWNLOAD_WAIT_TIMEOUT):
    """v1.0.425：Python 侧带登录态 cookie 直接 GET 附件 URL 并写盘——
    零页内 eval（无长 JS 截断风险）、零 base64 回传（无 50MB 限制）、零弹框。"""
    try:
        os.makedirs(download_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"下载目录创建失败：{e}"}
    cookies = _get_page_cookies(port, pick, url)
    if not cookies:
        return {"ok": False, "error": "未取得 OA 登录态 cookie"}
    origin = url.split("/")[0] + "//" + url.split("/")[2] if url.startswith("http") else ""
    headers = {"Referer": origin + "/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64)"}
    try:
        r = requests.get(url, cookies=cookies, headers=headers,
                         timeout=(5, timeout), stream=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"请求失败：{str(e)[:100]}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    ctype = (r.headers.get("content-type") or "").lower()
    if "text/html" in ctype:
        return {"ok": False, "error": "返回 HTML 页面而非文件（登录失效或链接错误）"}
    cd = r.headers.get("content-disposition") or ""
    fname = _decode_name(filename) or _name_from_cd(cd)     # v1.0.605：与页内直传同一套名字规范化
    if not fname:
        try:
            fname = unquote(os.path.basename(urlparse(url).path)) or "download.bin"
        except Exception:  # noqa: BLE001
            fname = "download.bin"
    fname = os.path.basename(fname.replace("/", "\\").replace(":", "")) or "download.bin"
    out = os.path.join(download_dir, fname)
    total = 0
    try:
        with open(out, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                total += len(chunk)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"写盘失败：{e}"}
    if total < 8:
        return {"ok": False, "error": f"响应体过小（{total}B），疑似空响应"}
    return {"ok": True, "path": out, "filename": fname, "bytes": total, "source": "http-cookies"}


def upload_file(pick, target, filepath, port=DEFAULT_PORT):
    """通过 CDP DOM.setFileInputFiles 把本地文件塞进页面 <input type=file>，实现 OA 附件上传。"""
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    if not os.path.exists(filepath):
        return {"ok": False, "error": f"本地文件不存在：{filepath}"}
    t = json.dumps(target)
    get_expr = (
        "(() => {"
        "  const t = " + t + ";"
        "  const els = Array.from(document.querySelectorAll('input[type=\"file\"]'));"
        "  let idx = (typeof t === 'number') ? t : -1;"
        "  if (idx < 0) { const tx = String(t); idx = els.findIndex(e => ((e.getAttribute('aria-label')||'')+' '+(e.name||'')).includes(tx)); }"
        "  if (idx < 0 || !els[idx]) return {__noid__:true, error:'文件输入框未找到: '+t};"
        "  return els[idx];"
        "})()"
    )
    try:
        ws = _open_ws(ws_url, CDP_UPLOAD_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        _logw(f"[wecom] 打开 CDP 会话失败：{e}")
        ws = {"ok": False, "error": "CDP 连接失败(未知原因)"}
    if isinstance(ws, dict):
        return ws
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable", "params": {}}))
        ws.send(json.dumps({"id": 2, "method": "DOM.enable", "params": {}}))
        ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate",
                            "params": {"expression": get_expr, "returnByValue": False, "awaitPromise": False}}))
        msg = _recv_until(ws, 3)
        if msg is None:
            return {"ok": False, "error": "获取文件输入框超时（可能是隐藏的 input，需先点击「上传」按钮展开）"}
        if "error" in msg:
            return {"ok": False, "error": f"获取文件输入框失败：{msg['error']}"}
        object_id = msg.get("result", {}).get("result", {}).get("objectId")
        if not object_id:
            return {"ok": False, "error": "未能取得文件输入框对象（可能是隐藏的 input，需先点击「上传」按钮展开）"}
        ws.send(json.dumps({"id": 4, "method": "DOM.setFileInputFiles",
                            "params": {"files": [filepath], "objectId": object_id}}))
        ack = _recv_until(ws, 4)
        if ack is None:
            # v1.0.361 审查修复（P14）：4s 未应答与"提交成功但无确认"无法区分——
            # 原实现当成功返回会让上层带着空 input 继续提交表单。改为失败可重试。
            return {"ok": False, "error": "设置文件超时（CDP 未应答）——可能上传控件未展开或页面失联，请重试一次"}
        if "error" in ack:
            return {"ok": False, "error": f"设置文件失败：{ack['error']}"}
        return {"ok": True, "file": filepath, "name": os.path.basename(filepath)}
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass  # silent-ok: 会话关闭失败不致命，底层连接会被自动回收

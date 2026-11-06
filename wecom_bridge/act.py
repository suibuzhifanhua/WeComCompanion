"""页面操作：滚动 / 下拉选择 / 链接枚举 / 文档编辑。"""

import json

from .cdp_client import DEFAULT_PORT, _cdp_eval, _cdp_eval_retry, _json_result, _json_ok_result
from .windows import _require_page
from core.result import is_err

def scroll_window(pick=None, direction="down", amount=800, port=DEFAULT_PORT):
    """滚动窗口以露出更多内容（长试卷/长文档）。direction: up/down/top/bottom。"""
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    d = json.dumps(direction)
    a = int(amount)
    expr = (
        "(() => {"
        "  const dir = " + d + "; const amt = " + str(a) + ";"
        "  if (dir === 'top') window.scrollTo(0,0);"
        "  else if (dir === 'bottom') window.scrollTo(0, document.body.scrollHeight);"
        "  else window.scrollBy(0, (dir==='up' ? -amt : amt));"
        "  return JSON.stringify({scrollY: Math.round(window.scrollY), scrollHeight: Math.round(document.body.scrollHeight)});"
        "})()"
    )
    raw = _cdp_eval(ws_url, expr)
    if isinstance(raw, str):
        return _json_result(raw, fail=lambda r, e: {"ok": True, "raw": r},
                            empty={"ok": True})   # v1.0.654 A5
    return _json_result(raw, empty={"ok": True})


def select_option(pick, target, value="", port=DEFAULT_PORT):
    """设置下拉(selected)/单选(radio)/勾选(checkbox)。target 为序号(整数)或文字匹配。
    value：select 时为选项值/文字；checkbox 时 'true'/'false'。"""
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    t = json.dumps(target)
    v = json.dumps(value)
    # 回读验证：写入后 150ms 回读 select.value / radio|checkbox.checked 比对，
    # 防 React 受控组件吞选择。verify:false 时模型应重试或改方式。
    expr = (
        "new Promise(resolve => {"
        "  const t = " + t + "; const val = " + v + ";"
        "  const tx = String(t);"
        "  const els = Array.from(document.querySelectorAll('input,select'));"
        "  let idx = (typeof t === 'number') ? t : -1;"
        "  if (idx < 0) {"
        "    idx = els.findIndex(e => {"
        "      const lab = ((e.getAttribute('aria-label')||'') + ' ' + (e.placeholder||'') + ' ' + (e.name||'') + ' ' + ((e.closest('label')&&e.closest('label').innerText)||'')).trim();"
        "      return lab.includes(tx) || (e.name||'').includes(tx);"
        "    });"
        "  }"
        "  if (idx < 0 || !els[idx]) { resolve(JSON.stringify({ok:false, error:'未找到元素: '+tx})); return; }"
        "  const el = els[idx];"
        "  if (el.tagName.toLowerCase() === 'select') { el.value = val; el.dispatchEvent(new Event('change',{bubbles:true})); el.dispatchEvent(new Event('input',{bubbles:true}));"
        "    setTimeout(() => { const got = el.value||''; resolve(JSON.stringify({ok:true, set_select:got, verify:(String(got).trim()===String(val).trim()), got:String(got).slice(0,60), want:String(val).slice(0,60)})); }, 150); return; }"
        "  if (el.type === 'radio') { if (!el.checked) el.click();"
        "    setTimeout(() => { resolve(JSON.stringify({ok:true, radio:true, checked:el.checked, value:el.value})); }, 150); return; }"
        "  if (el.type === 'checkbox') { const want = (val==='true'||val===true); if (el.checked !== want) el.click();"
        "    setTimeout(() => { const got = el.checked; resolve(JSON.stringify({ok:true, checkbox:true, checked:got, verify:(got===want)})); }, 150); return; }"
        "  resolve(JSON.stringify({ok:false, error:'该元素不是 select/radio/checkbox'}));"
        "})"
    )
    # 间歇性失败重试：页面繁忙/React 重渲染时 evaluate 偶发返回空（no response），重试可命中
    # 注：div 型选项（MUI 等无 input/select）的文字回退点击已由 click_element 覆盖，这里不重复
    raw = _cdp_eval(ws_url, expr, await_promise=True)
    if isinstance(raw, str):
        return _json_ok_result(raw)   # v1.0.654 A5：解析单源
    return _json_ok_result(raw)


# v1.0.405（M2）：改名 _LINKS_SIMPLE_JS——与 read.py 的 _LINKS_INDEXED_JS 原同名
# `_LINKS_JS` 但实现不同（本版：80 字截断/识别附件扩展名/不去重/返回纯数组），
# 同名异义易误改一处漏一处。索引自增版（click 数字索引空间）在 read.py。
_LINKS_SIMPLE_JS = r"""
(() => {
  const links = [];
  document.querySelectorAll('a[href]').forEach((a) => {
    const h = a.href || '';
    const t = (a.innerText||a.textContent||'').trim().slice(0,80);
    if (t || /\.(pdf|xlsx?|docx?|csv|zip|rar|7z|pptx?|png|jpe?g|gif|txt)(\?|$)/i.test(h)) {
      links.push({text:t, href:h});
    }
  });
  return JSON.stringify(links.slice(0, 300));
})()
"""


def list_links(pick=None, port=DEFAULT_PORT):
    """提取窗口内所有链接（含可下载文件链接），供 OA 文件下载等场景。"""
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    raw = _cdp_eval(ws_url, _LINKS_SIMPLE_JS)
    if isinstance(raw, str):
        try:
            return {"title": page.get("title"), "url": page.get("url"), "links": json.loads(raw)}
        except Exception:  # noqa: BLE001
            return {"title": page.get("title"), "url": page.get("url"), "links": [], "raw": raw}
    return {"title": page.get("title"), "url": page.get("url"), "links": raw or []}


def run_js(pick=None, expression="", port=DEFAULT_PORT):
    """在指定窗口执行一段 JavaScript（v1.0.260）：expression 为函数体语句，包成 IIFE
    执行并返回其 return 值。用于 click/read 工具覆盖不到的页面操作——典型场景：
    学习视频直接拖到结尾（const v=document.querySelector('video'); if(!v) return 'no video';
    v.currentTime = v.duration - 3; return JSON.stringify({cur:Math.round(v.currentTime), dur:Math.round(v.duration)});）、
    读取 video 播放状态、检测弹窗/按钮是否存在等。结果 JSON 序列化返回。"""
    if not expression or not str(expression).strip():
        return {"ok": False, "error": "expression 不能为空"}
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    expr = "(() => {\n" + str(expression) + "\n})()"
    raw = _cdp_eval(ws_url, expr)
    if is_err(raw):
        return raw
    if isinstance(raw, dict):
        val = raw.get("result", {}).get("value")
    else:
        val = raw
    try:
        val = json.loads(val) if isinstance(val, str) and val[:1] in ("{", "[", '"') else val
    except Exception:  # noqa: BLE001  非 JSON 字符串原样返回
        pass
    return {"ok": True, "result": val, "page": page}


def type_document(pick, text, port=DEFAULT_PORT):
    """向窗口内最大的可编辑区域(contenteditable，如在线文档正文)末尾插入文本。"""
    _err, ws_url, page = _require_page(pick, port)   # v1.0.654 A6：三连样板收敛
    if _err:
        return _err
    v = json.dumps(text)
    expr = (
        "(() => {"
        "  const val = " + v + ";"
        "  const eds = Array.from(document.querySelectorAll('[contenteditable=\"true\"],[contenteditable=\"\"]')).filter(e => e.offsetHeight > 80);"
        "  if (!eds.length) return JSON.stringify({ok:false, error:'未找到可编辑的文档区域(contenteditable)'});"
        "  let el = eds.sort((a,b)=>b.innerText.length - a.innerText.length)[0];"
        "  el.focus();"
        "  try { const r=document.createRange(); r.selectNodeContents(el); r.collapse(false); const s=window.getSelection(); s.removeAllRanges(); s.addRange(r); } catch(e){}"
        "  document.execCommand('insertText', false, val);"
        "  el.dispatchEvent(new Event('input',{bubbles:true}));"
        "  return JSON.stringify({ok:true, doc_len:el.innerText.length});"
        "})()"
    )
    # 间歇性失败重试：页面繁忙/React 重渲染时 evaluate 偶发返回空（no response），重试可命中
    raw = _cdp_eval_retry(ws_url, expr)
    if isinstance(raw, str):
        return _json_ok_result(raw)   # v1.0.654 A5：解析单源
    return _json_ok_result(raw)

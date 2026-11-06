"""企微 CDP 窗口交互（页面 WS 取用 / 互动元素扫描）+ 全量兼容再导出。

v1.0.656 大文件拆分：目标枚举/选择/页面生命周期 → targets.py；截图 → screenshot.py；
OS 窗口域（webhook/窗口树/Win32/收纳授权）→ os_windows.py；新窗口后台化 → tucker.py。
本模块保留窗口交互域实现，并把上述模块的全部公开/私有名**原样再导出**回
wecom_bridge.windows 命名空间——既有 `from .windows import X`、`monkeypatch.setattr
(wecom_bridge.windows, "X")` 一字不改继续工作。拆出模块内对这些名字的运行期引用
统一走 `from . import windows as _w` 的动态属性，与拆分前「模块内直调」语义一致。
"""

# v1.0.324 启动提速 + v1.0.364 M8 收敛：requests 懒加载代理实现收敛到 core_utils
from core_utils import _lazy_requests as requests
import ctypes
import json
import os
import threading
import time

from .cdp_client import DEFAULT_PORT, CDP_EVAL_TIMEOUT, CDP_SCREENSHOT_TRY_TIMEOUT, _logw, _open_ws, _recv_until, _cdp_eval, _wait_body_stable, pool_prune_dead, _json_result
from core.result import is_err, err_msg

# ---------- 5. 窗口交互（agent 操作窗口内容） ----------
def _page_ws(pick=None, port=DEFAULT_PORT):
    """返回 (page webSocketDebuggerUrl, page dict)，无可用窗口则 (None,None)。
    若 CDP 本身不可用，第二项为 {"ok": False, "error": ...} 以便上层透传真实原因。"""
    targets = list_targets(port)
    if is_err(targets):
        return None, {"ok": False, "error": err_msg(targets)}
    page = pick_window(targets, pick)
    if not page:
        return None, None
    return page.get("webSocketDebuggerUrl"), page


def _require_page(pick, port, with_windows=False):
    """取页面 WS 并做统一错误检查。返回 (err, ws_url, page)：err 非 None 时调用方
    应直接 return err；否则用 ws_url/page 继续。
    v1.0.654 A6：收敛 act/read/download/windows 多处逐字相同的
    "_page_ws 三连"（取页→__error__ 透传→无窗口兜底）样板。
    with_windows=True 时兜底错误附 window_list_summary（诊断用）。"""
    ws_url, page = _page_ws(pick, port)
    if is_err(page):
        return page, None, None
    if not ws_url:
        err = {"ok": False, "error": "没有可用的企微窗口"}
        if with_windows:
            err["windows"] = window_list_summary(port)
        return err, None, None
    return None, ws_url, page


# 统一候选扫描函数（JS）：标准可交互元素 + 可点击文本块/选项候选。
# 答题页选项常以 div/span 纯文本块呈现（无 button/role/pointer 光标），标准选择器抓不到，
# 导致元素列表缺选项、agent 用序号点击越界失败。这里把「叶子文本块」且具备可点击特征
# （pointer 光标 / onclick 绑定 / 选项式短文本如 "A. xxx"）的节点并入候选列表，
# 使 get_interactive_elements 的 index 与 click_element 的数字/文字索引完全对齐。
_BLOCK_SCAN_JS = r"""
  function __blockCandidates() {
    // P1-4: 补充 MUI / AntD / ElementUI / Radix / 自定义组件 class/role
    const extraSel = [
      '[class*="MuiButton"]','[class*="MuiIconButton"]','[class*="MuiTab"]',
      '[class*="MuiListItemButton"]','[class*="MuiChip"]','[class*="MuiFab"]',
      '[class*="MuiToggleButton"]',
      '[class*="ant-btn"]','[class*="ant-dropdown"]','[class*="ant-menu-item"]',
      '[class*="el-button"]','[class*="el-menu-item"]','[class*="el-tab"]',
      '[class*="el-dropdown"]',
      '[role="tab"],[role="menuitem"],[role="menu"],[role="checkbox"],[role="radio"]',
      '[role="treeitem"],[role="tree"]','[role="option"],[role="combobox"]',
      '[role="switch"]',
    ].join(',');
    const sel = 'a,button,input,textarea,select,label,[role="button"],[contenteditable="true"],[contenteditable=""]';
    const base = Array.from(document.querySelectorAll(sel + ',' + extraSel));
    const seenSigs = new Set(base.map(e => {
      const _t = ((e.innerText||e.textContent||'') + ' ' + (e.getAttribute('aria-label')||'')).trim();
      return (e.tagName||'').toLowerCase() + '|' + _t + '|' + (e.name||'') + '|' + (e.type||'');
    }));
    const optRe = /^[A-Ha-h][\s]*[\.、．:：)）]?\s*\S/;
    for (const el of document.querySelectorAll('div,span,label,li')) {
      if (el.children && el.children.length > 0) continue;          // 只要叶子文本块，避免容器噪音
      const txt = (el.innerText || el.textContent || '').trim();
      if (!txt || txt.length > 60) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      let cs = null; try { cs = window.getComputedStyle(el); } catch(e){}
      if (cs && (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity||'1') === 0)) continue;
      // P1-4: MUI/AntD 等组件可能不设 cursor:pointer，改用 className 判断
      const className = (el.className||'') + ' ' + (el.getAttribute('class')||'');
      const isMUI = /MuiButton|MuiIconButton|MuiTab|MuiChip|MuiFab|MuiToggle|MuiListItem/i.test(className);
      const isAntD = /ant-btn|ant-dropdown|ant-menu|ant-tabs|ant-pagination|ant-checkbox|ant-radio/i.test(className);
      const isEl = /el-button|el-menu|el-tab|el-pagination|el-checkbox|el-radio/i.test(className);
      const clickable = (cs && cs.cursor === 'pointer') || !!el.onclick || !!el.getAttribute('onclick')
                       || optRe.test(txt) || isMUI || isAntD || isEl;
      if (!clickable) continue;
      const sig = (el.tagName||'').toLowerCase() + '|' + txt;
      if (seenSigs.has(sig)) continue;
      seenSigs.add(sig);
      base.push(el);
    }
    return base;
  }
"""


_INTERACTIVE_JS = r"""
(() => {
__BLOCK_SCAN_JS__
  const els = __blockCandidates();
  const seen = new Set();
  const out = [];
  els.forEach((el, i) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;            // 跳过零尺寸(不可见)
    let cs = null;
    try { cs = window.getComputedStyle(el); } catch(e){}
    if (cs && (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity||'1') === 0)) return;  // 跳过隐藏元素
    const tag = el.tagName.toLowerCase();
    const text = ((el.innerText||el.textContent||'') + ' ' + (el.getAttribute('aria-label')||'')).trim().slice(0,60);
    const placeholder = el.placeholder || el.getAttribute('aria-label') || '';
    const name = el.name || el.getAttribute('name') || '';
    const type = el.type || '';
    const value = (el.value || '').slice(0,60);
    const sig = tag+'|'+text+'|'+name+'|'+type+'|'+value+'|'+placeholder;
    if (seen.has(sig)) return;                              // 去重
    seen.add(sig);
    out.push({index:i, tag, text, type, name, placeholder, value});
    if (out.length >= 400) return;
  });
  return JSON.stringify(out);
})()
""".replace("__BLOCK_SCAN_JS__", _BLOCK_SCAN_JS)


def get_interactive_elements(pick=None, port=DEFAULT_PORT):
    """列出某窗口的可点击/可输入元素，供 agent 定位操作目标。"""
    _err, ws_url, page = _require_page(pick, port, with_windows=True)   # v1.0.654 A6
    if _err:
        return _err
    raw = _cdp_eval(ws_url, _INTERACTIVE_JS)
    if is_err(raw):
        return {"ok": False, "error": err_msg(raw), "title": page.get("title"), "url": page.get("url"), "elements": []}
    data = _json_result(raw, empty=[])   # v1.0.654 A5：解析单源
    return {"title": page.get("title"), "url": page.get("url"),
            "elements": data if isinstance(data, list) else []}


# ---------- 兼容再导出（v1.0.656 拆分）：全部旧名挂回 windows 命名空间 ----------
# 注意：下列名字是**同名绑定**（非函数包装）——`monkeypatch.setattr(wecom_bridge.windows,
# "X", stub)` 对「windows 内部调用」与「windows as _wins 的属性访问」全局生效，
# 与拆分前模块内直调语义一致；`from .windows import X` 的消费方本就是绑定引用，
# 其 patch 口径（打消费模块）拆分前后不变。
from .targets import (
    _tgt_cache, list_targets, _pages, target_pick, pick_window,
    _AUTO_PICK_BY_SCOPE, _AUTO_PICK_LOCK, _PICK_CTX, _DEFAULT_SCOPE,
    set_auto_pick_scope, _pick_scope, current_pane_id, _get_auto_pick, _set_auto_pick,
    close_page, open_silent_page, _scan_new_page, _find_new_page, wait_page_load,
)
from .screenshot import (
    _CAPTURE_JS, capture, capture_screenshot, _DOM_RENDER_JS, _dom_render_screenshot,
)
from .os_windows import (
    WEBHOOK_MSGTYPES, send_webhook, _fetch_target_parents, window_list_summary, window_tree_text,
    _stage_guard, _enum_windows, list_os_window_titles, process_session_info, _title_match,
    new_window_baseline, foreground_hwnd, restore_foreground, minimize_windows_by_title,
    _wxwork_visible_hwnds, _minimize_hwnds, _title_path_ok,
    _OPEN_MARK_TTL, _OPEN_MARK_MAX, _tuck_lock, _open_marks, _unbound_warned, _bind_probe,
    note_agent_open, _pop_open_marks, _peek_open_marks,
)
from .tucker import (
    _WIN32, _LEARN_URL_MARKS, KEEP_URL_MARKS, _SILENT_KEEP_URL_MARKS,
    _effective_keep_marks, _effective_learn_marks,
    _BUSY_GRACE_S, _last_agent_active, _agent_busy, _TUCKER_STARTED, _silent_mode,
    window_enum_probe, _tuck_binding_ok, _target_title_for, _fresh_hwnds, _tuck_fresh_hwnds,
    _tucker_tick, start_window_tucker, tuck_new_windows,
)

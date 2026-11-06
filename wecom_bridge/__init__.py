"""
wecom_bridge — 企业微信 CDP 桥接包（原 wecom_bridge.py 就地升级）
====================================================================
对外接口与原模块完全一致：import wecom_bridge as wb 后所有旧符号
（wb.ensure_binding / wb.capture / wb.read_fields / wb.send_webhook 等）可用。

T02 结构（按原文件顺序切分）：
  cdp_client.py — CDP 通信内核（常量/日志注入/会话开关/eval/页面就绪等待）
  wecom_proc.py — 企微进程检测与 CDP 绑定（ensure_binding 等）
  windows.py    — 窗口发现与选取（枚举/截图/webhook/窗口树/互动元素扫描）
  read.py       — 页面读取与交互（点击/输入/读页/读表单字段）
  act.py        — 富操作（滚动/下拉选择/链接枚举/文档编辑）
  download.py   — 浏览器真实下载与上传
"""
from . import cdp_client  # __getattr__ 里 wb._log 的活引用需要模块对象本身
from .cdp_client import (
    DEFAULT_PORT, CDP_CONNECT_TIMEOUT, CDP_EVAL_TIMEOUT, CDP_UPLOAD_TIMEOUT,
    CDP_SCREENSHOT_TRY_TIMEOUT, DEVNULL, CDP_FLAGS,
    set_logger, _logw, CREATE_NO_WINDOW, _hide_startupinfo, _run_hidden,
    cdp_up, port_bindable, _cdp_ws_ok, _open_ws, _recv_until, _cdp_eval, _cdp_eval_retry,
    _wait_dom_ready, _BODY_LEN_JS, _wait_body_stable, _cdp_command,
)
from .wecom_proc import (
    detect_wecom_exe, find_wecom_pids, _find_wecom_pids_winapi,
    _find_wecom_pids_tasklist, windows_signature, _relaunch_with_cdp,
    ensure_binding,
)
from . import windows  # _AUTO_PICK 活引用需要模块对象（见下方 __getattr__）
from .windows import (
    list_targets, _pages, pick_window, target_pick, wait_page_load, _CAPTURE_JS, capture,
    capture_screenshot, _DOM_RENDER_JS, _dom_render_screenshot, WEBHOOK_MSGTYPES,
    send_webhook, _fetch_target_parents, window_list_summary, window_tree_text,
    _page_ws, _BLOCK_SCAN_JS, _INTERACTIVE_JS,
    get_interactive_elements, _get_auto_pick, _set_auto_pick, set_auto_pick_scope,
    _find_new_page,
    close_page, open_silent_page,
    list_os_window_titles, _title_match, minimize_windows_by_title,
    start_window_tucker, process_session_info, _stage_guard, window_enum_probe,
)
from .read import (
    click_element, type_text, _READ_PAGE_JS, read_page,
    _READ_FIELDS_JS, read_fields,
)
from .act import (
    scroll_window, select_option, _LINKS_SIMPLE_JS, list_links, type_document, run_js,
)
# v1.0.653 瘦身：_LINKS_JS 兼容别名删除（零引用；act/read 两版链接脚本已改名 _LINKS_SIMPLE_JS 等）
from .download import (
    DOWNLOAD_WAIT_TIMEOUT, DOWNLOAD_POLL_STEP, set_download_behavior,
    download_attachment, _fetch_download, upload_file,
    page_fetch_to_disk, find_attachment_url,
    fetch_via_http, _get_page_cookies,
)

# _log / _AUTO_PICK 活引用（PEP 562）：与原始单模块行为一致——set_logger 注入后
# wb._log 始终返回 cdp_client 内当前值，而非 import 时的快照。
# v1.0.588：_AUTO_PICK 同理必须走活引用——它随点击/关页频繁变化，`from .windows
# import _AUTO_PICK` 只会拿到 import 那一刻的 None，导致 agent/loop.py 的跟随窗提示
# 与 batch_flow._find_follow_pick 长期读到 None（功能静默失效）。
# 新代码请直接调用 _get_auto_pick()。
def __getattr__(name):
    if name == "_log":
        return cdp_client._log
    if name == "_AUTO_PICK":
        return windows._get_auto_pick()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return list(globals().keys()) + ["_log", "_AUTO_PICK"]


__all__ = [
    "DEFAULT_PORT", "CDP_CONNECT_TIMEOUT", "CDP_EVAL_TIMEOUT", "CDP_UPLOAD_TIMEOUT",
    "CDP_SCREENSHOT_TRY_TIMEOUT", "DEVNULL", "CDP_FLAGS",
    "_log", "set_logger", "_logw", "CREATE_NO_WINDOW", "_hide_startupinfo",
    "_run_hidden", "cdp_up", "port_bindable", "_cdp_ws_ok", "_open_ws", "_recv_until", "_cdp_eval",
    "_cdp_eval_retry", "_wait_dom_ready", "_BODY_LEN_JS", "_wait_body_stable",
    "_cdp_command",
    "detect_wecom_exe", "find_wecom_pids", "_find_wecom_pids_winapi",
    "_find_wecom_pids_tasklist", "windows_signature", "_relaunch_with_cdp",
    "ensure_binding", "list_targets", "_pages", "pick_window", "target_pick", "wait_page_load",
    "_CAPTURE_JS", "capture", "capture_screenshot", "_DOM_RENDER_JS",
    "_dom_render_screenshot", "WEBHOOK_MSGTYPES", "send_webhook",
    "_fetch_target_parents", "window_list_summary", "window_tree_text",
    "_page_ws", "_BLOCK_SCAN_JS", "_INTERACTIVE_JS",
    "get_interactive_elements", "_AUTO_PICK", "_get_auto_pick", "_set_auto_pick",
    "set_auto_pick_scope", "_find_new_page", "close_page", "open_silent_page", "click_element", "type_text", "_READ_PAGE_JS",
    "read_page", "_READ_FIELDS_JS", "read_fields", "scroll_window",
    "select_option", "list_links", "type_document", "run_js",
    "DOWNLOAD_WAIT_TIMEOUT", "DOWNLOAD_POLL_STEP", "set_download_behavior",
    "download_attachment", "_fetch_download", "upload_file",
    # v1.0.653 瘦身：stdlib 模块名（json/os/...）与幽灵项 requests 不再进 __all__
    # ——全仓零 wb.<stdlib> 消费，from wecom_bridge import * 反而会因 requests 不存在而炸。
]

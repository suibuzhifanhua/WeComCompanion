"""企微 CDP 目标枚举 / 窗口选择 / 页面生命周期（v1.0.656 自 windows.py 拆出）。

兼容口径：对 windows 命名空间的运行期引用统一走 `_w.<name>` 动态属性
（`from . import windows as _w`），保证 monkeypatch.setattr(wecom_bridge.windows, "X")
与拆分前「windows.py 模块内直调」语义完全一致（含本模块内互相调用）。
"""

import threading
import time

from .cdp_client import DEFAULT_PORT, pool_prune_dead
from . import windows as _w
from core.result import is_err, err_msg

# ---------- 4. 枚举 / 抓取 ----------
_tgt_cache = {"ts": 0.0, "data": None}


def list_targets(port=DEFAULT_PORT, force_refresh=False):
    """枚举 CDP target。HTTP 瞬时抖动时重试；最终失败返回 {"ok": False, "error": ...}。
    v1.0.425：timeout 改 (连接1s, 读2s) 元组——企微 CDP 服务偶发 accept 后不响应，
    标量 timeout 的读超时语义可能拖长等待（实测 _open_detail 在 list_targets 挂 3 分钟+）。
    v1.0.425b（2026-10-05 实证）：/json 端点会**间歇停摆数秒~数分钟**（WS 不受影响），
    期间所有走 /json 的工具全灭。加 2s TTL 缓存 + 失败回退最近一次成功值——停摆期
    工具用旧列表继续干活（target 开关少一瞬间，旧列表基本准确），彻底摆脱对 /json
    的实时依赖。
    v1.0.652（审计 P1-11）：force_refresh=True 跳过 TTL 缓存强制重取——open_silent_page
    开页后的轮询（1.5s）短于 2s TTL，旧实现全部命中开页前的旧缓存，新 target 永远
    看不到 → 静默开页系统性误判失败并把刚建的 target 关掉（回退前台抢焦点通道）。"""
    import time as _t
    now = _t.time()
    if not force_refresh and _w._tgt_cache.get("data") is not None and now - _w._tgt_cache["ts"] < 2.0:
        return _w._tgt_cache["data"]
    last = None
    for _ in range(2):
        try:
            r = _w.requests.get(f"http://127.0.0.1:{port}/json", timeout=(1, 2))
            r.raise_for_status()
            data = r.json()
            # /json 偶发返回错误对象(如 {"Error":...})，统一转成结构化错误
            if isinstance(data, dict) and data.get("Error"):
                return {"ok": False, "error": str(data.get("Error"))}
            _w._tgt_cache["ts"] = now
            _w._tgt_cache["data"] = data
            # v1.0.632（B6）：枚举到"现在有哪些页面"，顺手就是回收连接池的最佳时机——
            # 页面关了却还留在池里的槽位（Lock + 死 socket）永远不会自己消失。
            # 只吃一次 ≤池容量 的字典遍历，且失败绝不影响本次枚举结果。
            if not isinstance(data, dict):
                try:
                    pool_prune_dead(data)
                except Exception as _pe:  # noqa: BLE001
                    _w._logw(f"[cdp] 连接池回收异常（忽略）：{type(_pe).__name__}: {_pe}")
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.2)  # 预期：CDP /json 偶发失败，重试探测
    # 双重试仍失败：回退最近一次成功值（stale-while-error，标注来源）
    if _w._tgt_cache.get("data") is not None:
        return _w._tgt_cache["data"]
    return {"ok": False, "error": str(last)}


def _pages(targets):
    if isinstance(targets, dict):
        return []
    return [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def target_pick(target_id):
    """v1.0.555（P1-6）：把 CDP target id 包装成寻址 pick（"#tid:<id>"）。
    详情页全链路（wait_page_load/read_page/read_fields/download/close）改用
    targetId 寻址——原用"标题"当 pick：_pages 两次枚举之间 index 漂移会拿到
    别的页的标题（v1.0.425 为列表页修过同款，详情页复现），且 close_page/
    read_fields 按标题子串匹配，多个同名页（OAuth 中转页标题全同）时关/读
    第一个——可能关掉还要用的页、读到同名旧详情页（数据张冠李戴）。"""
    return f"#tid:{target_id}" if target_id else None


def pick_window(targets, pick=None):
    """按 pick 选择目标窗口。v1.0.413：pick 支持三种形态——
    int / 数字字符串：索引寻址（旧行为，窗口开关后会漂移）；
    非空字符串：**标题/URL 包含匹配**（大小写不敏感，跨窗口开关稳定，推荐）。
    匹配不到返回 None（上层报"未找到窗口"，绝不静默选错窗口）。"""
    pages = _w._pages(targets)
    if not pages:
        return None
    # v1.0.555（P1-6）：targetId 寻址（#tid:<id>）——精确匹配，优先于标题子串。
    # open_silent_page 返回 target_id，详情页流程应全程携带，免疫 index 漂移与
    # 同名页碰撞（标题子串匹配返回第一个，多个 OAuth 中转页时必错）。
    if isinstance(pick, str) and pick.startswith("#tid:"):
        _tid = pick[5:].strip()
        for i, p in enumerate(pages):
            if p.get("id") == _tid:
                return pages[i]
        return None
    # v1.0.413：字符串 pick → 标题/URL 包含匹配
    if isinstance(pick, str) and pick.strip() and not pick.strip().isdigit():
        sl = pick.strip().lower()
        for i, p in enumerate(pages):
            if sl in (p.get("title") or "").lower() or sl in (p.get("url") or "").lower():
                return pages[i]
        return None
    # v1.0.470 修复（P1-16）：int/数字串 pick 越界必须显式失败——原实现越界后不
    # return，掉进 _AUTO_PICK/内容页评分自动选窗，把写操作做在错误目标上（违反
    # docstring「绝不静默选错窗口」契约——该契约原本只对字符串 pick 成立）。
    if pick is not None:
        try:
            _pi = pick if isinstance(pick, int) else int(str(pick).strip())
        except (TypeError, ValueError):
            return None
        if 0 <= _pi < len(pages):
            return pages[_pi]
        return None
    # 未显式指定 pick 时：优先「当前跟随窗口」（点击链接自动跟随的新页面/上次操作窗口，
    # v1.0.192），保持多页面流程（列表页→详情页→回列表）的连续性；跟随窗口失效时
    # 回退到内容页评分自动选择。
    _ap = _w._get_auto_pick()
    if _ap is not None and 0 <= _ap < len(pages):
        return pages[_ap]
    def score(t):
        url = (t.get("url") or "").lower()
        # 排除明显不是内容页的 target（空白页/崩溃页/调试页通常不是可读内容）
        if not url or url == "about:blank" or url.startswith("chrome-error://") or "devtools" in url:
            return -1
        # 内容页优先：企微域 / 内嵌网页 / OAuth / 文档多为 http(s)
        if "weixin.qq.com" in url or "wxwork" in url or "wework" in url:
            return 4
        if url.startswith("https://") or url.startswith("http://"):
            return 3
        # Electron 本地资源(app://) / 本地文件 可能是文档查看器，次之
        if url.startswith("app://") or url.startswith("file://"):
            return 1
        return 0
    return sorted(pages, key=score, reverse=True)[0]


# 会话级「当前跟随窗口」index（v1.0.192）：click 检测到新页面后自动更新，
# 后续未显式 pick 的操作自动跟随；窗口关闭/列表刷新后失效回退自动评分。
#
# v1.0.588 两处修正：
#  1) 按 pane（会话）隔离。原为进程级单值：多任务并行时 A 任务点开的详情页会被
#     B 任务未显式带 pick 的 read/click 继承，直接把操作做在别人的窗口上。
#     作用域由 agent_tools.dispatch.set_current_pane 同步注入（无上下文时落默认域，
#     单会话行为与旧实现一致）。
#  2) 不再对外暴露模块变量。`from .windows import _AUTO_PICK` 是**值快照**，
#     导致 agent/loop.py 与 services/batch_flow.py 里的 `getattr(wb, "_AUTO_PICK")`
#     永远读到 import 时的 None（跟随提示与 _find_follow_pick 长期失效）。
#     读取统一走 _get_auto_pick()；包层 wecom_bridge.__getattr__ 提供活引用。
_AUTO_PICK_BY_SCOPE = {}
_AUTO_PICK_LOCK = threading.Lock()
_PICK_CTX = threading.local()
_DEFAULT_SCOPE = "_default"


def set_auto_pick_scope(key):
    """设置当前线程的 pane 作用域（agent_tools.dispatch 在设置 pane 时同步调用）。"""
    _w._PICK_CTX.scope = key or _w._DEFAULT_SCOPE


def _pick_scope():
    return getattr(_w._PICK_CTX, "scope", None) or _w._DEFAULT_SCOPE


def current_pane_id():
    """当前会话作用域的 pane key（agent_loop 每轮开头 set_auto_pick_scope 写入）。

    与 _pick_scope() 的区别：那个在未设置时回落到 _DEFAULT_SCOPE（一个假想的默认窗口
    索引作用域，不是真实会话），而结果回流必须知道"到底有没有发起会话"，所以这里
    未设置时返回 None。工具执行器用它把后台任务结论写回发起它的那个对话。"""
    return getattr(_w._PICK_CTX, "scope", None) or None


def _get_auto_pick():
    """当前会话的跟随窗口 index；无则 None。"""
    with _w._AUTO_PICK_LOCK:
        return _w._AUTO_PICK_BY_SCOPE.get(_w._pick_scope())


def _set_auto_pick(index):
    """更新当前会话的跟随窗口 index（越界/None 视为失效并清除）。"""
    with _w._AUTO_PICK_LOCK:
        k = _w._pick_scope()
        if index is not None and index >= 0:
            _w._AUTO_PICK_BY_SCOPE[k] = index
        else:
            _w._AUTO_PICK_BY_SCOPE.pop(k, None)


def close_page(pick=None, port=DEFAULT_PORT):
    """关闭指定窗口（标签页），v1.0.203。pick 缺省时关闭当前跟随窗口。
    关闭成功后清空跟随窗口（_AUTO_PICK=None）——模型需 list_wecom_windows 重新定位。

    用途：批量审核「审完一项 → 关闭详情页 → 回列表 → 点下一项」——
    详情页标签不关会越积越多（列表链接 target=_blank 每点一次开一个新标签），
    实测开出一堆重复「某分支」标签导致模型彻底迷路。

    返回 {ok, closed_index, title} 或 {ok:False, error}。
    v1.0.555（P1-6）：pick 支持 targetId（#tid:<id>）与标题/URL 子串——原只认
    index，batch_flow 传标题 pick 时 `0 <= "标题" < len(pages)` TypeError 被
    except 吞成 {"ok": False}（静默通道打开的详情页实际关不掉，标签越积越多）；
    标题子串匹配多个同名页（OAuth 中转页）时也只关第一个。"""
    try:
        targets = _w.list_targets(port)
        pages = _w._pages(targets)
        if not pages:
            return {"ok": False, "error": "没有可关闭的窗口/标签页"}
        idx = None
        if pick is None:
            idx = _w._get_auto_pick()
        elif isinstance(pick, int):
            idx = pick
        elif isinstance(pick, str):
            _s = pick.strip()
            if _s.startswith("#tid:"):
                _tid = _s[5:].strip()
                idx = next((i for i, p in enumerate(pages) if p.get("id") == _tid), None)
            elif _s.isdigit():
                idx = int(_s)
            else:
                _sl = _s.lower()
                idx = next((i for i, p in enumerate(pages)
                            if _sl in (p.get("title") or "").lower()
                            or _sl in (p.get("url") or "").lower()), None)
        if idx is None or not (0 <= idx < len(pages)):
            return {"ok": False, "error": f"未指定有效窗口（pick={pick}, 当前跟随={_w._get_auto_pick()}，共 {len(pages)} 个页面）。"
                                          "请先用 list_wecom_windows 查看序号再关。"}
        target_id = pages[idx].get("id")
        title = (pages[idx].get("title") or "")[:50]
        # CDP /json/close/<targetId> 关闭标签页（保持列表页等其它窗口不受影响）
        r = _w.requests.get(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=3)
        if r.status_code != 200:
            return {"ok": False, "error": f"关闭失败 HTTP {r.status_code}: {r.text[:120]}"}
        _ap = _w._get_auto_pick()
        if _ap is not None and idx == _ap:
            _w._set_auto_pick(None)  # 跟随窗口被关 → 清空，模型重新定位
        return {"ok": True, "closed_index": idx, "title": title,
                "result": f"已关闭窗口 {idx}（{title}）。请 list_wecom_windows 重新确认当前窗口。"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"关闭窗口异常：{e}"}


def open_silent_page(url, port=DEFAULT_PORT, timeout=8.0):
    """CDP /json/new 后台新开一个 page target 并直接导航到 url（v1.0.296）。

    与 click_element 触发 a[target=_blank] 不同：本方法走 Chromium 的 /json/new
    直接建 target，不经过企微 UI 的 window.open / tab 切换逻辑，因此**不会把新页
    提到前台抢焦点**——用于月报等批量审核「后台静默加载详情、读完即关」，避免打断
    用户正在打字/操作的前台页面。cookie 与企微同一 CDP browser 实例共享，OA 详情页
    可带登录态正常加载。

    返回 {ok:True, target_id, index, title, url}：index 为新 target 在 _pages 列表
    中的下标，供后续 read_page/read_fields/download_attachment/close_page 用 pick。
    失败返回 {ok:False, error}（调用方据此回退前台 click 通道）。"""
    if not url or not isinstance(url, str):
        return {"ok": False, "error": "open_silent_page: url 为空或非字符串"}
    # v1.0.629：这是"本软件自己开的页"，先拍窗口基线，成功后登记——静默收纳只认这条登记
    _base = _w.new_window_baseline()
    try:
        from urllib.parse import quote
        q = quote(url, safe="")
        # 新版 CDP 的 /json/new 要求 PUT；旧版仅支持 GET → 404/405 时降级
        r = _w.requests.put(f"http://127.0.0.1:{port}/json/new?{q}", timeout=timeout)
        if r.status_code in (404, 405):
            r = _w.requests.get(f"http://127.0.0.1:{port}/json/new?{q}", timeout=timeout)
        if r.status_code != 200:
            return {"ok": False, "error": f"/json/new HTTP {r.status_code}: {r.text[:120]}"}
        data = r.json()
        if not isinstance(data, dict):
            return {"ok": False, "error": f"/json/new 返回非 JSON 对象: {r.text[:120]}"}
        target_id = data.get("id")
        if not target_id:
            return {"ok": False, "error": f"/json/new 未返回 target id: {r.text[:120]}"}
        # 等新 target 出现在 /json 枚举里，定位 index（后续 read/download/close 按 pick 走）
        # v1.0.652（审计 P1-11）：轮询强制绕过 TTL 缓存——否则 5×0.3s=1.5s 全部命中
        # 开页前旧缓存，新 target 不可见 → 误判失败并关掉刚建的 target。
        index = None
        for _ in range(5):
            time.sleep(0.3)
            targets = _w.list_targets(port, force_refresh=True)
            if is_err(targets):
                continue
            for i, p in enumerate(_w._pages(targets)):
                if p.get("id") == target_id:
                    index = i
                    break
            if index is not None:
                break
        if index is None:
            # 已建 target 但枚举不到 → 尝试直接关闭避免 target 泄漏，视为失败
            try:
                _w.requests.get(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=2)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "error": f"新 target {target_id} 未出现在页面列表（可能被即时回收）"}
        _w.note_agent_open(_base, target_id=target_id, url=(data.get("url") or url)[:200],
                           why="静默开页")
        return {"ok": True, "target_id": target_id, "index": index,
                "title": (data.get("title") or "")[:60], "url": (data.get("url") or url)[:200]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"open_silent_page 异常：{e}"}


def _scan_new_page(port, before_ids, before_urls):
    """单次扫描当前页面列表，判断点击是否带来新页面（不 sleep、不轮询）。

    从 _find_new_page 的循环体拆出，供「新页 + 页面状态变化」合并采样复用
    （v1.0.623 提速）。取不到列表按「无新页」返回，与旧实现 except 后 continue
    的语义一致。"""
    try:
        targets = _w.list_targets(port)
    except Exception:  # noqa: BLE001
        return None, None
    pages = _w._pages(targets)
    if not pages:
        return None, None
    # 1) 新 page target（数量增加）
    for i, p in enumerate(pages):
        if p.get("id") and p.get("id") not in before_ids:
            return p, i
    # 2) 无新 target：当前跟随/首个内容页 url 变化（同标签页内导航）
    for i, p in enumerate(pages):
        if p.get("url") and p.get("url") not in before_urls and not p.get("url", "").startswith("about:"):
            return p, i
    return None, None


def _find_new_page(port, before_ids, before_urls, retries=6, interval=0.4):
    """点击后轮询检测是否出现了新页面（新标签页/新窗口，或当前页面导航到新 url）。
    返回 (page dict, 新页面在 pages 列表中的 index)；无新页面返回 (None, None)。"""
    for _ in range(retries):
        time.sleep(interval)
        p, i = _w._scan_new_page(port, before_ids, before_urls)
        if p is not None:
            return p, i
    return None, None


def wait_page_load(pick=None, timeout=8.0, port=DEFAULT_PORT, reload_on_timeout=False):
    """等待当前选中企微页面可读：readyState==='complete' + 正文长度稳定（SPA 异步渲染落定）。

    用于打开/刷新列表页、点击进入预览页/详情页之后，确保页面渲染完成再 read/click，
    避免读到空白或半渲染快照。注意：企微/财务 OA 为 SPA，常驻 loading 元素与自研遮罩
    类名不可靠，历史版本用「loading 元素判据」反复误判超时并触发整页 reload，反而把
    刚进入的表单/列表刷新回首页（表现为首页与表单来回跳）。故本版：
      - 阶段一：只等 readyState==='complete'（轮询预算 timeout，内部封顶 8s）；
      - 阶段二：等正文长度连续 3 次采样（0.4s 间隔）不变——财务系统详情页的表格/字段
        在 readyState 完成后仍异步渲染数秒，旧版固定 sleep(1.0) 不够用，是「页面没加载
        完就开始读」的直接原因；稳定预算从剩余时间分配（总预算 timeout 封顶 8s，
        正文稳定最多额外等 4s，两条取小）；正文持续变动（动画/轮询）时预算耗尽即继续，
        绝不阻塞；
      - 不执行 Page.reload（默认 reload_on_timeout=False），避免破坏当前操作上下文；
      - 等待超时不报致命错误，降级为 ok+warning 让 agent 乐观继续。

    任一情况都返回 {"ok": True, "waited": 秒[, "warning": 文案]}；
    仅 CDP/未选页面不可用时返回 {"ok": False, "error": 原因}。
    """
    ws_url, page = _w._page_ws(pick=pick, port=port)
    if ws_url is None:
        if is_err(page):
            return {"ok": False, "error": err_msg(page)}
        return {"ok": False, "error": "未找到可操作的企业微信页面（未选窗口或 CDP 不可用）"}
    timeout = min(float(timeout), 8.0)
    t0 = time.time()
    try:
        d = time.time() + timeout
        while time.time() < d:
            r = _w._cdp_eval(ws_url, "document.readyState", timeout=3.0)
            if r == "complete":
                # 阶段二：等 SPA 异步渲染落定（正文连续采样不变）。预算 = 总预算剩余 与 4s 取小。
                stable_budget = min(max(0.5, d - time.time()), 4.0)
                _w._wait_body_stable(ws_url, budget=stable_budget, interval=0.4, stable_checks=3)
                return {"ok": True, "waited": round(time.time() - t0, 1)}
            time.sleep(0.25)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.6)
    return {"ok": True, "waited": round(time.time() - t0, 1),
            "warning": f"{int(timeout)}s 内未确认 readyState=complete，已继续执行（由后续读取确认结果）"}

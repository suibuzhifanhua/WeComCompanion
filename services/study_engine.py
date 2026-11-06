# -*- coding: utf-8 -*-
"""study_engine.py — 内置学习中心自动跑课引擎（v1.0.276，把原 build/study_driver.py v6.7 并入软件）
====================================================================
原驱动器是沙箱外置脚本：HTTP 调 /api/chat 驱动 agent + 沙箱会话一重置就死。
现移植为进程内后台线程：agent 会话直接调 agent_loop（不走 HTTP），CDP 操作用
wecom_bridge.cdp_client（配置端口），日志进 companion.log [study-engine] + UI 环形缓冲。

逻辑与 study_driver.py v6.7 完全同口径：
- 三态 RESULT（PASS/DEAD/FAIL）；DEAD → 平台失效清单（不重试）；
- done 强刷地图核对出列，3 次不出列 → 跳过清单（假完成/需人工）；
- error/timeout/unknown = 基建故障 → defer 冷却重试（DEFER_MAX 次才降级跳过）；
- 会话过期自动点 Portal「新学习平台」磁贴（唯一重授权入口，禁绕过）+ 等登录 24h；
- 窗口全丢（企微重启）自动重开 + 等窗口 24h；
- 每门课独立 agent 会话（小上下文防复读），跑完清学习平台窗口只留地图。
"""
import json
import os
import threading
import time
from collections import deque
from datetime import date

from core_utils import log_event, ws_sub, workspace_root
from wecom_bridge.cdp_client import _cdp_eval, _cdp_command
from wecom_bridge import list_targets
from core.config_store import load_config
from core.daemon import start_periodic   # A13+B1：守护骨架收敛
from core.study_js import STUDY_MUTE_EXPR
from agent_tools import _cdp_port
from agent import (agent_loop, build_chat_messages, _set_abort, chat_states,
                   _chat_state_lock)
from agent import _auto_loop_chain  # v1.0.302：自 routes.chat 搬入 agent 核心层（消除层倒置）

# ---------- 常量（与 study_driver.py v6.7 一致） ----------
PER_COURSE_TIMEOUT = 3600   # 单门课最长 60 分钟（多视频系列课 2x 限速下 25min 必超时）
MAP_MARK = "studyMapList"
# v1.0.657 公开发布匿名化：学习平台标识只保留中性默认值。真实值由 config.json 的
# study 段提供（login_mark=登录页正文标记 / portal_mark=门户工作台域名 /
# learn_url_marks=课程域名列表）——config.json 是开发机红线文件（gitignore），不进仓库。
LOGIN_MARK_DEFAULT = "欢迎使用学习平台"
PORTAL_MARK_DEFAULT = "portal.example.com"
LEARN_URL_MARKS_DEFAULT = ("learn.example.com",)
NEW_PLAT_MARK = "新学习平台"
LOGIN_WAIT_MAX = 2880       # 会话过期最长等待 2880 × 30s = 24h
NO_WIN_WAIT_MAX = 2880      # 窗口全丢最长等待 2880 × 30s = 24h
MAX_ATTEMPTS = 3            # 单门课最多尝试次数
DEFER_COOLDOWN = 900        # 瞬态故障重试冷却 15min
DEFER_MAX = 4               # 累计 defer 上限，超过降级跳过
COURSE_CAP = 80             # 单次启动最多跑 80 门（防异常死循环）


def _study_marks():
    """学习平台标识（v1.0.657 公开发布匿名化）：config.study 优先，缺省回落中性默认。
    公开版代码不携带任何真实学习平台域名；启用无人值守跑课的用户在 config.json
    的 study 段填真实值（该文件 gitignore，不进仓库、不随发布产物分发）。
    返回 (login_mark, portal_mark, learn_marks)。load_config 有 (路径, mtime, 字节数)
    缓存，每 tick 调用开销仅一次 stat。"""
    try:
        s = (load_config() or {}).get("study") or {}
    except Exception:  # noqa: BLE001
        s = {}
    if not isinstance(s, dict):
        s = {}
    lm = s.get("learn_url_marks")
    if not isinstance(lm, (list, tuple)) or not all(str(x).strip() for x in lm):
        lm = LEARN_URL_MARKS_DEFAULT
    return (
        str(s.get("login_mark") or "").strip() or LOGIN_MARK_DEFAULT,
        str(s.get("portal_mark") or "").strip() or PORTAL_MARK_DEFAULT,
        tuple(str(x).strip() for x in lm),
    )

# v1.0.616（审查 P2）：CDP 探测类兜底要留痕，但**每处每轮只记第一条**。
# 这些 helper 的 `except Exception: return None/False` 是刻意的（探测失败由上层重试），
# 全记会在一次刷课里灌进成千上万行、把真信号埋掉；一条不记又导致"引擎说打不开门户，
# 到底是没窗口、CDP 断了、还是页面结构变了"事后无从判断（同 613 的"一句笼统失败"）。
_QUIET_LOGGED = set()


def _start_course_watchdog(should_abort, pane_id, interval=5.0):
    """v1.0.364（M9）的看门狗：超时/停止检查原本只在生成器 yield 事件时执行，
    阻塞在单个同步 CDP/LLM 调用上时两者全部失效——到期强制置 abort，由 agent
    步进间的检查兜底退出。v1.0.650 从 _run_course 里提到模块级（棘轮只许少不许多），
    行为一字未改。返回 (停止事件, 线程)。
    v1.0.656（A13）：线程骨架收敛到 core.daemon.start_periodic。注意它是**周期
    轮询型**——每 interval 秒查一次 should_abort、命中即置 abort 后不再动作，
    不是一次性超时，不能套 spawn_deadline_watchdog；first_delay=interval 对齐原
    「先 wait 再查」节奏（首查在 +interval 而非启动即查）；命名带 pane_id 保证
    逐门课的看门狗实例互不判重吞掉。"""
    fired = threading.Event()

    def _tick():
        if fired.is_set():          # 命中后只置一次 abort（原实现触发即 return）
            return
        if should_abort():
            fired.set()
            _set_abort(pane_id, True)

    th = start_periodic(f"study-watchdog-{pane_id}", _tick, interval,
                        first_delay=interval)
    return th.stop_event, th


def _quiet(site, e):
    if site in _QUIET_LOGGED:
        return
    _QUIET_LOGGED.add(site)
    try:
        log_event("[study] %s 走兜底返回：%s: %s" % (site, type(e).__name__, str(e)[:140]))
    except Exception:  # noqa: BLE001
        pass


class StudyEngine:
    """单例跑课引擎。start()/stop() 线程安全；status() 供 UI 轮询。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_flag = False
        self._current_pane = None          # 当前课程 agent 会话 pane_id（stop 时 abort 用）
        self._state = "idle"               # idle/running/waiting_login/waiting_window/cooldown
        self._current = ""
        self._course_n = 0
        self._unlearned = 0
        self._last_result = ""
        self._started_at = 0
        self._logs = deque(maxlen=200)
        self._count_cache = {}              # 清单条数的 (mtime,size)->n 记忆，见 _count_json

    # ---------- 日志 ----------
    def _log(self, msg):
        line = f"[study-engine] {msg}"
        self._logs.append(msg)
        log_event(line)

    # ---------- 清单读写（产物库/产出/，与 agent save_workspace_file 同目录） ----------
    def _list_dir(self, create=True):
        """清单所在目录。

        create=False 只算路径、不建目录，也**不开产物库闸门**：v1.0.619 前这里统一走
        ws_sub("产出")——那是写路径解析器，会顺手 makedirs 并把闸门打开，而 status() 被
        UI 每 3s 轮一次，等于把"进软件隔一会再建产物库"提前到了第一次轮询（首启 3 秒内）。
        """
        return ws_sub("产出") if create else os.path.join(workspace_root(), "产出")

    def _skip_file(self, create=True):
        return os.path.join(self._list_dir(create), "学习跳过清单.json")

    def _dead_file(self, create=True):
        return os.path.join(self._list_dir(create), "学习平台失效清单.json")

    def _load_json(self, path):
        try:
            with open(path, encoding="utf-8") as f:   # v1.0.361（P18）with 关闭句柄
                return json.load(f)
        except Exception:
            return []

    def _count_json(self, path):
        """清单条数（status() 用）。按 (mtime, size) 记忆：文件没变就不重复读盘解析。
        v1.0.619（审查⑤）：status() 每 3s 被 UI 轮一次，原实现每次把两份清单整本读进来
        只为拿 len()——清单能长到几百条，轮询线程和磁盘都在白干。"""
        try:
            st = os.stat(path)
            key = (int(st.st_mtime_ns), st.st_size)
        except OSError:
            return 0
        cached = self._count_cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        n = len(self._load_json(path))
        self._count_cache[path] = (key, n)
        return n

    def _save_json(self, path, data):
        # v1.0.361 审查修复（P18）：tmp+os.replace 原子写（status() 每 3s 并发读，
        # 直接覆盖写被杀会留截断 JSON 让清单静默清零）
        try:
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as e:
            self._log(f"清单写入失败 {os.path.basename(path)}：{e}")

    # ---------- 对外接口 ----------
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "引擎已在运行"}
            self._stop_flag = False
            _QUIET_LOGGED.clear()   # v1.0.616：每轮任务重新计一次兜底日志
            self._course_n = 0
            self._current = ""
            self._last_result = ""
            self._started_at = time.time()
            self._state = "running"
            self._thread = threading.Thread(target=self._main, name="study-engine", daemon=True)
            self._thread.start()
        return {"ok": True}

    def stop(self):
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return {"ok": False, "error": "引擎未在运行"}
            self._stop_flag = True
            pane = self._current_pane
        if pane:
            try:
                _set_abort(pane, True)
                with _chat_state_lock:
                    chat_states.pop(pane, None)
            except Exception:
                pass
        self._log("收到停止指令——当前课结束后退出（已在课程中途则立即中断）")
        return {"ok": True}

    def status(self):
        # v1.0.364 审查修复（低#12）：文件 I/O（清单 JSON 读取）移出引擎锁外——
        # UI 每 3s 轮询本方法，磁盘卡顿原会拖住 start()/stop() 的锁竞争。
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            snapshot = {"running": running,
                        "state": self._state if running else "idle",
                        "current": self._current,
                        "course_n": self._course_n,
                        "unlearned": self._unlearned,
                        "last_result": self._last_result,
                        "elapsed_min": round((time.time() - self._started_at) / 60, 1) if running else 0}
        snapshot["skips"] = self._count_json(self._skip_file(create=False))
        snapshot["dead"] = self._count_json(self._dead_file(create=False))
        snapshot["logs"] = list(self._logs)[-60:]
        return snapshot

    # ---------- CDP 辅助（配置端口） ----------
    def _port(self):
        # v1.0.648：原来这里还手抄了一份 `except: return 9222`——而 _cdp_port 内部
        # 已有「非法/越界一律回退 9222」，重复兜底既掩盖异常又是第二个默认值来源。
        return _cdp_port(load_config())

    def _zn_pages(self):
        try:
            pages = list_targets(self._port())
        except Exception:
            return []
        if not isinstance(pages, list):
            return []
        _lm = _study_marks()[2]
        return [p for p in pages if p.get("type") == "page" and any(k in (p.get("url") or "") for k in _lm)]

    def _ws_of(self, p):
        return f"ws://127.0.0.1:{self._port()}/devtools/page/{p.get('id')}"

    def _page_state(self, ws):
        try:
            r = _cdp_eval(ws, "JSON.stringify({login:(document.body&&document.body.innerText||'').includes('%s'), hash:location.hash||''})" % _study_marks()[0])
        except Exception as e:
            _quiet("_page_state.cdp_eval", e)
            return None
        if not isinstance(r, str):
            return None
        try:
            st = json.loads(r)
        except Exception as e:
            _quiet("_page_state.json", e)
            return None
        if st.get("login"):
            return "login"
        if MAP_MARK in (st.get("hash") or ""):
            return "map"
        return "other"

    def _session_alive(self):
        pages = self._zn_pages()
        if not pages:
            return None
        for p in pages:
            if self._page_state(self._ws_of(p)) in ("map", "other"):
                return True
        return False

    def _portal_ws(self):
        try:
            pages = list_targets(self._port())
        except Exception as e:
            _quiet("_portal_ws.list_targets", e)
            return None
        if not isinstance(pages, list):
            return None
        for p in pages:
            if p.get("type") == "page" and _study_marks()[1] in (p.get("url") or ""):
                return self._ws_of(p)
        return None

    def _refresh_portal(self):
        ws = self._portal_ws()
        if not ws:
            return False
        try:
            _cdp_eval(ws, "location.reload()")
        except Exception as e:
            _quiet("_refresh_portal.reload", e)
            return False
        for _ in range(25):
            time.sleep(2)
            if self._stop_flag:
                return False
            try:
                r = _cdp_eval(ws, "document.readyState", timeout=3)
            except Exception as e:
                _quiet("_refresh_portal.readyState", e)
                continue
            # v1.0.281：_cdp_eval 超时/CDP 错误时返回 {"ok": False, "error": ...} dict——
            # dict 直接参与 `in (str, str)` 会 TypeError 炸掉引擎（实测 02:02 崩溃点）。
            if isinstance(r, str) and r in ("interactive", "complete"):
                return True
        return False

    def _portal_click_once(self):
        ws = self._portal_ws()
        if not ws:
            return False
        try:
            _cdp_eval(ws, "(()=>{const el=[...document.querySelectorAll('a,div,span,li,td,p')]"
                         ".find(x=>(x.innerText||'').trim()==='%s' && x.offsetParent); if(!el) return 'none'; "
                         "el.scrollIntoView({block:'center'}); return 'ok';})()" % NEW_PLAT_MARK)
        except Exception as e:
            _quiet("_portal_click.scroll", e)
            pass
        time.sleep(1)
        loc_js = ("(()=>{const el=[...document.querySelectorAll('a,div,span,li,td,p')]"
                  ".find(x=>(x.innerText||'').trim()==='%s' && x.offsetParent); "
                  "if(!el) return 'none'; const r=el.getBoundingClientRect(); "
                  "return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2});})()" % NEW_PLAT_MARK)
        try:
            r = _cdp_eval(ws, loc_js)
        except Exception as e:
            _quiet("_portal_click.locate", e)
            return False
        if not isinstance(r, str) or r == "none":
            return False
        try:
            pos = json.loads(r)
        except Exception as e:
            _quiet("_portal_click.pos_json", e)
            return False
        before = len(self._zn_pages())
        try:
            _cdp_eval(ws, "(()=>{const el=[...document.querySelectorAll('a,div,span,li,td,p')].find(x=>(x.innerText||'').trim()==='%s' && x.offsetParent); if(!el) return 0; el.click(); return 1;})()" % NEW_PLAT_MARK)
        except Exception as e:
            _quiet("_portal_click.click", e)
            pass
        time.sleep(4)
        if len(self._zn_pages()) > before:
            return True
        try:
            for t in ("mousePressed", "mouseReleased"):
                _cdp_command(ws, "Input.dispatchMouseEvent",
                             {"type": t, "x": pos["x"], "y": pos["y"],
                              "button": "left", "clickCount": 1})
            time.sleep(4)
        except Exception:
            pass
        return len(self._zn_pages()) > before

    def _open_new_platform(self):
        """Portal 点「新学习平台」磁贴（唯一重授权入口）。已有学习平台窗口则不重复点。"""
        if self._zn_pages():
            return True
        for i in range(3):
            if i:
                self._log(f"磁贴点击无反应 → 刷新 Portal 工作台后重点（第 {i + 1}/3 轮）...")
                self._refresh_portal()
                time.sleep(5)
            if self._portal_click_once():
                return True
        return False

    def _prune_zn_windows(self):
        pages = self._zn_pages()
        keep_ids = {p.get("id") for p in pages if MAP_MARK in (p.get("url") or "")}
        if not keep_ids and pages:
            keep_ids = {pages[0].get("id")}
        before = {p.get("id") for p in pages}
        for p in pages:
            if p.get("id") in keep_ids:
                continue
            ws = self._ws_of(p)
            try:
                _cdp_command(ws, "Target.closeTarget", {"targetId": p.get("id")}, timeout=5)
            except Exception:
                try:
                    _cdp_eval(ws, "window.close()", timeout=3)
                except Exception:
                    pass
            time.sleep(1)
        time.sleep(1)
        after = {p.get("id") for p in self._zn_pages()}
        closed = len(before - after)
        if closed:
            self._log(f"已清理 {closed} 个学习窗口（只留地图窗口，学完即关）")
        return closed

    def _ensure_map_window(self):
        pages = self._zn_pages()
        if not pages:
            return None

        def prio(p):
            u = p.get("url") or ""
            if MAP_MARK in u:
                return 0
            if "studyDetail" in u:
                return 1
            if "courseDetail" in u:
                return 2
            return 3

        pages.sort(key=prio)
        any_login = False
        for p in pages:
            ws = self._ws_of(p)
            st = self._page_state(ws)
            if st is None:
                continue
            if st == "login":
                any_login = True
                continue
            if st == "other":
                try:
                    _cdp_eval(ws, "location.hash='#/home/studyMapList'")
                except Exception:
                    continue
                time.sleep(3)
                st2 = self._page_state(ws)
                if st2 == "map":
                    self._log("已将学习窗口导航回 studyMapList")
                    return ws
                if st2 == "login":
                    any_login = True
                continue
            return ws
        return "LOGIN" if any_login else None

    def _read_unlearned_names(self, ws):
        js = ("(()=>{const ul=document.querySelector('.MuiList-root'); if(!ul) return null; "
              "const lis=[...ul.children].filter(x=>(x.innerText||'').includes('学习截止')); "
              "if(lis.length===0){const t=document.body?document.body.innerText:'';"
              "if(/暂无|空空如也|没有未学习|无未学习/.test(t)) return []; return null;} "
              "return lis.map(x=>(x.innerText||'').trim().split('\\n')[0]);})()")
        try:
            r = _cdp_eval(ws, js)
        except Exception:
            return None
        if r is None:
            return None
        if isinstance(r, str):
            r = [r] if r.strip() else []
        if not isinstance(r, list):
            return None
        return [n.strip() for n in r if isinstance(n, str) and n.strip()]

    def _expand_more(self, ws):
        for _ in range(10):
            try:
                r = _cdp_eval(ws, "(()=>{const b=[...document.querySelectorAll('button,span,div')].find(x=>(x.innerText||'').trim()==='查看更多' && x.offsetParent); if(!b) return 'none'; b.click(); return 'clicked';})()")
            except Exception:
                break
            if not (isinstance(r, str) and r == "clicked"):
                break
            time.sleep(1.5)

    def _get_unlearned_list(self):
        """list（可空）/ 'LOGIN' / None。"""
        ws = self._ensure_map_window()
        if ws == "LOGIN":
            return "LOGIN"
        if not ws:
            return None
        self._expand_more(ws)
        for _ in range(6):
            names = self._read_unlearned_names(ws)
            if names:
                return names
            if names is None:
                time.sleep(2)
                continue
            time.sleep(3)
            names2 = self._read_unlearned_names(ws)
            if names2:
                return names2
            if names2 == []:
                return []
            time.sleep(2)
        self._force_refresh_list()
        time.sleep(3)
        self._expand_more(ws)
        for _ in range(4):
            names = self._read_unlearned_names(ws)
            if names is not None:
                return names
            time.sleep(2)
        return None

    def _force_refresh_list(self):
        ws = self._ensure_map_window()
        if not ws or ws == "LOGIN":
            return
        try:
            _cdp_eval(ws, "location.reload()")
        except Exception:
            return
        for _ in range(25):
            time.sleep(2)
            try:
                r = _cdp_eval(ws, "document.readyState", timeout=3)
            except Exception:
                continue
            # v1.0.281：同 _refresh_portal——__error__ dict 守卫（实测崩溃点：
            # 重载后页面未响应，eval 超时返回 dict → `dict in (str,str)` TypeError）
            if isinstance(r, str) and r in ("interactive", "complete"):
                return

    # ---------- 跑课（进程内 agent 会话，替代原 HTTP /api/chat SSE） ----------
    def _run_course(self, name):
        """返回 (result, elapsed, reason)。result: done/error/timeout/login/platform_dead"""
        prompt = (
            f"完成学习中心课程：{name}（study-solver 流程，单门课任务）：\n"
            "①list_wecom_windows 找学习中心窗口 → click_wecom_element 点该课程条目进详情；\n"
            "②逐个处理必修任务：视频=wecom_run_js 一步完成注入静音三件套（muted锁防播放器反制+静音循环+中和裸媒体iframe）+播放+拖到结尾（" + STUDY_MUTE_EXPR + "）等播完——若视频无法缓冲（大文件 rs=0）视为已访问，直接继续；【视频倍速上限】playbackRate 最高 2x，严禁 4x 及以上（平台风控会检测异常倍速判无效学习）；seek 拖尾不生效需实播等完时也只用 2x，耐心轮询等待即可；文档=点「立即学习」即已学；考试=点考试任务→详情先 read_wecom_page 读「剩余考试次数」→ **仅当剩余次数=无限时才点「开始考试」走 exam-solver 答题（无限次数试错到及格）**；若剩余次数=有限（如「剩 N 次」/具体数字）→ **不自动开考**，记台账「考试剩余 N 次有限，跳过自动考试（防白烧次数，需人工）」，继续处理该课其它任务（视频/文档照常完成）——【0题空考试】读不到题目就 history.back() 退出跳过；\n"
            "③全部任务完成后切回原窗口，若弹「你已前往任务学习，是否已完成学习？」→ click_wecom_element 点「已完成」→ 学习证书；\n"
            f"④save_workspace_file 追加 产物库/产出/学习进度_{date.today().strftime('%Y-%m-%d')}.md（课程名+结果）；\n"
            "⑤finish 末行【只写一个】RESULT 三态标记之一（正文里不要再出现 RESULT 字样，否则会被误判触发自动续跑）：\n"
            "   RESULT: PASS=全部必修任务真完成、课程可出列；\n"
            "   RESULT: DEAD=平台侧阻断、重试无意义（必修考试显示「考试已结束」/考试数量0题/无「开始考试」按钮/必修文档无法点完成），末行附一句原因，例「RESULT: DEAD 考试已结束(期2026-06-04,0题)」；\n"
            "   RESULT: FAIL=临时没做完、重试有意义（答错/超时/页面卡死/资源还在）。⚠️判定平台已失效（过期/0题）必须写 DEAD 不能写 FAIL——FAIL 会让外层再开一次全新会话重试，白烧十几分钟。\n"
            "⚠️ 执行纪律：每轮必须输出工具调用，禁止重复相同推理；推理工整 3-5 句；同按钮连点≤2次，点不动换 list_interactive_elements 序号点；"
            "【无响应纪律】同一按钮/条目连点 2 次后页面无任何变化（以重读页面内容核实）→ wecom_run_js 执行 location.reload() 强制刷新当前页，等约 5s 加载完成后重新定位元素再点；刷新 3 次仍无响应才放弃该任务标记未完成并继续下一任务；"
            "【窗口纪律】已有本课程的 studyDetail 窗口则直接复用，禁止重复点列表条目开重复窗口。"
        )
        cfg = load_config()
        port = _cdp_port(cfg)
        pane_id = f"study-engine-{int(time.time())}"
        user_msgs = [{"role": "user", "content": prompt}]
        _set_abort(pane_id, False)
        _set_abort(None, False)
        with _chat_state_lock:
            chat_states.pop(pane_id, None)
        self._current_pane = pane_id
        msgs = build_chat_messages(user_msgs, cfg, port, None, use_wecom_context=False)
        gen = agent_loop(msgs, cfg, pane_id=pane_id)
        gen = _auto_loop_chain(gen, pane_id=pane_id, cfg=cfg, port=port,
                               messages=user_msgs, pick=None, use_wecom_context=False,
                               allow_fail_restart=False)
        t0 = time.time()
        result = "unknown"
        reason = ""
        from agent.sse_parse import FinalAnswerTracker   # v1.0.654 A4：滚动缓冲单源化
        _fa = FinalAnswerTracker()
        final_answer = ""
        last_check = time.time()
        dead_streak = 0
        # v1.0.364 审查修复（M9）：看门狗线程——超时/停止检查原本只在生成器 yield
        # 事件时执行，阻塞在单个同步 CDP/LLM 调用上时超时与 stop 全部失效。
        # 看门狗到期强制置 abort（agent 步进间检查会兜底退出）。
        _wd_stop, _wd = _start_course_watchdog(
            lambda: self._stop_flag or time.time() - t0 > PER_COURSE_TIMEOUT, pane_id)
        try:
            for item in gen:
                if self._stop_flag:
                    _set_abort(pane_id, True)
                    return "stopped", time.time() - t0, ""
                if time.time() - t0 > PER_COURSE_TIMEOUT:
                    _set_abort(pane_id, True)
                    return "timeout", time.time() - t0, ""
                if time.time() - last_check > 30:
                    last_check = time.time()
                    sa = self._session_alive()
                    dead_streak = dead_streak + 1 if sa is False else 0
                    if dead_streak >= 2:
                        _set_abort(pane_id, True)
                        return "login", time.time() - t0, ""
                s = item.strip() if isinstance(item, str) else ""
                if not s.startswith("data:"):
                    continue
                try:
                    ev = json.loads(s[5:].strip())
                except Exception:
                    continue
                t = ev.get("type")
                if t == "token":
                    # v1.0.654 A4：滚动缓冲抽取——旧逐 chunk in 检测在「【完成】」跨
                    # chunk 分裂时漏检（v1.0.271 在 /api/chat 修过的问题此处仍在）
                    _fa.feed(ev.get("text") or "")
                    final_answer = _fa.result()
                if t == "error":
                    # v1.0.650（A4）：不再当场 return——链还挂在这个 for 里没排空，
                    # auto_loop 的「流式中断 → 从断点自动续跑」在后台跑课这条路径上
                    # **从未被执行过**（v1.0.271 修的就是它，但当时只覆盖了 /api/chat；
                    # 后台自己 break/return，把两条自愈都短路成了死代码）。
                    # 记下结论继续排空生成器，让链自己决定能不能从断点接着跑；真续上了
                    # 后面还会再来一个 done，把 result 覆盖回 done。
                    result = "error"
                    continue
                if t == "done":
                    if "RESULT: DEAD" in final_answer:
                        _m = [ln.strip() for ln in final_answer.splitlines() if "RESULT: DEAD" in ln]
                        reason = (_m[-1] if _m else "平台侧失效").strip()
                        result = "platform_dead"
                    else:
                        result = "done"
                    # 同上：排空而不是 break，链的收尾判定（PASS/FAIL/上限）才有机会跑。
                    continue
        except Exception as e:
            self._log(f"课程会话异常：{type(e).__name__}: {e}")
            return "error", time.time() - t0, ""
        finally:
            _wd_stop.set()   # v1.0.364（M9）：看门狗随课程结束退出
        return result, time.time() - t0, reason

    # ---------- 主循环 ----------
    @staticmethod
    def _in_list(lst, name):
        if name in lst:
            return True
        return any(name in n or n in name for n in lst)

    @staticmethod
    def _names(lst):
        """v1.0.281：把清单统一成纯课程名字符串列表。
        跳过清单本来就是字符串；平台失效清单条目是 {name,reason,date} dict——
        dict 直接参与 `k in 课程名` 必炸 TypeError（02:09 实测崩溃点）。
        两个清单口径不对称，是所有消费点的安全前提。"""
        out = []
        for x in lst or []:
            if isinstance(x, str):
                if x.strip():
                    out.append(x.strip())
            elif isinstance(x, dict):
                nm = (x.get("name") or "").strip()
                if nm:
                    out.append(nm)
        return out

    def _pick_next(self, skips, lst, dead):
        skip_names = self._names(skips)
        dead_names = self._names(dead)
        for n in lst:
            if any(k in n for k in skip_names):
                continue
            if any(k in n for k in dead_names):
                continue
            return n
        return ""

    def _add_platform_dead(self, name, reason, dead_list):
        if any((x.get("name") if isinstance(x, dict) else x) == name
               or (isinstance(x, dict) and ((x.get("name") or "") in name or name in (x.get("name") or "")))
               for x in dead_list):
            return False
        dead_list.append({"name": name, "reason": (reason or "平台侧失效").strip()[:160],
                          "date": date.today().strftime("%Y-%m-%d")})
        self._save_json(self._dead_file(), dead_list)
        return True

    def _main(self):
        try:
            skips = self._load_json(self._skip_file())
            dead = self._load_json(self._dead_file())
            self._log(f"启动，跳过清单 {len(skips)} 项 / 平台失效清单 {len(dead)} 项")
            self._prune_zn_windows()
            course_n = 0
            login_wait = 0
            no_win_wait = 0
            last_name = None
            last_result = None
            defer_until = {}
            defer_count = {}
            while course_n < COURSE_CAP and not self._stop_flag:
                lst = self._get_unlearned_list()
                if lst == "LOGIN":
                    self._state = "waiting_login"
                    login_wait += 1
                    if login_wait == 1:
                        if last_name and last_name in skips and last_result in ("timeout", "error", "unknown"):
                            skips.remove(last_name)
                            self._save_json(self._skip_file(), skips)
                            self._log(f"↩️ 已从跳过清单移除 {last_name}（疑被会话过期误伤，重登后自动重试）")
                        self._log("⚠️ 学习平台会话已过期——正在尝试从 Portal 工作台自动打开「新学习平台」...")
                        if self._open_new_platform():
                            self._log("✅ 已点开「新学习平台」新窗口——请在新窗口完成登录（手机号+验证码），登录后自动继续（上限 24 小时）")
                        else:
                            self._log("⚠️ 自动打开失败（Portal 窗口缺失/点击无效）——请手动回到 Portal 工作台点「新学习平台」重新登录（登录后自动继续）")
                    elif login_wait % 60 == 0:
                        self._log(f"⏳ 等待学习平台重新登录中（已等待 {login_wait * 30 // 60} 分钟，上限 24 小时）")
                    if login_wait >= LOGIN_WAIT_MAX:
                        self._log("退出：等待重登超时（24 小时），登录后可重新启动引擎")
                        break
                    time.sleep(30)
                    continue
                login_wait = 0
                if lst is None:
                    self._state = "waiting_window"
                    no_win_wait += 1
                    if no_win_wait == 1:
                        self._log("⚠️ 学习地图窗口不可用（可能企业微信刚重启）——尝试从 Portal 自动重开「新学习平台」...")
                        if self._open_new_platform():
                            self._log("✅ 已自动点开「新学习平台」新窗口，等待其加载...")
                        else:
                            self._log("⚠️ Portal 自动重开失败——请手动回到 Portal 工作台点「新学习平台」（窗口出现后自动继续）")
                    elif no_win_wait % 60 == 0:
                        self._log(f"⏳ 等待学习地图窗口恢复中（已等待 {no_win_wait * 30 // 60} 分钟）")
                    if no_win_wait >= NO_WIN_WAIT_MAX:
                        self._log("退出：等待学习地图窗口超时（24 小时），人工进入后可重新启动引擎")
                        break
                    time.sleep(30)
                    continue
                no_win_wait = 0
                if lst == []:
                    self._log("✅ 学习地图已无未学课程，全部完成")
                    break
                self._state = "running"
                self._unlearned = len(lst)
                now = time.time()
                deferred_now = [n for n in lst if defer_until.get(n, 0) > now]
                available = [n for n in lst if defer_until.get(n, 0) <= now]
                name = self._pick_next(skips, available, dead)
                if name == "":
                    if deferred_now:
                        self._state = "cooldown"
                        wake = min(defer_until[n] for n in deferred_now)
                        wait = max(10.0, min(300.0, wake - now))
                        self._log(f"⏳ 暂无可立即做的课（{len(deferred_now)} 门在冷却）→ {wait / 60:.0f}min 后自动重试")
                        for _ in range(int(wait // 5)):
                            if self._stop_flag:
                                break
                            time.sleep(5)
                        continue
                    # v1.0.281：_in_list 要求纯字符串列表——dead 是 dict 条目，
                    # 不过 _names() 同样会炸（name in dict 走键检查，dict in str 炸）
                    n_skip = len([n for n in lst if self._in_list(self._names(skips), n)])
                    n_dead = len([n for n in lst if self._in_list(self._names(dead), n)])
                    self._log(f"剩余 {len(lst)} 门未学课全部已归类（跳过 {n_skip} / 平台失效 {n_dead}）→ 退出（人工处理失效课或管理员恢复考试期后重启）")
                    break
                course_n += 1
                self._course_n = course_n
                self._current = name
                last_name = name
                self._log(f"[{course_n}] 开始：{name}（剩余未学 {len(lst)} 门）")
                attempts = 0
                while not self._stop_flag:
                    attempts += 1
                    result, elapsed, reason = self._run_course(name)
                    last_result = result
                    self._last_result = result
                    if result == "stopped":
                        break
                    self._log(f"[{course_n}] {result} 用时 {elapsed / 60:.1f}min（第{attempts}/{MAX_ATTEMPTS}次）：{name}")
                    if result == "platform_dead":
                        if self._add_platform_dead(name, reason, dead):
                            dead = self._load_json(self._dead_file())
                        self._log(f"[{course_n}] ⛔ 平台失效 → 加入平台失效清单（不再重试）：{name}\n    原因：{reason}")
                        break
                    # 注：原 driver 的 conn_err（8787 不可达）分支在进程内不可能发生——引擎就是后端本身
                    if result == "login":
                        self._log(f"[{course_n}] 课程因会话过期中断，未加跳过清单，重登后自动续跑")
                        time.sleep(5)
                        break
                    if result == "done":
                        self._force_refresh_list()
                    again_list = self._get_unlearned_list()
                    if not (isinstance(again_list, list) and self._in_list(again_list, name)):
                        if isinstance(again_list, list):
                            self._log(f"[{course_n}] ✅ 已完成出列：{name}")
                        else:
                            self._log(f"[{course_n}] 核对状态={again_list}（暂无法确认出列），外层下轮自动复核")
                        break
                    if attempts < MAX_ATTEMPTS:
                        self._log(f"[{course_n}] ⚠️ 该课仍在列表（结果={result}）→ 重试 {attempts + 1}/{MAX_ATTEMPTS}")
                        self._prune_zn_windows()
                        time.sleep(5)
                        continue
                    if result == "done":
                        self._log(f"[{course_n}] {MAX_ATTEMPTS} 次 done 均未出列 → 加入跳过清单（假完成/需人工）")
                        skips.append(name)
                        self._save_json(self._skip_file(), skips)
                        break
                    defer_count[name] = defer_count.get(name, 0) + 1
                    if defer_count[name] >= DEFER_MAX:
                        self._log(f"[{course_n}] 累计 defer {defer_count[name]} 次（结果={result}）仍未出列 → 加入跳过清单（人工确认后可删除）")
                        skips.append(name)
                        self._save_json(self._skip_file(), skips)
                        break
                    defer_until[name] = time.time() + DEFER_COOLDOWN
                    self._log(f"[{course_n}] {MAX_ATTEMPTS} 次（结果={result}）均未出列 → 判为瞬态故障，defer 冷却 {DEFER_COOLDOWN // 60}min 后自动重试（第 {defer_count[name]}/{DEFER_MAX} 次，不进跳过清单）")
                    break
                if self._stop_flag:
                    break
                self._prune_zn_windows()
                time.sleep(5)
            self._log("退出")
        except Exception as e:
            self._log(f"引擎异常退出：{type(e).__name__}: {e}")
            # v1.0.281：记完整 traceback——此前只有一行 str(e)，02:02 崩溃花了
            # 十几分钟靠代码考古才定位到行号；有现场直接看栈。
            try:
                import traceback as _tb
                self._log(_tb.format_exc())
            except Exception:
                pass
        finally:
            with self._lock:
                self._state = "idle"
                self._current = ""
                self._current_pane = None


engine = StudyEngine()

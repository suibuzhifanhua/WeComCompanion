# -*- coding: utf-8 -*-
"""scheduler — 定时自动化（v1.0.313，对齐 WorkBuddy「定时自动化」）
=================================================================
任务清单存 产物库/定时任务.json：[{id, name, prompt, cron, enabled, last_run, last_result}]
cron 支持两种口语格式（不引入 cron 解析依赖）：
  "daily HH:MM"        —— 每天 HH:MM 执行（如 "daily 09:30"）
  "every N minutes"    —— 每 N 分钟执行（N ≥ 5，防误配打爆）
调度 daemon 30s 一 tick：到点任务串行执行（同一时刻只跑一个，执行期间标 running 防重复）；
执行 = 直接构造 build_chat_messages → 消费 agent_loop 到结束 → 产物/答复落
产物库/产出/定时_<name>_<时间>.md。全部本地，不依赖外部服务。
"""
import datetime
import json
import os
import threading
import traceback
import uuid

from core_utils import workspace_root, log_event
from core.config import atomic_write_json  # v1.0.510（P2-13）
from core.daemon import spawn_deadline_watchdog, start_periodic  # A13+B1：守护骨架收敛

SCHED_FILE = os.path.join("定时任务.json")
_lock = threading.Lock()
_thread = None
_running_flag = set()          # 正在执行的 schedule id（防重叠触发）
# v1.0.470（P1-13）：到点任务队列 + 串行消费线程（取代每任务一线程）
_tick_queue = []
_tick_queue_lock = threading.Lock()
# v1.0.510 优化（P2-14）：队列唤醒事件——原 worker 空闲时固定 _time.sleep(1) 轮询，
# 每秒空转唤醒一次（常驻 busy-poll）。改 Event.wait：入队时 set 立即唤醒，
# 空闲时挂起不占 CPU（保留 1s 兜底超时防丢信号）。
_tick_wakeup = threading.Event()
_serial_thread = None

VALID_CRON = "cron 需为 \"daily HH:MM\"（如 daily 09:30）或 \"every N minutes\"（N≥5）"

# v1.0.405（M7）：单次定时任务总时长上限（秒）——agent_loop 自带死循环熔断与 LLM
# 熔断，但工具层的个别死等/慢速路径仍可能拖几十分钟；30 分钟硬顶由看门狗置 abort。
SCHED_MAX_SECONDS = 1800


def sched_path():
    return os.path.join(workspace_root(), SCHED_FILE)


def list_schedules():
    with _lock:
        return _list_unlocked()


def _list_unlocked():
    """无锁读取（供已持 _lock 的写操作内部复用——threading.Lock 不可重入，锁内再调
    list_schedules 会永久死锁，实测踩过）。"""
    from core.files import read_json   # v1.0.654 A8：句柄样板收敛
    data = read_json(sched_path())
    return data if isinstance(data, list) else []


def _save(data):
    # v1.0.510（P2-13）：tmp+os.replace 样板收敛到 atomic_write_json
    atomic_write_json(sched_path(), data, indent=1)


def upsert(item):
    """新建/更新一个定时任务。item 需含 name/prompt/cron；有 id 即更新。返回 (ok, err/id)。"""
    name = str(item.get("name") or "").strip()
    prompt = str(item.get("prompt") or "").strip()
    cron = str(item.get("cron") or "").strip().lower()
    if not name or not prompt:
        return False, "name 与 prompt 必填"
    if not _valid_cron(cron):
        return False, VALID_CRON
    with _lock:
        data = _list_unlocked()
        sid = str(item.get("id") or "")
        if sid:
            for i, it in enumerate(data):
                if it.get("id") == sid:
                    it.update({"name": name, "prompt": prompt, "cron": cron,
                               "enabled": bool(item.get("enabled", it.get("enabled", True)))})
                    _save(data)
                    return True, sid
            return False, "任务不存在"
        # v1.0.313 修复：原秒级时间戳 id 同秒建多个任务必碰撞（删除/启停会串任务）——加 uuid 后缀
        sid = "sch_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4]
        data.append({"id": sid, "name": name, "prompt": prompt, "cron": cron,
                     "enabled": bool(item.get("enabled", True)), "last_run": "", "last_result": ""})
        _save(data)
        return True, sid


def _valid_cron(cron):
    if cron.startswith("daily "):
        try:
            hh, mm = cron[6:].split(":")
            return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
        except Exception:  # noqa: BLE001
            return False
    if cron.startswith("every "):
        try:
            return int(cron[6:].split()[0]) >= 5
        except Exception:  # noqa: BLE001
            return False
    return False


def delete_schedule(sid):
    with _lock:
        before = _list_unlocked()
        data = [it for it in before if it.get("id") != sid]
        if len(data) == len(before):
            return False
        _save(data)
        return True


def set_enabled(sid, enabled):
    with _lock:
        data = _list_unlocked()
        for it in data:
            if it.get("id") == sid:
                it["enabled"] = bool(enabled)
                _save(data)
                return True
    return False


def _due(it, now):
    """判断任务此刻是否到点（基于上次运行时间；tick 30s 粒度）。"""
    cron = it.get("cron", "")
    last = it.get("last_run", "")
    try:
        last_dt = datetime.datetime.strptime(last, "%Y-%m-%d %H:%M:%S") if last else None
    except Exception:  # noqa: BLE001
        last_dt = None
    if cron.startswith("every "):
        n = int(cron[6:].split()[0])
        if last_dt is None:
            return True
        return (now - last_dt).total_seconds() >= n * 60
    if cron.startswith("daily "):
        hh, mm = map(int, cron[6:].split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if last_dt is None:
            return now >= target
        return last_dt < target <= now
    return False


def _mark(it_id, now, running=None, result=None, status=None):
    """更新一条任务记录并落盘（v1.0.614 收敛：原来"找 id → 改字段 → _save"抄了三遍）。
    running=True/False 控制「执行中」徽标——前端 js/11 一直在读 s.running，
    而后端原先只在结束时 pop 掉它、从来没人写 True，导致手动点「立即跑」后界面毫无反应。"""
    with _lock:
        data = _list_unlocked()
        for x in data:
            if x.get("id") == it_id:
                if now is not None:
                    x["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
                if result is not None:
                    x["last_result"] = result
                if status is not None:
                    # v1.0.614：结构化状态。看板原来靠 last_result 里有没有"异常|失败"
                    # 四个字判成败（routes/b_admin.py:344）——超时中止的文案里没有这两个词，
                    # 就会被算成成功；现在判据落在字段上。
                    x["last_status"] = status
                if running is True:
                    x["running"] = True
                elif running is False:
                    x.pop("running", None)
        _save(data)


def _run_one(it, cfg):
    """执行一个定时任务：完整走一轮 agent_loop，答复落 产物库/产出/定时_<name>_<ts>.md。
    v1.0.405（M7）：①独立 pane_id（sched-<id>）——abort/断点与用户会话互不干扰；
    ②看门狗总时长上限 SCHED_MAX_SECONDS（默认 1800s）超时置 abort——防工具死等/
    LLM 空转占住线程与 LLM 并发闸一整夜；③finally 清理本 pane 的 abort 标志与断点。

    v1.0.614 返回结构化结果 `{"text","status","note"}`（原来只回一个字符串）：
    超时与被"问用户"卡住这两种情况，过去都走正常返回路径、通知照旧写「✅ 定时任务完成」，
    用户第二天看到的是"已完成"，实际单据根本没写。status：ok / timeout / blocked。
    """
    from agent import agent_loop, build_chat_messages
    from agent.session import _set_abort, drop_chat_state
    from agent_tools import _cdp_port
    from core.config_store import load_config
    cfg = cfg or load_config()
    port = _cdp_port(cfg)
    sched_pid = f"sched-{it['id']}"
    user_text = f"【定时任务：{it['name']}】\n{it['prompt']}"
    msgs = build_chat_messages([{"role": "user", "content": user_text}], cfg, port, None, False)

    # 看门狗：超时置位该 pane 的 abort 标志，agent_loop 下轮检查到后走正常中止路径
    # v1.0.656（A13）：一次性超时看门狗骨架收敛到 core.daemon.spawn_deadline_watchdog
    _wd_fired = {"hit": False}          # v1.0.614：超时要能事后判定，不能只留一行日志

    def _on_wd_fire():
        _wd_fired["hit"] = True
        log_event(f"[sched] 任务 {it['name']} 超过 {SCHED_MAX_SECONDS}s 上限，置位中止")
        _set_abort(sched_pid, True)

    _wd_thread, _wd_cancel = spawn_deadline_watchdog(
        SCHED_MAX_SECONDS, _on_wd_fire, name=f"wd-{sched_pid}")
    _blocked = []                        # ask_user / 审批请求：后台没有弹窗可点，等于任务没做完
    try:
        buf = ""
        for ev in agent_loop(msgs, cfg, pane_id=sched_pid, notify=False):   # notify=False：完成通知由下方按任务名发，防双响
            if isinstance(ev, str) and ev.startswith("data: "):
                try:
                    d = json.loads(ev[6:].strip())
                except Exception:  # noqa: BLE001
                    continue
                if d.get("type") == "token":
                    buf += d.get("text") or ""
                elif d.get("type") in ("ask_user", "approval_request"):
                    _blocked.append(str(d.get("question") or d.get("command") or d.get("tool") or "需要确认")[:120])
                elif d.get("type") == "approval_denied":
                    # v1.0.616（审查 P1）：后台拒绝不可撤销写操作＝这一步没做成。
                    # 以前不发事件，任务以 ok 收尾、通知写「✅ 完成」，用户整晚白等。
                    _blocked.append("已拒绝（后台无弹窗）：" + str(d.get("reason") or d.get("tool") or "")[:110])
        final = buf.split("【完成】", 1)[1] if "【完成】" in buf else buf
    finally:
        _wd_cancel.set()   # 提前取消看门狗（超时已触发则无副作用）
        _set_abort(sched_pid, False)   # 清 abort 标志（下次任务干净起步）
        drop_chat_state(sched_pid)     # 清内存+盘上断点（sched 任务无需「继续」）
    ts = datetime.datetime.now().strftime("%m-%d_%H%M")
    safe = "".join(ch for ch in it["name"] if ch not in '\\/:*?"<>|').strip() or "任务"
    if _wd_fired["hit"]:
        status, note = "timeout", f"超过 {SCHED_MAX_SECONDS}s 上限被中止，本轮未完成"
    elif _blocked:
        status, note = "blocked", "任务中途要问用户（后台无弹窗）：" + "；".join(_blocked[:2])
    else:
        status, note = "ok", ""
    body = final.strip() + (("\n\n> ⚠️ " + note) if note else "")
    try:
        from core_utils import save_workspace_file
        save_workspace_file("产出", f"定时_{safe}_{ts}.md", f"# 定时任务：{it['name']}\n\n{body}\n")
    except Exception as e:  # noqa: BLE001
        log_event(f"[sched] 结果落盘失败：{e}")
    return {"text": ((note + "｜" if note else "") + (final.strip()[:200] or "（无输出）")),
            "status": status, "note": note}


def _tick(cfg):
    now = datetime.datetime.now()
    for it in list_schedules():
        if not it.get("enabled"):
            continue
        if not _due(it, now):
            continue
        # v1.0.373 审查修复（P3-10）：检查+登记入锁（原 in+add 非原子，_tick 与 run_now
        # 并发可双跑同一任务）
        with _lock:
            if it["id"] in _running_flag:
                continue
            _running_flag.add(it["id"])
        log_event(f"[sched] 触发定时任务：{it['name']}（{it['cron']}）")

        # v1.0.470（P1-13）：到点任务入全局队列，由单一 worker 线程串行消费——
        # 原实现每任务各起一个线程，两个 daily 任务同刻触发会并发跑两个 agent_loop
        # 争用企微 CDP 窗口与 LLM 并发闸（与模块 docstring「到点任务串行执行」声明冲突，
        # 也是 batch_flow 互踩事故的同源场景）。run_now（用户手动点击）仍走独立线程。
        with _tick_queue_lock:
            _tick_queue.append({"it": it, "cfg": cfg, "now": now})
        _tick_wakeup.set()   # v1.0.510（P2-14）：入队即唤醒串行 worker


def _serial_worker():
    """v1.0.470（P1-13）：定时任务串行消费线程（start_daemon 启动，随主进程退出）。
    v1.0.510（P2-14）：空转等待由 sleep(1) 轮询改为 Event.wait 挂起。"""
    while True:
        job = None
        with _tick_queue_lock:
            if _tick_queue:
                job = _tick_queue.pop(0)
        if job is None:
            _tick_wakeup.wait(timeout=1.0)
            _tick_wakeup.clear()
            continue
        it, cfg, now = job["it"], job["cfg"], job["now"]

        def _run_job(it=it, cfg=cfg, now=now):
            _notify_done = None
            _mark(it["id"], None, running=True)     # v1.0.614：让「执行中…」徽标真的亮起来
            try:
                r = _run_one(it, cfg)
                res = r.get("text") or ""
                _st = r.get("status") or "ok"
                _mark(it["id"], now, running=False, result=res, status=_st)
                if _st == "timeout":
                    _notify_done = ("⚠️ 定时任务超时中止", it["name"] + "：" + res[:80])
                elif _st == "blocked":
                    _notify_done = ("⚠️ 定时任务未完成（需要确认）", it["name"] + "：" + res[:80])
                else:
                    _notify_done = ("✅ 定时任务完成", it["name"] + "：" + res[:80])
            except Exception as e:
                log_event(f"[sched] 执行异常：{e}\n{traceback.format_exc().rstrip()}")
                _mark(it["id"], now, running=False, result=f"执行异常：{e}", status="error")
                _notify_done = ("⚠️ 定时任务失败", it["name"] + "：" + str(e)[:80])
            finally:
                _running_flag.discard(it["id"])
                # v1.0.373（体验B4/需求5）：完成/失败 → 原生 Toast（v1.0.498：通知中心文件簇已删，Toast 单通道）
                # v1.0.523（静默模式）：silent_mode 下跳过 Toast/托盘气泡——执行结果只记
                # 日志与任务面板，不打断用户（toast 横幅/气泡闪烁均属"影响用户使用电脑"）
                if _notify_done:
                    try:
                        from core.config_store import silent_mode
                        if not silent_mode():
                            try:
                                from core_utils import native_toast
                                native_toast(*_notify_done)
                            except Exception:  # noqa: BLE001
                                pass
                            try:
                                from desktop import close_guard
                                close_guard.tray_notify(*_notify_done)
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001
                        pass

        _run_job()


def start_daemon(cfg=None):
    """启动调度线程（backend 启动时调用一次；重复调用安全）。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    from core.config_store import load_config
    cfg = cfg or load_config()
    # v1.0.652（审计 P2-B3）：清扫上次崩溃残留的 running 徽标——running 是执行态
    # 却随任务 JSON 落盘，进程崩溃后 "running": true 永久残留，前端徽标卡"执行中"。
    # 新进程启动时不可能有在跑任务，全部清掉。
    try:
        with _lock:
            data = _list_unlocked()
            _dirty = False
            for x in data:
                if x.pop("running", None) is not None:
                    _dirty = True
            if _dirty:
                _save(data)
    except Exception as e:  # noqa: BLE001
        log_event(f"[sched] 清扫残留 running 标记失败：{e}")

    def _tick30():
        _tick(load_config())

    # v1.0.656（B1）：30s tick 守护循环骨架收敛到 core.daemon.start_periodic
    # （原 _loop 的 while True + time.sleep(30) + try/except 吞异常）。
    _thread = start_periodic("scheduler", _tick30, 30)
    log_event("[sched] 定时调度线程已启动（30s tick）")
    # v1.0.470（P1-13）：串行消费线程（重复调用安全）
    global _serial_thread
    if not (_serial_thread and _serial_thread.is_alive()):
        _serial_thread = threading.Thread(target=_serial_worker, daemon=True, name="scheduler-worker")
        _serial_thread.start()


def run_now(sid, cfg=None):
    """立即执行一次（UI 按钮）。后台线程跑，立即返回。
    v1.0.361 审查修复（P1）：worker 全程登记 _running_flag 防重叠——原实现反而
    discard 掉防重标记且 worker 不登记，手动执行期间到点触发/连点按钮会双份并发跑。"""
    it = next((x for x in list_schedules() if x.get("id") == sid), None)
    if not it:
        return False
    # v1.0.373 审查修复（P3-10）：检查+登记入锁（同 _tick）
    with _lock:
        if sid in _running_flag:
            log_event(f"[sched] 任务 {sid} 正在执行，忽略重复立即执行")
            return True
        _running_flag.add(sid)
    it = dict(it)
    it.pop("last_run", None)     # 清上次运行记录 → _due 判为到点
    it["id"] = sid
    # run_now 走独立执行（不走 _tick 判定）：直接起 worker
    def _worker():
        # v1.0.614：手动「立即跑」同样要落 running（前端徽标一直在读），
        # 且超时/被确认卡住不能再当成正常完成；原来这里还漏了 pop("running")。
        _mark(sid, None, running=True)
        try:
            r = _run_one(it, cfg)
            _mark(sid, datetime.datetime.now(), running=False, result=r.get("text") or "",
                  status=r.get("status") or "ok")
            if r.get("status") == "ok":
                log_event(f"[sched] 手动执行完成：{it.get('name')}")
            else:
                log_event(f"[sched] 手动执行未正常完成（{r.get('status')}）：{r.get('note')}")
        except Exception as e:  # noqa: BLE001
            log_event(f"[sched] 手动执行异常：{e}\n{traceback.format_exc().rstrip()}")
            _mark(sid, datetime.datetime.now(), running=False, result=f"执行异常：{e}", status="error")
        finally:
            _running_flag.discard(sid)
    threading.Thread(target=_worker, daemon=True, name=f"sched-now-{sid}").start()
    return True

# -*- coding: utf-8 -*-
"""knowledge_ops.py — 知识库空闲整理 + 每日 AI 消化（v1.0.301）

用户需求：「产物库知识库空闲时间自动整理分类，并且根据这些内容每天进行消化」。
口径（AskUserQuestion 确认）：
  - 消化 = AI 消化文档（要点摘要+复习题+跨文档关联），存 知识库/消化/
  - 整理 = 保守（索引+查重），绝不移动/删除/重命名用户的文件
  - 时机 = 跨天后首个空闲（不固定钟点）

两类后台维护，均 daemon 线程 60s 轮询，空闲自动触发：

1) organize — 空闲整理分类（保守）：
   - 扫描知识库三分类（课件/题库/制度）
   - 生成/更新 知识库/INDEX.md：每分类文件清单（按更新时间倒序）+ 统计
     + 同内容文件（SHA1）查重告警——只标注，不删不并
   - 4 小时内不重复重写索引

2) digest — 每日消化（AI 消化文档）：
   - 触发：当天尚无 消化_<日期>.md + 空闲
   - 增量：只消化最近 24h 内 mtime 更新过的 .md（最多 10 份，单份截 3 万字符）
   - LLM（resolve_model + _llm_complete，非流式）生成
     知识库/消化/消化_YYYY-MM-DD.md：新内容概览/要点/复习题/跨文档关联
   - 幂等：当天文件已存在即跳过（手动可 force 覆盖）；无新文件不写文件、
     不计数，等新文件出现后（仍属当天）自动补消化
   - LLM 失败当天最多重试 3 次

空闲判定（build.py 180s 守卫同款口径，窗口放大）：
   companion.log 尾部 400 行内最近 [agent]/[llm] 活动距今 < 600s → 忙；
   且学习引擎（study_engine）运行中 → 忙。窗口取 600s 是为了盖住单次
   长工具调用（pc_run 最长 300s 等待）期间无 LLM 请求的空档。

配置：config.json kb_auto 段 {organize: bool, digest: bool}（默认全开）。
必须声明进 config_store.DEFAULT_CONFIG（未声明的顶层段会被 load/save 抹掉，
v1.0.300 实测教训）。

手动触发：POST /api/knowledge/maintenance/run {action: organize|digest, force?}
"""
import hashlib
import os
import re
import threading
import time
from datetime import date, datetime

from core_utils import (log_event, workspace_root, companion_log_dir,
                        KB_ROOT, KB_CATEGORIES, last_agent_activity_epoch)
from core.config_store import load_config
from core.daemon import start_periodic   # A13+B1：守护骨架收敛
from llm_client import resolve_model, _llm_complete

# ---------- 常量 ----------
POLL_INTERVAL = 60            # 轮询间隔（秒）
IDLE_WINDOW = 600             # 空闲窗口（秒）：600s 无 [agent]/[llm] 活动才算空闲
ORGANIZE_MIN_GAP = 4 * 3600   # 索引最小更新间隔（秒），避免频繁重写
INDEX_NAME = "INDEX.md"       # 放在 知识库/ 根（不在三分类内，不污染分类口径）
DIGEST_DIR = "消化"           # 知识库/消化/（ws_sub 白名单外，直接拼 workspace_root）
DIGEST_MAX_FILES = 10         # 单次消化最多文件数
DIGEST_MAX_CHARS = 30000      # 单文件截断字符数
DIGEST_WINDOW = 24 * 3600     # 增量窗口（秒）
DIGEST_MAX_RETRY = 3          # 当天 LLM 失败重试上限
DIGEST_STARTUP_GRACE = 120    # v1.0.482：启动后 120s 内不自动消化——把模型留给用户第一件事
_HASH_LIMIT = 5 * 1024 * 1024  # 查重只 hash 前 5MB（知识库文件都是小文本）


# ============================================================
# 空闲判定
# ============================================================
def _last_activity_epoch():
    """日志尾部最近一条 [agent]/[llm] 的时间戳（epoch 秒）；无记录返回 None。
    v1.0.302：改接 core_utils.last_agent_activity_epoch 单点实现（原与 build.py 180s
    守卫各写一遍同款扫描算法）。"""
    return last_agent_activity_epoch(os.path.join(companion_log_dir(), "companion.log"))


def is_idle():
    """空闲 = 600s 内无 agent/LLM 活动 且 学习引擎未运行。日志无记录（全新）视为空闲。"""
    a = _last_activity_epoch()
    if a is not None and (time.time() - a) < IDLE_WINDOW:
        return False
    try:
        from services.study_engine import engine  # 惰性导入防循环
        if engine.status().get("running"):
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


# ============================================================
# 整理（保守：索引 + 查重，不动任何文件）
# ============================================================
def _sha1_fp(fp, limit=_HASH_LIMIT):
    try:
        h = hashlib.sha1()
        with open(fp, "rb") as f:
            h.update(f.read(limit))
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def _iter_kb_files(only_md=False):
    """v1.0.479：递归枚举知识库全部 .md（原实现只扫三个分类目录的**一层**，
    新增的 知识库/企微文件/（Cache/账号/年月 嵌套结构）完全参与不进来）。
    返回 [(cat, rel, fp, size, mtime)]：cat=顶层子目录名，rel=相对知识库根路径。
    消化/ 目录与 INDEX.md 跳过（消化产物不二次消化）。"""
    root = os.path.join(workspace_root(), KB_ROOT)
    out = []
    try:
        for dirpath, dirs, files in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
            # 消化产物目录整支跳过；INDEX.md 不入清单
            if rel_dir == DIGEST_DIR or rel_dir.startswith(DIGEST_DIR + "/"):
                dirs[:] = []
                continue
            for fn in files:
                if fn == INDEX_NAME:
                    continue
                if only_md and not fn.lower().endswith(".md"):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if not os.path.isfile(fp):
                    continue
                cat = rel_dir.split("/")[0] if rel_dir != "." else "(根)"
                rel = fn if rel_dir == "." else rel_dir + "/" + fn
                out.append((cat, rel, fp, st.st_size, st.st_mtime))
    except Exception as e:  # noqa: BLE001
        log_event(f"[kb-ops] 枚举知识库失败：{e}")
    return out


def _kb_files():
    """扫描知识库 → {cat: [{name,size,mtime,sha1}]}。v1.0.479 改递归（name 用相对路径）。"""
    out = {}
    for cat, rel, fp, size, mt in _iter_kb_files():
        rows = out.setdefault(cat, [])
        rows.append({"name": rel, "size": size, "mtime": mt, "sha1": _sha1_fp(fp)})
    return out


def organize():
    """生成 知识库/INDEX.md（清单+统计+查重告警）。绝不移动/删除/重命名文件。"""
    files = _kb_files()
    total = sum(len(v) for v in files.values())
    groups = {}
    for cat, rows in files.items():
        for r in rows:
            if r["sha1"]:
                groups.setdefault(r["sha1"], []).append(r["name"])   # v1.0.479：name 已含 cat 前缀
    dups = {h: ps for h, ps in groups.items() if len(ps) > 1}
    # v1.0.479：name 已含分类前缀（递归相对路径），重复告警不再拼 "cat/" 前缀

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # v1.0.479：分类动态化——递归扫描后顶层目录不止内置三分类（含 企微文件/ 等新增来源）
    _cats = [c for c in (list(KB_CATEGORIES) + [c for c in files if c not in KB_CATEGORIES])
             if files.get(c)]
    L = ["# 知识库索引（自动生成，请勿手改）",
         f"- 更新时间：{now}",
         f"- 文件总数：{total}",
         f"- 分类：{' / '.join(_cats) if _cats else '（空）'}",
         ""]
    for cat in _cats:
        rows = sorted(files[cat], key=lambda x: -x["mtime"])
        L.append(f"## {cat}（{len(rows)} 份）")
        if not rows:
            L.append("（空）")
        for r in rows:
            L.append(f"- {r['name']} | {r['size'] // 1024}KB | "
                     f"{datetime.fromtimestamp(r['mtime']).strftime('%Y-%m-%d %H:%M')}")
        L.append("")
    if dups:
        L.append("## 重复文件告警（内容哈希相同，请人工核对；自动整理不会删除）")
        for _h, ps in dups.items():
            L.append("- " + " ≡ ".join(sorted(ps)))
        L.append("")

    idx = os.path.join(workspace_root(), KB_ROOT, INDEX_NAME)
    try:
        os.makedirs(os.path.dirname(idx), exist_ok=True)
        # v1.0.364 审查修复（健壮性#25）：原子写（前端/agent 并发读索引，直接覆盖
        # 可能读到截断内容）
        tmp = idx + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        os.replace(tmp, idx)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"INDEX 写入失败：{e}"}
    log_event(f"[kb-ops] 整理完成：{total} 份文件，{len(dups)} 组重复，索引 {idx}")
    return {"ok": True, "files": total, "dup_groups": len(dups), "index": idx}


# ============================================================
# 每日消化（LLM 增量消化 → 知识库/消化/消化_<日期>.md）
# ============================================================
def _digest_dir():
    d = os.path.join(workspace_root(), KB_ROOT, DIGEST_DIR)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


def _digest_path(day=None):
    day = day or date.today().strftime("%Y-%m-%d")
    return os.path.join(_digest_dir(), f"消化_{day}.md")


def latest_digest_date():
    """最新一份消化的日期（YYYY-MM-DD）；从未消化返回 None。"""
    dates = []
    try:
        for fn in os.listdir(_digest_dir()):
            m = re.match(r"消化_(\d{4}-\d{2}-\d{2})\.md$", fn)
            if m:
                dates.append(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return max(dates) if dates else None


def _collect_new_files(window=DIGEST_WINDOW, max_files=DIGEST_MAX_FILES):
    """最近 window 秒内 mtime 更新过的 .md（递归全知识库，含企微文件嵌套目录），
    mtime 倒序，上限 max_files。返回 [(mtime, cat, rel, path, size)]。
    消化/ 目录自身不重复消化（_iter_kb_files 已跳过）。"""
    cutoff = time.time() - window
    items = [(mt, cat, rel, fp, size)
             for cat, rel, fp, size, mt in _iter_kb_files(only_md=True)
             if mt >= cutoff]
    items.sort(key=lambda x: -x[0])
    return items[:max_files]


def _read_md(fp, limit=DIGEST_MAX_CHARS):
    for enc in ("utf-8", "gbk"):
        try:
            with open(fp, "r", encoding=enc) as f:
                txt = f.read(limit + 1)
            if len(txt) > limit:
                return txt[:limit] + "\n…[已截断]"
            return txt
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:  # noqa: BLE001
            return ""
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception:  # noqa: BLE001
        return ""


_DIGEST_PROMPT = (
    "你是企业微信伴侣软件的知识管理员。用户下面是最近更新的知识库文件"
    "（课件=课程 OCR 素材 / 题库=考试答题记录 / 制度=公司制度原文）。"
    "请生成每日消化，用 Markdown，固定四个小节：\n\n"
    "## 新内容概览\n（每个文件一句话，说明它是什么）\n\n"
    "## 要点\n（提炼 3-10 条关键知识点；答题记录类总结锁定结论与易错点）\n\n"
    "## 复习题\n（基于内容出 3-5 道复习题，每题注明来源文件）\n\n"
    "## 跨文档关联\n（文件间有呼应/差异/互补则指出；没有写「无」）\n\n"
    "要求：简洁直接；只依据提供的文件内容，不要编造；不要复述原文大段内容。"
)


def digest(force=False):
    """每日 AI 消化。force=True 覆盖当天已有文件（手动「立即消化」用）。
    返回 {ok, path?/skipped?/error?, files?}。"""
    target = _digest_path()
    if os.path.exists(target) and not force:
        return {"ok": True, "skipped": "already", "path": target}
    items = _collect_new_files()
    if not items:
        return {"ok": True, "skipped": "no_new_files"}
    model = resolve_model(load_config())
    if not model:
        return {"ok": False, "error": "没有可用模型（先在模型页启用一个模型）"}
    parts = []
    names = []
    # v1.0.652（审计 P2-D1）：总字符预算 60k——旧最多 10 文件 × 单文件 30k = 30 万字符
    # 一次性塞进 LLM，本地小模型上下文直接顶爆（项目其它地方都在防这个）。
    _budget = 60000
    for mt, cat, fn, fp, size in items:
        if _budget <= 0:
            log_event(f"[kb-ops] 消化预算 60k 已用尽，剩余 {len(items) - len(names)} 份留待明日")
            break
        txt = _read_md(fp)[:_budget]
        _budget -= len(txt)
        names.append(f"{cat}/{fn}")
        parts.append(
            f"===== 文件：{cat}/{fn}（约 {size // 1024}KB，"
            f"更新于 {datetime.fromtimestamp(mt).strftime('%m-%d %H:%M')}）=====\n"
            + txt)
    if not parts:
        return {"ok": True, "skipped": "no_new_files"}
    user = "以下是最近更新的知识库文件清单：\n\n" + "\n\n".join(parts)
    try:
        out = _llm_complete(model, [
            {"role": "system", "content": _DIGEST_PROMPT},
            {"role": "user", "content": user},
        ])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"LLM 调用异常：{e}"}
    if not out or not out.strip():
        return {"ok": False, "error": "LLM 生成失败（返回为空）"}
    header = (f"# 知识库每日消化 {date.today().strftime('%Y-%m-%d')}\n\n"
              f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"- 消化范围：最近 24h 新增/更新 {len(items)} 份：" + "、".join(names) + "\n\n")
    try:
        # v1.0.652（审计 P2-D1）：原子写——旧裸覆盖写，中断留半截文件，而 digest()
        # 以 os.path.exists(target) 判"今日已消化"，半截文件让当日永远跳过。
        _tmp = target + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            f.write(header + out.strip() + "\n")
        os.replace(_tmp, target)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"消化文件写入失败：{e}"}
    log_event(f"[kb-ops] 每日消化完成：{len(items)} 份文件 → {target}")
    return {"ok": True, "path": target, "files": names}


# ============================================================
# 守护线程：空闲时自动 整理 + 每日消化
# ============================================================
class KnowledgeOps:
    """60s 轮询：空闲 + 索引过期(4h/缺失) → organize；空闲 + 当天未消化 → digest。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._busy = False
        self._busy_lock = threading.Lock()   # v1.0.364 审查修复（M11）：忙标志 check-and-set 原子化
        self._last_organize = 0
        self._digest_fails = {}  # 日期 -> 当天失败次数
        # v1.0.479（用户反馈：手动整理/消化看不到进度）：执行状态记账——
        # 当前动作/开始时间/上次结果，status() 暴露给前端轮询渲染
        self._action = None          # "organize" | "digest" | None
        self._started = 0.0
        self._last_result = None     # {"action","ok","detail","ts","manual"}
        self._digest_empty_date = None   # 当日自动消化跑过但 24h 无新增（区分"没执行"和"没东西可消化"）
        self._boot = 0.0                 # 线程启动时刻（消化启动缓冲用）

    def _action_begin(self, action):
        self._action = action
        self._started = time.time()

    def _action_end(self, action, r, manual):
        detail = ""
        if r.get("ok"):
            f = r.get("files")
            # v1.0.481：organize 返回 files=int（份数），digest 返回 files=list（文件名）
            if isinstance(f, int):
                detail = f"{f} 份文件"
            elif isinstance(f, list):
                detail = f"{len(f)} 份"
            else:
                detail = "完成"
        else:
            detail = str(r.get("error") or r.get("skipped") or "")[:80]
        self._last_result = {"action": action, "ok": bool(r.get("ok")),
                             "skipped": r.get("skipped") or "", "detail": detail,
                             "ts": time.strftime("%H:%M"), "manual": manual}
        self._action = None
        if action == "digest" and r.get("ok") and r.get("skipped") == "no_new_files":
            self._digest_empty_date = date.today().strftime("%Y-%m-%d")

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._boot = time.time()
            # v1.0.656（B1）：守护循环骨架收敛到 core.daemon.start_periodic
            # （原 self._loop 的 while + try/except 吞异常 + self._stop.wait(POLL_INTERVAL)）。
            # 类接口不变：self._stop 仍指当前线程的停止事件，stop() 语义原样。
            self._thread = start_periodic("kb-ops", self._tick, POLL_INTERVAL)
            self._stop = self._thread.stop_event
            log_event("[kb-ops] 知识库维护线程已启动（空闲整理 + 每日消化）")

    def stop(self):
        self._stop.set()

    def status(self):
        today = date.today().strftime("%Y-%m-%d")
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "busy": self._busy,
            "idle": is_idle(),
            "last_organize": self._last_organize,
            "latest_digest": latest_digest_date(),
            "today_digest": latest_digest_date() == today or self._digest_empty_date == today,
            "today_digest_empty": self._digest_empty_date == today,
            "digest_fails_today": self._digest_fails.get(today, 0),
            "idle_window_sec": IDLE_WINDOW,
            # v1.0.479：执行进度
            "action": self._action,
            "elapsed": int(time.time() - self._started) if self._busy and self._started else 0,
            "last_result": self._last_result,
        }

    def run_manual(self, action, force=False):
        """UI 手动触发（后台线程执行，立即返回 accepted；结果看 status 轮询）。
        v1.0.364 审查修复（M11）：check-and-set 加锁（threaded 双请求可同时过检查双跑）。"""
        if action not in ("organize", "digest"):
            return {"ok": False, "error": f"未知 action：{action}（可选 organize/digest）"}
        with self._busy_lock:
            if self._busy:
                return {"ok": False, "error": "维护任务进行中，稍后再试"}
            self._busy = True

        def _go():
            r = {"ok": False, "error": "未执行"}
            try:
                self._action_begin(action)
                if action == "organize":
                    r = organize()
                    if r.get("ok"):
                        self._last_organize = time.time()
                else:
                    r = digest(force=force)
                log_event(f"[kb-ops] 手动 {action} 结果：ok={r.get('ok')} "
                          f"skipped={r.get('skipped')} err={r.get('error', '')}")
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                log_event(f"[kb-ops] 手动 {action} 异常：{type(e).__name__}: {e}")
            finally:
                try:
                    self._action_end(action, r, manual=True)
                except Exception:  # noqa: BLE001
                    pass
                self._busy = False

        threading.Thread(target=_go, name=f"kb-ops-manual-{action}", daemon=True).start()
        return {"ok": True, "accepted": True, "action": action}

    # ---------- 内部 ----------
    def _tick(self):
        if self._busy:
            return
        cfg = load_config()
        ka = cfg.get("kb_auto")
        if not isinstance(ka, dict):
            ka = {}
        do_org = bool(ka.get("organize", True))
        do_dig = bool(ka.get("digest", True))
        if not (do_org or do_dig):
            return
        if not is_idle():
            return
        today = date.today().strftime("%Y-%m-%d")

        if do_org:
            idx = os.path.join(workspace_root(), KB_ROOT, INDEX_NAME)
            fresh = False
            try:
                fresh = (time.time() - os.path.getmtime(idx)) < ORGANIZE_MIN_GAP
            except OSError:
                pass  # 索引不存在 → 需要整理
            if not fresh:
                with self._busy_lock:   # v1.0.364（M11）：与 run_manual 同锁
                    if self._busy:
                        _org_go = False
                    else:
                        self._busy = True
                        _org_go = True
            else:
                _org_go = False
            if _org_go:
                try:
                    self._action_begin("organize")
                    r = organize()
                    if r.get("ok"):
                        self._last_organize = time.time()
                    self._action_end("organize", r, manual=False)
                except Exception as e:  # noqa: BLE001
                    self._action_end("organize", {"ok": False, "error": f"{type(e).__name__}: {e}"}, manual=False)
                    log_event(f"[kb-ops] 自动整理异常：{type(e).__name__}: {e}")
                finally:
                    self._busy = False

        if do_dig:
            if latest_digest_date() == today:
                return
            # v1.0.482：给用户让路——①启动后 2 分钟内不自动消化（实测 20:38 启动即
            # 抢跑消化，用户的先出方案请求在单槽 vLLM 上排队 2.5 分钟）；②任何
            # 用户对话/后台 LLM 调用在途时让路，下一轮 tick（60s 后）再试。
            if time.time() - getattr(self, "_boot", 0) < DIGEST_STARTUP_GRACE:
                return
            try:
                from llm_client import llm_active_count
                if llm_active_count() > 0:
                    return
            except Exception:  # noqa: BLE001
                pass
            fails = self._digest_fails.get(today, 0)
            if fails >= DIGEST_MAX_RETRY:
                return
            with self._busy_lock:   # v1.0.364（M11）：与 run_manual 同锁
                if self._busy:
                    return
                self._busy = True
            try:
                self._action_begin("digest")
                r = digest()
                if not r.get("ok") and r.get("skipped") != "no_new_files":
                    self._digest_fails[today] = fails + 1
                    log_event(f"[kb-ops] 每日消化失败（{self._digest_fails[today]}/{DIGEST_MAX_RETRY}）："
                              f"{r.get('error')}")
                # no_new_files：不写文件、不计数——等新文件出现后（仍属当天）自动补消化
                self._action_end("digest", r, manual=False)
            except Exception as e:  # noqa: BLE001
                self._action_end("digest", {"ok": False, "error": f"{type(e).__name__}: {e}"}, manual=False)
                log_event(f"[kb-ops] 自动消化异常：{type(e).__name__}: {e}")
            finally:
                self._busy = False


keeper = KnowledgeOps()

# -*- coding: utf-8 -*-
"""services.wecom_archive — 企业微信聊天文件知识库（v1.0.477）
=================================================================
静默分析企微本机存储的【可读数据】，沉淀为知识库（agent 直接可检索）：

- Documents/WXWork/<账号ID>/Cache/File/<年月>/：聊天收发的附件（明文：pdf/docx/xlsx/txt…）
- Documents/WXWork/<账号ID>/WeDrive/：企业网盘文件（明文）

合规边界（明确不做的事）：
- 消息文本库（Data/message.db、Index/message_index.db 等）为 SQLCipher 类 AES 加密，
  本模块【不做】内存密钥提取——加密是企业微信有意设计，强行解密属合规风险。
  合法的消息文本通道是企业管理端「会话存档」API（需管理员开通），后续可接。

产出：产物库/知识库/企微文件/<CacheFile|WeDrive>/<年月>/<原文件名>.md
      （内容 + 元信息头），复用 kb-ops 的定时整理自动进 INDEX。
增量：按 (size, mtime) 指纹台账（产物库/台账/企微文件索引.json），只解析新增/变更文件。
"""
import io
import json
import os
import re
import threading
import time
import datetime

from core.paths import workspace_root
from core.logging import log_event
from core.config import atomic_write_json
from core.daemon import start_periodic   # A13+B1：守护骨架收敛

# 解析支持的扩展名（与 core.files._parse_file_content 能力对齐）
_DOC_EXTS = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
             ".txt", ".md", ".csv", ".json", ".rtf", ".html", ".xml", ".log")
_MAX_FILE_BYTES = 50 * 1024 * 1024      # 单文件上限（大视频/压缩包跳过）
_SCAN_DIR_NAMES = ("Cache/File", "WeDrive")  # 扫描的相对目录（跳过 Temp/.Trash）

_state = {"running": False, "last_scan": "", "last_result": {}, "accounts": []}
_state_lock = threading.Lock()


# ---------------- 用户授权闸（v1.0.588 合规收口） ----------------
# 本模块会把 Documents/WXWork 下聊天附件的【正文】复制进 产物库/知识库。v1.0.477 起
# enabled 默认 True，等于首启 2 分钟就未经任何人同意开始搬运聊天内容——与模块头部
# 写的合规边界声明不一致。现拆成两个开关：enabled=用户是否想要该功能，
# consent=是否已明确授权解析聊天文件；两者都为真才真正跑。
def _consented():
    try:
        from core.config_store import load_config
        return bool(((load_config() or {}).get("wecom_archive") or {}).get("consent"))
    except Exception:  # noqa: BLE001
        return False   # fail-closed：读不到配置就不碰他人聊天文件


def set_consent(value):
    """写入授权状态（知识库页「授权」按钮调用）。返回是否落盘成功。"""
    try:
        from core.config_store import update_config

        def _m(cfg):
            sec = cfg.get("wecom_archive")
            if not isinstance(sec, dict):
                sec = {}
                cfg["wecom_archive"] = sec
            sec["consent"] = bool(value)
            return True
        return bool(update_config(_m))
    except Exception:  # noqa: BLE001
        return False


def locate_accounts():
    """枚举企微数据目录（Documents/WXWork/<账号ID>/）。返回 [{dir, account, mtime}]。
    Documents 定位兼容重定向：注册表/环境变量不可靠时按常见候选试。"""
    import glob as _glob
    out = []
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    # v1.0.652（审计 P2-F1）：ONEDRIVE 缺失时不再用 "\0" 当无效路径哨兵——
    # `os.path.join("\0", "Documents", "WXWork")` 含 WXWork 会被加进 roots，
    # glob 对含空字节路径抛 ValueError（embedded null character），locate_accounts
    # 无 try → 整个归档功能在未装 OneDrive 的机器上全灭且报错误导。
    cands = [docs, os.path.join(os.environ.get("USERPROFILE", docs) or docs, "Documents")]
    _od = os.environ.get("ONEDRIVE") or ""
    if _od:
        cands.append(os.path.join(_od, "Documents", "WXWork"))
    roots = {os.path.join(docs, "WXWork")}
    for c in cands:
        if c and "WXWork" in c:
            roots.add(c)
    for root in roots:
        for d in _glob.glob(os.path.join(root, "*")):
            if os.path.isdir(d) and re.fullmatch(r"\d{10,}", os.path.basename(d)):
                try:
                    mt = os.path.getmtime(d)
                except OSError:
                    mt = 0
                out.append({"dir": d, "account": os.path.basename(d), "mtime": mt})
    out.sort(key=lambda x: -x["mtime"])   # 最近使用的账号优先
    return out


def _iter_source_files(account_dir):
    """遍历账号目录下 Cache/File 与 WeDrive 的目标文件。yield (abs_path, rel_kind, rel_sub)。"""
    for kind in _SCAN_DIR_NAMES:
        base = os.path.join(account_dir, kind.replace("/", os.sep))
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in (".Temp", ".Trash", "Temp") and not d.startswith(".")]
            for fn in filenames:
                ap = os.path.join(dirpath, fn)
                ext = os.path.splitext(fn)[1].lower()
                if ext not in _DOC_EXTS:
                    continue
                try:
                    if os.path.getsize(ap) > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(ap, base).replace("\\", "/")
                yield ap, kind.split("/")[0], rel


def _ledger_path():
    return os.path.join(workspace_root(), "台账", "企微文件索引.json")


def _load_ledger():
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_ledger(d):
    try:
        os.makedirs(os.path.dirname(_ledger_path()), exist_ok=True)
        # v1.0.619：改走 atomic_write_json——原 json.dump(..., open(...)) 句柄不关，
        # 且写一半被中断会留下半截台账（_load_ledger 只能整本当损坏返回 {}，归档计数清零）。
        atomic_write_json(_ledger_path(), d, indent=1)
    except Exception as e:  # noqa: BLE001
        log_event(f"[wecom-archive] 台账写入失败（忽略）：{e}")


def status():
    """知识库页展示：账号/文件量/已归档/待OCR/上次扫描。"""
    ledger = _load_ledger()
    accounts = locate_accounts()
    total_src = 0
    for a in accounts[:3]:   # 最多看前 3 个活跃账号
        for _ap, _k, _r in _iter_source_files(a["dir"]):
            total_src += 1
    needs_ocr = sum(1 for v in ledger.values() if "扫描件" in (v.get("note") or ""))
    consent = _consented()   # 取配置在锁外做，不进 _state_lock
    with _state_lock:
        return {
            "accounts": [{"account": a["account"][-8:], "dir": a["dir"]} for a in accounts],
            "source_files": total_src,
            "archived": len([v for v in ledger.values() if v.get("archived_to")]),
            "needs_ocr": needs_ocr,
            "consent": consent,
            "last_scan": _state.get("last_scan", ""),
            "last_result": _state.get("last_result", {}),
            "running": _state.get("running", False),
            "note": "消息文本（Data/message.db 等）为企微加密存储，本功能只分析明文附件与企业网盘文件；"
                    "聊天文字的合规通道是企业管理端「会话存档」。",
        }


def archive_once(max_files=200, force=False):
    """静默分析主入口：增量解析企微明文文件入知识库。返回 {ok, scanned, new, failed, skipped}。"""
    if not _consented():
        log_event("[wecom-archive] 未获用户授权，跳过本轮扫描（知识库页「企微文件」处可授权）")
        return {"ok": False, "need_consent": True,
                "error": "尚未授权解析本机企微聊天文件；请先在知识库页点「授权解析」"}
    with _state_lock:
        if _state.get("running"):
            return {"ok": False, "error": "上一轮扫描仍在进行"}
        _state["running"] = True
    t0 = time.time()
    scanned = new = failed = skipped = 0
    try:
        accounts = locate_accounts()
        if not accounts:
            log_event("[wecom-archive] 未找到企微数据目录（Documents/WXWork）")
            return {"ok": False, "error": "未找到企微数据目录（请确认企微已安装并登录过）"}
        ledger = _load_ledger()
        from core_utils import _parse_file_content
        kb_base = os.path.join(workspace_root(), "知识库", "企微文件")
        for a in accounts:
            acc_tag = a["account"][-6:]
            for ap, kind, rel in _iter_source_files(a["dir"]):
                scanned += 1
                if scanned > max_files * 3:   # 安全上限（防止首启超大库卡死）
                    break
                try:
                    st = os.stat(ap)
                except OSError:
                    continue
                fp = f"{st.st_size}:{int(st.st_mtime)}"
                prev = ledger.get(ap)
                if prev and prev.get("fp") == fp and not force:
                    skipped += 1
                    continue
                try:
                    _sz = st.st_size
                    kind_, content, note = _parse_file_content(ap, _sz)
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    log_event(f"[wecom-archive] 解析失败 {ap}：{e}")
                    continue
                text = (content or "").strip()
                if not text:
                    # v1.0.477：空文本标记原因（扫描件可 OCR / 老格式需 Office 转换），
                    # 台账记 fp 防重复重试，status() 汇总 needs_ocr 数
                    skipped += 1
                    _ext = os.path.splitext(ap)[1].lower()
                    _note = ""
                    if _ext == ".pdf" or kind_ == "pdf":
                        _note = "无文本层（疑似扫描件）——待 OCR 能力处理"
                    elif _ext in (".xls", ".doc", ".ppt"):
                        _note = "老格式需本机 Office/WPS 转换，本机不可用"
                    ledger[ap] = {"fp": fp, "archived_to": "", "ts": time.strftime("%Y-%m-%d %H:%M"),
                                  "note": _note}
                    continue
                # 落盘：知识库/企微文件/<kind>/<年月>/<原名>.md
                rel_parts = rel.split("/")
                sub = "/".join(rel_parts[:-1])
                base = rel_parts[-1]
                out_dir = os.path.join(kb_base, kind, acc_tag, sub.replace("/", os.sep))
                os.makedirs(out_dir, exist_ok=True)
                out_name = re.sub(r'[\\/:*?"<>|]', "_", base) + ".md"
                out_path = os.path.join(out_dir, out_name)
                meta = (f"---\n来源: 企微{('聊天文件' if kind == 'Cache' else '企业网盘')}\n"
                        f"原路径: {kind}/{rel}\n"
                        f"采集时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                        f"大小: {_sz // 1024}KB\n解析: {kind_}" +
                        (f"（{note}）" if note else "") + "\n---\n\n")
                try:
                    with io.open(out_path, "w", encoding="utf-8") as f:   # v1.0.619：原写法句柄不关
                        f.write(meta + text)
                    new += 1
                    ledger[ap] = {"fp": fp, "archived_to": out_path, "ts": time.strftime("%Y-%m-%d %H:%M")}
                except OSError as e:
                    failed += 1
                    log_event(f"[wecom-archive] 落盘失败 {out_path}：{e}")
        _save_ledger(ledger)
        r = {"ok": True, "scanned": scanned, "new": new, "failed": failed,
             "skipped": skipped, "elapsed": round(time.time() - t0, 1)}
        with _state_lock:
            _state["last_scan"] = time.strftime("%Y-%m-%d %H:%M")
            _state["last_result"] = r
        log_event(f"[wecom-archive] 静默分析完成：扫描 {scanned}，新增入库 {new}，"
                  f"失败 {failed}，跳过 {skipped}（{r['elapsed']}s）")
        # 触发知识库索引刷新（organize 幂等；失败不致命——kb-ops 定时也会整理）
        try:
            from services import knowledge_ops
            knowledge_ops.organize()
        except Exception as e:  # noqa: BLE001
            log_event(f"[wecom-archive] 知识库索引刷新失败（kb-ops 会兜底）：{e}")
        return r
    except Exception as e:  # noqa: BLE001
        log_event(f"[wecom-archive] 静默分析异常：{e}")
        return {"ok": False, "error": str(e)}
    finally:
        with _state_lock:
            _state["running"] = False


def archive_async():
    """后台线程触发（UI 按钮用），立即返回当前状态。"""
    with _state_lock:
        if _state.get("running"):
            return dict(_state)
    threading.Thread(target=archive_once, daemon=True, name="wecom-archive").start()
    with _state_lock:
        return dict(_state)


_daemon = None

# v1.0.656（B1）：调度循环收敛到 core.daemon.start_periodic 的 30s tick 状态机，
# 与原 _loop 的 while+for 分段等待逐拍等价（tick 间隔仍 30s）：
#   · 首启 120s 后跑一次增量，之后每 6h 一次（_cycle["wait"] 首拍 120s，之后恒 6h）；
#   · 每 30s 读一次归档开关：任一拍读到关闭 → 本轮作废、重新起算 6h（原 break 语义，
#     首启段被中止后同样回到 6h）；读失败留痕按启用继续等；
#   · 全分段走完才在到期拍复核开关并执行（原 for-else 语义，复核 fail-open）。
_SEGMENT_S = 30
_FIRST_WAIT = 120
_CYCLE_WAIT = 6 * 3600
_cycle = {"start": None, "wait": _FIRST_WAIT}


def _switch_enabled_logged():
    """分段读归档开关：读失败留痕并按启用继续（原 for-body 语义）。"""
    try:
        from core.config_store import load_config
        return bool(((load_config() or {}).get("wecom_archive") or {}).get("enabled", True))
    except Exception as e:  # noqa: BLE001
        # v1.0.646：读不到开关就当"仍开启"继续等，必须留痕——否则用户关了
        # 归档却看到它还在跑，日志里一行都没有
        log_event(f"[archive] 归档开关读取失败（按启用继续等待）：{e}")
        return True


def _switch_enabled_quiet():
    """到期复核读归档开关：读失败按启用处理（原 for-else 的 fail-open 语义）。"""
    try:
        from core.config_store import load_config
        return bool(((load_config() or {}).get("wecom_archive") or {}).get("enabled", True))
    except Exception:  # noqa: BLE001
        return True   # 读不到开关按启用执行（与原内层 except-pass 等价）


def _tick():
    """30s 一拍：分段读开关 + 累计到期执行增量（原 _loop 逐拍等价）。"""
    now = time.time()
    st = _cycle
    if st["start"] is None:
        st["start"] = now - _SEGMENT_S   # 首拍在启动 +30s，等价原循环自启动起算
    if not _switch_enabled_logged():
        st["start"] = now                # 原 break：本轮作废，重新起算 6h 周期
        st["wait"] = _CYCLE_WAIT
        return
    if now - st["start"] < st["wait"]:
        return                           # 分段未走完，继续等
    try:
        if _switch_enabled_quiet():      # 原 for-else：复核开关（fail-open）后执行
            archive_once()
    except Exception as e:  # noqa: BLE001
        log_event(f"[wecom-archive] 调度增量异常：{e}")
    st["start"] = time.time()            # 周期从本轮跑完起算（原 while 下一轮起点）
    st["wait"] = _CYCLE_WAIT


def start_archive_daemon():
    """启动静默调度线程（backend 启动时调用一次；重复调用安全）：
    首启 2 分钟后跑一次增量，之后每 6 小时一次。开关：config.wecom_archive.enabled。"""
    global _daemon
    if _daemon and _daemon.is_alive():
        return
    _cycle["start"], _cycle["wait"] = None, _FIRST_WAIT   # 新线程首启语义（原 _loop 闭包每次新建）
    _daemon = start_periodic("wecom-archive", _tick, _SEGMENT_S,
                             first_delay=_SEGMENT_S)
    log_event("[wecom-archive] 聊天文件静默分析线程已启动（首次 2 分钟后，之后每 6 小时增量）")

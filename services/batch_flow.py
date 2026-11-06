# -*- coding: utf-8 -*-
"""batch_flow — 确定性流程脚本执行器（v1.0.210）
用户需求：「点击打开流程、审核这些步骤做成脚本，判断文件里的元素以及需要智能判断的东西时才引入大模型」。

设计原则：
- 整条流水线（找列表→解析链接→逐个点开→读字段→下载附件→统计→判定→记录→关页）
  由 Python 脚本执行，毫秒级、零 LLM 消耗；
- 脚本只在「智能判断」场景产出疑问项（issues）：
    * 指定字段在页面上 count=0 且近义匹配失败（需要 LLM 对照 all_labels 找字段）
    * 附件 Excel 列名无法识别（需要 LLM 看表头判断哪列是状态/类型）
    * 附件无法下载/读取
- 疑问项随结果一次性返回，由外层 LLM 针对少数疑问做二次处理或标注跳过。

v1.0.656 三层化（可测试性投资，行为等价、import 路径 services.batch_flow 不变）：
- ReportExtractor（纯函数层）：字段数字提取 / xlsx 修复与统计 / 条目识别——零 I/O 零 CDP；
- FlowTransport（传输层）：页面枚举/列表加载/开详情/关页/下载/探活——可整层 mock；
- AuditOrchestrator（状态机）：批审编排/单张审计/断点进度/后台任务/报告。
模块级名字全部保留为薄委托（签名不变），类内部对这些名字的调用一律经**模块全局晚绑定**
（同模块裸名调用天然晚绑定）——`monkeypatch.setattr(bf, ...)` 语义与三层化前一致。
"""
import os
import re
import time
import json
import traceback

import wecom_bridge as wb
from wecom_bridge import windows as _wins
from core_utils import workspace_root, log_event, _lazy_requests as requests
from core.files import find_col_in_header  # v1.0.510（P2-13）：find_col 收敛
from core.config import atomic_write_json  # v1.0.619：断点/历史 JSON 一律原子写
from core.result import is_err

# 月报审核字段清单（read_fields 用）
# v1.0.604：去掉「在用计算机数量」——用户口径取消在用设备数比对，只看清单是否上传。
# 少抓一个字段=少一类"页面值未能提取"疑问项（8 月那轮 20 条噪声全来自它）。
REPORT_FIELDS = [
    "机构名称", "月报所示月份", "本月应运维天数", "实际运维天数", "附件",
]

# 列表页/月报链接识别
LIST_TITLE_KWS = ("待办", "任务")

# —— v1.0.555（P1-3）：统一降级重试（对齐用户偏好口径：每 30s 一次、最多 20 次）——
# 原实现两处不符：①CDP 健康闸单次 (1,2)s 探测失败即 break 中止整批——注释自述
# "企微 /json 端点繁忙时会短暂无响应（实测 3 分钟级挂死与秒级抖动都有）"，秒级
# 抖动下剩余 ~15 张直接不处理、连 pending 占位都没有，"共 N 份" silently 少报；
# ②附件下载（页内 fetch → download_attachment）各只尝试一次，网络抖动即
# "附件下载失败" → pending → 机构被要求"重新上传"。全子系统找不到 30s×20 量级
# 的降级重试。仅瞬时错误（超时/连接重置/5xx）重试；确定性错误不重试。
AUDIT_RETRY_INTERVAL = 30.0
AUDIT_RETRY_MAX = 20


def _is_transient_error(e):
    """薄委托 → AuditOrchestrator.is_transient_error（v1.0.656 三层化；monkeypatch 仍打在此名上）。"""
    return _ORCH.is_transient_error(e)


def _retry_degraded(fn, what, interval=AUDIT_RETRY_INTERVAL, max_tries=AUDIT_RETRY_MAX,
                    abort_check=None):
    """薄委托 → AuditOrchestrator.retry_degraded（签名/语义不变）。"""
    return _ORCH.retry_degraded(fn, what, interval=interval, max_tries=max_tries,
                                abort_check=abort_check)

REPORT_LINK_KWS = ("信息技术运维月报", "信息技术月报", "分支机构信息技术月报", "运维月报")

DOWNLOAD_SUB = "下载"

# v1.0.425：审计流水线运行标志——运行期间 /api/wecom/bind 的 confirm_kill 请求
# 会被拒绝（2026-10-01 23:34 实测：模型在附件下载卡住时自行 curl 绑定 API 重启
# 企微，打断整条流水线）。杀企微是破坏性动作，只能由用户在 UI 手动触发。
import threading as _threading_mod
_AUDIT_RUNNING = _threading_mod.Event()
_AUDIT_LOCK = _threading_mod.Lock()   # v1.0.470（P1-11）：流水线自身并发互斥（_AUDIT_RUNNING 只挡杀企微）

# ---------- 后台批审（v1.0.523-6） ----------
# 批审从 agent 工具同步执行（5-9 分钟独占 SSE 流）改为后台线程：agent 立即返回
# "已开始"，用户可继续对话；进度经 _BG_STATE 暴露给 /api/audit/progress 供前端
# 进度条轮询，完成时托盘轻提醒。同步路径保留（background=False 供测试/调试）。
_BG_STATE = {"running": False, "started": "", "total": 0, "done": 0,
             "green": 0, "red": 0, "pending": 0, "current": "",
             "finished": False, "ok": None, "summary": "", "report_path": "",
             "pane": "",              # v1.0.599 发起会话 key（conv_xxx），结果回流落点
             "abort_requested": False}
_BG_LOCK = _threading_mod.Lock()


def request_abort():
    """薄委托 → AuditOrchestrator.request_abort（docstring 见实现，语义不变）。"""
    return _ORCH.request_abort()


def request_audit_abort():
    return request_abort()


def _bg_state(**kw):
    """薄委托 → AuditOrchestrator.bg_state（持锁局部更新后台状态）。"""
    return _ORCH.bg_state(**kw)


def _bg_update(results):
    """薄委托 → AuditOrchestrator.bg_update（每张完成后的进度汇总打点）。"""
    return _ORCH.bg_update(results)


def background_progress():
    """薄委托 → AuditOrchestrator.background_progress（进度快照，供 API 序列化）。"""
    return _ORCH.background_progress()


def audit_history(limit=5):
    """薄委托 → AuditOrchestrator.audit_history（批审历史轮次摘要）。"""
    return _ORCH.audit_history(limit)


def _publish_result_to_pane(pane, result):
    """薄委托 → AuditOrchestrator.publish_result_to_pane（结果回流发起会话）。"""
    return _ORCH.publish_result_to_pane(pane, result)


def start_background_audit(port, mode="all", auto_approve=False, max_items=None, pane=None,
                           flow="monthly-report"):
    """薄委托 → AuditOrchestrator.start_background_audit（签名/语义不变）。"""
    return _ORCH.start_background_audit(port, mode=mode, auto_approve=auto_approve,
                                        max_items=max_items, pane=pane, flow=flow)


def _progress_path(name="月报进度.json"):
    """薄委托 → AuditOrchestrator.progress_path（断点进度文件路径）。"""
    return _ORCH.progress_path(name)


def _save_progress(results, name="月报进度.json"):
    """薄委托 → AuditOrchestrator.save_progress（断点快照，永不抛异常）。"""
    return _ORCH.save_progress(results, name)


def _load_progress(name="月报进度.json"):
    """薄委托 → AuditOrchestrator.load_progress。"""
    return _ORCH.load_progress(name)


def resume_key(rec):
    """薄委托 → AuditOrchestrator.resume_key（断点/待办判重键，纯函数）。"""
    return _ORCH.resume_key(rec)


def resume_partition(prev_results, items):
    """薄委托 → AuditOrchestrator.resume_partition（resume 分流，纯函数）。"""
    return _ORCH.resume_partition(prev_results, items)


def _pages(port):
    """薄委托 → FlowTransport.pages。"""
    return _TRANSPORT.pages(port)


# v1.0.425（用户截图：OAuth 标签堆积）：批量审计的页面白名单——这些域的页面
# 即使是"处理期间新出现的"也保留（列表/门户/企微文档），其余全部差分关闭。
# v1.0.616：名单唯一事实源在 wecom_bridge/windows.py::KEEP_URL_MARKS（收纳守护也用同一份），
# 这里只做别名。以前是各抄一份字面量、靠注释提醒"两处同步改"——改一漏一就会把批审
# 列表页当成"新出现的窗口"最小化掉。
_KEEP_URL_PAT = _wins.KEEP_URL_MARKS


def _close_new_targets(port, base_ids):
    """薄委托 → FlowTransport.close_new_targets（targetId 差分关页）。"""
    return _TRANSPORT.close_new_targets(port, base_ids)


def _find_list_page(port):
    """薄委托 → FlowTransport.find_list_page（找待办列表页窗口）。"""
    return _TRANSPORT.find_list_page(port)


def _load_all_report_links(list_pick, port, max_scrolls=10, match=None, label="月报链接"):
    """薄委托 → FlowTransport.load_all_report_links（滚动加载全量链接）。"""
    return _TRANSPORT.load_all_report_links(list_pick, port, max_scrolls=max_scrolls,
                                            match=match, label=label)


def _find_follow_pick(port):
    """薄委托 → FlowTransport.find_follow_pick（点击后跟随窗口）。"""
    return _TRANSPORT.find_follow_pick(port)


def _num_from_field(field, pattern=None):
    """薄委托 → ReportExtractor.num_from_field（字段对象取第一个数字，纯函数）。"""
    return _EXTRACTOR.num_from_field(field, pattern)


def _repair_xlsx_raw(path):
    """薄委托 → ReportExtractor.repair_xlsx_raw（PK 签名 raw 扫描重建 xlsx）。"""
    return _EXTRACTOR.repair_xlsx_raw(path)


def _extract_excel_pcs(download_dir, filename):
    """薄委托 → ReportExtractor.extract_excel_pcs（读 xlsx 统计在用台式+笔记本数）。"""
    return _EXTRACTOR.extract_excel_pcs(download_dir, filename)


def _get_full_href(port, list_pick, text):
    """薄委托 → FlowTransport.get_full_href（取完整 href，不截断）。"""
    return _TRANSPORT.get_full_href(port, list_pick, text)


def _detail_has_content(fp, port):
    """薄委托 → FlowTransport.detail_has_content（详情页内容轻量校验）。"""
    return _TRANSPORT.detail_has_content(fp, port)


def _js_click_link(port, list_pick, text):
    """薄委托 → FlowTransport.js_click_link（JS 直点列表项锚点）。"""
    return _TRANSPORT.js_click_link(port, list_pick, text)


def _wait_new_page(port, titles_before, timeout=5.0, poll=0.4):
    """薄委托 → FlowTransport.wait_new_page（轮询等待新页面出现）。"""
    return _TRANSPORT.wait_new_page(port, titles_before, timeout=timeout, poll=poll)


def _open_detail_by_click(port, list_pick, link, issues):
    """薄委托 → FlowTransport.open_detail_by_click（前台 click 打开详情页）。"""
    return _TRANSPORT.open_detail_by_click(port, list_pick, link, issues)


def _open_detail(port, list_pick, link, issues):
    """薄委托 → FlowTransport.open_detail（静默优先开详情页，回退前台 click）。"""
    return _TRANSPORT.open_detail(port, list_pick, link, issues)


def _approve_in_detail(port, fp):
    """薄委托 → FlowTransport.approve_in_detail（详情页内自动点「通过」）。"""
    return _TRANSPORT.approve_in_detail(port, fp)


def _iter_audit_items(items, per_item_fn, *, t0, budget_s, port, progress_name,
                      placeholder_for, log_label):
    """薄委托 → AuditOrchestrator.iter_audit_items（批审公共循环骨架）。"""
    return _ORCH.iter_audit_items(items, per_item_fn, t0=t0, budget_s=budget_s, port=port,
                                  progress_name=progress_name, placeholder_for=placeholder_for,
                                  log_label=log_label)


def _audit_one(port, list_pick, link, download_dir, idx, auto_approve=False):
    """薄委托 → AuditOrchestrator._audit_one（审核单个月报链接）。"""
    return _ORCH._audit_one(port, list_pick, link, download_dir, idx, auto_approve=auto_approve)


# ==================== v1.0.650（F6）：财务单据脚本流水线 ====================
# 财务审核此前只有"模型驱动"一条路：技能 SOP 让模型逐张 点→读→判→记→关，
# 每张都要走一轮 LLM（读整页字段 + 复述判定），30 张单据烧掉几十万 token，
# 而且"逐张点"这件事本身就是确定性的——判定逻辑早就脚本化了（rule_check.check_finance），
# 缺的只是把开页/读字段/关页这三步也从模型轮次里拿出来。
# 红线：本流水线**全程只读**——只 read_fields/read_page + 关自己开的页面，
# 不调用 `_approve_in_detail`、不接受 `auto_approve` 参数、不点任何审批按钮。
# （run_audit_flow 的 auto_approve 是"参数级不可撤销写"，靠 _ALWAYS_ASK 弹窗守；
#   本 flow 从入口就没有这个参数，模型连误传的机会都没有。）
FINANCE_FIELDS = ["专管部门", "明细项目", "业务类型", "支出类别", "预算执行单", "PMP项目", "附件"]
FINANCE_PROGRESS = "财务进度.json"


def _is_finance_doc_link(txt):
    """薄委托 → ReportExtractor.is_finance_doc_link（按单据编号格式认条目）。"""
    return _EXTRACTOR.is_finance_doc_link(txt)


def _audit_one_finance(port, list_pick, link, idx):
    """薄委托 → AuditOrchestrator.audit_one_finance（单张单据：开→读→判→关）。"""
    return _ORCH.audit_one_finance(port, list_pick, link, idx)


def _close_detail(port, fp):
    """薄委托 → FlowTransport.close_detail（关掉本流水线自己打开的详情页）。"""
    return _TRANSPORT.close_detail(port, fp)


def _write_finance_report(results, ok_n, fail_n, pending_n):
    """薄委托 → AuditOrchestrator.write_finance_report（财务核查报告落盘）。"""
    return _ORCH.write_finance_report(results, ok_n, fail_n, pending_n)


def audit_finance_bills(port=None, max_items=None, mode="all"):
    """薄委托 → AuditOrchestrator.audit_finance_bills（财务单据审核流水线，只读）。"""
    return _ORCH.audit_finance_bills(port=port, max_items=max_items, mode=mode)


def _default_port():
    """v1.0.364 审查修复（H3）：端口从配置读取（原写死 9222，用户改端口后月报批量
    审核直接失联）。v1.0.588 收敛到 core.config_store.cdp_port 统一口径。"""
    from core.config_store import cdp_port
    return cdp_port()


def _history_path():
    """薄委托 → AuditOrchestrator.history_path（判定历史文件路径）。"""
    return _ORCH.history_path()


def _load_history():
    """薄委托 → AuditOrchestrator.load_history。"""
    return _ORCH.load_history()


def _load_last_verdicts():
    """薄委托 → AuditOrchestrator.load_last_verdicts（上一轮 {主键: verdict}）。"""
    return _ORCH.load_last_verdicts()


def _save_verdict_history(results):
    """薄委托 → AuditOrchestrator.save_verdict_history（判定历史入库 + 趋势一句话）。"""
    return _ORCH.save_verdict_history(results)


def audit_monthly_reports(port=None, max_items=None, mode="all", auto_approve=False):
    """薄委托 → AuditOrchestrator.audit_monthly_reports（月报审核流水线，签名/语义不变）。"""
    return _ORCH.audit_monthly_reports(port=port, max_items=max_items, mode=mode,
                                       auto_approve=auto_approve)


def _write_report(results, ok_n, fail_n, pending_n, issues_total):
    """薄委托 → AuditOrchestrator.write_report（Markdown 报告落盘）。"""
    return _ORCH.write_report(results, ok_n, fail_n, pending_n, issues_total)


def _probe_cdp(port):
    """薄委托 → FlowTransport.probe_cdp（CDP 健康探活单次探测）。"""
    return _TRANSPORT.probe_cdp(port)


# ==================== 三层实现（v1.0.656） ====================
# 顺序说明：实现放在模块级薄委托之后，`src.index("def ...")` 类源码锁先命中的
# 仍是原名模块级函数（签名/位置不变），类方法名与之同名但不影响既有源码断言。


class ReportExtractor:
    """纯函数层：字段/表头/条目解析与 xlsx 修复统计。零 I/O 服务、零 CDP、零窗口。

    可独立单测（tmp 造文件或构造字段文本即可）。模块级 _num_from_field /
    _repair_xlsx_raw / _extract_excel_pcs / _is_finance_doc_link 是本类默认实例
    _EXTRACTOR 的薄委托。"""

    def num_from_field(self, field, pattern=None):
        """从 read_fields 的 field 对象（key/count/snippets）提取第一个数字。"""
        snips = field.get("snippets") or []
        if not snips:
            return None
        text = str(snips[0])
        m = re.search(r"(\d+(?:\.\d+)?)", text)
        return m.group(1) if m else None

    def repair_xlsx_raw(self, path):
        """v1.0.425：绕过 central directory，按 PK 签名 raw 扫描恢复 xlsx 成员并重建


        干净 zip。WPS 增量保存/上传截断会产生 local header 与目录不一致的文件
        （openpyxl 报 BadZipFile: Bad magic number）。关键成员（xl/worksheets/sheet*.xml）
        缺失 = 数据流本体损坏，返回 None（业务侧需重新上传）。"""
        import struct as _struct
        import zipfile as _zipfile
        import zlib as _zlib
        try:
            with open(path, "rb") as _f:
                raw = _f.read()
        except Exception:  # noqa: BLE001
            return None
        entries = {}
        pos = 0
        while True:
            i = raw.find(b"PK\x03\x04", pos)
            if i < 0:
                break
            try:
                _v, flags, method, _m1, _m2, _crc, csize, _us, nlen, elen = _struct.unpack(
                    "<HHHHHIIIHH", raw[i + 4:i + 30])
                fname = raw[i + 30:i + 30 + nlen].decode("utf-8", "replace")
                dstart = i + 30 + nlen + elen
                if fname.endswith("/") or csize == 0:
                    pos = dstart
                    continue
                comp = raw[dstart:dstart + csize]
                if method == 8:
                    data = _zlib.decompressobj(-15).decompress(comp)
                elif method == 0:
                    data = comp
                else:
                    pos = dstart + max(csize, 1)
                    continue
                if data and fname not in entries:
                    entries[fname] = data
                pos = dstart + csize
            except Exception:  # noqa: BLE001
                pos = i + 4
        if not any(k.startswith("xl/worksheets/sheet") for k in entries):
            return None
        out_p = path.rsplit(".", 1)[0] + "_repaired.xlsx"
        try:
            with _zipfile.ZipFile(out_p, "w", _zipfile.ZIP_DEFLATED) as out:
                for name, data in entries.items():
                    out.writestr(name, data)
            return out_p
        except Exception:  # noqa: BLE001
            return None

    def extract_excel_pcs(self, download_dir, filename):
        """复用附件统计逻辑：读 xlsx 统计在用台式+笔记本数。
        返回 (count, header, error_hint)。"""
        import openpyxl
        path = os.path.join(download_dir, filename)
        if not os.path.isfile(path):
            # v1.0.221：下载落盘文件名可能与接口名不一致（实测某分支文件带"工作表 在 "前缀），
            # 扫描目录模糊匹配（文件名包含目标的关键片段）。
            key = filename.rsplit(".", 1)[0][:12] if "." in filename else filename[:12]
            hit = None
            try:
                # v1.0.364 审查修复（M10）：模糊匹配改按 mtime 取最新且要求 mtime 晚于
                # 本批次开始——下载目录跨批次共享，同名前缀模板会读到旧附件统计出错。
                best_mtime = 0
                for f in os.listdir(download_dir):
                    fp_ = os.path.join(download_dir, f)
                    if f.endswith((".xlsx", ".xls")) and key and key in f:
                        mt = os.path.getmtime(fp_)
                        if mt > best_mtime:
                            best_mtime = mt
                            hit = f
            except Exception:  # noqa: BLE001
                hit = None
            if hit:
                path = os.path.join(download_dir, hit)
            else:
                return None, None, f"文件不存在：{filename}"
        try:
            try:
                wb_ = openpyxl.load_workbook(path, read_only=True, data_only=True)
            except Exception:
                # v1.0.425：WPS 增量保存/上传截断产生 local header 与目录不一致的
                # xlsx——raw 扫描重建后重试；数据流本体损坏给业务明确话术。
                _repaired = self.repair_xlsx_raw(path)
                if not _repaired:
                    return None, None, "Excel 文件损坏（内部数据流异常，需该机构重新上传附件）"
                wb_ = openpyxl.load_workbook(_repaired, read_only=True, data_only=True)
            ws = wb_.active
            rows = list(ws.iter_rows(values_only=True))
            # v1.0.364 审查修复（M10）：read_only 模式主动 close（Windows 滞留文件句柄，
            # 同名附件重新下载覆盖会 PermissionError）
            try:
                wb_.close()
            except Exception:  # noqa: BLE001
                pass
            if not rows:
                return None, None, "Excel 为空表"

            # v1.0.211：表头可能不在首行（设备清单前几行是"表格填写人员/截止日期"信息）——
            # v1.0.654 A7：扫描块收敛到 core.files.scan_table_header（原与 executors 逐字两份）
            from core.files import scan_table_header
            header, header_row_idx = scan_table_header(rows)

            # v1.0.510（P2-13）：find_col 收敛到 core.files.find_col_in_header（原与
            # executors 各持一份相同实现）
            status_col = find_col_in_header(header, [["设备状态"], ["在用状态"], ["状态"]])
            type_col = find_col_in_header(header, [["设备类型"], ["类型"], ["类别"]])
            if status_col is None or type_col is None:
                return None, header, "未识别状态/类型列，表头：" + " | ".join(header[:20])
            count = 0
            for r in rows[header_row_idx + 1:]:
                # v1.0.652（审计 P2-A2）：行宽防御——损坏/非常规 xlsx 的 iter_rows 行宽
                # 可能小于表头列下标（dimension 元数据错误时 read_only 尤甚），旧 r[col]
                # 越界 IndexError 被归"解析失败"，可解析的边缘文件被误判。
                st = str(r[status_col] if status_col < len(r) else "").strip()
                tp = str(r[type_col] if type_col < len(r) else "").strip()
                if "在用" in st and ("台式" in tp or "笔记本" in tp or "电脑" in tp):
                    count += 1
            return count, header, None
        except Exception as e:  # noqa: BLE001
            return None, None, f"Excel 解析失败：{e}"

    def is_finance_doc_link(self, txt):
        """列表链接算不算"一张单据"：按单据编号格式认（与 rule_check 列表筛单同源）。"""
        from services.rule_check import DOC_NO_RE
        return bool(DOC_NO_RE.search(txt or ""))


class FlowTransport:
    """传输层：页面/窗口/下载/探活操作。默认实现保持现行为，可整层替换（FakeTransport）。

    实现内**跨方法调用一律经 _m()**：默认实例走模块级名晚绑定（monkeypatch 语义
    不变），子类（注入的 Fake）走 self 分发（覆盖到哪层就隔离到哪层）。
    模块级 _pages / _find_list_page / ... 是本类默认实例 _TRANSPORT 的薄委托。"""

    def _m(self, name, *a, **k):
        """层内跨方法调用：默认实例取模块级 name（晚绑定，可 monkeypatch）；
        子类实例走 self 分发（注入的 Fake 覆盖到哪层就隔离到哪层）。"""
        if type(self) is FlowTransport:
            return globals()[name](*a, **k)
        return getattr(self, name)(*a, **k)

    def probe_cdp(self, port):
        """CDP 健康探活（单次）：/json/version 200 即活；非 200 抛 OSError 交降级重试。"""
        rr = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=(1, 2))
        if rr.status_code != 200:
            raise OSError(f"HTTP {rr.status_code}")
        return True

    def pages(self, port):
        return wb._pages(wb.list_targets(port))

    def close_new_targets(self, port, base_ids):
        """v1.0.425：按 **targetId 差分**关闭新出现的非白名单页面。
        title 差分的缺陷：OAuth 中转页标题全部相同，close_page(title) 只关第一个
        （实测 22 个 OAuth 标签堆满）；targetId 唯一，逐个精确关。
        v1.0.425b（2026-10-07 实证）：/json/close 返回 200"Target is closing"但标签
        **实际不关闭**（企微 Electron 无视）——改为 **ws window.close() 为主**（脚本
        window.open 出的窗口允许脚本关，实测有效），/json/close 仅兜底。返回关闭数。"""
        closed = 0
        try:
            targets = wb.list_targets(port)
            if is_err(targets):
                return 0
            import websocket as _wslib
            for t in targets:
                if t.get("type") != "page":
                    continue
                tid = t.get("id")
                url = t.get("url") or ""
                if not tid or tid in base_ids:
                    continue
                if any(k in url for k in _KEEP_URL_PAT):
                    continue
                # ① ws window.close()（对 window.open 出的标签实测有效）
                try:
                    wsu = t.get("webSocketDebuggerUrl")
                    if wsu:
                        w = _wslib.create_connection(wsu, timeout=4)
                        w.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                           "params": {"expression": "window.close(); 'c'",
                                                      "returnByValue": True}}))
                        w.settimeout(3)
                        try:
                            w.recv()
                        except Exception:  # noqa: BLE001
                            pass
                        w.close()
                except Exception:  # noqa: BLE001
                    pass
                # ② /json/close 兜底
                try:
                    requests.get(f"http://127.0.0.1:{port}/json/close/{tid}", timeout=2)
                except Exception:  # noqa: BLE001
                    pass
                closed += 1
            # ③ 复核：1s 后仍活着的从计数中扣除（真实关闭数）
            time.sleep(1.0)
            alive_ids = {t.get("id") for t in wb.list_targets(port)
                         if isinstance(t, dict) and t.get("type") == "page"}
            closed = sum(1 for t in targets
                         if t.get("id") not in alive_ids and t.get("id") not in base_ids
                         and t.get("type") == "page"
                         and not any(k in (t.get("url") or "") for k in _KEEP_URL_PAT))
        except Exception as e:  # noqa: BLE001
            log_event(f"[audit-flow] 差分关页异常：{str(e)[:60]}")
        return closed

    def find_list_page(self, port):
        """找待办列表页窗口（标题含'待办/任务'）。
        v1.0.425：返回**窗口标题**而非 index——/json/list 的 target 顺序不保证稳定
        （新 target 插入/排序变化会让 index 漂移，实测 read 打到完全无关的页面读到
        空正文），标题经 pick_window 的字符串匹配寻址，跨顺序变化稳定。"""
        pages = self._m("_pages", port)
        for i, p in enumerate(pages):
            title = p.get("title") or ""
            if any(k in title for k in LIST_TITLE_KWS):
                return title, pages
        return None, pages

    def load_all_report_links(self, list_pick, port, max_scrolls=10, match=None, label="月报链接"):
        """滚动加载列表页全量月报链接（v1.0.220）。
        实测：待办列表是虚拟滚动，一次 read_page 只拿到可见部分（~5 条），
        17:24 那轮脚本只处理 5 张、其余 16 张被模型编成'待定'——根因。
        这里滚动到底 → 重读 → 合并去重，直到链接数稳定（最多 max_scrolls 次）。
        返回去重后的月报链接列表 [{text,url,index}, ...]。

        v1.0.650（F6）：`match`/`label` 参数化——财务单据流水线要的是同一套滚动加载，
        只是"哪些链接算目标"不同（月报按标题关键词，单据按单据编号格式）。
        复制一份滚动逻辑就等于复制它踩过的所有坑（v1.0.413 滚过头、v1.0.555 静默截断）。
        v1.0.413（2026-09-30 实测 14:07）：滚动到第 2 次起 read_page 连续读空
        （虚拟滚动滚过头 DOM 重建/清空），所以收敛条件除"链接数稳定"外还要覆盖
        "骤降/清零"——cur_n=0 永远 ≠ prev_n=22，会空转满 max_scrolls 轮
        （10 轮 × 14s ≈ 140s 零进展）。读到空/骤降时先回滚 scroll up 重读一次做合并
        抢救，仍不增长则认为已到底，立即结束。"""
        _match = match or (lambda txt: any(kw in txt for kw in REPORT_LINK_KWS))
        seen = set()
        report_links = []
        prev_n = -1
        import time as _t

        def _merge(links):
            """去重合并本批链接，返回本批新增数。"""
            added = 0
            for i, lk in enumerate(links):
                txt = lk.get("text") or ""
                if not _match(txt):
                    continue
                key = lk.get("url") or txt
                if key in seen:
                    continue
                seen.add(key)
                report_links.append({**lk, "index": i})
                added += 1
            return added

        for k in range(max_scrolls):
            rp = wb.read_page(list_pick, port=port)
            links = rp.get("links") or []
            _merge(links)
            cur_n = len(report_links)
            log_event(f"[audit-flow] 滚动加载第{k+1}次：累计{label} {cur_n} 条")
            if cur_n == prev_n and k >= 1:
                break  # 链接数稳定 = 已到底
            if cur_n == 0 or cur_n < prev_n:
                # v1.0.413：滚过头（页面 DOM 清空/重建，读到的链接比上轮还少）——
                # 回滚一点重读一次抢救，仍无增长即按已收集数据结束，绝不空转。
                try:
                    # v1.0.555（P0-1 修复）：原调用 wb.scroll_wecom_window——该符号在
                    # wecom_bridge 包内不存在（只导出 act.scroll_window），AttributeError
                    # 被下方 except: break 吞掉，滚动加载永远只跑第 1 轮（日志实证 54 次
                    # "第1次"/0 次"第2次"）。当前靠首屏恰好返回全部 22 条链接掩盖，
                    # 列表一旦虚拟滚动即静默截断。
                    wb.scroll_window(list_pick, "up", port=port)
                    _t.sleep(1.2)
                    rp2 = wb.read_page(list_pick, port=port)
                    _merge(rp2.get("links") or [])
                    cur_n = len(report_links)
                    log_event(f"[audit-flow] 滚过头回滚重读：累计 {cur_n} 条")
                except Exception as _e:  # noqa: BLE001
                    # v1.0.555：原静默 pass——回滚抢救失败不可见，排障时无从下手
                    log_event(f"[audit-flow] 滚过头回滚重读失败（按已收集数据结束）：{_e}")
                if cur_n <= prev_n:
                    break   # 已到底（或页面异常），带着已收集的链接继续审计
            prev_n = cur_n
            try:
                wb.scroll_window(list_pick, "bottom", port=port)
            except Exception as _e:  # noqa: BLE001
                # v1.0.555（P0-1 配套）：原 except: break 静默退出——编程错误/环境异常
                # 必须留痕，否则"只处理第一屏"类回归永远无声
                log_event(f"[audit-flow] 滚动加载中止（scroll_window 失败）：{_e}")
                break
            _t.sleep(1.0)
        return report_links

    def find_follow_pick(self, port):
        """当前跟随窗口（点击链接后自动定位到的新页面）。
        v1.0.425：返回**标题**而非 index（/json/list 顺序漂移会让 index 打错窗口）。
        v1.0.555（P1-6）：进一步返回 **targetId pick**（#tid:）——标题子串匹配在多个
        同名页（OAuth 中转页标题全同）时关/读第一个，targetId 精确定位无歧义。"""
        ap = wb._get_auto_pick()
        if ap is not None:
            _pl = self._m("_pages", port)
            if 0 <= ap < len(_pl):
                _tid = (_pl[ap] or {}).get("id")
                if _tid:
                    return wb.target_pick(_tid)
                return (_pl[ap].get("title") or "").strip() or None
        return None

    def get_full_href(self, port, list_pick, text):
        """在列表页按文本精确/包含匹配 a 标签，返回其【完整 href】（不截断，v1.0.296）。
        _LINKS_JS 把 url 截到 200 字符，OA 详情 URL 带长参数会被截断无法直连，
        这里单独取完整 href 供 open_silent_page 后台 navigate。
        javascript:/# /空 /未匹配 → 返回 None（调用方回退前台 click）。"""
        if not text:
            return None
        try:
            ws_url, page = wb._page_ws(list_pick, port)
            if not ws_url:
                return None
            t = json.dumps((text or "").strip())
            expr = (
                "(() => {"
                "  const t = " + t + ";"
                "  const norm = s => String(s||'').replace(/\\s+/g,'');"
                "  const nt = norm(t);"
                "  if (!nt) return JSON.stringify({href:null});"
                "  const as = Array.from(document.querySelectorAll('a[href]'));"
                "  let exact=null, best=null, bestLen=1e9;"
                "  for (const a of as) {"
                "    const at = norm((a.innerText||a.textContent||''));"
                "    if (at === nt) { exact = a; break; }"
                "    if (at && at.includes(nt) && at.length < bestLen) { best = a; bestLen = at.length; }"
                "  }"
                "  const el = exact || best;"
                "  if (!el) return JSON.stringify({href:null});"
                "  const h = el.href || '';"
                "  if (!h || h === '#' || h.indexOf('javascript:')===0) return JSON.stringify({href:null});"
                "  return JSON.stringify({href:h});"
                "})()"
            )
            raw = wb._cdp_eval_retry(ws_url, expr)
            if isinstance(raw, str):
                d = json.loads(raw)
                h = d.get("href")
                if isinstance(h, str) and h and h != "#" and not h.startswith("javascript:"):
                    return h
        except Exception:  # noqa: BLE001
            pass
        return None

    def detail_has_content(self, fp, port):
        """轻量校验详情页是否渲染出月报关键内容（防后台页登录墙/未就绪被误判成功）。
        body 足够长或含任一关键字（运维天数/在用计算机/月报/机构/设备/附件）→ 视为有内容。"""
        try:
            rp = wb.read_page(fp, port=port)
            body = (rp.get("body") or "") if isinstance(rp, dict) else ""
            if len(body) >= 600:
                return True
            kws = ("应运维天数", "实际运维天数", "在用计算机", "月报", "机构名称", "设备", "附件")
            return any(k in body for k in kws)
        except Exception:  # noqa: BLE001
            return False

    def js_click_link(self, port, list_pick, text):
        """v1.0.425b：JS 直点列表项锚点（scrollIntoView + click）——真实坐标点击在
        虚拟滚动列表上会打错视口位置（滚动加载把列表滚走后，元素坐标≠目标行，
        实测 11/22 张详情打不开、reused_page 错拿残留详情页）。返回 True=已点击。"""
        try:
            from wecom_bridge.cdp_client import _cdp_eval as _ev
            ws_url, _pg = wb._page_ws(list_pick, port)
            if not ws_url:
                return False
            t = json.dumps((text or "").strip())
            js = (
                "(() => {"
                "  const norm = s => String(s||'').replace(/\\s+/g,'');"
                "  const nt = norm(" + t + ");"
                "  if (!nt) return 'no-text';"
                "  const cands = Array.from(document.querySelectorAll('a, [onclick]'))"
                "    .filter(e => norm(e.innerText).indexOf(nt) >= 0);"
                "  if (!cands.length) return 'not-found';"
                "  cands.sort((x, y) => norm(x.innerText).length - norm(y.innerText).length);"
                "  const el = cands[0];"
                "  el.scrollIntoView({block: 'center'});"
                "  const a = el.tagName === 'A' ? el : (el.querySelector('a') || el.closest('a'));"
                "  (a || el).click();"
                "  return 'clicked:' + (a || el).tagName;"
                "})()"
            )
            r = _ev(ws_url, js, timeout=8)
            return isinstance(r, str) and r.startswith("clicked")
        except Exception:  # noqa: BLE001
            return False

    def wait_new_page(self, port, titles_before, timeout=5.0, poll=0.4):
        """v1.0.523-5（批审提速）：轮询等待新页面 target 出现（取代固定 sleep 2.5/2.0s）。
        就绪即返回标题（典型 0.4-1.2s），超时返回 None（调用方走既有兜底链）。
        只查 /json/list（轻量），不读内容——内容校验交给后续 _detail_has_content。

        v1.0.616 说明（为什么不与 wecom_bridge.windows._find_new_page 合并成一套）：
        两者差分的键不同——这里按**标题**判"新页出现"（批审点的是同域列表→详情，URL 前缀
        一样、id 会复用），那里按 **id+url** 判（收纳守护要认"哪个窗口是新的"）；
        超时语义也不同（这里 5s 后走兜底链，那里 6×0.4s 只是加速判定）。
        合并只会造出一个带两个开关参数的四不像，收益是零——保留两份，但把差异写死在这。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for p in self._m("_pages", port):
                title = (p.get("title") or "").strip()
                if title and title not in titles_before:
                    return title
            time.sleep(poll)
        return None

    def open_detail_by_click(self, port, list_pick, link, issues):
        """前台 click 打开详情页（原通道，作静默通道兜底）。返回 fp index 或 None（失败）。"""
        # v1.0.599（用户反馈"跑任务时开的页面会影响用户"）：这条兜底通道是企微把详情页
        # 开成一个新的可见窗口并抢走前台焦点。开页前拍 OS 窗口基线 + 记住当前前台窗口，
        # 内容一确认就立刻收纳这个新窗口、把焦点当场还回去。
        # 为什么不等后台 window-tucker：它的收纳门禁是 _agent_busy()（v1.0.594 红线修复，
        # 空闲时绝不动用户窗口），任务中按 1.2~2.5s 轮询才收一次——一轮 23 张就是抢 23 次
        # 焦点、每次可见 1~2.5s。这里只收"本函数点击之后新出现"的窗口，用户自己开着的
        # 窗口永远不在差集里。
        _win_base = _wins.new_window_baseline()
        _fg = _wins.foreground_hwnd()

        def _tuck_own_window():
            try:
                _wins.tuck_new_windows(_win_base, why="批审详情", restore_to=_fg)
            except Exception:  # noqa: BLE001
                pass

        _titles_before = {(p.get("title") or "") for p in self._m("_pages", port)}
        # v1.0.425b（2026-10-07 实证）：优先 JS 直点锚点——真实坐标点击在虚拟滚动列表
        # 上会打错视口位置（实测 11/22 张详情打不开、reused_page 错拿残留详情页）。
        if self._m("_js_click_link", port, list_pick, link.get("text") or ""):
            # v1.0.523-5：固定 sleep(2.5) → 轮询新页出现（就绪即走，实测典型 <1.5s）
            fp = self._m("_wait_new_page", port, _titles_before, timeout=5.0)
            if fp and self._m("_detail_has_content", fp, port):
                _tuck_own_window()      # 内容确认后再收：过早最小化怕 CEF 节流读不到正文
                log_event(f"[audit-flow] JS 锚点直点打开详情：{fp[:36]}")
                return fp
            if fp:
                try:
                    wb.close_page(fp, port=port)
                except Exception:  # noqa: BLE001
                    pass
        _click_target = link.get("index")
        import time as _t
        try:
            _fresh = wb.read_page(list_pick, port=port)
            _f_links = _fresh.get("links") or []
            # v1.0.425（2026-10-01 22:3x 实测）：列表页 DOM 会瞬时空白（"可交互元素共
            # 0 个"，外部同时刻读正常）——后台页被企微回收/重载中。fresh 读空时先
            # reload 列表页唤醒，等 3.5s 再读一次，恢复后按文字匹配点击，避免逐张全灭。
            if not _f_links:
                try:
                    wb.run_js(list_pick, "location.reload(); 'reloading'", port=port)
                    _t.sleep(3.5)
                    _fresh = wb.read_page(list_pick, port=port)
                    _f_links = _fresh.get("links") or []
                except Exception:  # noqa: BLE001
                    pass
            _norm_t = (link.get("text") or "").replace(" ", "")
            for _i, _lk in enumerate(_f_links):
                if (_lk.get("text") or "").replace(" ", "") == _norm_t:
                    _click_target = _i
                    break
        except Exception:  # noqa: BLE001
            pass
        r = wb.click_element(list_pick, _click_target)
        if not (isinstance(r, dict) and r.get("ok")):
            r2 = wb.click_element(list_pick, link.get("text"))
            if not (isinstance(r2, dict) and r2.get("ok")):
                issues.append("点击进入详情失败：" + str(r.get("error") or r2.get("error") or "")[:80])
                return None
        # v1.0.523-5：固定 sleep(2.0) → 轮询新页出现（就绪即走）
        fp = self._m("_wait_new_page", port, _titles_before, timeout=5.0)
        if fp is None:
            fp = self._m("_find_follow_pick", port)
        if fp is None:
            # v1.0.364 审查修复（H1）：识别不出新窗口时**返回 None 而非列表页**——
            # 原兜底 fp=list_pick 会让调用方把列表页当详情页读取（产出全 miss 的垃圾
            # 判定）并无条件关闭，列表页一关后续所有单据全部失败。
            issues.append("未能定位新打开的详情页（标题匹配失败），跳过本条避免误关列表页")
            return None
        wb.wait_page_load(fp, timeout=8, port=port)
        _tuck_own_window()
        return fp

    def open_detail(self, port, list_pick, link, issues):
        """打开单个月报详情页，返回 fp index 或 None（打不开）。
        静默优先：CDP /json/new 后台开页（不抢前台焦点，不打断用户打字）→ 校验内容；
        静默不可用（拿不到完整 href / 开页失败 / 后台页无有效内容）→ 回退前台 click。"""
        text = link.get("text") or ""
        full_href = self._m("_get_full_href", port, list_pick, text)
        # v1.0.425（用户实测：跑月报时系统默认浏览器被开 3 个标签）：列表项 href 是
        # open.weixin.qq.com OAuth 跳转链——点击通道触发时企微对 weixin 域走
        # shell.openExternal 甩给系统浏览器（Chrome 标签就是这么来的）。解法：从 OAuth
        # href 的 redirect_uri 参数解出真实详情 URL，静默直开真实页（带企微会话 cookie
        # 正常加载），点击通道退为最后兜底。
        if full_href and "open.weixin.qq.com" in full_href:
            try:
                from urllib.parse import urlparse as _u, parse_qs as _pq
                _qs = _pq(_u(full_href).query)
                _ru = (_qs.get("redirect_uri") or [""])[0]
                if _ru:
                    log_event("[audit-flow] OAuth 链接解码 redirect_uri 直开（避免外跳浏览器）")
                    full_href = _ru
            except Exception:  # noqa: BLE001
                pass
        if full_href:
            opened = wb.open_silent_page(full_href, port=port)
            if isinstance(opened, dict) and opened.get("ok"):
                # v1.0.555（P1-6）：fp 改用 targetId 寻址——原 _pages(port) 重新枚举取
                # opened["index"] 的标题当 pick：两次枚举之间若有新 target（企微消息
                # 弹窗）index 漂移 → 拿到别的页的标题，后续 read/download/close 全作用
                # 到错页；且 close_page/read_fields 按标题子串匹配，多个同名页（OAuth
                # 中转页标题全同）时关/读第一个，可能关掉还要用的页、读到同名旧详情页
                # （数据张冠李戴）。open_silent_page 本就返回 target_id，弃之不用是浪费。
                fp = wb.target_pick(opened.get("target_id"))
                if not fp:
                    # target_id 缺失的兜底（理论不发生的防御路径）：退回原 index 寻址
                    _pl0 = self._m("_pages", port)
                    _oi = opened.get("index")
                    fp = (_pl0[_oi].get("title") or "").strip() if isinstance(_oi, int) and _oi < len(_pl0) else None
                # OAuth 检测：按 target_id 在当前枚举里反查同一 target 的 url/title
                # （与 fp 同源，避免"关的是 A 页、判的是 B 页"）
                _pl = self._m("_pages", port)
                _tid = opened.get("target_id")
                _t = next((p for p in _pl if p.get("id") == _tid), None) if _tid else None
                _t_url = (_t or {}).get("url") or ""
                _t_title = (_t or {}).get("title") or ""
                if "open.weixin.qq.com" in _t_url or "open.weixin.qq.com" in _t_title:
                    try:
                        wb.close_page(fp, port=port)
                    except Exception:  # noqa: BLE001
                        pass
                    issues.append("静默通道被 OAuth 重定向卡死（后台无原生桥接层），回退前台点击")
                    # v1.0.555（P0-2 修复）：原此处 return None——调用方 _audit_one 拿到
                    # fp=None 立即返回，四字段全空，issue 文案承诺的"回退前台点击"从不执行
                    # （近三日每轮 6~11 份待定实证）。改为落到函数末尾统一的
                    # _open_detail_by_click 兜底，与下方"静默后台页无有效内容"支同构。
                else:
                    wb.wait_page_load(fp, timeout=8, port=port)
                    if self._m("_detail_has_content", fp, port):
                        log_event(f"[audit-flow] 静默后台打开详情（不打断前台）：{full_href[:80]}")
                        return fp
                    try:
                        wb.close_page(fp, port=port)
                    except Exception:  # noqa: BLE001
                        pass
                    issues.append("静默后台页无有效内容，回退前台点击")
        else:
            issues.append("未取到完整详情链接，回退前台点击")
        return self._m("_open_detail_by_click", port, list_pick, link, issues)

    def approve_in_detail(self, port, fp):
        """v1.0.468（auto_approve）：在已打开的详情页里自动点「通过」。
        扫描顶层文档 + 同源 iframe 的可见可点元素，精确匹配「通过/同意」（绝不碰
        驳回/退回/暂存）；点击后处理意见框（填"同意"）并确认。返回 (ok, note)。
        局限：按钮结构未知时返回明确 note，由监督轮次修正匹配规则。"""
        try:
            from wecom_bridge.cdp_client import _cdp_eval as _ev
            ws_url, page = wb._page_ws(fp, port)
            if not ws_url:
                return False, "详情页不可达"
            scan_js = (
                "(function scan(doc, depth) {"
                "  const norm = s => String(s||'').replace(/\\s+/g,'');"
                "  let out = [];"
                "  const els = doc.querySelectorAll('a, button, input[type=button], input[type=submit], [onclick], span, div');"
                "  for (const e of els) {"
                "    const tx = norm(e.innerText || e.value || '');"
                "    if (tx !== '通过' && tx !== '同意') continue;"
                "    const r = e.getBoundingClientRect();"
                "    if (r.width <= 0 || r.height <= 0) continue;"
                "    out.push(e);"
                "  }"
                "  if (out.length) {"
                "    out.sort((a, b) => a.getBoundingClientRect().width - b.getBoundingClientRect().width);"
                "    const el = out[0];"
                "    el.scrollIntoView({block: 'center'});"
                "    el.click();"
                "    return 'clicked:' + el.tagName + ':' + norm(el.innerText);"
                "  }"
                "  if (depth < 2) {"
                "    for (const f of doc.querySelectorAll('iframe')) {"
                "      try { const r = scan(f.contentDocument, depth + 1); if (r) return r; } catch(e) {}"
                "    }"
                "  }"
                "  return '';"
                "})(document, 0)"
            )
            r = _ev(ws_url, scan_js, timeout=10)
            if not (isinstance(r, str) and r.startswith("clicked")):
                return False, "未找到可见的「通过/同意」按钮（可能在流程处理面板内，需人工点一次后固化规则）"
            time.sleep(1.8)
            # 意见框/确认框处理：出现 textarea 填"同意"，再点 确认/提交/确定
            confirm_js = (
                "(async () => {"
                "  const norm = s => String(s||'').replace(/\\s+/g,'');"
                "  const ta = document.querySelector('textarea');"
                "  if (ta && !norm(ta.value)) {"
                "    ta.value = '同意'; ta.dispatchEvent(new Event('input', {bubbles: true}));"
                "  }"
                "  const btns = document.querySelectorAll('a, button, input[type=button], input[type=submit], [onclick]');"
                "  for (const b of btns) {"
                "    const tx = norm(b.innerText || b.value || '');"
                "    if (tx === '确认' || tx === '提交' || tx === '确定' || tx === '发送') {"
                "      const r = b.getBoundingClientRect();"
                "      if (r.width > 0) { b.click(); return 'confirmed:' + tx; }"
                "    }"
                "  }"
                "  return 'no-confirm';"
                "})()"
            )
            r2 = _ev(ws_url, confirm_js, timeout=10, await_promise=True)
            note = "已点通过"
            if isinstance(r2, str) and "confirmed" in r2:
                note += "（含确认框）"
            time.sleep(1.0)
            return True, note
        except Exception as e:  # noqa: BLE001
            return False, f"点通过异常：{str(e)[:60]}"

    def close_detail(self, port, fp):
        """关掉本流水线自己打开的详情页。详情页不关会一路堆标签（月报流水线同口径）。"""
        try:
            wb.close_page(fp, port=port)
        except Exception as e:  # noqa: BLE001
            log_event(f"[audit-finance] 关闭详情页失败（不阻断流水线）：{e}")


class AuditOrchestrator:
    """状态机层：批审编排 / 单张审计 / 断点进度 / 后台任务 / 报告。

    构造器 `AuditOrchestrator(transport=None, extractor=None)`：
    - transport=None（默认）：传输操作经**模块级薄委托**（_find_list_page 等）晚绑定
      调用——monkeypatch.setattr(bf, ...) 语义与三层化前完全一致；底层仍是默认
      FlowTransport 实例 _TRANSPORT。
    - transport=FlowTransport 子类/假实现：发现类操作直接走注入实例（零 CDP）。
    - extractor=None（默认）用 ReportExtractor 默认实例 _EXTRACTOR。
    可 patch 的模块级名字（wb / log_event / workspace_root / _audit_one / _save_progress
    / _retry_degraded / _pages / _get_full_href / ...）在本类内部一律**裸名调用**
    （同模块全局查找＝晚绑定），绝不存进实例属性。"""

    def __init__(self, transport=None, extractor=None):
        self.transport = transport    # None = 默认传输（经模块级委托到 _TRANSPORT）
        self.extractor = _EXTRACTOR if extractor is None else extractor

    # ---------- 传输路由（注入 transport 用实例；默认走模块级晚绑定） ----------
    def _r_find_list_page(self, port):
        if self.transport is not None:
            return self.transport.find_list_page(port)
        return _find_list_page(port)

    def _r_load_all_report_links(self, list_pick, port, **kw):
        if self.transport is not None:
            return self.transport.load_all_report_links(list_pick, port, **kw)
        return _load_all_report_links(list_pick, port, **kw)

    def _r_probe_cdp(self, port):
        if self.transport is not None:
            return self.transport.probe_cdp(port)
        return _probe_cdp(port)

    def _r_open_detail(self, port, list_pick, link, issues):
        if self.transport is not None:
            return self.transport.open_detail(port, list_pick, link, issues)
        return _open_detail(port, list_pick, link, issues)

    def _r_close_new_targets(self, port, base_ids):
        if self.transport is not None:
            return self.transport.close_new_targets(port, base_ids)
        return _close_new_targets(port, base_ids)

    def _r_approve_in_detail(self, port, fp):
        if self.transport is not None:
            return self.transport.approve_in_detail(port, fp)
        return _approve_in_detail(port, fp)

    def _r_close_detail(self, port, fp):
        if self.transport is not None:
            return self.transport.close_detail(port, fp)
        return _close_detail(port, fp)

    # ---------- 降级重试 ----------
    @staticmethod
    def is_transient_error(e):
        """瞬时错误判定：网络类/超时类才值得重试；确定性错误（参数错/文件损坏/4xx）不重试。
        v1.0.652（审计 P2-A4）：确定性 OSError 子类排除——FileNotFoundError/PermissionError
        同为 OSError，旧一律算瞬时会触发 20×30s 降级重试（单张最坏 10 分钟，22 张最坏数小时）。"""
        if isinstance(e, (FileNotFoundError, PermissionError, FileExistsError, IsADirectoryError)):
            return False
        if isinstance(e, (TimeoutError, ConnectionError, OSError)):
            return True
        # requests 的异常层次（ConnectionError/Timeout 都是 RequestException 子类，
        # 但 HTTPError 也可能是 4xx 确定性错误——按状态码细分）
        try:
            import requests as _rq
            if isinstance(e, _rq.exceptions.RequestException):
                if isinstance(e, _rq.exceptions.HTTPError):
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    return code is None or code >= 500
                return True
        except Exception:  # noqa: BLE001
            pass
        msg = str(e).lower()
        return any(k in msg for k in ("timeout", "timed out", "connection", "reset", "503", "502", "500", "504"))

    def retry_degraded(self, fn, what, interval=AUDIT_RETRY_INTERVAL, max_tries=AUDIT_RETRY_MAX,
                       abort_check=None):
        """降级重试执行 fn()。返回 (ok, result, err)。
        - 仅瞬时错误重试（_is_transient_error）；确定性错误立即返回失败；
        - 间隔分段睡眠（每次 1s）——30s 长睡期间持续响应 abort，不让"停止"按钮失灵；
        - 达上限或 abort 命中返回 (False, None, err)。"""
        last = None
        for i in range(max_tries):
            if abort_check is not None:
                try:
                    if abort_check():
                        return False, None, "aborted"
                except Exception:  # noqa: BLE001
                    pass
            try:
                return True, fn(), None
            except Exception as e:  # noqa: BLE001
                last = e
                if not _is_transient_error(e):
                    log_event(f"[audit-flow] {what} 确定性错误（不重试）：{str(e)[:100]}")
                    return False, None, e
                if i < max_tries - 1:
                    log_event(f"[audit-flow] {what} 瞬时失败（第 {i+1}/{max_tries} 次，"
                              f"{interval:.0f}s 后重试）：{str(e)[:80]}")
                    _slept = 0.0
                    while _slept < interval:
                        if abort_check is not None:
                            try:
                                if abort_check():
                                    return False, None, "aborted"
                            except Exception:  # noqa: BLE001
                                pass
                        time.sleep(min(1.0, interval - _slept))
                        _slept += 1.0
        return False, None, last

    # ---------- 后台状态机 ----------
    def request_abort(self):
        """v1.0.537（用户需求）：请求中止后台批审——主循环每张开跑前检查，命中即
        break 走既有收尾（报告生成/断点 _save_progress 已存 → 可 resume 续跑剩余）。"""
        with _BG_LOCK:
            if not _BG_STATE["running"]:
                return {"ok": False, "error": "当前没有正在运行的批审"}
            _BG_STATE["abort_requested"] = True
        log_event("[audit-flow] 用户请求中止批审（当前张跑完后停止，已处理的保留）")
        return {"ok": True, "hint": "已请求中止——当前单据跑完后停止，已处理结果自动生成报告，剩余可稍后 resume 续跑"}

    def bg_state(self, **kw):
        """局部更新后台状态（持锁；在流水线主线程调用）。"""
        with _BG_LOCK:
            _BG_STATE.update(kw)

    def bg_update(self, results):
        """每张完成后的进度汇总打点（results 为当前累计列表）。"""
        with _BG_LOCK:
            _BG_STATE["done"] = len(results)
            _BG_STATE["green"] = sum(1 for r in results if r.get("verdict") == "green")
            _BG_STATE["red"] = sum(1 for r in results if r.get("verdict") == "red")
            _BG_STATE["pending"] = sum(1 for r in results if r.get("verdict") not in ("green", "red"))

    def background_progress(self):
        """进度快照（只读拷贝，供 API 序列化）。v1.0.540 加 resumable（断点存在可续跑）。"""
        with _BG_LOCK:
            snap = dict(_BG_STATE)
        try:
            snap["resumable"] = not snap["running"] and bool(_load_progress())
        except Exception:  # noqa: BLE001
            snap["resumable"] = False
        return snap

    def audit_history(self, limit=5):
        """v1.0.540：批审历史轮次摘要（最近 limit 轮，供看板趋势展示）。"""
        try:
            hist = _load_history()
            runs = hist.get("runs") or []
            out = []
            for r in runs[-limit:]:
                rs = r.get("results") or []
                out.append({"ts": r.get("ts") or "",
                            "total": len(rs),
                            "green": sum(1 for x in rs if x.get("verdict") == "green"),
                            "red": sum(1 for x in rs if x.get("verdict") == "red"),
                            "pending": sum(1 for x in rs if x.get("verdict") not in ("green", "red"))})
            return list(reversed(out))   # 最新在前
        except Exception:  # noqa: BLE001
            return []

    def publish_result_to_pane(self, pane, result):
        """v1.0.599（用户反馈"说跑完了但界面没有结果"）：后台批审完成后，把结论落进
        **发起它的那个对话**。此前完成信号只有两条通道——Windows Toast（系统勿扰/焦点
        助手会静默吞掉，且发送侧无法确认送达）与看板进度格（跑完即消失）。用户看到的
        最后一句还停在 agent 的"预计耗时 3-6 分钟"。

        只认 `conv_<id>` 形态的 pane（工作台里的真实会话）；scratch/定时任务/跑课等
        没有会话落点的直接跳过。写失败不影响批审本身（结果已在 _BG_STATE 与报告里）。
        """
        if not pane or not str(pane).startswith("conv_"):
            return False
        cid = str(pane)[5:]
        if result.get("ok"):
            text = (f"📋 批量审核完成：{result.get('done', 0)}/{result.get('total', 0)} 张 · "
                    f"通过 {result.get('green', 0)} / 不通过 {result.get('red', 0)} / "
                    f"待定 {result.get('pending', 0)}，疑问项 {result.get('issues_total', 0)} 条。"
                    f"报告：产物库/{result.get('report_path') or '（未生成）'}")
        else:
            text = "⚠️ 批量审核失败：" + str(result.get("error") or "未知错误")[:200]
        try:
            from services import conversations as CONV
            CONV.append_message(cid, "agent", text)
            log_event(f"[audit-flow] 结果已回流发起对话 {cid}")
            return True
        except Exception as e:  # noqa: BLE001
            log_event(f"[audit-flow] 结果回流对话失败（不影响批审结果）：{e}")
            return False

    def start_background_audit(self, port, mode="all", auto_approve=False, max_items=None, pane=None,
                              flow="monthly-report"):
        """把批审流水线放到后台线程执行。立即返回 {"ok": True, "background": True}；
        已有批审在跑（后台或同步）返回 busy。结果不回传调用方——完成时**写回发起会话**
        （v1.0.599）+ 托盘提醒 + 报告落盘（audit_monthly_reports 自带），进度走
        background_progress()。pane 为发起会话的 key（conv_xxx），用于结果回流。
        v1.0.650（F6）：`flow` 决定跑哪条流水线（monthly-report / finance）——后台通道
        那一整套（互斥、进度打点、结果回流会话、Toast/托盘、异常兜底）两条流水线本就一样，
        复制一份只会让其中一份先烂。auto_approve 只对月报有效，财务路径没有这个参数。"""
        _label = "财务单据核查" if flow == "finance" else "月报批审"
        with _BG_LOCK:
            if _BG_STATE.get("running"):
                return {"ok": False, "busy": True,
                        "error": f"已有一条审核流水线在后台运行（{_label} 同用一条通道）——完成后可再次发起"}
            _BG_STATE.update({"running": True, "finished": False, "ok": None,
                              "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "total": 0, "done": 0, "green": 0, "red": 0, "pending": 0,
                              "current": "启动中", "summary": "", "report_path": "",
                              "pane": pane or "",
                              "abort_requested": False})

        def _job():
            try:
                if flow == "finance":
                    result = audit_finance_bills(port=port, max_items=max_items, mode=mode)
                else:
                    result = audit_monthly_reports(port=port, max_items=max_items,
                                                   mode=mode, auto_approve=auto_approve)
                with _BG_LOCK:
                    _BG_STATE["finished"] = True
                    _BG_STATE["running"] = False
                    _BG_STATE["ok"] = bool(result.get("ok"))
                    if result.get("ok"):
                        # 最终计数以结果 dict 为准（resume/预算中止路径循环打点可能偏少）
                        _BG_STATE["total"] = int(result.get("total") or _BG_STATE["total"])
                        _BG_STATE["done"] = int(result.get("done") or _BG_STATE["done"])
                        _BG_STATE["green"] = int(result.get("green") or 0)
                        _BG_STATE["red"] = int(result.get("red") or 0)
                        _BG_STATE["pending"] = int(result.get("pending") or 0)
                        _BG_STATE["current"] = ""
                        _BG_STATE["summary"] = str(result.get("summary") or "")[:200]
                        _BG_STATE["report_path"] = str(result.get("report_path") or "")
                        if _BG_STATE.get("abort_requested"):
                            _BG_STATE["summary"] = "（用户中止）" + _BG_STATE["summary"]
                    else:
                        _BG_STATE["summary"] = str(result.get("error") or "失败")[:200]
                        _BG_STATE["current"] = ""
                # 结果先回流会话，再发 Toast：Toast 可能被系统勿扰静默吞掉，不能当唯一通道。
                _publish_result_to_pane(pane, result)
                # v1.0.570（用户需求）：批审完成升级为 Windows 原生 Toast 横幅（原仅托盘
                # 气泡不弹横幅，用户看不到）；Toast 失败时保留托盘气泡兜底。
                try:
                    from core.notify import native_toast
                    if result.get("ok"):
                        native_toast("✅ 批量审核完成",
                                     f"通过 {result.get('green', 0)} / 不通过 {result.get('red', 0)}"
                                     f" / 待定 {result.get('pending', 0)}（详情见报告）")
                    else:
                        native_toast("❌ 批量审核失败", str(result.get("error") or "")[:80])
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from desktop import close_guard
                    if result.get("ok"):
                        close_guard.tray_notify(f"{_label}完成",
                                                f"通过 {result.get('green', 0)} / 不通过 {result.get('red', 0)}"
                                                f" / 待定 {result.get('pending', 0)}（详情见报告）")
                    else:
                        close_guard.tray_notify(f"{_label}失败", str(result.get("error") or "")[:80])
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                with _BG_LOCK:
                    _BG_STATE.update({"running": False, "finished": True, "ok": False,
                                      "summary": f"后台线程异常：{str(e)[:150]}"})
                log_event(f"[audit-flow] 后台线程异常：{e}")

        th = _threading_mod.Thread(target=_job, name="audit-bg", daemon=True)
        th.start()
        return {"ok": True, "background": True, "mode": mode}

    # ---------- 断点进度 ----------
    def progress_path(self, name="月报进度.json"):
        """v1.0.470（P1-12）：断点进度文件（产物库/台账/月报进度.json）。
        v1.0.650（F6）：财务流水线用同一个机制、另一个文件名（两条流水线的断点不能互吃）。"""
        return os.path.join(workspace_root(), "台账", name)

    def save_progress(self, results, name="月报进度.json"):
        """每完成一份立即快照断点（中止/崩溃后 resume 不丢前 N 份）。永不抛异常。

        v1.0.619（审查④）改走 core.config.atomic_write_json：原实现是
        `json.dump(..., open(pp, "w"))`——句柄不显式关（靠 GC），且**不是原子替换**，
        进程写到一半被杀就留半截 JSON，`_load_progress` 解析失败返回 None、断点全丢，
        而这恰好是断点续跑最该防的场景（批审就是被中断才需要 resume）。"""
        try:
            pp = _progress_path(name)
            os.makedirs(os.path.dirname(pp), exist_ok=True)
            atomic_write_json(pp, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results},
                              indent=None)
        except Exception:  # noqa: BLE001
            pass

    def load_progress(self, name="月报进度.json"):
        # v1.0.654 A8：句柄样板收敛 core.files.read_json（原裸 open 依赖 GC）
        from core.files import read_json
        d = read_json(_progress_path(name))
        rs = d.get("results") if isinstance(d, dict) else None
        return rs if isinstance(rs, list) else None

    def resume_key(self, rec):
        """断点进度与待办项的**判重键**：有详情链接用链接（[:200]），没链接退回标题。

        为什么不直接用 `(rec.get("url") or "")`：列表里有一类单据取不到完整详情链接
        （走"回退前台点击"那条路），它的 url 就是空串。空串进 `_done_keys` 之后，
        `"" in keys` 对所有无链接项恒真 → resume 时**整批无链接单据被当已完成跳过**，
        而报告总数照旧守恒，属于查不出来的漏审。
        字段名也不统一：断点记录里标题叫 `title`，待办链接里叫 `text`（同一份文案
        两个名字），两个都要认——只读 title 会让所有待办项都退化成空标题。
        """
        u = (rec.get("url") or "").strip()[:200]
        if u:
            return u
        return "T:" + (rec.get("title") or rec.get("text") or "").strip()[:60]

    def resume_partition(self, prev_results, items):
        """resume 模式的分流：返回 (要保留的旧记录, 还需跑的待办)。

        只保留已判定 green/red 的旧记录——pending 丢弃由本轮重跑生成新记录
        （v1.0.555 P1-5：原实现把 pending 旧记录也留在 results，items 又重跑同一项
        并 append → 同一机构两条、done/green/red/pending 计数虚高、前端进度跳变）。
        """
        done = {resume_key(r) for r in (prev_results or []) if r.get("verdict") in ("green", "red")}
        keep = [r for r in (prev_results or []) if resume_key(r) in done]
        todo = [lk for lk in (items or []) if resume_key(lk) not in done]
        return keep, todo

    # ---------- 批审循环骨架 ----------
    def iter_audit_items(self, items, per_item_fn, *, t0, budget_s, port, progress_name,
                         placeholder_for, log_label):
        """批审公共循环骨架（v1.0.654 A1）：月报/财务两条流水线原 60-70% 逐字同构的
        「预算截断 / 用户中止 / CDP 探活失败占位 / 逐张落盘断点 / for-else 完整一轮」
        收敛于此。per_item_fn(idx, lk)->rec；placeholder_for(idx, lk, issue) 构造各自
        记录形态（月报无 verdict / 财务 pending）。返回 (results, run_finished)。"""
        results = []
        _run_finished = False
        for idx, lk in enumerate(items):
            if time.time() - t0 > budget_s:
                results.append(placeholder_for(idx, lk, "已达总时长预算，跳过剩余"))
                break
            with _BG_LOCK:
                _abort_hit = _BG_STATE.get("abort_requested")
            if _abort_hit:
                results.append(placeholder_for(idx, lk, "用户请求中止，跳过剩余项"))
                log_event(f"[{log_label}] 用户中止生效：第 {idx + 1}/{len(items)} 张起停止，"
                          f"已处理 {len(results)} 份（含本占位），断点已存可 resume")
                break
            # CDP 健康闸（v1.0.555 P1-3：30s×20 降级重试，秒级抖动不退场；
            # 剩余项显式占位保证报告总数守恒）
            def _cdp_probe():
                return self._r_probe_cdp(port)

            _probe_ok, _, _perr = _retry_degraded(
                _cdp_probe, "CDP 健康探活",
                abort_check=lambda: _BG_STATE.get("abort_requested"))
            if not _probe_ok:
                if _perr == "aborted":
                    results.append(placeholder_for(idx, lk, "用户请求中止，跳过剩余项"))
                    log_event(f"[{log_label}] 用户中止生效（探活重试期间）：第 {idx + 1}/{len(items)} 张起停止")
                    break
                for _j, _lk2 in enumerate(items[idx:], start=idx):
                    results.append(placeholder_for(_j, _lk2,
                        f"企微 CDP 服务持续不可达（{str(_perr)[:60]}），未处理——请在企微页重新绑定后重跑"))
                log_event(f"[{log_label}] CDP 健康探活连续 {AUDIT_RETRY_MAX} 次失败，流水线中止，"
                          f"剩余 {len(items) - idx} 份已标记未处理")
                break
            _bg_state(current=(lk.get("text") or "")[:36], total=len(items))
            rec = per_item_fn(idx, lk)
            results.append(rec)
            _save_progress(results, progress_name)
            _bg_update(results)
        else:
            # v1.0.652（审计 P0-3）：for-else——仅当循环**没有 break** 自然跑完才算完整一轮
            _run_finished = True
        return results, _run_finished

    # ---------- 单张审计 ----------
    def _audit_one(self, port, list_pick, link, download_dir, idx, auto_approve=False):
        """审核单个月报链接。返回结果 dict（含 issues）。"""
        issues = []
        # v1.0.425（用户反馈"打开的网页不会关闭"）：处理前快照页面集合，尾部差分清理——
        # 处理期间新出现的所有页面（详情/预览/误开页）一律关闭，彻底杜绝标签堆积
        # （原实现只在 fp 非 None 时关 fp，点击失败路径的页面全泄漏）。
        # base 存 **targetId 集**而不是 title（title 会重复，OAuth 页全同名关不全）：
        # 尾部 _close_new_targets 按 id 精确差分关闭。
        # v1.0.619（审查③）：删掉被 _base_ids 取代的那份 title 快照——无人再读，
        # 却仍按每份月报多打一次 CDP /json 请求。
        # v1.0.652（审计 P1-9）：基线快照纳入容错——list_targets 网络抖动抛异常时，
        # 旧实现在 try 块**之外**，异常直接逸出 _audit_one、主循环无 per-item 保护 →
        # 本轮 22 张整体作废（与"单张失败不毒死整批"设计目标不符）。现失败置 None，
        # 尾部跳过差分关页（宁可少关不误关），单张继续处理。
        try:
            _base_ids = {t.get("id") for t in wb.list_targets(port)
                         if isinstance(t, dict) and t.get("type") == "page"}
        except Exception as _be:  # noqa: BLE001
            _base_ids = None
            issues.append("页面基线快照失败（收尾将跳过关页清理）：" + str(_be)[:60])
        # v1.0.470（P2-10）：rec 记录 url——retry 模式按 url 精确匹配，替代原
        # 「双向子串匹配 title」（"甲营业部" 会误命中 "甲营业部二部"）。
        rec = {"index": idx, "title": link.get("text", "")[:60], "url": (link.get("url") or "")[:200]}
        try:
            # 1. 打开详情：静默优先（CDP /json/new 后台开页，不打断用户前台打字），
            #    拿不到完整 href / 开页失败 / 后台页无有效内容 → 自动回退前台 click（v1.0.296）。
            fp = self._r_open_detail(port, list_pick, link, issues)
            if fp is None:
                rec["issues"] = issues
                return rec
            # 3. 读字段
            # 3. 读字段（v1.0.211 双通道：read_page 的 fields 数组 value + read_fields snippets）
            flds = wb.read_fields(fp, REPORT_FIELDS, port=port)
            fmap = {}
            if isinstance(flds, dict):
                for f in (flds.get("fields") or []):
                    if isinstance(f, dict):
                        fmap[f.get("key")] = f
                if flds.get("all_labels"):
                    rec["all_labels"] = flds["all_labels"][:25]
            else:
                issues.append("read_fields 返回异常：" + str(flds)[:80])
            # 数值提取（v1.0.211 顺序：正则 body 优先 → snippets 兜底）：
            # snippets 第一个数字可能是年份（"2026 年 7月"），正则"应运维天数： 23"更精确。
            _pr = None
            try:
                _pr = wb.read_page(fp, port=port)
                body = _pr.get("body") or ""
            except Exception as _e:  # noqa: BLE001
                body = ""
                issues.append("read_page 异常：" + str(_e)[:60])
            expect = actual = None
            if body:
                m = re.search(r"应运维天数[：:]\s*(\d+)", body)
                if m:
                    expect = m.group(1)
                m = re.search(r"实际运维天数[：:]\s*(\d+)", body)
                if m:
                    actual = m.group(1)
            # 兜底：snippets / input value（v1.0.604：在用计算机数量的三条提取通道
            # ——正文正则、字段 value、DOM 表格控件——整体删除，判定不再用它）
            if expect is None:
                expect = self.extractor.num_from_field(fmap.get("本月应运维天数"))
            if actual is None:
                actual = self.extractor.num_from_field(fmap.get("实际运维天数"))
            if expect is None or actual is None:
                try:
                    for f in ((_pr or {}).get("fields") or []):
                        if not isinstance(f, dict):
                            continue
                        label = str(f.get("label") or "")
                        value = str(f.get("value") or "")
                        if not value:
                            continue
                        if "应运维天数" in label and expect is None:
                            m = re.search(r"(\d+(?:\.\d+)?)", value)
                            if m:
                                expect = m.group(1)
                        elif "实际运维天数" in label and actual is None:
                            m = re.search(r"(\d+(?:\.\d+)?)", value)
                            if m:
                                actual = m.group(1)
                except Exception as _e:  # noqa: BLE001
                    issues.append("字段 value 通道异常：" + str(_e)[:60])
            rec["expect_days"] = expect
            rec["actual_days"] = actual
            for key, nm in (("本月应运维天数", "expect_days"), ("实际运维天数", "actual_days")):
                f = fmap.get(key)
                # v1.0.652（审计 P2-A3）：只有**值也没提取到**才记疑问项——旧只要 snippets
                # 没命中就记，正文正则已成功提取天数时纯噪声（疑问项计数虚高、报告被占）。
                if (f is None or f.get("count", 0) == 0) and rec.get(nm) is None:
                    issues.append(f"字段[{key}] snippets 未命中（all_labels 已附，需 LLM 对照找近义名）")
            # 5. 附件下载（v1.0.211 优先从正文正则找 xlsx 文件名——附件在正文展示
            # "甲营业部信息设备清单202607.xlsx 24.07KB"，不在 links）
            # v1.0.604（用户口径「弹窗去掉」）：**取消③点击下载兜底**。企微 Electron 无视
            # CDP 下载行为设置（browser 端点实测命令成功仍弹原生「另存为」并挂住任务），
            # 原靠 save_dialog_auto 事后自动应答=窗口在用户眼前闪一遍。现只留两条静默通道：
            # ①详情页 DOM 找附件真实 href → 页内 fetch 直传；②KM 结构页内 fetch（行 id=fdId）。
            # 两条都取不到 → 记疑问项「清单已上传但无法静默获取」，判定归 pending，不碰 UI。
            xlsx_name = None
            if body:
                # v1.0.425：字符类补点号——附件名常含版本点（"清单2026.8.31.xlsx"），
                # 原正则不含点号会把文件名截成"31.xlsx"（含于原名仍能匹配行，但落盘
                # 文件名错误、find_attachment/name 匹配口径混乱）
                # v1.0.652（审计 P0-4）：扩展名放宽到 xlsx/xlsm/xls/et + 字符类补【】《》。
                # 旧正则只认 .xlsx——机构传 .xls/带书名号命名的清单完全匹配不上 →
                # list_found=False → 规则层直接红判"未上传设备清单"，误伤机构（false positive）。
                m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9_\-（）()【】《》.]+\.(?:xlsx|xlsm|xls|et)(?![A-Za-z0-9]))", body)
                if m:
                    xlsx_name = m.group(1).strip()
            # v1.0.652（审计 P0-4）二级通道：正文没抓到文件名时，从 links 的 text/href
            # 基名里找表格类文件（附件可能只挂在链接区不进正文渲染）。
            if not xlsx_name:
                try:
                    for _lk in ((_pr or {}).get("links") or []):
                        if not isinstance(_lk, dict):
                            continue
                        _cand = (str(_lk.get("text") or "").strip().split("\n")[0].strip()
                                 or os.path.basename(str(_lk.get("href") or "").split("?")[0]))
                        if re.search(r"\.(?:xlsx|xlsm|xls|et)$", _cand, re.I):
                            xlsx_name = _cand[:120]
                            break
                except Exception:  # noqa: BLE001
                    pass
            list_found = bool(xlsx_name)
            if not list_found:
                # v1.0.652（审计 P0-4）：识别失败 ≠ 没传。正文没读到、或正文提到附件/清单
                # 却解析不出文件名（命名格式超出识别范围）时，无法证明"机构没上传"——
                # 归 pending（rule_check 的 lf=None 分支"清单是否存在未探明"），不红判。
                if not body or re.search(r"附件|清单|设备表|台账", body):
                    list_found = None
                    issues.append("附件文件名未能从正文/链接识别（命名格式可能超出识别范围），"
                                  "归待定由人工确认是否已上传")
            list_ok = False
            list_note = ""
            excel_pcs = None
            if xlsx_name:
                # v1.0.425：预删同名旧附件——下载目录跨张/跨批次共享，同名旧文件会让
                # _extract_excel_pcs 的"前缀模糊命中"读到上一张清单（张冠李戴）。
                try:
                    _old = os.path.join(download_dir, xlsx_name)
                    if os.path.exists(_old):
                        os.remove(_old)
                except Exception:  # noqa: BLE001
                    pass
                dl = None
                try:
                    # ① DOM 找附件 URL → 页内 fetch 直传（零弹框）
                    # v1.0.555（P1-3）：附件下载整体纳入降级重试（30s×20）——原各级通道
                    # 各只尝试一次，网络抖动即"附件下载失败" → pending → 机构被要求
                    # "重新上传"（误伤：文件在机构侧是好的）。仅瞬时错误重试；
                    # abort 期间立即放弃。
                    def _dl_channels():
                        att_url = wb.find_attachment_url(port, fp, xlsx_name)
                        if att_url:
                            d1 = wb.page_fetch_to_disk(port, fp, att_url, download_dir, filename=xlsx_name)
                            if isinstance(d1, dict) and d1.get("ok"):
                                log_event(f"[audit-flow] 第{idx+1}张 附件页内直传成功：{d1.get('filename')}（{d1.get('bytes')}B）")
                                return d1
                        # ② 页内 fetch KM 通道（allow_click 默认 False：不点击、不弹框）
                        return wb.download_attachment(fp, xlsx_name, download_dir, port=port)

                    _dl_ok, _dl_res, _dl_err = _retry_degraded(
                        _dl_channels, f"第{idx+1}张附件下载",
                        abort_check=lambda: _BG_STATE.get("abort_requested"))
                    if _dl_ok:
                        dl = _dl_res
                    elif _dl_err == "aborted":
                        dl = {"ok": False, "error": "用户中止，下载放弃"}
                    else:
                        dl = {"ok": False, "error": f"重试 {AUDIT_RETRY_MAX} 次仍失败：{str(_dl_err)[:60]}"}
                except Exception as _e:
                    dl = {"ok": False, "error": str(_e)[:60]}
                fname = (dl.get("filename") if isinstance(dl, dict) else None) or xlsx_name
                if isinstance(dl, dict) and dl.get("ok"):
                    excel_pcs, header, hint = self.extractor.extract_excel_pcs(download_dir, fname)
                    if hint:
                        list_note = hint
                        issues.append("附件解析：" + hint)
                    else:
                        list_ok = True
                else:
                    list_note = str(dl.get("error") if isinstance(dl, dict) else dl)[:80]
                    issues.append("清单已上传但无法静默获取（不点击、不弹框）：" + list_note[:80])
            else:
                issues.append("正文未见 xlsx 设备清单附件")
            rec["excel_pcs"] = excel_pcs
            rec["list_found"] = list_found
            rec["list_ok"] = list_ok
            # 6. 规则判定（monthly-report 脚本）
            from services.rule_check import run_rule
            verdict = run_rule("monthly-report", {
                "expect_days": expect, "actual_days": actual,
                "list_found": list_found, "list_ok": list_ok, "list_note": list_note,
                "list_detail": (f"在用 {excel_pcs} 台" if list_ok and excel_pcs is not None else ""),
                "excel_pcs": excel_pcs,
            })
            rec["verdict"] = verdict.get("result")
            rec["checks"] = verdict.get("checks")
            rec["reason"] = verdict.get("reason")
            # v1.0.468（auto_approve）：判定通过且启用时，当场在仍打开的详情页点「通过」
            # （真实审批动作——仅 auto_approve=true 时触发，工具参数默认关闭）
            if rec.get("verdict") == "green" and auto_approve and fp:
                aok, anote = self._r_approve_in_detail(port, fp)
                rec["approve"] = anote if aok else f"点通过失败：{anote}"
                log_event(f"[audit-flow] 第{idx+1}张 auto_approve：{anote}")
            log_event(f"[audit-flow] 第{idx+1}张 {link.get('text','')[:25]} 处理完：verdict={rec.get('verdict')} issues={len(issues)}")
        # 7. 差分清理：按 targetId 关闭处理期间新出现的所有非白名单页面
            try:
                if _base_ids is not None:   # v1.0.652：基线快照失败时跳过（None≠空集，空集会误关预存页）
                    _closed = self._r_close_new_targets(port, _base_ids)
                    if _closed:
                        log_event(f"[audit-flow] 第{idx+1}张处理完，已清理新开页面 {_closed} 个")
            except Exception as e:  # noqa: BLE001
                issues.append("差分清理失败：" + str(e)[:50])
        except Exception as e:  # noqa: BLE001
            issues.append("单张处理异常：" + str(e)[:80])
        rec["issues"] = issues
        return rec

    # ---------- 财务单据流水线 ----------
    def audit_one_finance(self, port, list_pick, link, idx):
        """单张单据：开详情 → 读字段 → 脚本判定 → 关页。返回与月报同形的记录。"""
        issues = []
        rec = {"index": idx, "title": str(link.get("text") or "")[:120],
               "url": (link.get("url") or "")[:200], "verdict": "pending",
               "reason": "", "issues": issues}
        fp = None
        try:
            # v1.0.652（审计 P1-9）：_open_detail 原在 try 之外——open_silent_page/
            # wait_page_load 任一异常直接逸出，财务主循环旧无 per-item 保护，单张炸穿整批。
            fp = self._r_open_detail(port, list_pick, link, issues)
        except Exception as e:  # noqa: BLE001
            rec["reason"] = f"打开详情页异常：{str(e)[:60]}"
            return rec
        if not fp:
            rec["reason"] = "详情页没打开或没定位到"
            return rec
        try:
            flds = wb.read_fields(fp, FINANCE_FIELDS, port=port)
        except Exception as e:  # noqa: BLE001
            rec["reason"] = f"读字段异常：{str(e)[:60]}"
            self._r_close_detail(port, fp)   # v1.0.652：早退也要关页（原只在成功尾部关，异常路径漏页）
            return rec
        data = flds if isinstance(flds, dict) else {}
        rec["field_values"] = {f.get("key"): str(f.get("value") or "")[:60]
                               for f in (data.get("fields") or []) if isinstance(f, dict)}
        if data.get("all_labels"):
            rec["all_labels"] = data["all_labels"][:25]
        from services.rule_check import run_rule
        try:
            v = run_rule("finance-check", data)
        except Exception as e:  # noqa: BLE001
            rec["reason"] = f"规则判定异常：{str(e)[:60]}"
            self._r_close_detail(port, fp)   # v1.0.652：早退也要关页
            return rec
        rec["verdict"] = v.get("result") or "pending"
        rec["checks"] = v.get("checks") or []
        rec["reason"] = str(v.get("reason") or "")[:300]
        if v.get("all_fields_missed"):
            issues.append("字段全部未命中：当前页可能不是单据表单（脚本不猜，交人工确认）")
        self._r_close_detail(port, fp)
        return rec

    def write_finance_report(self, results, ok_n, fail_n, pending_n):
        """财务核查报告落盘（产物库/产出/财务核查_日期.md）。数字只来自脚本记录。"""
        stamp = time.strftime("%Y-%m-%d %H:%M")
        lines = [f"# 财务单据核查报告（{stamp}）", "",
                 f"- 共核查 **{len(results)}** 张：符合 **{ok_n}** / 不符合 **{fail_n}** / 待定 **{pending_n}**",
                 "", "| 单据 | 判定 | 原因 | 关键字段 |", "| --- | --- | --- | --- |"]
        for r in results:
            vd = {"green": "✅ 符合", "red": "❌ 不符合"}.get(r.get("verdict"), "⏳ 待定")
            fv = r.get("field_values") or {}
            keys = "、".join(f"{k}:{(fv.get(k) or '-')[:18]}" for k in FINANCE_FIELDS[:5] if k in fv)
            lines.append("| " + str(r.get("title") or "")[:40].replace("|", "｜") + " | " + vd
                         + " | " + str(r.get("reason") or "")[:80].replace("|", "｜").replace("\n", " ")
                         + " | " + (keys or "-").replace("|", "｜") + " |")
        body = "\n".join(lines) + "\n"
        fname = "财务核查_%s.md" % time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(workspace_root(), "产出", fname)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as e:  # noqa: BLE001
            log_event(f"[audit-finance] 报告写入失败：{e}")
            return ""
        return os.path.join("产出", fname)

    def audit_finance_bills(self, port=None, max_items=None, mode="all"):
        """财务单据审核流水线（脚本驱动、零 LLM、只读）。返回与月报同形的结果 dict，
        好让后台通道（进度格/回流会话/托盘通知）一条代码两用。"""
        if port is None:
            port = _default_port()
        if not _AUDIT_LOCK.acquire(blocking=False):
            return {"ok": False, "busy": True,
                    "error": "已有一条审核流水线在运行（月报或财务）——请等待其完成后再发起"}
        t0 = time.time()
        _AUDIT_RUNNING.set()
        with _BG_LOCK:
            _BG_STATE["abort_requested"] = False
        try:
            list_pick, pages = self._r_find_list_page(port)
            if list_pick is None:
                return {"ok": False, "error": "未找到待办列表窗口（标题含'待办/任务'）",
                        "windows": [p.get("title", "")[:30] for p in pages]}
            links = self._r_load_all_report_links(list_pick, port, match=_is_finance_doc_link, label="单据链接")
            if not links:
                return {"ok": True, "total": 0, "done": 0, "results": [], "green": 0, "red": 0,
                        "pending": 0, "summary": "待办列表未发现单据编号格式的链接（已滚动加载）"}
            results = []
            items = links[:max_items] if max_items else links
            if mode == "resume":
                prev_results = _load_progress(FINANCE_PROGRESS)
                if prev_results:
                    results, items = resume_partition(prev_results, items)
                    log_event("[audit-finance] resume：断点已有 %d 张完成，剩余 %d 张"
                              % (len(results), len(items)))
            def _ph(idx, lk, issue):
                return {"index": idx, "title": (lk.get("text") or "")[:60],
                        "url": (lk.get("url") or "")[:200], "verdict": "pending", "issues": [issue]}

            def _per_item(_i, _l):
                try:
                    # v1.0.652（审计 P1-9）：per-item 防线——单张任何异常转 pending，不毒整批
                    return _audit_one_finance(port, list_pick, _l, _i)
                except Exception as _fe:  # noqa: BLE001
                    log_event(f"[audit-finance] 第{_i + 1}张异常（继续下一张）：{_fe}")
                    return {"index": _i, "title": str(_l.get("text") or "")[:60],
                            "url": (_l.get("url") or "")[:200], "verdict": "pending",
                            "reason": "单张处理异常（流水线继续）", "issues": [str(_fe)[:80]]}

            results, _run_finished = self.iter_audit_items(
                items, _per_item,
                t0=t0, budget_s=1500, port=port, progress_name=FINANCE_PROGRESS,
                placeholder_for=_ph, log_label="audit-finance")
            ok_n = sum(1 for r in results if r.get("verdict") == "green")
            fail_n = sum(1 for r in results if r.get("verdict") == "red")
            pending_n = sum(1 for r in results if r.get("verdict") not in ("green", "red"))
            report_path = _write_finance_report(results, ok_n, fail_n, pending_n)
            summary = (f"共核查 {len(results)} 张单据：符合 {ok_n} / 不符合 {fail_n} / 待定 {pending_n}"
                       + (f"。报告：{report_path}" if report_path else "。（报告未写出）"))
            if results and pending_n == len(results):
                summary += ("。⚠️ 全部待定——脚本没能从详情页读到可比对字段，这是页面定位问题不是单据问题："
                            "向用户说明后重跑一次 run_audit_flow，不要手动逐张点击。")
            if _run_finished:   # v1.0.652（审计 P0-3）：仅完整跑完才清断点（同月报，旧条件恒真）
                try:
                    if os.path.isfile(_progress_path(FINANCE_PROGRESS)):
                        os.remove(_progress_path(FINANCE_PROGRESS))
                except Exception as e:  # noqa: BLE001
                    # 清不掉＝下次跑可能误当断点续跑（少审一批），必须留痕
                    log_event(f"[audit-finance] 断点进度清除失败：{e}")
            return {"ok": True, "flow": "finance", "total": len(items), "done": len(results),
                    "green": ok_n, "red": fail_n, "pending": pending_n,
                    "results": results, "summary": summary, "report_path": report_path,
                    "elapsed_s": round(time.time() - t0, 1)}
        finally:
            _AUDIT_LOCK.release()
            _AUDIT_RUNNING.clear()

    # ---------- 判定历史 ----------
    def history_path(self):
        """v1.0.468：判定历史文件（产物库/台账/月报判定.json）。"""
        return os.path.join(workspace_root(), "台账", "月报判定.json")

    def load_history(self):
        from core.files import read_json   # v1.0.654 A8：句柄样板收敛
        d = read_json(_history_path())
        return d if isinstance(d, dict) and isinstance(d.get("runs"), list) else {"runs": []}

    def load_last_verdicts(self):
        """上一轮的 {主键: verdict}（retry 模式与趋势对比用）。
        v1.0.470（P2-10）：主键优先用 url（精确），历史记录无 url 时回退 title 截断 60 字
        ——旧记录（纯 title key）仍可被双向子串匹配兜底命中。"""
        runs = _load_history()["runs"]
        if not runs:
            return {}
        last = runs[-1]
        out = {}
        for r in last.get("results", []):
            key = (r.get("url") or "").strip() or (r.get("title") or "")
            out[key] = r.get("verdict") or ""
        return out

    def save_verdict_history(self, results):
        """追加拉取结果到判定历史（保留最近 30 轮），返回趋势一句话（无上一轮则空）。"""
        try:
            hp = _history_path()
            os.makedirs(os.path.dirname(hp), exist_ok=True)
            hist = _load_history()
            prev = None
            if hist["runs"]:
                prev = {r.get("title") or "": r.get("verdict") or "" for r in hist["runs"][-1].get("results", [])}
            cur = [{"title": (r.get("title") or "")[:60], "verdict": r.get("verdict") or "",
                    "url": (r.get("url") or "")[:200]}   # v1.0.470（P2-10）：url 入历史作精确主键
                   for r in results]
            hist["runs"].append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "results": cur})
            hist["runs"] = hist["runs"][-30:]
            atomic_write_json(hp, hist, indent=1)   # v1.0.619：原 json.dump(..., open(...)) 句柄不关、非原子
            # 趋势对比
            if not prev:
                return ""
            new_red, turned_green, still_red = [], [], []
            for r in cur:
                pv = prev.get(r["title"])
                if pv and pv != "red" and r["verdict"] == "red":
                    new_red.append(r["title"][:18])
                elif pv == "red" and r["verdict"] == "green":
                    turned_green.append(r["title"][:18])
                elif pv == "red" and r["verdict"] == "red":
                    still_red.append(r["title"][:18])
            parts = []
            if new_red:
                parts.append("新红：" + "、".join(new_red[:5]))
            if turned_green:
                parts.append("转绿：" + "、".join(turned_green[:5]))
            if still_red:
                parts.append(f"连续红 {len(still_red)} 家（{'、'.join(still_red[:5])}）")
            return ("趋势对比上一轮：" + "；".join(parts)) if parts else "趋势对比上一轮：无变化"
        except Exception as e:  # noqa: BLE001
            log_event(f"[audit-flow] 判定历史写入失败：{str(e)[:60]}")
            return ""

    # ---------- 月报流水线 ----------
    def audit_monthly_reports(self, port=None, max_items=None, mode="all", auto_approve=False):
        """月报审核流水线（脚本驱动，零 LLM）。
        返回 {ok, total, done, results: [...], issues_total, summary}
        v1.0.470（P1-11）：模块级非阻塞互斥——原实现可重入，定时任务与用户会话同时触发
        两条流水线会互踩 CDP、互删下载文件（_AUDIT_RUNNING 只挡杀企微不挡自身并发）。
        v1.0.470（P1-12）：断点续跑——每完成一份增量落盘到 台账/月报进度.json；
        mode="resume" 从进度接续（跳过已完成项），完整跑完自动清进度。"""
        if port is None:
            port = _default_port()
        # v1.0.470（P1-11）：占用中直接拒绝，绝不排队重入
        if not _AUDIT_LOCK.acquire(blocking=False):
            return {"ok": False, "busy": True,
                    "error": "已有一条月报审核流水线在运行——请等待其完成后再发起"}
        t0 = time.time()
        _AUDIT_RUNNING.set()
        # v1.0.555（P1-4）：持锁后立即归零 abort 标志——原只在 start_background_audit
        # 重置，用户中止一次后台批审后，任何直接调用 audit_monthly_reports(background=False)
        # （调试/测试路径，或未来调度器同步触发）都会在第一项处 break，返回 ok=True +
        # 0 处理——无声空跑，且该标志在非运行期无入口清除。持锁即重置保证每次运行
        # 语义独立。
        with _BG_LOCK:
            _BG_STATE["abort_requested"] = False
        try:
            list_pick, pages = self._r_find_list_page(port)
            if list_pick is None:
                return {"ok": False, "error": "未找到待办列表窗口（标题含'待办/任务'）",
                        "windows": [p.get("title", "")[:30] for p in pages]}
            # 滚动加载全量月报链接（v1.0.220：列表是虚拟滚动，一次 read 只拿到可见部分）
            report_links = self._r_load_all_report_links(list_pick, port)
            if not report_links:
                return {"ok": True, "total": 0, "done": 0, "results": [],
                        "summary": "待办列表未发现月报链接（已滚动加载）"}
            # 下载目录
            download_dir = os.path.join(workspace_root(), DOWNLOAD_SUB)
            os.makedirs(download_dir, exist_ok=True)
            results = []
            items = report_links[:max_items] if max_items else report_links
            # v1.0.468（mode="retry"）：增量模式——只重跑上一轮判定非 green 的项
            #（配合"分支机构重传附件后复查"场景，30 秒级而非全量 3 分钟）
            if mode == "retry":
                prev = _load_last_verdicts()
                if prev:
                    _bad = {k for k, v in prev.items() if v != "green"}
                    # v1.0.470（P2-10）：url 精确匹配优先，双向子串只对旧格式（无 url 的
                    # 历史 key）兜底——原纯双向子串会把"甲营业部二部"误并入"甲营业部"。
                    items = [lk for lk in items
                             if ((lk.get("url") or "")[:200] in _bad)
                             or any(b in (lk.get("text") or "") or (lk.get("text") or "") in b
                                    for b in _bad if "/" not in b and "\\" not in b)]
                    log_event(f"[audit-flow] retry 模式：上轮非 green {len(_bad)} 项，本轮重跑 {len(items)} 张")
                else:
                    log_event("[audit-flow] retry 模式：无历史判定，退化为全量")
            # v1.0.470（P1-12）：resume 模式——读断点进度，跳过已完成项接着跑
            # （v1.0.619 审查：删掉同行的 `_resumed` 标记——只写不读，两处赋值没有任何下游消费）
            if mode == "resume":
                prev_results = _load_progress()
                if prev_results:
                    # v1.0.470（P1-12）：resume 模式——读断点进度，跳过已完成项接着跑
                    #（v1.0.619 审查：删掉同行的 `_resumed` 标记——只写不读，两处赋值没有任何下游消费）
                    # v1.0.626：判重键抽成函数并修一处漏审——原先所有记录都用
                    # `(r.get("url") or "")` 当键，**没取到详情链接的单据键就是空串**：
                    # 断点里只要有一条这样的 green/red，`"" in _done_keys` 就让 resume
                    # 把本轮**全部**无链接单据当成已完成跳过，报告总数看着还守恒。
                    # 现在没链接的按标题匹配（标题同名的多张单据概率远低于"整批被跳过"，
                    # 且宁可重跑一张已审的，也不要静默漏审）。
                    results, items = resume_partition(prev_results, items)
                    log_event("[audit-flow] resume 模式：断点已有 %d 份完成，剩余 %d 份（无链接项按标题判重）"
                              % (len({resume_key(r) for r in results}), len(items)))
                else:
                    log_event("[audit-flow] resume 模式：无断点进度，退化为全量")
            def _ph(idx, lk, issue):
                return {"index": idx, "title": (lk.get("text") or "")[:60],
                        "url": (lk.get("url") or "")[:200], "issues": [issue]}

            results, _run_finished = self.iter_audit_items(
                items, lambda _i, _l: _audit_one(port, list_pick, _l, download_dir, _i,
                                                auto_approve=auto_approve),
                t0=t0, budget_s=1800, port=port, progress_name="月报进度.json",
                placeholder_for=_ph, log_label="audit-flow")
            ok_n = sum(1 for r in results if r.get("verdict") == "green")
            fail_n = sum(1 for r in results if r.get("verdict") == "red")
            pending_n = sum(1 for r in results if r.get("verdict") not in ("green", "red"))
            issues_total = sum(len(r.get("issues") or []) for r in results)
            # v1.0.220：脚本自己生成报告文件（模型只能引用，禁止编造数字——
            # 17:24 实测脚本只处理 5 张，模型把剩余 16 张编成'待定'）。
            report_path = _write_report(results, ok_n, fail_n, pending_n, issues_total)
            # v1.0.413：全待定且失败原因集中 → summary 给模型明确指引，防止模型拿到
            # "22 份待定"后开始手动逐张点击/乱翻文件系统（2026-09-30 14:12 实测）。
            summary = f"共 {len(results)} 份月报：通过 {ok_n} / 不通过 {fail_n} / 待定 {pending_n}，疑问项 {issues_total} 条。报告已生成：{report_path}"
            if results and pending_n == len(results):
                fail_detail = "；".join((r.get("issues") or [""])[0] for r in results[:3])[:150]
                summary += ("。⚠️ 全部待定——脚本未能打开任何详情页（" + fail_detail + "）。"
                            "这是列表页状态问题而非单据问题，正确做法：向用户说明后**重新运行一次 run_audit_flow**，"
                            "不要手动逐张点击、不要调用 pc_takeover/dsh、不要翻文件系统。")
            elif pending_n:
                # v1.0.425（2026-10-03 实证）：部分待定（附件缺失/损坏等数据问题）时
                # 模型会"逐一手动处理"——手动点击引发 CDP 连接争用与序号漂移错误，
                # 模型将其误读为"CDP 断开"。明确告知：待定=数据问题，只汇报不操作。
                _pend_names = [((r.get("title") or "")[:20]) for r in results if r.get("verdict") == "pending"][:6]
                summary += ("。⚠️ 待定项均为**数据问题**（附件缺失/Excel 损坏等），不需要也不允许手动点击处理"
                            "——手动逐张点击会引发 CDP 连接争用。正确做法：直接向用户汇报待定机构名单"
                            "（" + "、".join(_pend_names) + "），请这些机构重新上传附件后重跑即可。")
            # v1.0.468：判定历史入库（产物库/台账/月报判定.json，保留最近 30 轮）+ 趋势
            trend = _save_verdict_history(results)
            if trend:
                summary += "。" + trend
            # v1.0.470（P1-12）→ v1.0.652（审计 P0-3）：只有完整跑完（for-else 未 break）
            # 才清断点。旧条件 `mode != "resume" or len(results) >= len(items)` 在
            # mode="all" 恒真——中止/预算截断时占位记录让 len 也成立，断点照样被删，
            # resume 退化全量重跑（与 request_abort"断点已存可续跑"承诺直接矛盾）。
            if _run_finished:
                try:
                    os.remove(_progress_path())
                except OSError:
                    pass  # silent-ok: 进度文件不存在＝本就是全新一轮，无需清理
            # v1.0.468（②瘦身）：不再回传全量 results（22 条全字段曾把会话上下文灌到
            # 7.6 万字符触发压缩）——只回非 green 项的关键字段 + 统计；明细在报告文件。
            slim = [{"index": r.get("index"), "title": (r.get("title") or "")[:36],
                     "verdict": r.get("verdict"),
                     "reason": "；".join((r.get("issues") or []) + ([r["reason"]] if r.get("reason") else []))[:120],
                     "approve": r.get("approve")}
                    for r in results if r.get("verdict") != "green"]
            return {"ok": True, "total": len(items), "done": len(results),
                    "green": ok_n, "red": fail_n, "pending": pending_n,
                    "issues_total": issues_total, "report_path": report_path,
                    "results": slim,
                    "summary": summary}
        except Exception as e:  # noqa: BLE001
            # 这条 except 原本只把异常塞进返回值、日志里零痕迹——今天（10-22）月报流水线
            # 在第一张就 TypeError，日志只有"滚动加载 23 条"，排查只能靠猜。补一行带栈。
            log_event(f"[audit-flow] 流水线异常终止：{type(e).__name__}: {e}\n"
                      + traceback.format_exc()[-1200:])
            return {"ok": False, "error": f"流水线异常：{e}"}
        finally:
            _AUDIT_RUNNING.clear()
            _AUDIT_LOCK.release()   # v1.0.470（P1-11）：与 acquire 配对（异常路径也必须释放）

    def write_report(self, results, ok_n, fail_n, pending_n, issues_total):
        """脚本生成 Markdown 报告（v1.0.220）。返回报告相对路径。"""
        import datetime
        today = datetime.date.today().strftime("%Y%m%d")
        # v1.0.652（审计 P2-A5）：文件名加时分秒——旧只精确到日，同日 retry 复查会覆盖
        # 全量报告，上一轮 22 张明细丢失（判定趋势库尚在但明细没了）。
        fname = f"月报检查_{today}_{datetime.datetime.now().strftime('%H%M%S')}.md"
        rel = os.path.join("产出", fname)
        path = os.path.join(workspace_root(), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines = [
            "# 分支机构信息技术运维月报检查结果",
            "",
            f"- 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（脚本自动生成，非模型总结）",
            f"- 共检查 **{len(results)}** 份（脚本滚动加载列表后逐张处理）",
            "",
            "## 总体统计",
            "",
            "| 通过 | 不通过 | 待定(疑问) | 疑问项数 |",
            "|------|--------|-----------|----------|",
            f"| {ok_n} | {fail_n} | {pending_n} | {issues_total} |",
            "",
            "## 明细",
            "",
            "| 序号 | 机构/单据 | 应运维 | 实际运维 | 设备清单 | 在用台数(参考) | 结论 | 疑问/原因 |",
            "|------|-----------|--------|----------|----------|----------------|------|-----------|",
        ]
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "")[:38].replace("|", "/")
            exp = r.get("expect_days") if r.get("expect_days") is not None else "-"
            act = r.get("actual_days") if r.get("actual_days") is not None else "-"
            # v1.0.604：口径改为「清单是否上传」，在用台数只作参考列展示（不再参与判定）
            if r.get("list_ok"):
                lf = "✅ 已上传"
            elif r.get("list_found"):
                lf = "⚠️ 已上传未取到"
            elif r.get("list_found") is False:
                lf = "❌ 未上传"
            else:
                lf = "-"
            ep = r.get("excel_pcs") if r.get("excel_pcs") is not None else "-"
            vd = {"green": "✅ 通过", "red": "❌ 不通过"}.get(r.get("verdict"), "⚠️ 待定")
            iss = "；".join((r.get("issues") or [])[:3])[:80].replace("|", "/") or "-"
            lines.append(f"| {i} | {title} | {exp} | {act} | {lf} | {ep} | {vd} | {iss} |")
        lines.append("")
        lines.append("> 说明：判定只看「运维天数一致」与「设备清单是否上传」；在用台数为参考信息，"
                     "口径与表单不完全一致，不参与红绿。待定项为脚本无法确定（字段未命中/清单无法静默获取），需人工或 LLM 对照 all_labels 确认。")
        lines.append("")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_event(f"[audit-flow] 报告已生成：{rel}")
        return rel


# 三层默认实例（模块级薄委托的落地点；晚绑定——委托在调用时才解析到这里）
_EXTRACTOR = ReportExtractor()
_TRANSPORT = FlowTransport()
_ORCH = AuditOrchestrator()

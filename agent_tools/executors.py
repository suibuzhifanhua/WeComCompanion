"""agent_tools.executors — 工具执行器（常量 / _run_* / _TOOL_HANDLERS / run_tool / _cdp_port）。"""

# v1.0.324 启动提速 + v1.0.364 M8 收敛：requests 懒加载代理实现收敛到 core_utils
from core_utils import _lazy_requests as requests
import os
import re
import threading

import wecom_bridge as wb

from services import rule_check

from core.config_store import _load_experiences, _save_experiences, _exp_rec
from core.config_store import cdp_port as _cfg_cdp_port   # v1.0.588：端口取值口径统一

from core_utils import log_event, append_ledger, _timestamp, _pick_int, _pick_flex, _safe_ws_read_path, dnd_allowed, _parse_file_content, ws_rel_path, ws_sub, _ALLOWED_SUBS, READ_TRUNCATE_BYTES, _mask_args
from core import paths  # noqa: F401（路径函数属性查找，可被测试 patch）
from core.workspace_io import save_workspace_file, archive_progress_round, _progress_items  # v1.0.562 修复：v1.0.555 core_utils 拆分后此函数在 workspace_io，原 save_workspace_file 断链 → agent save_file 全部报错（v1.0.561 发货版实测）

from llm_client import judge_model, _llm_complete
from core.files import find_col_in_header  # v1.0.510（P2-13）：find_col 收敛

from . import os_ops
from . import dsh_bridge  # v1.0.298 PC 接管通道（deepseek-harness：dsh_check/dsh_prepare/dsh_run）
from . import office_gen  # v1.0.313 办公生成三件套（create_excel/create_docx/create_pptx）
from .registry import TOOLS

# ---------- 工具层常量（随模块迁移） ----------
DEFAULT_SCROLL_AMOUNT = 800    # scroll_wecom_window 默认滚动像素
DEFAULT_MSG_TYPE = "text"      # send_wecom_message 默认消息类型
DOWNLOAD_TIMEOUT = 60          # download_file 下载超时(秒)

# 工具名 -> 定义 的索引。_validate_tool_args 在**每次工具调用前**都要取一次定义，
# 原实现是每次遍历 TOOLS（49 项 × 嵌套 .get），百轮任务即数千次线性扫描；
# 长任务里它还在并行读工具的每个 worker 线程上各跑一遍。v1.0.588 改为懒建索引，
# 并保留对 TOOLS 被替换/增删（测试 monkeypatch）的自愈：身份或长度变了就重建。
_TOOL_INDEX = {}
_TOOL_INDEX_SRC = None


def _tool_by_name(name):
    global _TOOL_INDEX, _TOOL_INDEX_SRC
    if _TOOL_INDEX_SRC is not TOOLS or len(_TOOL_INDEX) != len(TOOLS):
        _TOOL_INDEX = {t.get("function", {}).get("name"): t for t in TOOLS}
        _TOOL_INDEX_SRC = TOOLS
    return _TOOL_INDEX.get(name)


def _validate_tool_args(name, args):
    """工具调用前轻量参数 schema 校验（对标 Hermes tool-use 可靠性）：
    必填缺失 / 枚举越界 / 类型错误 提前拦截，返回结构化错误（__error__）让模型
    修正参数后重新调用同一工具——避免无效执行浪费轮次，也避免漏参调用在页面
    上产生副作用（如 click 缺 target、scroll 传非法 direction）。

    校验规则从 TOOLS 注册表的 parameters（properties + required + enum + type）派生：
    - required 缺失（或 None）→ 提示缺哪些 + 合法选项/类型
    - 枚举值不匹配 → 列出合法值
    - 类型不符（integer/array/boolean）→ 提示正确类型；数字接受字符串形式（模型常见输出）
    返回 None 表示校验通过；返回 dict 表示校验失败（作为工具结果回灌给模型）。
    """
    t = _tool_by_name(name)
    if not t:
        return None
    fn = t.get("function", {}) or {}
    params = fn.get("parameters", {}) or {}
    props = params.get("properties") or {}
    req = params.get("required") or []
    args = args or {}
    missing = [k for k in req if k not in args or args.get(k) is None]
    if missing:
        hints = []
        for k in missing:
            p = props.get(k) or {}
            if isinstance(p, dict) and p.get("enum"):
                hints.append(f"{k}(可选: {'/'.join(str(x) for x in p['enum'])})")
            elif isinstance(p, dict) and p.get("type"):
                hints.append(f"{k}({p['type']})")
            else:
                hints.append(k)
        return {"ok": False, "error":
                f"工具 {name} 缺少必填参数：{'、'.join(hints)}。请先补齐参数再重新调用本工具，不要换用无关工具。"}
    for k, v in (args or {}).items():
        p = props.get(k) or {}
        if not isinstance(p, dict):
            continue
        if p.get("enum") is not None and v not in p["enum"]:
            return {"ok": False, "error": f"工具 {name} 参数 {k}={v!r} 不合法，可选值：{'/'.join(str(x) for x in p['enum'])}"}
        pt = p.get("type")
        if pt == "integer":
            # 宽松：int 或数字字符串都放行（模型常输出字符串数字）
            if isinstance(v, bool) or not (
                isinstance(v, int)
                or (isinstance(v, str) and str(v).lstrip("+-").isdigit())
            ):
                return {"ok": False, "error": f"工具 {name} 参数 {k} 应为整数，实际是 {type(v).__name__}={v!r}"}
        elif pt == "array":
            if not isinstance(v, list):
                return {"ok": False, "error": f"工具 {name} 参数 {k} 应为数组，实际是 {type(v).__name__}"}
        elif pt == "boolean":
            if not isinstance(v, bool):
                return {"ok": False, "error": f"工具 {name} 参数 {k} 应为布尔值，实际是 {type(v).__name__}={v!r}"}
    return None


def _finish_tool():
    return _tool_by_name("finish")

def _run_create_skill(args, cfg):
    name = re.sub(r'[^A-Za-z0-9_\-]', "_", str(args.get("name", "skill")))[:40] or "skill"
    md = args.get("content", "")
    desc = args.get("description", "")
    header = f"---\nname: {name}\ndescription: {desc}\n---\n\n" if desc else ""
    return save_workspace_file("技能", f"{name}.md", header + md)


def _run_save_file(args, cfg):
    """save_file：写文件到产物库，落盘成功后自动登记产物台账（P4-5）。"""
    out = save_workspace_file(args.get("subdir", "产出"),
                              args.get("filename", "untitled.txt"),
                              args.get("content", ""))
    if isinstance(out, dict) and out.get("ok"):
        append_ledger(out.get("sub", "产出"), out.get("filename", ""), source="save_file",
                      note=(args.get("note") or "")[:40])
    return out


def _run_save_workspace_file(args, cfg):
    """save_workspace_file（v1.0.230）：产物库写入，支持追加模式。

    为什么单独开一个工具：答题记录/进度流水是「逐条累积」的内容，
    用 save_file（覆盖写）要每次重写全量——20 题就是 20 次全量输出，
    费 token 且中途出错会丢历史。追加模式一条条写即可。
    """
    out = save_workspace_file(args.get("subdir") or "产出",
                              args.get("filename", "untitled.txt"),
                              args.get("content", ""),
                              append=str(args.get("mode", "append") or "append").lower() == "append")
    if isinstance(out, dict) and out.get("ok"):
        append_ledger(out.get("sub", "产出"), out.get("filename", ""),
                      source="save_workspace_file", note=(args.get("note") or "")[:40])
    return out


def _run_close_page(args, cfg):
    """close_wecom_page（v1.0.203）：关闭指定窗口/标签页（pick 缺省关当前跟随窗口）。
    批量审核流程：审完一项 → append_progress 保存 → close_wecom_page 关详情页 →
    回列表页 → 点下一项。防详情页标签堆积（列表链接 target=_blank 每点一次开一个）。"""
    out = wb.close_page(args.get("pick"), _cdp_port(cfg))
    return out


def _run_append_progress(args, cfg):
    """append_progress（v1.0.198）：把一条「单项处理结果」追加到进度文件
    「产物库/产出/<key>_进度.md」，返回已累计条目数。

    批量任务（逐项审核/逐张处理）的上下文隔离核心：每处理完一项就立即落盘，
    最后汇总时 read_workspace_file 读进度文件合并输出总结——中间结果不丢、
    单轮上下文不用背负全部历史（配合 loop 的自动上下文精简，每项处理完
    只留指令+进度+最近状态）。"""
    key = str(args.get("key", "")).strip() or "任务"
    content = str(args.get("content", "")).strip()
    if not content:
        return {"ok": False, "error": "缺少必填参数：content（本条处理结果的记录内容）"}
    # 文件名做安全化（防路径穿越）
    safe = "".join(c for c in key if c not in '\\/:*?"<>|').strip() or "任务"
    fname = f"{safe}_进度.md"
    # 每条记录用 --- 分隔（计数依赖），格式：---\n<content>\n\n
    out = save_workspace_file("产出", fname, f"---\n{content}\n\n", append=True)
    if not (isinstance(out, dict) and out.get("ok")):
        return out or {"ok": False, "error": "追加进度文件失败"}
    # 统计累计条目数（进度文件现有内容按 --- 分隔计数）
    import os
    
    try:
        full = os.path.join(paths.workspace_root(), "产出", fname)
        n = 0
        if os.path.exists(full):
            # v1.0.361 审查修复（O3）：with 关闭句柄（原裸 open 依赖 GC）
            with open(full, encoding="utf-8") as f:
                txt = f.read()
            # v1.0.650（F5）：计数口径与归档路径共用 _progress_items，别再各数各的
            n = _progress_items(txt)
        return {"ok": True, "filename": fname, "items": n,
                "result": f"已记录第 {n} 条，累计 {n} 项。继续处理下一项。"}
    except Exception:  # noqa: BLE001
        return {"ok": True, "filename": fname, "result": "已记录（计数失败不影响）"}


def _run_new_progress_round(args, cfg):
    """new_progress_round（v1.0.650，F5）：把本轮进度台账改名归档，同一 key 另起一轮。

    进度文件是纯追加、从不轮转的，而快捷入口的提示词写着"已记过的直接跳过"——
    上一轮的记录会喂错这一轮（新轮一开头就被提醒"这些都做过了"）。归档由用户显式发起：
    说"开新一轮/重新跑一轮"或直接点产物库里那一行的「↻ 开新一轮」。
    """
    key = str(args.get("key", "")).strip()
    if not key:
        return {"ok": False, "error": "缺少必填参数：key（要开新一轮的任务标识，如 月报审核 / 答题记录）"}
    safe = "".join(c for c in key if c not in '\\/:*?"<>|').strip() or "任务"
    out = archive_progress_round("产出", f"{safe}_进度.md")
    if not (isinstance(out, dict) and out.get("ok")):
        return out or {"ok": False, "error": "开新一轮失败"}
    return {"ok": True, "result": (
        f"已把上一轮 {out['items']} 条记录归档为「{out['sub']}/{out['archived']}」，"
        f"本轮从空的 {out['cleared']} 开始——之前记过的条目本轮不再算“已处理”，可以重新跑。"),
        "archived": out["archived"], "items": out["items"]}


def _run_save_experience(args, cfg):
    """save_experience：把一条「问题+解决方法」沉淀到经验库（experiences.json）。
    供 agent 在答题/审批/流程分析等场景发现新规则或踩坑经验时主动调用，让后续同类任务
    直接复用（Claw 的 memory 思想）。模块字段用于经验库 UI 分类筛选。
    失败不阻塞主流程，错误以结构化 __error__ 返回。"""
    module = str(args.get("module", "")).strip()
    problem = str(args.get("problem", "")).strip()
    solution = str(args.get("solution", "")).strip()
    if not problem or not solution:
        return {"ok": False, "error": "缺少必填参数：problem 和 solution 都要写（即使简单也要写），"
                              "否则经验库内容无价值。"}
    items = _load_experiences()
    rec = _exp_rec({"module": module, "problem": problem, "solution": solution})
    items.append(rec)
    ok, err = _save_experiences(items)
    if not ok:
        return {"ok": False, "error": f"经验保存失败：{err}"}
    return {"ok": True, "id": rec["id"], "module": rec["module"],
            "summary": f"已沉淀：[{rec['module']}] {problem[:40]}{'...' if len(problem) > 40 else ''}"}


def _run_download_file(args, cfg):
    """升级版 download_file：校验 http/https、超时、非 200 报错、按 Content-Disposition 命名。"""
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "缺少 url"}
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return {"ok": False, "error": f"仅支持 http/https 链接：{url}"}
    sub = args.get("subdir") or "下载"
    filename = args.get("filename")
    try:
        r = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True,
                         headers={"User-Agent": "WeComCompanion/1.0"})
        if r.status_code != 200:
            return {"ok": False, "error": f"下载失败：HTTP {r.status_code}"}
        if not filename:
            # v1.0.616（审查 P2）：原来这里自己抄了一遍裸正则取名，与
            # wecom_bridge/download.py::_name_from_cd 两套口径——OA 给的
            # `filename*=UTF-8''%E8%AE…` 走这条路径会落成不可读的 %xx 文件名
            # （下载完还得自己猜是哪个文件）。统一走那个解码器（RFC5987 + latin-1 修复
            # + basename/非法字符消毒都在里面）。
            from wecom_bridge.download import _name_from_cd
            filename = _name_from_cd(r.headers.get("Content-Disposition", ""))
        if not filename:
            fn = url.rstrip("/").split("?")[0].split("#")[0].split("/")[-1]
            filename = fn if fn and "." in fn else f"download_{_timestamp()}.bin"
        path = ws_rel_path(sub, filename)
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return {"ok": True, "path": path, "filename": os.path.basename(path), "bytes": os.path.getsize(path)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"下载失败：{e}"}


def _run_read_workspace_file(args, cfg):
    """读取产物库内某文件，自动按扩展名解析（文本/pdf/docx/xlsx/pptx），限制在产物库范围内。

    v1.0.294：新增可选 max_chars——读取前 N 字符的「头部概览」（digest）。默认不传=原行为
    （上限 READ_TRUNCATE_BYTES=50KB）。用途：超大素材（多份视频 OCR 叠加可达 80KB+）先读
    头部概览定位相关段、保持上下文精简避免大上下文空转，确需某段全文再不带 max_chars 单独读。
    """
    try:
        # v1.0.369：_safe_ws_read_path = 产物库内正常收敛 + 拖拽白名单只读放行
        # （用户拖入聊天框的本地文件登记白名单后，agent 可直接读原路径分析，无需先上传复制）
        p = _safe_ws_read_path(args.get("path", ""))
        if not p:
            return {"ok": False, "error": "路径不合法或超出产物库范围（拖入的本地路径需先拖进聊天框登记）"}
        if not os.path.isfile(p):
            return {"ok": False, "error": f"文件不存在：{args.get('path')}"}
        # v1.0.294：可选 max_chars（头部概览/有界读取），非法值忽略回退默认
        max_chars = None
        _mc = args.get("max_chars")
        if _mc is not None:
            try:
                max_chars = int(_mc)
            except Exception:  # noqa: BLE001
                max_chars = None
            if max_chars is not None and max_chars <= 0:
                max_chars = None
        size = os.path.getsize(p)
        kind, content, note = _parse_file_content(p, size)
        if kind is None:
            return {"ok": False, "error": f"解析失败：{note}", "path": p, "size": size,
                    "filename": os.path.basename(p)}
        if content:
            # 防止超大内容撑爆上下文：有 max_chars 时以更小者为准
            _orig_len = len(content)
            _limit = max_chars if (max_chars and max_chars < READ_TRUNCATE_BYTES) else READ_TRUNCATE_BYTES
            if _orig_len > _limit:
                content = content[:_limit]
                if max_chars:
                    note += (f"（max_chars 头部概览：已读前 {_limit} 字符，原文共 {_orig_len} 字符；"
                             f"确需某段全文可去掉 max_chars 重读或按题干关键词定位）")
                else:
                    note += f"（已截断至 {READ_TRUNCATE_BYTES} 字节，原文共 {_orig_len} 字符）"
            return {"ok": True, "path": p, "size": size, "kind": kind,
                    "filename": os.path.basename(p), "note": note,
                    "truncated": _orig_len > _limit,
                    "content": content}
        return {"ok": True, "path": p, "size": size, "kind": kind,
                "filename": os.path.basename(p),
                "content": f"（{os.path.basename(p)} 无可用文本内容）{note}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取失败：{e}"}


def _run_analyze_monthly_attachment(args, cfg):
    """analyze_monthly_attachment（v1.0.204）：读设备清单 Excel，脚本统计「在用 台式/笔记本」数。
    模型只需 download 附件 → 一次调用本工具拿统计数字 → run_rule_check 判定，
    不再让 LLM 逐行读 Excel 做统计（省 1-2 次 LLM 轮次，且不会数错）。

    自动识别表头（设备状态/在用状态/状态、设备类型/类型/类别 等近义列），
    统计 状态含「在用」且 类型含「台式/笔记本」的条目数。"""
    path = str(args.get("path", "")).strip()
    try:
        p = _safe_ws_read_path(path)   # v1.0.369：支持拖拽白名单路径（只读）
        if not p:
            return {"ok": False, "error": "路径不合法或超出产物库范围（拖入的本地路径需先拖进聊天框登记）"}
        if not os.path.isfile(p):
            return {"ok": False, "error": f"文件不存在：{path}"}
        import openpyxl
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return {"ok": True, "total_rows": 0, "pcs_in_use": None,
                    "error_hint": "Excel 为空表", "filename": os.path.basename(p)}
        # v1.0.221：表头可能不在首行（前几行是"表格填写人员/截止日期"信息）——
        # v1.0.654 A7：扫描块收敛到 core.files.scan_table_header（原与 batch_flow 逐字两份）
        from core.files import scan_table_header
        header, header_row_idx = scan_table_header(rows)

        # v1.0.510（P2-13）：find_col 收敛到 core.files.find_col_in_header（原与
        # batch_flow 各持一份相同实现）
        status_col = find_col_in_header(header, [["设备状态"], ["在用状态"], ["状态"]])
        type_col = find_col_in_header(header, [["设备类型"], ["类型"], ["类别"]])
        if status_col is None or type_col is None:
            return {"ok": True, "total_rows": len(rows) - header_row_idx - 1, "pcs_in_use": None,
                    "header": header[:20], "filename": os.path.basename(p),
                    "error_hint": "未识别到状态/类型列，表头：" + " | ".join(header[:20])}
        count = 0
        for r in rows[header_row_idx + 1:]:
            st = str(r[status_col] or "").strip()
            tp = str(r[type_col] or "").strip()
            if "在用" in st:
                if "台式" in tp or "笔记本" in tp or "电脑" in tp:
                    count += 1
        return {"ok": True, "total_rows": len(rows) - header_row_idx - 1, "pcs_in_use": count,
                "header": header[:20], "filename": os.path.basename(p),
                "result": f"在用设备数（台式+笔记本）={count}，共 {len(rows)-header_row_idx-1} 行数据"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"分析 Excel 失败：{e}"}


def _run_learn_capture(args, cfg):
    """learn_capture（v1.0.229）：在线课程内容采集——PDF 逐页「截图→OCR→翻页」
    / 视频按间隔抽帧 OCR，合并成 Markdown 存 产物库/知识库/课件/<课程名>.md。
    解决：学习平台课件渲染在 canvas/video 里，read_page 拿不到文字，
    而考试题直接出自课件（实测盲猜只有 60 分，及格 90）。"""
    from services import learn_capture
    pick = args.get("pick")
    if pick is not None:
        try:
            pick = int(pick)
        except Exception:  # noqa: BLE001
            pick = None
    return learn_capture.capture_course(
        pick=pick,
        port=_cdp_port(cfg),
        name=str(args.get("name", "") or "课程"),
        mode=str(args.get("mode", "auto") or "auto"),
    )


def _run_audit_flow(args, cfg):
    """run_audit_flow（v1.0.210）：脚本流水线执行器——整条确定性流程由 Python 脚本
    毫秒级跑完（找列表→解析链接→逐个点开→读字段→下载附件→统计→判定→记录→关页），
    零 LLM 消耗；只在「智能判断」场景（字段未命中/Excel 列名无法识别/附件异常）
    产出疑问项 issues，随结果返回由 LLM 处理。flow=monthly-report 为月报审核流水线。
    v1.0.523-6：默认后台执行——22 张 5-9 分钟不再独占对话（agent 立即拿到"已开始"
    并告知用户，完成后托盘提醒 + 报告落盘，进度条在工作台实时可见）；
    background=false 显式传参时保留同步模式（调试/单测用）。"""
    flow = str(args.get("flow", "")).strip() or "monthly-report"
    if flow not in ("monthly-report", "finance"):
        return {"ok": False, "error": f"未知流水线：{flow}（当前支持 monthly-report / finance）"}
    from services import batch_flow
    port = _cdp_port(cfg)
    background = args.get("background", True)
    _mode = str(args.get("mode", "all")).strip() or "all"
    if flow == "finance":
        # v1.0.650（F6）：财务单据流水线**只读**——判定脚本早已存在（rule_check.check_finance），
        # 这里补的是"逐张点开-读字段-关页"这三步也脱离模型轮次。它没有 auto_approve 参数，
        # 也就没有任何一条代码路径会去点「通过/同意/提交」。
        if args.get("auto_approve"):
            return {"ok": False, "error": "财务流水线只读，不执行任何审批点击：auto_approve 不适用。"
                                 "要真实提交审批请由人在企微页面自己点（或明确改用月报流水线的确认路径）。"}
        if not background:
            return batch_flow.audit_finance_bills(port=port, mode=_mode)
        try:
            from wecom_bridge.windows import current_pane_id as _pane_id
            _pane = _pane_id()
        except Exception:  # noqa: BLE001
            _pane = None
        r = batch_flow.start_background_audit(port=port, mode=_mode, pane=_pane, flow="finance")
        if r.get("ok"):
            return {"ok": True, "background": True,
                    "result": "财务单据核查已在后台开始执行（脚本逐张读字段+判定，零 LLM 消耗）。"
                              "期间可继续对话；完成后结果自动写回本对话并弹系统通知，报告落在产物库/产出。"
                              "不要轮询等待、也不要重复调用本工具。"}
        return r
    if not background:
        return batch_flow.audit_monthly_reports(
            port=port,
            mode=str(args.get("mode", "all")).strip() or "all",
            auto_approve=bool(args.get("auto_approve", False)))
    try:
        from wecom_bridge.windows import current_pane_id as _pane_id
        _pane = _pane_id()
    except Exception:  # noqa: BLE001
        _pane = None
    r = batch_flow.start_background_audit(port=port,
                                          mode=str(args.get("mode", "all")).strip() or "all",
                                          auto_approve=bool(args.get("auto_approve", False)),
                                          pane=_pane)
    if r.get("ok"):
        return {"ok": True, "background": True,
                "result": "月报批审已在后台开始执行（约 3-6 分钟）。期间可继续对话；"
                          "完成后结果会自动写回本对话（并弹系统通知），报告自动写入产物库；"
                          "工作台顶部的进度格可实时查看已处理数量。"
                          "不要轮询等待、也不要重复调用本工具。"}
    return r


def _run_list_workspace_files(args, cfg):
    """列出产物库某子目录（或全部子目录，含知识库嵌套子目录）的文件。

    subdir 支持扁平子目录（对话记录/下载/产出/技能/上传）与知识库嵌套子目录
    （知识库/课件、知识库/题库、知识库/制度，正/反斜杠均可）。不传则返回全量树。
    v1.0.369：path=绝对目录（须在拖拽白名单内，即用户拖入聊天框的文件夹）→
    直接列出该外部目录内容，支撑「拖个文件夹让 agent 分析」场景。
    """
    try:
        root = paths.workspace_root()
        # v1.0.369：拖拽白名单目录优先（用户拖入的外部文件夹）
        d_arg = str(args.get("path") or "").strip()
        if d_arg:
            if not dnd_allowed(d_arg):
                return {"ok": False, "error": "目录不在拖拽白名单（仅支持用户拖入聊天框的文件夹）；"
                                     "分析产物库请改用 subdir 参数"}
            d = os.path.realpath(d_arg)
            if not os.path.isdir(d):
                return {"ok": False, "error": f"目录不存在：{d_arg}"}
            files = []
            for f in sorted(os.listdir(d)):
                fp = os.path.join(d, f)
                # v1.0.373（P3-12）：listdir→getsize 之间文件可能被删/改名，逐项容错
                try:
                    files.append({"name": f, "size": os.path.getsize(fp) if os.path.isfile(fp) else 0,
                                  "is_dir": os.path.isdir(fp)})
                except OSError:
                    continue
            return {"ok": True, "dir": d, "files": files,
                    "note": "拖入文件夹清单；读其中文件用 read_workspace_file(path=完整路径)"}
        sub = str(args.get("subdir") or "").replace("\\", "/").strip()
        if sub:
            if sub not in _ALLOWED_SUBS:
                return {"ok": False, "error": f"未知子目录：{sub}；可选：{', '.join(_ALLOWED_SUBS)}"}
            d = os.path.join(root, sub)
            files = [{"name": f, "size": os.path.getsize(os.path.join(d, f)),
                      "is_dir": os.path.isdir(os.path.join(d, f))} for f in sorted(os.listdir(d))]
            return {"ok": True, "subdir": sub, "dir": d, "files": files}
        out = []
        for s in _ALLOWED_SUBS:
            d = os.path.join(root, s)
            try:
                files = sorted(os.listdir(d))
            except Exception as e:  # noqa: BLE001
                log_event(f"[workspace] 读取子目录 {s} 失败：{e}")
                files = []
            out.append({"sub": s, "dir": d,
                        "files": [{"name": f, "size": os.path.getsize(os.path.join(d, f))} for f in files]})
        return {"ok": True, "root": root, "tree": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"列举失败：{e}"}


# 工具名 -> 执行函数(args, cfg) 的注册表，替代过长的 if/elif 链。
# 新增工具只需在此登记一项，dispatch_tool / build_agent_tools 体系无需改动。
def _cdp_port(cfg):
    """从配置取 CDP 端口；未配置/非法时回退默认 9222，保证绑定与实际操作使用同一端口。
    v1.0.588：实现收敛到 core.config_store.cdp_port（全项目唯一口径），本函数保留为
    既有调用面（agent_tools/__init__ re-export、routes、services 共 10+ 处在用）。"""
    return _cfg_cdp_port(cfg)


def _bing_rss_search(query, max_results, timeout=10):
    """Bing RSS 搜索（主后端）：`bing.com/search?q=…&format=rss` 返回干净 XML，
    无需 ddgs、无需 bs4，stdlib（urllib + ElementTree）即可解析。
    本机实测（2026-09-04）：duckduckgo.com 被 GFW 墙，bing.com:443 直连可达且
    RSS 稳定返回 <item><title>/<link>/<description>。返回 [{title,url,snippet}]，
    连接失败/无结果抛异常或返回空列表，由调用方决定兜底。
    """
    import urllib.parse
    import urllib.request
    import xml.etree.ElementTree as ET
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query) + "&format=rss"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    data = urllib.request.urlopen(req, timeout=timeout).read()
    root = ET.fromstring(data)
    results = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if not link:
            continue
        results.append({"title": title, "url": link, "snippet": desc})
        if len(results) >= max_results:
            break
    return results


def _ddgs_search(query, max_results):
    """DuckDuckGo 搜索（海外兜底后端）：依赖 ddgs 库，国内通常被墙不可用，
    仅在 Bing 主后端失败时尝试。返回 [{title,url,snippet}]。"""
    from ddgs import DDGS
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title": (r.get("title") or "").strip(),
                "url": r.get("href") or r.get("url") or "",
                "snippet": (r.get("body") or r.get("snippet") or "").strip(),
            })
    return results


def _run_web_search(args, cfg):
    """联网搜索：按关键词返回 标题/链接/摘要 列表。

    双后端：①Bing RSS（主，国内可达、stdlib 解析）→ ②DuckDuckGo/ddgs（海外兜底）。
    主后端连接失败/超时自动切兜底；两者都无结果给 __info__，都失败给 __error__。
    """
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "缺少 query 参数（搜索关键词）。"}
    try:
        max_results = max(1, min(int(args.get("max_results") or 5), 10))
    except (TypeError, ValueError):
        max_results = 5

    errors = []
    # ① 主后端：Bing RSS
    try:
        results = _bing_rss_search(query, max_results)
        if results:
            return {"query": query, "count": len(results), "results": results, "source": "bing"}
        errors.append("bing: 无结果")
    except Exception as e:  # noqa: BLE001
        errors.append(f"bing: {type(e).__name__} {str(e)[:120]}")
    # ② 兜底：DuckDuckGo（ddgs 缺失则跳过）
    try:
        results = _ddgs_search(query, max_results)
        if results:
            return {"query": query, "count": len(results), "results": results, "source": "duckduckgo"}
        errors.append("ddgs: 无结果")
    except ImportError:
        errors.append("ddgs: 库未安装（仅海外兜底，可忽略）")
    except Exception as e:  # noqa: BLE001
        errors.append(f"ddgs: {type(e).__name__} {str(e)[:120]}")

    if all("无结果" in x for x in errors):
        return {"__info__": f"未搜索到与「{query}」相关的结果，请尝试更换关键词或减少 max_results。"}
    return {"ok": False, "error": f"搜索失败（{'；'.join(errors)}）。请稍后重试或更换关键词。"}


def _run_fetch_webpage(args, cfg):
    """抓取指定 URL 网页正文：自动处理编码、剥离脚本样式、截断到 max_chars（默认 8000）。

    依赖 requests（backend 顶层已导入）+ beautifulsoup4；bs4 懒加载，缺库时返回友好提示。
    非 200 返回错误。
    """
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "缺少 url 参数（要抓取的网页地址）。"}
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "仅支持 http/https 链接。"}
    try:
        max_chars = max(500, min(int(args.get("max_chars") or 8000), 50000))
    except (TypeError, ValueError):
        max_chars = 8000
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {"ok": False, "error": "缺少依赖 beautifulsoup4：请先执行 pip install beautifulsoup4 安装后重启应用。"}
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
        if r.status_code != 200:
            return {"ok": False, "error": f"请求失败，HTTP {r.status_code}：{url}"}
        r.encoding = r.apparent_encoding or r.encoding
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside"]):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        text = soup.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        total = len(text)
        if total > max_chars:
            text = text[:max_chars] + f"\n...[已截断：原文 {total} 字符，仅显示前 {max_chars} 字符]"
        if not text:
            return {"__info__": "页面无可见正文（可能是纯 JS 渲染页面，请换其他链接）。"}
        return {"url": url, "title": title, "char_count": len(text), "content": text}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": f"抓取超时（20 秒）：{url}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"抓取失败：{e}"}

# ask_judge 结果缓存（v1.0.236）：agent 重试/试错时同一道判断题（同模型+同问题+同上下文）
# 不再重复烧大模型——答题「试一试排除」每轮会反复确认锁定结论，实测大模型（27b）单次 10s+。
_ASK_JUDGE_CACHE = {}          # key=(model_id, question, context) -> advice
_ASK_JUDGE_CACHE_MAX = 128     # 简易 LRU 上限
_ASK_JUDGE_CACHE_LOCK = threading.Lock()   # v1.0.361 审查修复（O2）：多 pane 并发无锁共享
# v1.0.555（P1-7）：ask_judge 工具的墙钟上限。对齐 _judge_escalate 的 deadline=300
# 但更紧——judge 是 think 已关的短问答，正常 5~20s；挂起时 120s 即放弃回退小模型
# 自行判断，不再同步阻塞工具分发 15 分钟。
_ASK_JUDGE_DEADLINE = 120


def _ask_judge_cache_get(key):
    with _ASK_JUDGE_CACHE_LOCK:
        v = _ASK_JUDGE_CACHE.get(key)
        if v is not None:                       # 命中即移到末尾（LRU）
            _ASK_JUDGE_CACHE.pop(key, None)
            _ASK_JUDGE_CACHE[key] = v
    return v


def _ask_judge_cache_put(key, val):
    with _ASK_JUDGE_CACHE_LOCK:
        _ASK_JUDGE_CACHE[key] = val
        while len(_ASK_JUDGE_CACHE) > _ASK_JUDGE_CACHE_MAX:   # 淘汰最旧
            _ASK_JUDGE_CACHE.pop(next(iter(_ASK_JUDGE_CACHE)))


def _run_ask_user(args, cfg):
    """ask_user（v1.0.373 用户需求）：任务中需要用户抉择/提供信息时弹窗询问。
    返回 __ask_user__ 标记 → agent_loop 挂起本轮并推 SSE ask_user 事件 →
    前端弹选择卡（点选项或自由输入）→ /chat/continue 以【用户决定】resume_note 恢复。
    与安全审批（__approval_needed__）同一条挂起/恢复管道，但无需令牌、不重试工具。"""
    q = str(args.get("question") or "").strip()
    if not q:
        return {"ok": False, "error": "缺少 question——要问用户的问题不能为空"}
    opts = args.get("options") or []
    if not isinstance(opts, list):
        opts = []
    opts = [str(o).strip()[:60] for o in opts if str(o).strip()][:6]
    return {"__ask_user__": True,
            "question": q[:300],
            "options": opts,
            "allow_text": bool(args.get("allow_text", True))}


def _run_ask_judge(args, cfg):
    """双模型分工：小模型把判断/决策交给大模型（judge_model）。同问命中缓存直接返回。"""
    jm = judge_model(cfg)
    if not jm:
        return {"ok": False, "error": "未配置判断模型（在「模型管理」设置「判断模型」）。请基于上下文自行判断，或改回单模型。"}
    q = (args or {}).get("question") or ""
    ctx = (args or {}).get("context") or ""
    if not q:
        return {"ok": False, "error": "ask_judge 缺少 question 参数"}
    jname = jm.get("name") or jm.get("model") or ""
    ck = (jname, q.strip(), ctx.strip())
    cached = _ask_judge_cache_get(ck)
    if cached is not None:
        return {"ok": True, "judge": jname, "advice": cached, "cached": True}
    sys_p = ("你是判断大模型，负责做明确、可执行的最终判断。agent（小模型）会把需要判断的问题与上下文交给你。"
             "请直接给出结论（如『选 B』『通过』『不通过，原因：…』），不要复述问题、不要写冗长分析。"
             "信息不足时明确指出缺什么。用中文，简洁。")
    msgs = [{"role": "system", "content": sys_p},
            {"role": "user", "content": (q + ("\n\n上下文：\n" + ctx if ctx else ""))[:8000]}]
    # v1.0.555（P1-7 修复）：原调用无 deadline 无 poll_abort——judge 挂起时单次
    # ask_judge 最坏 3 次流尝试 ×(240~300s 熔断) ≈ 12~15 分钟同步阻塞在工具分发里，
    # 且用户点停止无效（_llm_complete 的 deadline/poll_abort 机制本身完备，只是这里没用）。
    # judge 是 think 已关的短问答（正常 5~20s），120s 足够宽松。
    from agent_tools.dispatch import pane_abort_hook as _pane_abort_hook
    ans = _llm_complete(jm, msgs, disable_thinking=True,
                        poll_abort=_pane_abort_hook(), deadline=_ASK_JUDGE_DEADLINE)
    if not ans:
        return {"ok": False, "error": "判断模型无响应（服务未启动或超时），请自行判断"}
    _ask_judge_cache_put(ck, ans)
    return {"ok": True, "judge": jname, "advice": ans}


_TOOL_HANDLERS = {
    "capture_wecom_window":     lambda a, c: wb.capture(_pick_flex(a, "pick"), _cdp_port(c)),
    "list_wecom_windows":       lambda a, c: wb.window_list_summary(_cdp_port(c)),
    "send_wecom_webhook":       lambda a, c: wb.send_webhook(
        (c.get("wecom") or {}).get("webhook", ""), a.get("content", ""), a.get("msgtype", DEFAULT_MSG_TYPE)),
    "list_interactive_elements": lambda a, c: wb.get_interactive_elements(_pick_flex(a, "pick"), _cdp_port(c)),
    "click_wecom_element":      lambda a, c: wb.click_element(_pick_flex(a, "pick"), a.get("target"), _cdp_port(c)),
    "type_wecom_text":          lambda a, c: wb.type_text(_pick_flex(a, "pick"), a.get("target"), a.get("text", ""), _cdp_port(c)),
    "read_wecom_page":          lambda a, c: wb.read_page(_pick_flex(a, "pick"), _cdp_port(c)),
    "read_wecom_fields":        lambda a, c: wb.read_fields(_pick_flex(a, "pick"), a.get("fields", []), _cdp_port(c)),
    "run_rule_check":           lambda a, c: rule_check.run_rule(str(a.get("module", "")), a.get("data") or {}),
    "run_audit_flow":           _run_audit_flow,
    "learn_capture":            _run_learn_capture,
    "select_wecom_option":      lambda a, c: wb.select_option(_pick_flex(a, "pick"), a.get("target"), a.get("value", ""), _cdp_port(c)),
    "scroll_wecom_window":      lambda a, c: wb.scroll_window(_pick_flex(a, "pick"), a.get("direction", "down"),
                                                              a.get("amount", DEFAULT_SCROLL_AMOUNT), _cdp_port(c)),
    "wait_wecom_page_load":     lambda a, c: wb.wait_page_load(_pick_flex(a, "pick"),
                                                              _pick_int(a, "timeout") or 25, _cdp_port(c)),
    "list_wecom_links":         lambda a, c: wb.list_links(_pick_flex(a, "pick"), _cdp_port(c)),
    "upload_wecom_file":        lambda a, c: wb.upload_file(_pick_flex(a, "pick"), a.get("target"), a.get("filepath", ""), _cdp_port(c)),
    "type_wecom_document":      lambda a, c: wb.type_document(_pick_flex(a, "pick"), a.get("text", ""), _cdp_port(c)),
    "save_file":                _run_save_file,
    "save_workspace_file":      _run_save_workspace_file,
    "append_progress":          _run_append_progress,
    "new_progress_round":       _run_new_progress_round,
    "close_wecom_page":         _run_close_page,
    "save_experience":          _run_save_experience,
    "download_file":            _run_download_file,
    "download_wecom_attachment": lambda a, c: wb.download_attachment(
        _pick_flex(a, "pick"), a.get("target"), ws_sub("下载"), _cdp_port(c),
        timeout=_pick_int(a, "timeout") or 90),
    "read_workspace_file":      _run_read_workspace_file,
    "analyze_monthly_attachment": _run_analyze_monthly_attachment,
    "list_workspace_files":     _run_list_workspace_files,
    "create_skill":             _run_create_skill,
    "web_search":               _run_web_search,
    "fetch_webpage":            _run_fetch_webpage,
    "finish":                   lambda a, c: {"__finish__": True, "final_answer": a.get("final_answer", "任务已完成。")},
    "ask_user":                 _run_ask_user,
    "ask_judge":                _run_ask_judge,
    # —— PC 操控（v1.0.297）：OS 级工具，实现见 agent_tools/os_ops.py ——
    "pc_screenshot":            lambda a, c: os_ops.pc_screenshot(a.get("window")),
    "pc_files":                 lambda a, c: os_ops.pc_files(a.get("op"), a.get("path"),
                                                            a.get("target"), a.get("content"), c),
    "pc_run":                   lambda a, c: os_ops.pc_run(a.get("command"),
                                                          a.get("shell", "cmd"), a.get("timeout"), c),
    "pc_apps":                  lambda a, c: os_ops.pc_apps(a.get("op"), a.get("name")),
    "pc_clipboard":             lambda a, c: os_ops.pc_clipboard(a.get("op"), a.get("text")),
    "pc_system_info":           lambda a, c: os_ops.pc_system_info(),
    # —— PC 接管通道（v1.0.298）：deepseek-harness headless 委托，实现见 agent_tools/dsh_bridge.py ——
    "dsh_status":               lambda a, c: dsh_bridge.dsh_check(c),
    "dsh_prepare":              lambda a, c: dsh_bridge.dsh_prepare(c),
    "pc_takeover":              lambda a, c: dsh_bridge.dsh_run(c, a.get("task"), a.get("cwd"), a.get("timeout")),
    # —— 办公文件生成三件套（v1.0.313）：Excel/Word/PPT 可验收交付物，实现见 agent_tools/office_gen.py ——
    "create_excel":             lambda a, c: office_gen.create_excel(a, c),
    "create_docx":              lambda a, c: office_gen.create_docx(a, c),
    "create_pptx":              lambda a, c: office_gen.create_pptx(a, c),
    # —— 企微主窗口发消息（v1.0.468 ④）：OS 级搜索+发送，实现见 wecom_bridge/win_send.py ——
    "send_wecom_message":       lambda a, c: _send_wecom_message_safe(a),
    # v1.0.470（P1-10）：wecom_run_js 加危险 API 拦截——带登录态的 OA 页面里任意 JS
    # （fetch 外传数据/读 cookie/改表单）完全绕过审批管道，现按 pc_run 同款 fail-closed
    "wecom_run_js":             lambda a, c: _run_wecom_js_safe(a, c),
}

# v1.0.470（P0-2）加载期交叉校验：registry.TOOLS 的 schema 名与执行器 key 必须一一对应。
# 此前 send_wecom_message 在 TOOLS 定义两份、_TOOL_HANDLERS 字面量写两遍（后者静默覆盖
# 前者），schema 与执行器错位导致首轮调用必失败。dict 字面量重复 key 不报错，只能在这里拦。
_tool_schema_names = [t.get("function", {}).get("name") for t in TOOLS if t.get("function")]
_dup_names = sorted({n for n in _tool_schema_names if _tool_schema_names.count(n) > 1})
if _dup_names:
    raise RuntimeError(f"registry.TOOLS 存在重复工具名（后者覆盖前者）：{_dup_names}")
# 这两个工具在 agent/loop.py 有专门分支（挂起-恢复弹窗交互），不走 _TOOL_HANDLERS
_LOOP_SPECIAL_TOOLS = {"request_skill", "import_skill_source"}
_missing_handlers = sorted(set(_tool_schema_names) - set(_TOOL_HANDLERS.keys()) - _LOOP_SPECIAL_TOOLS)
if _missing_handlers:
    raise RuntimeError(f"registry.TOOLS 里有 schema 但 _TOOL_HANDLERS 缺执行器：{_missing_handlers}")


def _run_wecom_js_safe(a, c):
    """v1.0.470（P1-10）：wecom_run_js 危险 API 拦截包装。
    带登录态的 OA 页面里执行任意 JS 可 fetch 外传数据/读 cookie/篡改表单——原实现
    完全绕过审批管道。命中危险模式转 __approval_needed__ 人工审批（pc_run 同款
    fail-closed 管道）；安全表达式（视频拖进度/读播放状态/查元素存在）直接放行。
    v1.0.555（P0-5 修复）：原 12 条黑名单对图灵完备的 JS 本质失效——字符串拼接
    （"fe"+"tch("）、window["Fun"+"ction"]、document.location=、parent.postMessage、
    document.write、navigator.clipboard.readText、atob+拼接链均不在列，漏一个模式
    就是一条无审批的带登录态数据外传通道（页面内容可经提示注入诱导模型调用）。
    现补全三类缺口：
      ① 外传/持久化通道：postMessage/clipboard/atob/btoa/import()/Worker/SRPDOC；
      ② 导航与跨帧：location 赋值/assign/replace、history.pushState/replaceState、
         opener/parent/top./frames 跨帧访问；
      ③ 混淆启发式：字符串字面量拼接（"fe"+"tch("）、全局对象括号取值
         （window["Fun"+"ction"]）、字符串参数定时器（setTimeout("..."））。
    未采用"纯白名单只放行窄只读模式"的原因：跑课引擎文档化纪律依赖页面本地
    DOM/媒体操作（静音三件套 mega 表达式含 setInterval/defineProperty/play/seek，
    及 history.back() 退出考试），纯白名单会把整个自动化跑课流程全部推去人工
    审批，功能实质下线。黑名单+启发式在保住合法用例的同时关闭已证实的外传通道。"""
    expr = str(a.get("expression") or "")
    low = expr.lower()
    import re as _re
    _danger = [
        # —— v1.0.470 原有 12 条 ——
        (r"\bfetch\s*\(", "fetch 网络请求"),
        (r"\bxmlhttprequest\b", "XMLHttpRequest 网络请求"),
        (r"\bnavigator\.sendbeacon\b", "sendBeacon 数据上报"),
        (r"\bwebsocket\b", "WebSocket 连接"),
        (r"\bdocument\.cookie\b", "读取 cookie"),
        (r"\blocalstorage\b|\bsessionstorage\b|\bindexeddb\b", "读写本地存储"),
        (r"\beval\s*\(|\bnew\s+function\s*\(", "动态代码执行"),
        (r"\.submit\s*\(\s*\)", "表单提交"),
        (r"\bsrc\s*=\s*['\"]?https?:", "注入外链资源"),
        (r"\bwindow\.open\s*\(|(?<![a-z_.])open\s*\(\s*['\"]https?:", "window.open 弹新窗口"),
        (r"(?<![a-z_.])alert\s*\(|(?<![a-z_.])confirm\s*\(|(?<![a-z_.])prompt\s*\(", "模态弹窗打断用户"),
        # —— v1.0.555 外传/持久化通道 ——
        (r"\bpostmessage\b", "postMessage 跨帧/跨窗数据发送"),
        (r"\bclipboard\b", "剪贴板读写"),
        (r"\batob\s*\(|\bbtoa\s*\(", "base64 编解码（外传编码链常用）"),
        (r"\bimport\s*\(|\brequire\s*\(", "动态模块加载"),
        (r"\bnew\s+worker\b|\bsharedworker\b|\bserviceworker\b|\bworker\s*\(", "Worker 通道"),
        (r"\.srcdoc\s*=", "iframe srcdoc 注入"),
        (r"\bdocument\.write(?:ln)?\s*\(", "document.write 文档重写"),
        (r"\bnavigator\.(userAgent|platform|language|connection|hardwareConcurrency)\b", "读取设备指纹"),
        (r"\bwindow\.name\b", "window.name 跨页传递"),
        # —— v1.0.555 导航与跨帧 ——
        (r"\blocation\s*(?:\.\s*(?:href|assign|replace)\s*=|\.\s*href\s*=|\[)", "location 导航劫持/赋值"),
        (r"\blocation\s*=\s*", "location 整体赋值"),
        (r"\bhistory\.(pushstate|replacestate)\b", "history 状态篡改"),
        (r"\bopener\b|\bparent\b|\btop\s*\.|\bframes\b", "跨帧访问（可向父帧外传）"),
        # —— v1.0.555 混淆启发式（黑名单正则的固有盲区）——
        (r"['\"]\s*\+\s*['\"]", "字符串字面量拼接（疑似绕过黑名单，如 \"fe\"+\"tch(\"）"),
        (r"\b(?:window|document|navigator|globalThis|self)\s*\[", "全局对象括号取值（如 window[\"Fun\"+\"ction\"]）"),
        (r"\b(?:setTimeout|setInterval)\s*\(\s*['\"]", "字符串参数定时器（等价 eval）"),
    ]
    for pat, why in _danger:
        if _re.search(pat, low):
            from security_ops import audit, args_hash
            audit("wecom_run_js", expr[:200], False, f"命中危险 API：{why}")
            return {"__approval_needed__": True, "kind": "js", "command": expr[:300],
                    "hash": args_hash("wecom_run_js", expr),
                    "reason": f"页面 JS 命中危险 API（{why}）——需人工确认。若为误报，可改用等价的只读表达式。"}
    return wb.run_js(_pick_flex(a, "pick"), expr, _cdp_port(c))


def _send_wecom_message_safe(a):
    """v1.0.468：包装 win_send——target/text 校验 + 延迟导入（OS 级模块仅在
    真正调用时加载，避免影响主链路导入）。"""
    if not a.get("target") or not a.get("text"):
        return {"ok": False, "error": "send_wecom_message 需要 target（联系人/群名称）与 text（消息内容）"}
    try:
        from wecom_bridge.win_send import send_wecom_message
        return send_wecom_message(a.get("target"), a.get("text"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"发送异常：{type(e).__name__}: {str(e)[:100]}"}


def run_tool(name, args, cfg):
    """按注册表路由到对应工具执行函数。未知工具返回结构化错误。

    统一兜底：handler 返回值必须是 dict，否则包成 {"ok": True, "result": <原值>}。
    （list_wecom_windows 等工具原生返回 list，若直接上抛，agent_loop 对 out 调用
    .get() 会抛 AttributeError 导致 SSE 断流、前端报 network error。）

    Hermes 风格：执行前先做参数 schema 轻量校验（必填/枚举/类型），失败直接返回
    结构化错误不执行——让模型修正参数后重试，避免无效工具调用空耗轮次。
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"unknown tool {name}"}
    verr = _validate_tool_args(name, args)
    if verr:
        log_event(f"[tool-validate] {name} 参数校验失败：{_mask_args(args)}")
        return verr
    out = handler(args or {}, cfg)
    if isinstance(out, dict):
        return out
    return {"ok": True, "result": out}

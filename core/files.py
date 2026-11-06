# -*- coding: utf-8 -*-
"""core.files — 文件内容解析（pdf/docx/xls/ppt/图片 OCR/HTML）（自 core_utils 拆分 v1.0.402）
"""
# -*- coding: utf-8 -*-
import json
import os
from core.ocr import ocr_ready_or_install, _ocr_image_arr, _ocr_image_file  # noqa
from core.logging import READ_TRUNCATE_BYTES  # noqa
from core.logging import log_event  # noqa

# ---------------- v1.0.373（用户需求4）老版本 Office 格式优先转换后再解析 ----------------
# .doc/.xls/.ppt 是二进制 OLE 老格式，python 库无法直接解析——检测到时优先用本机
# Office/WPS 的 COM 接口（经 PowerShell 调用）转换为对应新格式（.docx/.xlsx/.pptx）
# 再走既有解析管道；转换失败（未装 Office/WPS 或超时）降级为明确提示。
# v1.0.647：原 core/office.py（73 行）只有一个消费者（本文件），整体并入此处。
_LEGACY_OFFICE_MAP = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}
_PS_FMT = {"doc": 16, "docx": 16, "xls": 51, "xlsx": 51, "ppt": 24, "pptx": 24}
_PS_APP = {".doc": ["Word.Application", "KWPS.Application"],
           ".xls": ["Excel.Application", "KET.Application"],
           ".ppt": ["PowerPoint.Application", "KWPP.Application"]}


def find_col_in_header(header, priority_groups):
    """表头列定位（v1.0.510 P2-13：原 agent_tools/executors.py 与 services/batch_flow.py
    两份一字不差的内部函数收敛于此）。按优先级分组匹配——同组内任一关键词是表头
    子串即命中，返回列下标；全组未命中返回 None。
    v1.0.221 语义保留："设备状态"优先于"状态"，否则"财务登记状态"含"状态"
    抢先命中导致列错位统计全 0（实测）。"""
    for group in priority_groups:
        for i, h in enumerate(header):
            if any(k in h for k in group):
                return i
    return None


def read_json(path, default=None):
    """读 JSON 文件，失败返回 default（v1.0.654 A8：收敛 9 处
    `json.load(open(p, encoding="utf-8"))` 句柄样板——裸 open 依赖 GC 关句柄，
    且每处各写一遍 try/except 返回默认值）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def scan_table_header(rows, status_keys=("设备状态", "在用状态", "状态"),
                      type_keys=("设备类型", "类型", "类别"), max_scan=8):
    """设备清单表头定位：扫描前 max_scan 行找**同时**含状态列与类型列的行作为表头
    （表头可能不在首行——前几行是"表格填写人员/截止日期"信息）；找不到则回落第 0 行。
    v1.0.654 A7：原 executors.py 与 batch_flow.py 两份 12 行逐字相同的扫描块收敛于此
    （v1.0.510 只收敛了 find_col，表头扫描本身漏收）。"""
    header = None
    header_row_idx = None
    for ri in range(min(max_scan, len(rows))):
        cand = [str(c or "").strip() for c in rows[ri]]
        if any(any(k in h for k in status_keys) for h in cand) \
           and any(any(k in h for k in type_keys) for h in cand):
            header = cand
            header_row_idx = ri
            break
    if header is None:
        header = [str(c or "").strip() for c in rows[0]]
        header_row_idx = 0
    return header, header_row_idx


# v1.0.648：以下 _parse_* 是从 _parse_file_content（原 247 行一长串 if/elif）里
# 按格式抽出来的。契约统一：入参路径，返回 (kind, content, note)，异常向上抛
# （由分派函数统一转成 (None, None, "（解析失败：…）")）。逐个抽而不是一次重写，
# 是为了让每个分支的边界（60 页 / 800 段 / 500 行 / 前 8 页 OCR）原样保留。


def _parse_pdf(p):
    try:
        import pypdfium2 as _pdfium
        # v1.0.361 审查修复（P6）：with 关闭文档句柄（原每解析一个 PDF 泄漏一个，
        # Windows 上该文件随后无法删除/替换）
        with _pdfium.PdfDocument(p) as _doc:
            _npages = len(_doc)
            pages = []
            for i in range(min(_npages, 60)):
                try:
                    t = _doc[i].get_textpage().get_text_bounded() or ""
                except Exception as e:  # noqa: BLE001
                    log_event(f"[read-file] PDF 第 {i+1} 页提取文本失败：{e}")
                    t = ""
                if t.strip():
                    pages.append(f"--- 第{i+1}页 ---\n{t}")
            note = ""
            if _npages > 60:
                note = f"（PDF 共 {_npages} 页，仅解析前 60 页）"
            if not pages:
                # 扫描件无文本层：自动 OCR 前 8 页
                import numpy as np
                ocr_pages = min(_npages, 8)
                for i in range(ocr_pages):
                    try:
                        img = _doc[i].render(scale=2.0).to_pil()
                        t = _ocr_image_arr(np.asarray(img))
                        if t.strip():
                            pages.append(f"--- 第{i+1}页(OCR) ---\n{t}")
                    except Exception as e:  # noqa: BLE001
                        log_event(f"[read-file] PDF 第 {i+1} 页 OCR 失败：{e}")
                if pages:
                    note = f"（扫描件，已对前 {ocr_pages} 页自动 OCR）"
                else:
                    note += "（扫描件且 OCR 未识别到文本）"
            return "pdf", "\n".join(pages), note
    except Exception as e:  # noqa: BLE001
        log_event(f"[read-file] pypdfium2 解析失败回退 pypdf：{e}")
    from pypdf import PdfReader
    # v1.0.361（P6）：pypdf 回退分支同样及时关闭
    # v1.0.470（P2-2）：finally 关 reader 流——原实现 PdfReader 惰性读
    # 句柄存活到 GC，Windows 上解析完的 PDF 随后无法删除/替换（P6 同症状）
    reader = PdfReader(p)
    try:
        pages = []
        for i, pg in enumerate(reader.pages[:60]):
            try:
                t = pg.extract_text() or ""
            except Exception as e2:  # noqa: BLE001
                log_event(f"[read-file] PDF 第 {i+1} 页提取文本失败：{e2}")
                t = ""
            if t.strip():
                pages.append(f"--- 第{i+1}页 ---\n{t}")
        content = "\n".join(pages)
        note = ""
        if len(reader.pages) > 60:
            note = f"（PDF 共 {len(reader.pages)} 页，仅解析前 60 页）"
        if not content.strip():
            note += "（未提取到文本，可能是扫描件图片，需 OCR）"
        return "pdf", content, note
    finally:
        try:
            reader.stream.close()
        except Exception:  # noqa: BLE001
            pass


def _parse_doc(p):
    # 老式 OLE Word 文档：基于 olefile 解析 WordDocument 流 + FIB 提取正文
    import olefile, struct
    ole = olefile.OleFileIO(p)
    try:
        wd = ole.openstream('WordDocument').read()
        # FIB: flags(0x0A) 的 fExtChar 位标记编码，fcMin(0x18)/fcMac(0x1C) 为正文区间
        flags = struct.unpack('<H', wd[0x0A:0x0C])[0]
        fc_min = struct.unpack('<I', wd[0x18:0x1C])[0]
        fc_max = struct.unpack('<I', wd[0x1C:0x20])[0]
        is_unicode = bool(flags & 0x0002)  # fExtChar：1=UTF-16，0=ANSI
        if 0 <= fc_min < fc_max <= len(wd):
            raw = wd[fc_min:fc_max]
            # 部分 WPS 文档 fExtChar 位不可靠，用启发式双解码择优
            u16 = raw.decode('utf-16-le', errors='replace')
            try:
                ansi = raw.decode('gbk')
            except Exception:  # noqa: BLE001
                ansi = raw.decode('cp1252', errors='replace')

            def _readable_ratio(s):
                if not s:
                    return 0.0
                good = sum(1 for ch in s if ch.isprintable() or ch in '\n\r\t')
                return good / len(s)

            if is_unicode or _readable_ratio(u16) >= max(_readable_ratio(ansi), 0.6):
                txt = u16
            else:
                txt = ansi
        else:
            # 正文落在 Table 流（复杂版式），本实现不覆盖
            txt = ""
        # 清洗控制字符：\x07 单元格/字段分隔符，\x0b 分页符
        txt = (txt.replace('\x07', '\n').replace('\x0b', '\n')
                  .replace('\x00', ' '))
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        content = "\n".join(lines)
        note = ""
        if not content.strip():
            note = "（未提取到文本，可能是图片型文档或复杂版式，需 OCR）"
        return "doc", content, note
    finally:
        ole.close()


def _parse_xls(p):
    # 老式 Excel 97-2003 表格
    import xlrd
    wb = xlrd.open_workbook(p)
    parts = []
    for ws in wb.sheets()[:10]:
        parts.append(f"=== 工作表：{ws.name} ===")
        for r_i in range(min(ws.nrows, 500)):
            vals = [str(ws.cell_value(r_i, c)).strip()
                    for c in range(min(ws.ncols, 20))]
            vals = [v for v in vals if v]
            if vals:
                parts.append(" | ".join(vals))
    content = "\n".join(parts)
    note = ""
    if len(wb.sheets()) > 10:
        note = f"（共 {len(wb.sheets())} 个工作表，仅解析前 10 个）"
    if not content.strip():
        note += "（未提取到内容）"
    try:
        wb.release_resources()   # v1.0.470（P2-2）：释放 xlrd 文件句柄
    except Exception:  # noqa: BLE001
        pass
    return "xls", content, note


def _parse_rtf(p):
    # 富文本格式：striprtf 清洗为纯文本
    from striprtf.striprtf import rtf_to_text
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        content = rtf_to_text(f.read())
    content = "\n".join(ln.strip() for ln in content.splitlines() if ln.strip())
    note = ""
    if not content.strip():
        note += "（未提取到文本）"
    return "rtf", content, note


def _parse_image(p):
    # 图片：直接 OCR（中英）
    txt = _ocr_image_file(p)
    note = ""
    if not txt.strip():
        note = "（OCR 未识别到文本）"
    return "image-ocr", txt, note


def _parse_docx(p):
    import docx
    d = docx.Document(p)
    parts = []
    for para in d.paragraphs[:800]:
        if para.text.strip():
            parts.append(para.text)
    for tbl in d.tables[:50]:
        for row in tbl.rows[:200]:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells[:20]]
            if any(cells):
                parts.append(" | ".join(cells))
    content = "\n".join(parts)
    note = ""
    if len(d.paragraphs) > 800 or len(d.tables) > 50:
        note = "（文档较大，仅提取部分段落/表格）"
    if not content.strip():
        note += "（未提取到文本）"
    return "docx", content, note


def _parse_xlsx(p):
    import openpyxl
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets[:10]:
        parts.append(f"=== 工作表：{ws.title} ===")
        for r_i, row in enumerate(ws.iter_rows(values_only=True)):
            if r_i >= 500:
                parts.append("…(行数过多，仅显示前 500 行)")
                break
            vals = [str(v) for v in row if v is not None]
            if vals:
                parts.append(" | ".join(vals))
    content = "\n".join(parts)
    note = ""
    if len(wb.worksheets) > 10:
        note = f"（共 {len(wb.worksheets)} 个工作表，仅解析前 10 个）"
    if not content.strip():
        note += "（未提取到内容）"
    # v1.0.470（P2-2）：close 挪进 finally——原只在成功路径关闭，异常路径泄漏句柄
    try:
        wb.close()
    except Exception:  # noqa: BLE001
        pass
    return "xlsx", content, note


def _parse_pptx(p):
    from pptx import Presentation
    prs = Presentation(p)
    parts = []
    for i, slide in enumerate(prs.slides[:60]):
        sparts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                sparts.append(shape.text)
            if shape.has_table:
                for row in shape.table.rows:
                    sparts.append(" | ".join(c.text.strip() for c in row.cells))
        if sparts:
            parts.append(f"--- 幻灯片{i+1} ---\n" + "\n".join(sparts))
    content = "\n".join(parts)
    note = ""
    if len(prs.slides) > 60:
        note = f"（共 {len(prs.slides)} 页，仅解析前 60 页）"
    if not content.strip():
        note += "（未提取到文本）"
    return "pptx", content, note


def _parse_textish(p, size, ext):
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(READ_TRUNCATE_BYTES)
    note = ""
    if size > READ_TRUNCATE_BYTES:
        note = f"（文件较大，原文 {size} 字节，仅显示前 {READ_TRUNCATE_BYTES} 字节）"
    return ext.lstrip("."), content, note


def _parse_unknown(p):
    # 未知类型：尝试按文本读，失败则报告为二进制
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(READ_TRUNCATE_BYTES)
    note = ""
    if not content.strip() or "\x00" in content[:2000]:
        content = ""
        note = "（二进制或无法解析的文件类型，仅报告文件名与大小）"
    return "unknown", content, note


_PDF_EXT = (".pdf",)
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
_TEXT_EXT = (".txt", ".csv", ".md", ".json", ".py", ".js", ".html", ".xml", ".log",
             ".ini", ".yaml", ".yml", ".sql", ".sh", ".bat", ".ts", ".tsx", ".css")


def _parse_file_content(p, size):
    """按扩展名解析各类文件为文本：txt/csv/md/json 等直接读；pdf/doc/docx/xlsx/xls/pptx/rtf/图片 提取文本。

    返回 (kind, content, note)。解析失败或二进制无文本时返回原始读取结果。
    v1.0.373（用户需求4）：.doc/.xls/.ppt 老格式先经本机 Office/WPS COM 转换为
    .docx/.xlsx/.pptx 再走新格式解析管道；转换失败降级为明确提示。
    v1.0.648：各格式的解析体拆成 _parse_* helper，本函数只留分派与老格式前置处理。
    """
    ext = os.path.splitext(p)[1].lower()
    # —— 老格式优先转换（.doc/.xls/.ppt → .docx/.xlsx/.pptx）——
    if ext in _LEGACY_OFFICE_MAP:
        converted = convert_legacy_office(p)
        if converted:
            new_ext = os.path.splitext(converted)[1].lower()
            kind, content, note = _parse_file_content(converted, size)
            return kind, content, (f"（检测到老版本 {ext} 格式，已自动转换为 {new_ext} 后解析）" + (note or ""))
        return "legacy-office", "", (f"检测到老版本 {ext} 格式，本机未安装 Office/WPS 无法自动转换。"
                                     f"请用 Office/WPS 打开后另存为 {_LEGACY_OFFICE_MAP[ext]} 再上传。")
    try:
        if ext in _PDF_EXT:
            return _parse_pdf(p)
        if ext == ".doc":
            return _parse_doc(p)
        if ext == ".xls":
            return _parse_xls(p)
        if ext == ".rtf":
            return _parse_rtf(p)
        if ext in _IMAGE_EXT:
            return _parse_image(p)
        if ext in (".docx", ".docm"):
            return _parse_docx(p)
        if ext in (".xlsx", ".xlsm"):
            return _parse_xlsx(p)
        if ext == ".pptx":
            return _parse_pptx(p)
        if ext in _TEXT_EXT:
            return _parse_textish(p, size, ext)
        return _parse_unknown(p)
    except Exception as e:  # noqa: BLE001
        return None, None, f"（解析失败：{e}）"
PREVIEW_LIMIT = 5 * 1024 * 1024   # 预览上限：超大文件只回前 5MB，避免整份读进内存拖垮后端


def read_preview(path, limit=PREVIEW_LIMIT, truncated_hint=None):
    """带截断的文本预览，返回 (content, size, truncated)。
    v1.0.614 收敛：这段"取大小→比上限→截断读→追加提示"原先在 routes/knowledge.py 与
    routes/workspace.py 各写一遍、阈值也各写一遍（改一处漏一处，两处提示文案已经漂了）。
    truncated_hint 是模板，可用 {size} / {limit} 占位。"""
    size = os.path.getsize(path)
    truncated = size > limit
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(limit if truncated else size)
    if truncated:
        hint = truncated_hint or "\n…（文件过大 {size} 字节，仅预览前 {limit} 字节）"
        content += hint.format(size=size, limit=limit)
    return content, size, truncated


def convert_legacy_office(src):
    """老格式 → 新格式。成功返回新文件路径；失败返回 None（调用方降级提示）。

    转换产物与源文件同目录同名（.docx 等），已存在直接复用。
    v1.0.647：随 core/office.py 整体并入本文件——该文件只有本文件一个消费者。
    """
    try:
        ext = os.path.splitext(src)[1].lower()
        new_ext = _LEGACY_OFFICE_MAP.get(ext)
        if not new_ext:
            return None
        dst = os.path.splitext(src)[0] + new_ext
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        # 依序尝试 MS Office / WPS 的 COM 组件
        last_err = ""
        for app in _PS_APP.get(ext, []):
            prog_id = app.split(".")[0]           # Word/Excel/PowerPoint/KWPS...
            fmt = _PS_FMT.get(new_ext.lstrip("."))
            # SaveAs2/SaveAs 的格式码：Word wdFormatXMLDocument=16，Excel 51，PPT 24
            open_method = {"Word": "Documents.Open", "Excel": "Workbooks.Open",
                           "KWPS": "Documents.Open", "PowerPoint": "Presentations.Open",
                           "KWPP": "Presentations.Open"}.get(prog_id)
            ps = (
                "$ErrorActionPreference='Stop';"
                f"$app = New-Object -ComObject {app};"
                "$app.Visible = $false;"
                "try {"
                f"  $src = '{src.replace(chr(39), chr(39)*2)}';"
                f"  $dst = '{dst.replace(chr(39), chr(39)*2)}';"
                f"  $doc = $app.{open_method}($src);"
                f"  $doc.SaveAs($dst, {fmt});"
                "  $doc.Close($false);"
                "} finally { $app.Quit(); }"
                "if (Test-Path $dst) { Write-Output 'OK' } else { exit 1 }"
            )
            try:
                import subprocess
                r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                                   capture_output=True, timeout=90,
                                   creationflags=0x08000000 if os.name == "nt" else 0)
                if r.returncode == 0 and b"OK" in (r.stdout or b"") and os.path.isfile(dst):
                    log_event(f"[office] 老格式转换成功：{os.path.basename(src)} → {new_ext}（{app}）")
                    return dst
                last_err = (r.stderr or b"").decode("utf-8", "replace")[:200]
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
            try:
                os.remove(dst)   # 半成品清理
            except OSError:
                pass
        log_event(f"[office] 老格式转换失败（{ext}）：{last_err}——本机可能未安装 Office/WPS")
        return None
    except Exception as e:  # noqa: BLE001
        log_event(f"[office] 老格式转换异常：{e}")
        return None

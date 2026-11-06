# -*- coding: utf-8 -*-
"""agent_tools.office_gen — 办公文件生成三件套（v1.0.313，对齐 WorkBuddy「本地文件执行权」）
=================================================================
create_excel / create_docx / create_pptx：agent 直接产出可验收交付物（不只是建议）。
统一落「产物库/产出/」，写台账（append_ledger），依赖 openpyxl/python-docx/python-pptx
（构建 venv 与 exe hiddenimports 均已包含）。
"""
import os

from core_utils import ws_sub, append_ledger, log_event

_OUT_SUB = "产出"


def _out_path(filename):
    safe = os.path.basename(str(filename or "").strip()) or "未命名"
    root = ws_sub(_OUT_SUB)
    return os.path.join(root, safe), root, safe


def _ok(path, extra=None):
    out = {"ok": True, "path": path, "size": os.path.getsize(path)}
    if extra:
        out.update(extra)
    append_ledger(_OUT_SUB, os.path.basename(path), source="office_gen")
    return out


def _err(msg):
    return {"ok": False, "error": msg}


def create_excel(args, cfg=None):
    """生成 .xlsx。sheets=[{name, rows:[[...]], freeze_header(True)}]；单元格可直接写公式字符串。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except Exception as e:  # noqa: BLE001
        return _err(f"openpyxl 不可用：{e}")
    sheets = args.get("sheets")
    path, root, safe = _out_path(args.get("filename"))
    if not safe.lower().endswith(".xlsx"):
        safe += ".xlsx"
        path = os.path.join(root, safe)
    if not isinstance(sheets, list) or not sheets:
        return _err("缺少必填参数：sheets（数组，每项 {name, rows}）")
    wb = Workbook()
    wb.remove(wb.active)
    total_rows = 0
    for si, sh in enumerate(sheets):
        if not isinstance(sh, dict) or not isinstance(sh.get("rows"), list) or not sh["rows"]:
            return _err(f"sheets[{si}] 缺少 rows（二维数组）")
        name = str(sh.get("name") or f"Sheet{si + 1}")[:31]
        ws = wb.create_sheet(title=name)
        for ri, row in enumerate(sh["rows"], 1):
            for ci, val in enumerate(row, 1):
                ws.cell(row=ri, column=ci, value=val)
            total_rows += 1
        if sh.get("freeze_header", True) and len(sh["rows"]) > 1:
            for ci in range(1, len(sh["rows"][0]) + 1):
                cell = ws.cell(row=1, column=ci)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="2563EB")
            ws.freeze_panes = "A2"
        # 列宽自适应（按前 50 行内容估宽，8~48 封顶）
        for ci in range(1, min((max(len(r) for r in sh["rows"]) if sh["rows"] else 1), 60) + 1):
            width = 8
            for row in sh["rows"][:50]:
                v = row[ci - 1] if ci - 1 < len(row) else ""
                width = max(width, min(len(str(v)) * 1.6 + 2, 48))
            ws.column_dimensions[get_column_letter(ci)].width = width
    try:
        wb.save(path)
    except Exception as e:  # noqa: BLE001
        return _err(f"保存失败：{e}")
    log_event(f"[office] create_excel -> {path}（{len(sheets)} 表 {total_rows} 行）")
    return _ok(path, {"sheets": len(sheets), "rows": total_rows})


def create_docx(args, cfg=None):
    """生成 .docx。blocks=[{type:"h1|h2|h3|p|bullet|number|table", text?, rows?}]。"""
    try:
        import docx
        from docx.shared import Pt
    except Exception as e:  # noqa: BLE001
        return _err(f"python-docx 不可用：{e}")
    blocks = args.get("blocks")
    path, root, safe = _out_path(args.get("filename"))
    if not safe.lower().endswith(".docx"):
        safe += ".docx"
        path = os.path.join(root, safe)
    if not isinstance(blocks, list) or not blocks:
        return _err("缺少必填参数：blocks（数组，每项 {type, text} 或 {type:'table', rows}）")
    doc = docx.Document()
    for bi, b in enumerate(blocks):
        if not isinstance(b, dict):
            return _err(f"blocks[{bi}] 应为对象")
        t = str(b.get("type", "p"))
        text = str(b.get("text", ""))
        if t in ("h1", "h2", "h3"):
            doc.add_heading(text, level=int(t[1]))
        elif t == "bullet":
            doc.add_paragraph(text, style="List Bullet")
        elif t == "number":
            doc.add_paragraph(text, style="List Number")
        elif t == "table":
            rows = b.get("rows")
            if not isinstance(rows, list) or len(rows) < 1:
                return _err(f"blocks[{bi}] table 需要 rows（二维数组，首行为表头）")
            cols = max(len(r) for r in rows)
            tb = doc.add_table(rows=len(rows), cols=cols)
            tb.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci in range(cols):
                    cell = tb.cell(ri, ci)
                    cell.text = str(row[ci]) if ci < len(row) else ""
                    if ri == 0:
                        for run in cell.paragraphs[0].runs or [cell.paragraphs[0].add_run("")]:
                            run.font.bold = True
        else:  # p
            para = doc.add_paragraph(text)
            if text.startswith("标题："):
                pass
            for run in para.runs:
                run.font.size = Pt(11)
    try:
        doc.save(path)
    except Exception as e:  # noqa: BLE001
        return _err(f"保存失败：{e}")
    log_event(f"[office] create_docx -> {path}（{len(blocks)} 块）")
    return _ok(path, {"blocks": len(blocks)})


def create_pptx(args, cfg=None):
    """生成 .pptx。slides=[{title, bullets:[...], notes?}]，默认「标题+内容」版式；title_slide=True 时首页用标题版式。"""
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except Exception as e:  # noqa: BLE001
        return _err(f"python-pptx 不可用：{e}")
    slides = args.get("slides")
    path, root, safe = _out_path(args.get("filename"))
    if not safe.lower().endswith(".pptx"):
        safe += ".pptx"
        path = os.path.join(root, safe)
    if not isinstance(slides, list) or not slides:
        return _err("缺少必填参数：slides（数组，每项 {title, bullets}）")
    prs = Presentation()
    for si, s in enumerate(slides):
        if not isinstance(s, dict):
            return _err(f"slides[{si}] 应为对象")
        title = str(s.get("title", f"第 {si + 1} 页"))
        bullets = s.get("bullets") or []
        if si == 0 and args.get("title_slide", True):
            layout = prs.slide_layouts[0]           # 标题幻灯片
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = title
            if bullets:
                sub = slide.placeholders[1]
                sub.text = "\n".join(str(b) for b in bullets[:3])
        else:
            layout = prs.slide_layouts[1]           # 标题+内容
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = title
            body = slide.placeholders[1].text_frame
            body.word_wrap = True
            for i, b in enumerate(bullets[:14]):
                para = body.paragraphs[0] if i == 0 else body.add_paragraph()
                para.text = str(b)
                para.level = 0
                try:
                    para.font.size = Inches(0.22)
                except Exception:  # noqa: BLE001
                    pass
        if s.get("notes"):
            slide.notes_slide.notes_text_frame.text = str(s["notes"])[:1000]
    try:
        prs.save(path)
    except Exception as e:  # noqa: BLE001
        return _err(f"保存失败：{e}")
    log_event(f"[office] create_pptx -> {path}（{len(slides)} 页）")
    return _ok(path, {"slides": len(slides)})

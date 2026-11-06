# -*- coding: utf-8 -*-
"""tests/test_batch_flow_layers.py — 批审流水线三层化（v1.0.656）可测试性证明

三组证据：
1. FakeTransport 注入测试：AuditOrchestrator(transport=Fake, extractor=ReportExtractor())
   跑通一条批审路径——发现类操作全走注入实例，零 CDP/窗口调用、进度落盘走 tmp_path、
   结果条数正确；
2. ReportExtractor 纯函数测试：_num_from_field / _extract_excel_pcs 真断言；
3. patch 兼容回归：三层化前的旧打法 monkeypatch.setattr(bf, "_find_list_page", ...)
   等必须仍被运行时调用到（晚绑定红线）。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import batch_flow as bf  # noqa: E402


class FakeTransport:
    """按 FlowTransport 协议实现的假传输层（零 CDP/零窗口）。

    只实现批审发现路径需要的三个方法；协议其余方法**默认不允许被触达**——
    触达即记账并抛错，用作"零 CDP/窗口调用"的反向证明。"""

    def __init__(self):
        self.calls = []
        self.probe_calls = 0

    def find_list_page(self, port):
        self.calls.append("find_list_page")
        return "假待办列表", [{"title": "假待办列表"}]

    def load_all_report_links(self, list_pick, port, max_scrolls=10, match=None, label="月报链接"):
        self.calls.append("load_all_report_links")
        return [{"text": "乙营业部 2026-09 信息技术运维月报", "url": "http://fake/1"},
                {"text": "丙营业部 2026-09 信息技术运维月报", "url": "http://fake/2"},
                {"text": "己营业部 2026-09 信息技术运维月报", "url": "http://fake/3"}]

    def probe_cdp(self, port):
        self.calls.append("probe_cdp")
        self.probe_calls += 1
        return True

    def __getattr__(self, name):
        def _boom(*a, **k):
            self.calls.append(name)
            raise AssertionError("FakeTransport 不应被触达的方法：%s" % name)
        return _boom


class _BombWB:
    """任何 wb.* 触达都记账（批审发现路径应零触达 wecom_bridge）。"""

    def __init__(self, touches):
        self._touches = touches

    def __getattr__(self, name):
        self._touches.append(name)
        raise AssertionError("批审发现路径不应触达 wb.%s" % name)


def test_fake_transport_injection_runs_pipeline_with_zero_cdp(tmp_path, monkeypatch):
    """① FakeTransport 注入：跑通批审路径，零 CDP/窗口调用、进度落盘走 tmp_path。"""
    monkeypatch.setattr(bf, "workspace_root", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(bf, "log_event", lambda *a, **k: None)
    wb_touches = []
    monkeypatch.setattr(bf, "wb", _BombWB(wb_touches))

    # 建议形态之一：mock 掉 _audit_one（给假审计函数）——单张审计不进真实开页/读字段
    audited = []

    def fake_audit_one(port, list_pick, link, download_dir, idx, auto_approve=False):
        audited.append(link["text"])
        return {"index": idx, "title": link["text"], "url": link["url"],
                "verdict": "green" if idx % 2 == 0 else "pending",
                "issues": [] if idx % 2 == 0 else ["附件缺失"]}

    monkeypatch.setattr(bf, "_audit_one", fake_audit_one)

    # 进度写盘 spy：走真实 save_progress，落盘位置必须在 tmp_path
    saved = []
    real_save = bf._save_progress

    def spy_save(results, name="月报进度.json"):
        real_save(results, name)
        pp = bf._progress_path(name)
        saved.append({"name": name, "n": len(results), "path": pp,
                      "exists": os.path.isfile(pp)})

    monkeypatch.setattr(bf, "_save_progress", spy_save)

    transport = FakeTransport()
    orch = bf.AuditOrchestrator(transport=transport, extractor=bf.ReportExtractor())
    r = orch.audit_monthly_reports(port=9222)

    assert r.get("ok") is True, r
    assert r["total"] == 3 and r["done"] == 3, r
    assert r["green"] == 2 and r["pending"] == 1, r
    assert audited == ["乙营业部 2026-09 信息技术运维月报",
                       "丙营业部 2026-09 信息技术运维月报",
                       "己营业部 2026-09 信息技术运维月报"]
    # 零 CDP/窗口调用：wb 全程未被触达；FakeTransport 也只跑了三个允许的方法
    assert wb_touches == [], "批审发现路径触达了 wb：%s" % wb_touches
    assert set(transport.calls) <= {"find_list_page", "load_all_report_links", "probe_cdp"}, \
        transport.calls
    assert transport.probe_calls == 3, "每张探活一次且必须走注入 transport"
    # 进度逐张落盘且位置在 tmp_path（完整跑完后断点按契约清除，spy 在写入时刻取证）
    assert len(saved) == 3, saved
    for s in saved:
        assert str(tmp_path) in s["path"], s["path"]
        assert s["exists"] is True, s
        assert s["n"] in (1, 2, 3)
    # 报告/判定历史副作用也全部落在 tmp_path
    assert os.path.isfile(os.path.join(str(tmp_path), r["report_path"]))
    assert os.path.isfile(os.path.join(str(tmp_path), "台账", "月报判定.json"))
    # 完整跑完清断点（现存断点不该留下）
    assert not os.path.isfile(os.path.join(str(tmp_path), "台账", "月报进度.json"))


def test_injectable_transport_routes_discovery_but_default_stays_patchable(monkeypatch):
    """①补充：注入 transport 用实例；默认实例（transport=None）仍走模块级晚绑定。"""
    fake = FakeTransport()
    orch = bf.AuditOrchestrator(transport=fake, extractor=bf.ReportExtractor())
    pick, _pages = orch._r_find_list_page(9222)
    assert pick == "假待办列表"
    assert fake.calls == ["find_list_page"]

    monkeypatch.setattr(bf, "_find_list_page", lambda port: ("patched", []))
    orch_default = bf.AuditOrchestrator()
    assert orch_default._r_find_list_page(9222) == ("patched", [])


def test_report_extractor_num_from_field_pure():
    """② 纯函数：字段对象取第一个数字（snippets 为空/缺字段 → None）。"""
    ex = bf.ReportExtractor()
    assert ex.num_from_field({"snippets": ["本月应运维天数： 23 天"]}) == "23"
    assert ex.num_from_field({"snippets": ["2026 年 7 月"]}) == "2026"
    assert ex.num_from_field({"snippets": ["共 3.5 台"]}) == "3.5"
    assert ex.num_from_field({"snippets": []}) is None
    assert ex.num_from_field({}) is None
    # 模块级薄委托同语义（外部调用点/旧代码路径）
    assert bf._num_from_field({"snippets": ["合计 8"]}) == "8"


def test_report_extractor_extract_excel_pcs_counts_in_use_computers(tmp_path):
    """② 纯函数：xlsx 统计在用台式/笔记本/电脑数（表头不在首行也要认，v1.0.211）。"""
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "甲营业部信息设备清单202609.xlsx"
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["表格填写人员：张三", "截止日期：2026-09-30"])   # 信息行（非表头）
    ws.append(["设备名称", "设备状态", "设备类型"])
    ws.append(["PC1", "在用", "台式机"])
    ws.append(["PC2", "在用", "笔记本"])
    ws.append(["PC3", "维修中", "台式机"])     # 非在用不计
    ws.append(["PC4", "在用", "打印机"])       # 非电脑类型不计
    ws.append(["PC5", "在用", "台式电脑"])     # "电脑" 口径计
    book.save(str(path))

    count, header, hint = bf._extract_excel_pcs(str(tmp_path), "甲营业部信息设备清单202609.xlsx")
    assert hint is None
    assert count == 3
    assert "设备状态" in header and "设备类型" in header


def test_report_extractor_extract_excel_pcs_missing_file(tmp_path):
    """② 纯函数：文件不存在给出明确 error_hint（不抛异常）。"""
    count, header, hint = bf._extract_excel_pcs(str(tmp_path), "不存在的清单.xlsx")
    assert count is None and header is None
    assert "文件不存在" in hint


def test_legacy_module_patch_still_intercepts_pipeline(tmp_path, monkeypatch):
    """③ patch 兼容回归（test_audit_batch pipe 形态）：旧打法 setattr(bf, ...)
    打在 _find_list_page / _load_all_report_links / _audit_one / _save_progress 上，
    三层化后运行时仍必须命中——晚绑定红线。"""
    seen = []
    monkeypatch.setattr(bf, "workspace_root", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(bf, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(bf, "_find_list_page",
                        lambda port: (seen.append("list"), ("pick", []))[1])
    monkeypatch.setattr(bf, "_load_all_report_links",
                        lambda pick, port: (seen.append("links"),
                                            [{"text": "A 月报", "url": "http://x/1"}])[1])
    monkeypatch.setattr(bf, "_retry_degraded",
                        lambda fn, label, **kw: (True, None, None))
    monkeypatch.setattr(bf, "_audit_one",
                        lambda port, pick, lk, dl, idx, auto_approve=False:
                        (seen.append("audit"),
                         {"index": idx, "title": lk["text"], "url": lk["url"],
                          "verdict": "green", "issues": []})[1])
    monkeypatch.setattr(bf, "_save_progress",
                        lambda results, *a, **k: seen.append("save"))

    r = bf.audit_monthly_reports(port=9222)
    assert r.get("ok") is True and r["done"] == 1, r
    assert seen.count("list") == 1 and seen.count("links") == 1
    assert seen.count("audit") == 1 and seen.count("save") == 1, seen


def test_legacy_module_patch_still_intercepts_open_detail(monkeypatch):
    """③ patch 兼容回归（test_p02 形态）：_get_full_href 与 _open_detail_by_click
    的 patch 必须穿透 FlowTransport 默认实例（经模块级晚绑定）。"""
    monkeypatch.setattr(bf, "_get_full_href", lambda port, pick, text: None)
    monkeypatch.setattr(bf, "_open_detail_by_click",
                        lambda port, pick, link, issues: "FP_PATCHED")
    issues = []
    fp = bf._open_detail(9222, "pick", {"text": "某月报", "index": 0}, issues)
    assert fp == "FP_PATCHED"
    assert any("回退" in s for s in issues), issues


def test_legacy_module_patch_still_intercepts_pages(monkeypatch):
    """③ patch 兼容回归（test_batch_flow_wait 形态）：_pages 的 patch 穿透
    wait_new_page（FlowTransport 层内经 _m() 晚绑定）。"""
    monkeypatch.setattr(bf, "_pages",
                        lambda port: [{"title": "列表页"}, {"title": "新详情"}])
    assert bf._wait_new_page(9222, {"列表页"}, timeout=1.0) == "新详情"

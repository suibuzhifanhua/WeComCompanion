# -*- coding: utf-8 -*-
"""test_batch_flow_wait.py — 批审提速的轮询就绪检测（v1.0.523-5）
覆盖：_wait_new_page 立即就绪/延迟就绪/超时 None/过滤空标题。
"""
import time

import pytest

from services import batch_flow as bf


def test_wait_new_page_immediate(monkeypatch):
    titles = {"列表页"}
    pages = [{"title": "列表页"}, {"title": "乙月报详情"}]
    monkeypatch.setattr(bf, "_pages", lambda port: pages)
    t0 = time.time()
    r = bf._wait_new_page(9222, titles, timeout=3.0)
    assert r == "乙月报详情"
    assert time.time() - t0 < 1.0            # 立即返回，没白等


def test_wait_new_page_delayed(monkeypatch):
    titles = {"列表页"}
    state = {"n": 0}
    def _pages(port):
        state["n"] += 1
        if state["n"] < 3:                    # 前两次没有新页
            return [{"title": "列表页"}]
        return [{"title": "列表页"}, {"title": "新详情"}]
    monkeypatch.setattr(bf, "_pages", _pages)
    t0 = time.time()
    r = bf._wait_new_page(9222, titles, timeout=5.0, poll=0.1)
    assert r == "新详情"
    assert 0.1 <= time.time() - t0 < 2.0      # 轮询到就返回


def test_wait_new_page_timeout(monkeypatch):
    monkeypatch.setattr(bf, "_pages", lambda port: [{"title": "列表页"}])
    t0 = time.time()
    r = bf._wait_new_page(9222, {"列表页"}, timeout=0.8, poll=0.2)
    assert r is None
    assert 0.6 <= time.time() - t0 < 2.0      # 超时返回 None 不死等


def test_wait_new_page_filters_blank_titles(monkeypatch):
    """新页标题为空串（about:blank 过渡态）不算就绪，继续等真标题。"""
    state = {"n": 0}
    def _pages(port):
        state["n"] += 1
        if state["n"] < 3:
            return [{"title": "列表页"}, {"title": "   "}]   # 空白标题
        return [{"title": "列表页"}, {"title": "真详情"}]
    monkeypatch.setattr(bf, "_pages", _pages)
    r = bf._wait_new_page(9222, {"列表页"}, timeout=5.0, poll=0.1)
    assert r == "真详情"

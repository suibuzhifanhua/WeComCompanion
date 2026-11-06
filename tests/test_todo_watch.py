# -*- coding: utf-8 -*-
"""test_todo_watch.py — 待办变更轻提醒（v1.0.523-4）
覆盖：快照读写、差分（新增检测/首次建基线不提醒）、CDP 断链跳过、配置解析。
"""
import json

import pytest

from services import todo_watch as tw


@pytest.fixture(autouse=True)
def isolated_ws(tmp_path, monkeypatch):
    import core_utils
    monkeypatch.setattr(core_utils, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(tw, "workspace_root", lambda: str(tmp_path))
    # 快照落在 tmp：清历史
    yield tmp_path


class _FakeWB:
    """wecom_bridge 替身：可注入链接列表。"""
    links = []
    def list_links(self, pick=None, port=None):
        return {"title": pick, "links": self.links}


def test_first_baseline_no_notify(isolated_ws, monkeypatch):
    """首次建基线：条目全记入快照，但不提醒（存量不算新）。"""
    fake = _FakeWB()
    fake.links = [{"text": "乙月报 9 月"}, {"text": "己月报 9 月"}, {"text": "ok"}]
    monkeypatch.setattr("services.batch_flow._find_list_page",
                        lambda port: ("待办任务", []))
    import wecom_bridge as wb
    monkeypatch.setattr(wb, "list_links", lambda pick=None, port=None: fake.list_links(pick, port))
    r = tw.check_once(port=9222, notify=True)
    assert r["skipped"] is False
    assert r["new"] == []                     # 首次不提醒
    assert r["total"] == 2                    # "ok" 长度 <4 被过滤
    snap = tw._load_snapshot()
    assert sorted(snap["items"]) == ["乙月报 9 月", "己月报 9 月"]


def test_diff_detects_new_items(isolated_ws, monkeypatch):
    notified = []
    monkeypatch.setattr("services.batch_flow._find_list_page",
                        lambda port: ("待办任务", []))
    import wecom_bridge as wb
    from desktop import close_guard
    monkeypatch.setattr(close_guard, "tray_notify",
                        lambda t, m: notified.append((t, m)))

    fake = _FakeWB()
    fake.links = [{"text": "乙月报 9 月"}]
    monkeypatch.setattr(wb, "list_links",
                        lambda pick=None, port=None: fake.list_links(pick, port))
    tw.check_once(port=9222, notify=True)     # 基线

    fake.links = [{"text": "乙月报 9 月"}, {"text": "丙月报 10 月"}]
    r = tw.check_once(port=9222, notify=True)
    assert r["new"] == ["丙月报 10 月"]
    assert len(notified) == 1
    assert "丙月报" in notified[0][1]


def test_no_change_no_notify(isolated_ws, monkeypatch):
    notified = []
    monkeypatch.setattr("services.batch_flow._find_list_page",
                        lambda port: ("待办任务", []))
    import wecom_bridge as wb
    from desktop import close_guard
    monkeypatch.setattr(close_guard, "tray_notify", lambda t, m: notified.append(m))
    fake = _FakeWB()
    fake.links = [{"text": "乙月报 9 月"}]
    monkeypatch.setattr(wb, "list_links",
                        lambda pick=None, port=None: fake.list_links(pick, port))
    tw.check_once(port=9222, notify=True)
    tw.check_once(port=9222, notify=True)     # 无变化
    assert notified == []


def test_cdp_down_skips(isolated_ws, monkeypatch):
    """待办页不在 → skipped=True 且不写快照。"""
    monkeypatch.setattr("services.batch_flow._find_list_page",
                        lambda port: (None, []))
    r = tw.check_once(port=9222, notify=True)
    assert r["skipped"] is True
    assert tw._load_snapshot()["items"] == []


def test_cfg_parsing(tmp_path, monkeypatch):
    import core.config_store as cs
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"todo_watch": {"enabled": True, "interval_min": 5}}),
                        encoding="utf-8")
    monkeypatch.setattr(cs, "config_path", lambda: str(cfg_path))
    cs._CFG_CACHE["cfg"] = None; cs._CFG_CACHE["mtime"] = None
    assert tw._cfg(cs.load_config()) == {"enabled": True, "interval_min": 5}
    # 脏值兜底
    cfg_path.write_text(json.dumps({"todo_watch": {"enabled": 1, "interval_min": "x"}}),
                        encoding="utf-8")
    cs._CFG_CACHE["cfg"] = None; cs._CFG_CACHE["mtime"] = None
    c = tw._cfg(cs.load_config())
    assert c == {"enabled": True, "interval_min": 10}


def test_default_declared():
    from core.config_store import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["todo_watch"]["enabled"] is False
    assert DEFAULT_CONFIG["todo_watch"]["interval_min"] == 10

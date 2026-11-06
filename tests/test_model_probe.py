# -*- coding: utf-8 -*-
"""test_model_probe.py — 模型真连通三态检测（v1.0.528-2）
覆盖：probe 三态（ok/auth/down）、无 base_url、带 key、API 缓存与后台刷新。
真实网络不打（requests 全 mock）。
"""
import time

import pytest

from llm_client import probe_model_reachable


@pytest.fixture()
def fake_requests(monkeypatch):
    """core_utils._lazy_requests 的替身（probe 内函数级 import）。"""
    class _Resp:
        def __init__(self, code):
            self.status_code = code
    holder = {"code": 200, "calls": []}

    def _get(url, headers=None, timeout=None):
        holder["calls"].append({"url": url, "headers": headers or {}, "timeout": timeout})
        if isinstance(holder["code"], Exception):
            raise holder["code"]
        return _Resp(holder["code"])

    import core_utils
    monkeypatch.setattr(core_utils, "_lazy_requests",
                        type("M", (), {"get": staticmethod(_get)})())   # 实例（与 _LazyRequests 同形）
    return holder


def test_probe_ok(fake_requests):
    fake_requests["code"] = 200
    r = probe_model_reachable({"base_url": "http://127.0.0.1:11434/v1"})
    assert r["state"] == "ok"
    assert fake_requests["calls"][0]["url"] == "http://127.0.0.1:11434/v1/models"
    assert fake_requests["calls"][0]["timeout"] == (1.5, 2.5)


def test_probe_auth(fake_requests):
    fake_requests["code"] = 401
    r = probe_model_reachable({"base_url": "https://api.x.com/v1", "api_key": "sk-live-abc"})
    assert r["state"] == "auth"
    assert fake_requests["calls"][0]["headers"].get("Authorization") == "Bearer sk-live-abc"


def test_probe_down_connection_error(fake_requests):
    fake_requests["code"] = ConnectionError("refused")
    r = probe_model_reachable({"base_url": "http://10.255.255.1:1/v1"})
    assert r["state"] == "down"


def test_probe_no_base_url():
    r = probe_model_reachable({})
    assert r["state"] == "down" and "base_url" in r["detail"]


def test_probe_enc_key_not_sent(fake_requests):
    """仍为 enc: 密文的 key（异常态）绝不外发。"""
    fake_requests["code"] = 200
    probe_model_reachable({"base_url": "http://x/v1", "api_key": "enc:v1:AAAA"})
    assert "Authorization" not in fake_requests["calls"][0]["headers"]


def test_status_api_has_probe_field():
    import backend as backend_mod
    import routes.b_admin as ba
    ba._MODEL_PROBE["ts"] = 0.0
    ba._MODEL_PROBE["data"] = None
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        d = c.get("/api/status").get_json()
        assert "model_probe" in d           # 首查 None=检测中，不阻塞响应
        time.sleep(0.5)                      # 后台线程完成
        d2 = c.get("/api/status").get_json()
        assert d2["model_probe"] is not None and "state" in d2["model_probe"]

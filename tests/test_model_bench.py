# -*- coding: utf-8 -*-
"""test_model_bench — 模型综合评测（v1.0.319）。

用例：
1. 校验器单测：instr/toolcall/logic/chinese/extract 各过/挂分支给分
2. run_benchmark 全链（mock model_bench._ask）：逐项 bench_item + bench_done 总分与等级
3. SSE 端点：精确匹配 model_id，找不到 404
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_store  # noqa: E402


def test_scorers():
    from services.model_bench import _score_item, _speed_score, _extract_json
    assert _score_item("resp", "好的", 100)[0] == 100
    assert _score_item("resp", None, 100)[0] == 0
    # instr：严格 JSON
    s, d = _score_item("instr", '{"status": "ok", "n": 3}', 100)
    assert s == 100, d
    # instr：包在代码块里也能宽松提取
    s, _ = _score_item("instr", '```json\n{"status":"ok","n":3}\n```', 100)
    assert s >= 80
    # instr：JSON 错值
    s, _ = _score_item("instr", '{"status": "bad", "n": 9}', 100)
    assert s == 20
    # chinese：开头+长度
    assert _score_item("chinese", "备份是把重要数据复制到另一存储介质以防丢失的过程。", 100)[0] == 100
    s, _ = _score_item("chinese", "数据备份指复制数据。", 100)
    assert s < 100  # 没以「备份是」开头
    # logic
    assert _score_item("logic", "15", 100)[0] == 100
    assert _score_item("logic", "答案是 16", 100)[0] == 0
    # toolcall：函数名/参数逐项给分
    s, _ = _score_item("toolcall", '{"name":"send_message","arguments":{"to":"张三","content":"明天上午开会"}}', 100)
    assert s == 100
    s, _ = _score_item("toolcall", '{"name":"send_message","arguments":{"to":"李四","content":"hi"}}', 100)
    assert s == 40  # 只对函数名
    # extract
    assert _score_item("extract", "14:30", 100)[0] == 100
    assert _score_item("extract", "下午三点", 100)[0] == 0
    # 速度分线性
    assert _speed_score(1000) == 100 and _speed_score(20000) == 0
    assert 0 < _speed_score(5000) < 100
    assert _extract_json('废话 {"a":1} 尾巴') == {"a": 1}
    print("PASS 校验器：7 项过/挂分支 + 宽松 JSON 提取 + 速度线性")


def test_run_benchmark_full(monkeypatch=None):
    import services.model_bench as model_bench
    # mock _ask：按 prompt 关键字返回正确答案，模拟耗时
    def fake_ask(model, prompt, timeout):
        if "回复两个字" in prompt: return "好的", 1200
        if '"status"' in prompt: return '{"status": "ok", "n": 3}', 1800
        if "备份是" in prompt: return "备份是将数据复制到其他介质以防止丢失的操作。", 2000
        if "小明" in prompt: return "15", 2500
        if "send_message" in prompt: return '{"name":"send_message","arguments":{"to":"张三","content":"明天上午开会"}}', 3000
        if "会议时间是几点" in prompt: return "14:30", 2200
        return "ok", 1000
    orig = model_bench._ask
    model_bench._ask = fake_ask
    try:
        events = list(model_bench.run_benchmark({"name": "测试模型", "model": "test"}))
        kinds = [e["type"] for e in events]
        assert kinds.count("bench_item") == 7, kinds
        done = events[-1]
        assert done["type"] == "bench_done" and done["total"] == 100, done
        assert done["grade"] == "优秀"
        # 权重和=100
        assert sum(i["weight"] for i in done["items"]) == 100
    finally:
        model_bench._ask = orig
    # 部分失败场景：全部超时 → 总分低
    model_bench._ask = lambda m, p, t: (None, 30000)
    try:
        events = list(model_bench.run_benchmark({"name": "坏模型", "model": "x"}))
        done = events[-1]
        assert done["total"] < 20, done
        assert done["grade"] == "较差"
    finally:
        model_bench._ask = orig
    print("PASS run_benchmark 全链：7 项事件 + 满分 100 优秀 / 全挂 0 分较差")


def test_endpoint():
    tmpdir = tempfile.mkdtemp(prefix="wec_bench_")
    orig = config_store.config_path
    config_store.config_path = lambda: os.path.join(tmpdir, "config.json")
    try:
        with open(config_store.config_path(), "w", encoding="utf-8") as f:
            json.dump({"schema_version": 2, "models": [
                {"id": "m1", "name": "本地模型", "model": "qwen", "base_url": "http://127.0.0.1:1/v1",
                 "enabled": True}], "judge_model_id": ""}, f, ensure_ascii=False)
        from flask import Flask
        from routes.management import bp
        app = Flask(__name__)
        app.register_blueprint(bp)
        c = app.test_client()
        # 精确匹配存在的 id（_ask 真连 127.0.0.1:1 会失败→各项 0 分，但流程走完）
        r = c.post("/api/models/bench7", json={"model_id": "m1"})
        assert r.status_code == 200 and r.mimetype.startswith("text/event-stream")
        body = r.get_data(as_text=True)
        assert "bench_start" in body and "bench_done" in body, body[:200]
        # 不存在的 id → 404（不静默测默认）
        r2 = c.post("/api/models/bench7", json={"model_id": "nope"})
        assert r2.status_code == 404, r2.status_code
        print("PASS 端点：SSE 流走完 + 不存在的模型 404")
    finally:
        config_store.config_path = orig


if __name__ == "__main__":
    test_scorers()
    test_run_benchmark_full()
    test_endpoint()
    print("\n== 全部通过：模型综合评测 ==")

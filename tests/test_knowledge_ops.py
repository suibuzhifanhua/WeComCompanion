# -*- coding: utf-8 -*-
"""test_knowledge_ops — 知识库空闲整理 + 每日 AI 消化（v1.0.301）。

覆盖：
1. DEFAULT_CONFIG 声明 kb_auto 段（顶层段不声明会被 load/save 抹掉——v1.0.300 同款坑）
2. organize：INDEX.md 生成 + 同哈希查重告警 + 绝不移动/删除/重命名文件
3. digest：24h 增量窗口（旧文件排除）→ LLM 生成 消化_<日期>.md → 幂等 skip → force 覆盖
4. digest：无新文件 → skipped=no_new_files 且不写文件
5. digest：LLM 返回 None / 抛异常 → ok=False 且不写文件
6. is_idle：无日志→空闲；旧时间戳→空闲；新时间戳→忙；引擎运行→忙
7. KnowledgeOps：status 字段齐全；run_manual 参数校验 / busy 守卫 / 后台执行落盘

隔离手段：monkeypatch knowledge_ops 模块级引用（workspace_root/companion_log_dir/
_llm_complete/load_config/log_event）；注入 fake study_engine 模块防 is_idle
惰性导入拉真实引擎。
"""
import os
import sys
import tempfile
import time
import types
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在 from services import knowledge_ops 前注入 fake study_engine（is_idle 内惰性 import engine）
_fake_engine_running = {"running": False}


class _FakeEngine:
    def status(self):
        return {"running": _fake_engine_running["running"]}


_fake_se = types.ModuleType("study_engine")
_fake_se.engine = _FakeEngine()
sys.modules.setdefault("study_engine", _fake_se)
# v1.0.401 归位后 is_idle 惰性导入的是 services.study_engine——覆盖其 engine 属性
import services.study_engine as _real_se  # noqa: E402
_real_se.engine = _FakeEngine()

from services import knowledge_ops  # noqa: E402
from core.config_store import DEFAULT_CONFIG  # noqa: E402

KB = "知识库"
CATS = ("课件", "题库", "制度")


class _Patch:
    """把 knowledge_ops 的路径/LLM/配置/日志全部指向 tmp；退出时还原。"""

    def __init__(self, tmp):
        self.tmp = tmp
        self.logdir = os.path.join(tmp, "logs")
        os.makedirs(self.logdir, exist_ok=True)
        self.orig = {}

    def __enter__(self):
        self.orig = {
            "workspace_root": knowledge_ops.workspace_root,
            "companion_log_dir": knowledge_ops.companion_log_dir,
            "_llm_complete": knowledge_ops._llm_complete,
            "load_config": knowledge_ops.load_config,
            "log_event": knowledge_ops.log_event,
        }
        knowledge_ops.workspace_root = lambda: self.tmp
        knowledge_ops.companion_log_dir = lambda: self.logdir
        knowledge_ops.log_event = lambda msg: None
        knowledge_ops.load_config = lambda: {"models": [{"id": "m1", "enabled": True}]}
        knowledge_ops._llm_complete = lambda model, messages, **kw: (
            "## 新内容概览\n- new.md：测试文件\n\n## 要点\n- 点1\n\n## 复习题\n- Q1（来源：new.md）\n\n## 跨文档关联\n无\n")
        return self

    def __exit__(self, *a):
        for k, v in self.orig.items():
            setattr(knowledge_ops, k, v)


def _mk_kb(tmp):
    for cat in CATS:
        os.makedirs(os.path.join(tmp, KB, cat), exist_ok=True)


def _touch(tmp, cat, name, content, age_sec=0):
    fp = os.path.join(tmp, KB, cat, name)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    if age_sec:
        t = time.time() - age_sec
        os.utime(fp, (t, t))
    return fp


def _all_kb_files(tmp):
    out = set()
    for cat in CATS:
        d = os.path.join(tmp, KB, cat)
        out.update(os.listdir(d))
    return out


def _today_digest_path(tmp):
    day = date.today().strftime("%Y-%m-%d")
    return os.path.join(tmp, KB, "消化", f"消化_{day}.md")


# ---------- 用例 ----------
def test_default_kb_auto_declared():
    d = DEFAULT_CONFIG.get("kb_auto")
    assert isinstance(d, dict), "kb_auto 段必须声明进 DEFAULT_CONFIG（否则磁盘段被 load/save 抹掉）"
    assert "organize" in d and "digest" in d, f"kb_auto 缺键：{d}"
    print("PASS DEFAULT_CONFIG 含 kb_auto 段（organize/digest）")


def test_organize_index_and_dedup():
    tmp = tempfile.mkdtemp(prefix="wec_kb_org_")
    with _Patch(tmp):
        _mk_kb(tmp)
        _touch(tmp, "课件", "a.md", "same content")
        _touch(tmp, "题库", "b.md", "same content")   # 与 a 同哈希 → 重复组
        _touch(tmp, "课件", "c.md", "different content")
        before = _all_kb_files(tmp)
        r = knowledge_ops.organize()
        assert r["ok"], r
        assert r["files"] == 3, r
        assert r["dup_groups"] == 1, r
        idx = os.path.join(tmp, KB, "INDEX.md")
        assert os.path.isfile(idx), "INDEX.md 未生成"
        txt = open(idx, encoding="utf-8").read()
        assert "a.md" in txt and "c.md" in txt and "重复文件" in txt, txt[:400]
        assert _all_kb_files(tmp) == before, "整理不得移动/删除/重命名任何文件"
    print("PASS organize：INDEX 生成 + 查重告警 + 零文件改动")


def test_digest_success_and_idempotent():
    tmp = tempfile.mkdtemp(prefix="wec_kb_dig_")
    with _Patch(tmp):
        _mk_kb(tmp)
        _touch(tmp, "课件", "new.md", "课件内容 123")
        target = _today_digest_path(tmp)
        r = knowledge_ops.digest()
        assert r["ok"] and r.get("path") == target, r
        assert os.path.isfile(target), "消化文件未写入"
        txt = open(target, encoding="utf-8").read()
        assert txt.startswith("# 知识库每日消化"), txt[:80]
        assert "new.md" in txt and "## 新内容概览" in txt, txt[:400]
        # 幂等：当天已存在 → skip
        r2 = knowledge_ops.digest()
        assert r2["ok"] and r2["skipped"] == "already", r2
        # force 覆盖
        r3 = knowledge_ops.digest(force=True)
        assert r3["ok"] and not r3.get("skipped"), r3
    print("PASS digest：生成 → 幂等 skip → force 覆盖")


def test_digest_window_excludes_old():
    tmp = tempfile.mkdtemp(prefix="wec_kb_win_")
    with _Patch(tmp):
        _mk_kb(tmp)
        _touch(tmp, "课件", "old.md", "old", age_sec=48 * 3600)   # 48h 前 → 排除
        _touch(tmp, "课件", "fresh.md", "fresh")
        _touch(tmp, "题库", "old_log.txt", "not md")              # 非 .md → 排除
        # v1.0.479：_collect_new_files 改递归后 rel 为「分类/文件名」相对路径
        names = [os.path.basename(x[2]) for x in knowledge_ops._collect_new_files()]
        rels = [x[2] for x in knowledge_ops._collect_new_files()]
        assert "fresh.md" in names, names
        assert "old.md" not in names, names
        assert "old_log.txt" not in names, names
        assert any(r.startswith("课件/") for r in rels), rels   # rel 带分类前缀
    print("PASS digest 增量窗口：24h 外排除 + 非 .md 排除")


def test_digest_no_new_files():
    tmp = tempfile.mkdtemp(prefix="wec_kb_none_")
    with _Patch(tmp):
        _mk_kb(tmp)
        r = knowledge_ops.digest()
        assert r["ok"] and r["skipped"] == "no_new_files", r
        assert not os.path.exists(_today_digest_path(tmp)), "无新文件不应写消化文件"
    print("PASS digest：无新文件 → no_new_files 且不写文件")


def test_digest_llm_failure():
    tmp = tempfile.mkdtemp(prefix="wec_kb_fail_")
    with _Patch(tmp):
        _mk_kb(tmp)
        _touch(tmp, "课件", "x.md", "x")
        # 返回 None → 失败且不写文件
        knowledge_ops._llm_complete = lambda m, msgs, **kw: None
        r = knowledge_ops.digest()
        assert not r["ok"], r
        assert not os.path.exists(_today_digest_path(tmp)), "LLM 失败不得写半成品"
        # 抛异常 → 失败
        def boom(m, msgs, **kw):
            raise RuntimeError("net down")
        knowledge_ops._llm_complete = boom
        r2 = knowledge_ops.digest(force=True)
        assert not r2["ok"] and "异常" in r2["error"], r2
    print("PASS digest：LLM 失败（None/异常）→ 不写文件")


def _write_log(log, offset_sec, tag="[agent] 跑课中"):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - offset_sec))
    with open(log, "w", encoding="utf-8") as f:
        f.write(ts + " " + tag + "\n")


def test_is_idle_heuristic():
    tmp = tempfile.mkdtemp(prefix="wec_kb_idle_")
    with _Patch(tmp):
        log = os.path.join(tmp, "logs", "companion.log")
        # 无日志 → 空闲
        assert knowledge_ops.is_idle() is True
        # 20 分钟前活动（> 600s 窗口）→ 空闲
        _write_log(log, 1200)
        assert knowledge_ops.is_idle() is True
        # 60 秒前活动（< 600s）→ 忙
        _write_log(log, 60)
        assert knowledge_ops.is_idle() is False
        # 引擎运行中 → 忙（即便日志空闲）
        _write_log(log, 3600)
        _fake_engine_running["running"] = True
        try:
            assert knowledge_ops.is_idle() is False
        finally:
            _fake_engine_running["running"] = False
        assert knowledge_ops.is_idle() is True
    print("PASS is_idle：日志时间戳窗口 + 引擎运行双判据")


def test_status_and_manual_run():
    tmp = tempfile.mkdtemp(prefix="wec_kb_manual_")
    with _Patch(tmp):
        _mk_kb(tmp)
        _touch(tmp, "制度", "s.md", "s")
        st = knowledge_ops.keeper.status()
        for k in ("running", "busy", "idle", "last_organize",
                  "latest_digest", "today_digest", "digest_fails_today"):
            assert k in st, f"status 缺字段 {k}"
        # 参数校验
        bad = knowledge_ops.keeper.run_manual("bogus")
        assert not bad["ok"], bad
        # busy 守卫
        knowledge_ops.keeper._busy = True
        try:
            busy_r = knowledge_ops.keeper.run_manual("organize")
            assert not busy_r["ok"], "busy 时应拒绝"
        finally:
            knowledge_ops.keeper._busy = False
        # 手动 organize：后台执行 + 落盘 + _last_organize 更新
        acc = knowledge_ops.keeper.run_manual("organize")
        assert acc["ok"] and acc["accepted"], acc
        deadline = time.time() + 10
        while knowledge_ops.keeper._busy and time.time() < deadline:
            time.sleep(0.2)
        assert not knowledge_ops.keeper._busy, "后台线程未在 10s 内完成"
        assert os.path.isfile(os.path.join(tmp, KB, "INDEX.md")), "手动整理未落盘"
        assert knowledge_ops.keeper._last_organize > 0
    print("PASS KnowledgeOps：status 字段 / 参数校验 / busy 守卫 / 手动整理落盘")


if __name__ == "__main__":
    test_default_kb_auto_declared()
    test_organize_index_and_dedup()
    test_digest_success_and_idempotent()
    test_digest_window_excludes_old()
    test_digest_no_new_files()
    test_digest_llm_failure()
    test_is_idle_heuristic()
    test_status_and_manual_run()
    print("\n== 全部通过：knowledge_ops 空闲整理 + 每日消化 ==")

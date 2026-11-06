# -*- coding: utf-8 -*-
"""跑课/学习引擎域回归锁（归并自 tests/test_v619_study_poll.py + tests/test_v623_mute_sync.py）。

按域合并说明：两文件同锁 services/study_engine 这条链路的两端——一端是**每 3s 轮询
别把产物库建出来**，另一端是**真正注入页面的静音脚本只有一份**。版本出处保留在
各 class docstring。合并时顺带解掉一条测试间依赖：静音防火墙用例原先
`from test_v555_batch3 import _js_verdict`，现在统一取 conftest.js_verdict。
"""

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import pytest  # noqa: E402

from conftest import js_verdict  # noqa: E402
from core import paths as P  # noqa: E402
from core.study_js import STUDY_MUTE_EXPR as E  # noqa: E402
from services.study_engine import StudyEngine  # noqa: E402

# 只有循环、没有属性锁的旧副本特征串（v1.0.266 那版）
OLD_COPY = "if(!window.__muteLoop){window.__muteLoop=setInterval(()=>{document.querySelectorAll('video,audio').forEach(m=>{if(!m.muted)"


class TestStudyPollIsSideEffectFree:
    """v1.0.619 · 批3（原 tests/test_v619_study_poll.py）：
    学习引擎轮询不再读盘、也不再顺手把产物库建出来。

    UI 每 3s 轮一次 `/api/study/status` → `StudyEngine.status()`。原实现在这里
    `len(self._load_json(self._skip_file()))`：
      · 每次轮询把「学习跳过清单」「平台失效清单」两本整本读进来解析，只为取一个 len()；
      · 而 `_skip_file()` 走的是 `ws_sub("产出")`（**写路径**解析器，会 makedirs 并打开
        v1.0.619 的首启产物库闸门），于是"进软件隔一会再建产物库"被第一次轮询打回原形。"""

    @pytest.fixture()
    def eng(self, tmp_path, monkeypatch):
        P._reset_workspace_gate_for_tests()
        monkeypatch.setattr(P, "_app_dir", lambda: str(tmp_path))
        monkeypatch.setattr(P, "workspace_root", lambda: os.path.join(str(tmp_path), "产物库"))
        from services import study_engine as SE
        monkeypatch.setattr(SE, "workspace_root", lambda: os.path.join(str(tmp_path), "产物库"))
        yield StudyEngine(), tmp_path

    def test_status_does_not_create_workspace(self, eng):
        e, tmp = eng
        snap = e.status()
        assert isinstance(snap["skips"], int) and isinstance(snap["dead"], int)
        assert not os.path.exists(os.path.join(str(tmp), "产物库")), \
            "status() 走 ws_sub 把产物库闸门开了——首启 3 秒内就建目录，延后白做"
        assert not P.workspace_ready(), "轮询路径不该有任何开闸副作用（只有写操作才允许）"

    def test_status_count_is_memoized_not_reparsed(self, eng):
        import json
        e, tmp = eng
        calls = []
        real_load = e._load_json

        def counting(path):
            calls.append(path)
            return real_load(path)

        e._load_json = counting
        # 造两份清单（status 的只读路径 = workspace_root()/产出，不建目录，所以这里手动摆）
        d = os.path.join(str(tmp), "产物库", "产出")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "学习跳过清单.json"), "w", encoding="utf-8") as f:
            json.dump([{"a": 1}, {"a": 2}], f)
        assert e.status()["skips"] == 2, "第一次轮询就要读到真实条数"
        calls.clear()
        assert e.status()["skips"] == 2
        assert not calls, f"第二次轮询又读盘解析了（{calls}）——3s 轮询白干"
        # 文件变了必须重算（记忆不能变成陈旧）
        with open(os.path.join(d, "学习跳过清单.json"), "w", encoding="utf-8") as f:
            json.dump([{"a": 1}, {"a": 2}, {"a": 3}], f)
        os.utime(os.path.join(d, "学习跳过清单.json"), (1_700_000_123, 1_700_000_123))
        assert e.status()["skips"] == 3, "清单改了还吃旧计数（缓存不失效）"

    def test_writer_paths_still_create(self, eng):
        """只读侧不建目录，写侧照旧：保存清单仍必须落到 产物库/产出/ 并把闸门带上。"""
        e, _tmp = eng
        p = e._skip_file()                       # 默认 create=True
        assert p.endswith(os.path.join("产出", "学习跳过清单.json"))
        assert P.workspace_ready(), "写路径没开闸，清单会写不出去"
        assert os.path.isdir(os.path.dirname(p))


class TestStudyMuteSingleSource:
    """v1.0.623（原 tests/test_v623_mute_sync.py）：静音三件套只剩一份事实源。

    同一段 JS 之前活着**四份**：跑课引擎 prompt、study-solver 技能、快捷入口提示词、
    一条测试里的手抄副本。引擎先修成「__muteLock 属性锁 + 400ms 循环 + 裸媒体 iframe
    中和」，另三份停在只有 `__muteLoop` 的旧版——播放器会把 muted 重置回 false，光靠
    循环反制不掉；裸媒体 iframe（src 直指 .mp4/m3u8 或 learn/app/filehub）里根本没有
    `<video>` 可拖，学习不计数。用户点快捷入口跑课用的就是那一份旧副本。
    现在 `core/study_js.py` 是唯一出处：锁「不许再出现第二份」+「引用真的解析成那段 JS」。"""

    def test_expression_has_all_three_parts(self):
        assert "__muteLock" in E, "属性锁丢了：播放器把 muted 重置回 false 就没人管"
        assert "__muteLoop" in E, "循环丢了：页面后插的媒体元素不再被兜底"
        assert "iframe" in E and "data:text/html" in E, "裸媒体 iframe 没中和：那种页查不到 <video> 可拖"
        assert "v.duration-3" in E and "v.play()" in E, "播放+拖尾丢了"
        assert E.count("if(!window.__mute") == 2, "锁/循环的自守卫次数变了（重复注入会叠 interval）"

    def test_skill_prompt_carries_the_shared_expression(self):
        from agent_tools.skills import SKILLS

        s = next(x for x in SKILLS if x["id"] == "study-solver")
        assert E in s["prompt"], \
            "study-solver 技能里不是单源那段——快捷入口让它「以技能原文为准」就成了空指头"
        assert OLD_COPY not in s["prompt"], "技能里还留着只有循环的旧副本"

    def test_engine_uses_the_shared_constant_not_a_second_copy(self):
        src = io.open(os.path.join(ROOT, "services", "study_engine.py"), encoding="utf-8").read()
        assert "from core.study_js import STUDY_MUTE_EXPR" in src
        assert "__muteLock" not in src, "引擎里又出现第二份内联副本（这次收敛白做了）"
        assert "STUDY_MUTE_EXPR" in src

    def test_quicklink_points_at_the_skill_instead_of_copying_js(self):
        js = io.open(os.path.join(ROOT, "js", "02-nav-quicklinks.js"), encoding="utf-8").read()
        assert "__muteLoop=setInterval" not in js, "快捷入口又内嵌了一份 JS 副本"
        assert "静音三件套" in js and "study-solver" in js, "入口没把「用哪份表达式」指清楚"

    def test_no_second_copy_anywhere_in_production_code(self):
        """生产码里**可执行的副本**只许出现在 core/study_js.py 一处。

        判据取 JS 的起手式而不是 `__muteLock` 这个词：技能与快捷入口的说明文字里也要
        提到属性锁，按词查会把它们误判成副本（第一版就这么假红过）。"""
        copy_mark = "if(!window.__muteLock){try{"
        hits = []
        for pkg in ("agent_tools", "services", "routes", "core", "wecom_bridge", "desktop", "js", "css"):
            d = os.path.join(ROOT, pkg)
            for _r, _ds, _fs in os.walk(d):
                if "__pycache__" in _r:
                    continue
                for fn in _fs:
                    if not fn.endswith((".py", ".js", ".html", ".css")):
                        continue
                    p = os.path.join(_r, fn)
                    if copy_mark in io.open(p, encoding="utf-8", errors="replace").read():
                        hits.append(os.path.relpath(p, ROOT))
        hits += [p for p in ("backend.py", "index.html")
                 if os.path.isfile(os.path.join(ROOT, p))
                 and copy_mark in io.open(os.path.join(ROOT, p), encoding="utf-8",
                                          errors="replace").read()]
        assert hits == [os.path.join("core", "study_js.py")], \
            "静音表达式散在多处：%s —— 改一处漏三处就是这么来的" % hits

    def test_firewall_still_passes_the_shared_expression(self):
        """真正会发给页面的那段 JS 必须过得了 wecom_run_js 的安全判定
        （引擎改单源后这条测的就是原文本身，不是手抄副本）。"""
        assert js_verdict(E) == "pass", "静音表达式被 run_js 防火墙拦了 → 跑课会卡在人工审批"

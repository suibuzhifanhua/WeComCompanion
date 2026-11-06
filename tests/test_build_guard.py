# -*- coding: utf-8 -*-
"""tests/test_build_guard.py — 打包守护域回归锁合集（v1.0.646 按域归并）。

本文件由 5 个「按版本号起的文件」按主题合并而来，每条 class 的 docstring 保留
原始出处与事故现场——版本编号是这些锁的**理由**，抹掉它们等于把锁降级成断言。

| 原文件                        | 版本出处   | 守护对象                       | 用例数 |
|-------------------------------|------------|--------------------------------|--------|
| test_v603_clear_chats.py      | v1.0.603   | 打包清空历史对话的口径         |   6    |
| test_v623_bp_guard.py         | v1.0.623   | 断点新鲜度守卫（防搬走在跑的任务）|   8    |
| test_v623_ctxmenu.py          | v1.0.623   | 聊天框右键菜单补丁（防空补丁） |   8    |
| test_v632_giant_ratchet.py    | v1.0.632   | 巨型函数棘轮（只许少不许多）   |   9    |
| test_v632_static_noise.py     | v1.0.632   | 静态噪声清零 + 进门禁          |   8    |
| test_v648_silent_gate.py      | v1.0.648   | 静默 except 两道闸门（棘轮+声明）|   7    |
| test_v648_silent_gate.py      | v1.0.648   | 静默 except 两道闸门（棘轮+声明）|   8    |

共同点：它们守的都是 build.py 里的**闸门**，且失败方式一致——闸门在、判据松，
于是「看起来有、实际永远绿」。所以每个 class 里都有反向验证：不只是「能放行」，
还要「该拦的真的拦住」。
"""
import ast
import inspect
import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile

import build as B   # noqa: E402

# v1.0.65x（C 档拆分）：build.py 的实现按域移入 buildlib/，monkeypatch 目标
# 随之改到实现所在的模块（build 模块上的同名 re-export 只是名字契约，patch 它
# 不会影响实现模块里的全局查找）。
from buildlib import chatlog as _CH   # noqa: E402
from buildlib import gates as _G   # noqa: E402
from buildlib import release as _REL   # noqa: E402


# ============================================================ v1.0.603 清空历史对话
class TestClearChats:
    """用户要求：每次打包出来的新版本不要带历史对话，但**其他设置一律保留**。

    两条底线：
    ① 只搬两类对话数据（会话/对话记录），台账·产物·技能·宏·定时任务·组件·配置都不动；
       **断点/ 原地保留**（v1.0.623 口径修正，见 TestBreakpointGuard）；
    ② 是"搬到 build/_chat_archive/<时间戳>"而不是删除——本机没有 git，删了就回不来。
    """

    @pytest.fixture
    def fake_dist(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        lib = root / "dist" / "产物库"
        for d in ("会话", "对话记录", "断点", "台账", "产出", "技能"):
            (lib / d).mkdir(parents=True)
        (lib / "会话" / "conv_1.json").write_text('{"id":"conv_1"}', encoding="utf-8")
        (lib / "对话记录" / "老对话.md").write_text("x", encoding="utf-8")
        (lib / "断点" / "opPane.json").write_text("{}", encoding="utf-8")
        (lib / "台账" / "月报判定.json").write_text("{}", encoding="utf-8")
        (lib / "产出" / "月报检查.md").write_text("x", encoding="utf-8")
        (lib / "技能" / "keep.json").write_text("{}", encoding="utf-8")
        cfg = root / "dist" / "config.json"
        cfg.write_text(json.dumps({"wecom": {"cdp_port": 9222}, "security": {"command_mode": "block"}}),
                       encoding="utf-8")
        monkeypatch.setattr(_CH, "BASE", str(root))
        monkeypatch.setattr(_CH, "_CHAT_ARCHIVE", str(root / "build" / "_chat_archive"))
        return root

    def _epoch(self, root):
        return json.loads((root / "dist" / "config.json").read_text(encoding="utf-8")).get("chat_epoch", 0)

    def test_chat_dirs_are_emptied_and_archived(self, fake_dist):
        B.clear_chat_history()
        lib = fake_dist / "dist" / "产物库"
        for d in ("会话", "对话记录"):
            assert os.listdir(lib / d) == [], f"{d} 没清干净"
        arch = fake_dist / "build" / "_chat_archive"
        stamps = os.listdir(arch)
        assert len(stamps) == 1
        moved = []
        for _, _, fs in os.walk(arch / stamps[0]):
            moved += fs
        assert set(moved) == {"conv_1.json", "老对话.md"}, moved
        # 目录本身还在（应用启动不依赖 mkdir 之外的东西）
        assert (lib / "会话").is_dir()

    def test_breakpoints_are_never_archived(self, fake_dist):
        """v1.0.623 口径修正：断点不是「历史对话」，是「停在待点继续」的任务状态。

        v603 原口径把三类一起清，实测代价是 v1.0.622 把用户**正在进行**的一轮答题断点
        搬进了归档——点「继续」时文件已不在原地。新鲜度守卫（breakpoint_busy，30 分钟）
        只拦得住「刚写过」的那种，而「三小时前暂停、现在想继续」本来就是正常用法。
        """
        assert "断点" in B._CHAT_KEEP_DIRS and "断点" not in B._CHAT_DIRS
        lib = fake_dist / "dist" / "产物库"
        B.clear_chat_history()
        assert os.listdir(lib / "断点") == ["opPane.json"], "断点被搬走了：用户的「继续」会落空"
        arch_files = []
        for _r, _d, fs in os.walk(fake_dist / "build" / "_chat_archive"):
            arch_files += fs
        assert "opPane.json" not in arch_files

    def test_other_data_untouched(self, fake_dist):
        lib = fake_dist / "dist" / "产物库"
        before = {d: sorted(os.listdir(lib / d)) for d in ("台账", "产出", "技能")}
        B.clear_chat_history()
        after = {d: sorted(os.listdir(lib / d)) for d in ("台账", "产出", "技能")}
        assert before == after, "非对话数据被动了"

    def test_config_settings_survive_and_epoch_bumps(self, fake_dist):
        B.clear_chat_history()
        cfg = json.loads((fake_dist / "dist" / "config.json").read_text(encoding="utf-8"))
        assert cfg["wecom"]["cdp_port"] == 9222 and cfg["security"]["command_mode"] == "block"
        assert self._epoch(fake_dist) == 1
        B.clear_chat_history()
        assert self._epoch(fake_dist) == 2, "第二次打包没再 +1，前端不会清缓存"

    def test_keep_chats_is_a_noop(self, fake_dist):
        B.clear_chat_history(keep=True)
        lib = fake_dist / "dist" / "产物库"
        assert os.listdir(lib / "会话") == ["conv_1.json"]
        assert self._epoch(fake_dist) == 0
        assert not (fake_dist / "build" / "_chat_archive").exists()

    def test_missing_dist_is_tolerated(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(_CH, "BASE", str(tmp_path))
        monkeypatch.setattr(_CH, "_CHAT_ARCHIVE", str(tmp_path / "x"))
        B.clear_chat_history()          # 不该抛
        assert "未找到 dist/产物库" in capsys.readouterr().out


# ============================================================ v1.0.623 断点新鲜度守卫
class TestBreakpointGuard:
    """打包守卫要认得「停在待点继续」的断点（任务 #61）。

    真实事故（v1.0.622）：用户正在跑答题，我从外部执行打包，`clear_chat_history` 把
    dist/产物库 的 会话/对话记录/断点 三类整簇搬进 build/_chat_archive/<时间戳>/，
    其中就有那轮还在进行的答题断点——用户点「继续」时文件已不在原地（300 个文件
    事后手工搬回）。当时的活动守卫只看日志尾部 180s：**挂起等待恰恰不写日志**，
    闸门在最需要它的场景里是空的。

    所以这里锁三件事：
    ① 断点新鲜度判据本身（含「读不到不拦合法空机器」与「只看最新那份」）；
    ② 守卫在 main() 里真的被调用，且排在 bump_version 之前（版本号不白耗）；
    ③ --force 仍是显式放行通道（软件内一键打包走的就是这条路）。
    """

    def _mk_bp(self, tmp_path, ages_min):
        """在假 dist 下造 产物库/断点/*.json，ages_min=每份距“现在”多少分钟。"""
        d = tmp_path / "dist" / "产物库" / "断点"
        d.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for i, age in enumerate(ages_min):
            p = d / f"pane_{i}.json"
            p.write_text("{}", encoding="utf-8")
            os.utime(p, (now - age * 60, now - age * 60))
        return d

    def test_fresh_breakpoint_means_busy(self, tmp_path):
        self._mk_bp(tmp_path, [4.0])
        busy, age = B.breakpoint_busy(dist_dir=str(tmp_path / "dist"))
        assert busy is True and age is not None and age < 5

    def test_stale_breakpoint_does_not_block(self, tmp_path):
        """暂停了三天的断点不该把打包永久挡住——它已经不是一个进行中的任务。"""
        self._mk_bp(tmp_path, [60 * 24 * 3])
        busy, age = B.breakpoint_busy(dist_dir=str(tmp_path / "dist"))
        assert busy is False and age > 60

    def test_newest_file_wins(self, tmp_path):
        """多份断点共存时按最新那份判：旧的那份不能掩盖刚挂起的会话。"""
        self._mk_bp(tmp_path, [60 * 24, 1.0])
        busy, age = B.breakpoint_busy(dist_dir=str(tmp_path / "dist"))
        assert busy is True and age < 2, f"没取到最新那份断点：age={age}"

    def test_missing_dir_is_not_busy(self, tmp_path):
        """没跑过应用/没有产物库的机器：判「不忙」，交给日志侧守卫，而不是把打包挡死。"""
        busy, age = B.breakpoint_busy(dist_dir=str(tmp_path / "nope"))
        assert (busy, age) == (False, None)

    def test_empty_dir_and_subdirs_are_ignored(self, tmp_path):
        d = self._mk_bp(tmp_path, [])
        (d / "子目录").mkdir()
        assert B.newest_breakpoint_epoch(dist_dir=str(tmp_path / "dist")) is None
        assert B.breakpoint_busy(dist_dir=str(tmp_path / "dist")) == (False, None)

    def test_unreadable_mtime_does_not_crash(self, tmp_path):
        """文件正在被应用写/被占用时 getmtime 可能抛——守卫不能因此炸掉整个打包。"""
        d = self._mk_bp(tmp_path, [1.0])
        real = os.path.getmtime

        def _boom(p):
            if str(p).startswith(str(d)):
                raise OSError("拒绝访问")
            return real(p)

        os.path.getmtime = _boom
        try:
            assert B.newest_breakpoint_epoch(dist_dir=str(tmp_path / "dist")) is None
        finally:
            os.path.getmtime = real
        busy, age = B.breakpoint_busy(dist_dir=str(tmp_path / "dist"))
        assert busy is True, "getmtime 恢复后仍应读到那份新鲜断点（异常只是跳过这份，不是把它清掉）"

    def test_window_is_the_documented_constant(self):
        assert B._BP_BUSY_MIN >= 10, "窗口太短就又会漏掉题与题之间的挂起（v1.0.622 的成因）"

    def test_guard_runs_in_main_before_version_bump(self):
        """守卫真的接进 main()，而且排在 bump_version 之前——376 空号那类版本白耗。"""
        src = inspect.getsource(B.main)
        call = src.index("breakpoint_busy(")
        bump = src.index("bump_version(")
        assert call < bump, "断点守卫在 bump_version 之后：中止时版本号已被消耗"
        assert "not force and _bp_busy" in src, "--force 通道没了（软件内一键打包依赖它）"
        assert "_BREAKPOINT_DIR" in inspect.getsource(B.newest_breakpoint_epoch), \
            "扫描目录没走单点常量，改目录名时会静默扫到空目录（守卫恒绿）"


# ============================================================ v1.0.623 右键菜单补丁
class TestContextMenuPatch:
    """聊天框右键菜单——补丁必须真的改到赋值，而不是贴个注释。

    用户报「聊天框不能使用鼠标右键，按了右键没有菜单」。查出来是 v1.0.501 起就存在的
    **空补丁**：

        old = "settings.AreDefaultContextMenusEnabled = _state['debug']"
        new = old + "  # PATCHED(...)"

    `old` 是整条赋值语句，替换后右边仍然是 `_state['debug']`，只是尾上加了句注释；
    函数开头 `if marker in src: return` 又让此后每次打包都判定「已打过」。所以生产
    exe 的 WebView2 原生右键菜单从来没启用过（pywebview 只在 debug 模式给菜单），
    而 site-packages 里的文件看着像打过——磁盘上「有 PATCHED 字样」和「赋值生效」
    从来不是同一件事。

    锁的层次：
    ① 行为——打完补丁那行右边必须是常量 True（不是 `_state[...]`、不是 `debug` 变量）；
    ② 迁移——旧的空补丁形态要能被认出并改对（否则这台机器永远以为已打过）；
    ③ 幂等——第二次跑不许再动文件（避免每次打包都重写 site-packages）；
    ④ 找不到那行时明确报出来，不许静默 return（那正是 623 之前"绿但没改"的形状）；
    ⑤ 顺序——main() 里补丁在 PyInstaller 之前（补丁在打包后才跑等于没打进包）。
    """

    ATTR = "AreDefaultContextMenusEnabled"
    UNPATCHED = (
        "class WebView2:\n"
        "    def init(self):\n"
        "        settings = self.CoreWebView2.Environment.CreateSettings()\n"
        "        settings.AreDefaultContextMenusEnabled = _state['debug']\n"
        "        settings.AreDevToolsEnabled = _state['debug']\n"
    )
    # v1.0.501～v1.0.622 之间那台机器上的实际形态：赋值没改，只贴了注释
    COSMETIC = (
        "class WebView2:\n"
        "    def init(self):\n"
        "        settings = self.CoreWebView2.Environment.CreateSettings()\n"
        "        settings.AreDefaultContextMenusEnabled = _state['debug']"
        "  # PATCHED(企微伴侣 v1.0.501): 生产环境保留右键菜单（粘贴/剪切/复制）\n"
        "        settings.AreDevToolsEnabled = _state['debug']\n"
    )
    NO_LINE = "class WebView2:\n    def init(self):\n        pass\n"

    def _mk(self, tmp_path, text, crlf=False):
        p = tmp_path / "edgechromium.py"
        io.open(p, "w", encoding="utf-8", newline="").write(
            text.replace("\n", "\r\n") if crlf else text)
        return str(p)

    def _rhs(self, path):
        """读回那行赋值的右值文本（去注释）。"""
        for ln in io.open(path, encoding="utf-8").read().splitlines():
            if self.ATTR in ln and "=" in ln.split("#")[0]:
                return ln.split("=", 1)[1].split("#", 1)[0].strip(), ln
        return None, None

    def test_fresh_file_is_patched_to_constant_true(self, tmp_path, capsys):
        p = self._mk(tmp_path, self.UNPATCHED)
        assert B._patch_pywebview_contextmenu(p) is True
        rhs, line = self._rhs(p)
        assert rhs == "True", "补丁后右值仍是 %r——右键菜单不会被启用" % rhs
        assert "PATCHED" in line and "_state['debug']" not in line.split("#")[0]
        assert "AreDevToolsEnabled = _state['debug']" in io.open(p, encoding="utf-8").read(), \
            "只该动右键菜单那一行，DevTools 开关不归它管"
        print(capsys.readouterr().out)

    def test_cosmetic_patch_from_501_is_migrated(self, tmp_path):
        """这台机器/装过旧版的用户机上就是这种形态：必须改对，而不是"已打过"跳过。"""
        p = self._mk(tmp_path, self.COSMETIC)
        assert B._patch_pywebview_contextmenu(p) is True
        rhs, _line = self._rhs(p)
        assert rhs == "True", \
            "认不出 v1.0.501 的空补丁形态（marker 命中就 return），生产包依旧没有右键菜单"

    def test_idempotency_keys_on_the_value_not_on_the_comment(self, tmp_path):
        """「打过」的判据必须是**右值**，不是注释里有没有 PATCHED 字样。

        这条是被变异自检逼出来的：把守卫写成 `if marker in line: return` 时，上一切
        测试仍全绿——因为 v1.0.501 那份注释里是旧 marker 字样。可真要哪天有人又写出
        「带今天注释、右值却没改对」的半成品，空补丁就会第二次骗过整条链。
        """
        half_baked = self.COSMETIC.replace("PATCHED(企微伴侣 v1.0.501)", "PATCHED(企微伴侣 v1.0.623)")
        p = self._mk(tmp_path, half_baked)
        assert B._patch_pywebview_contextmenu(p) is True
        rhs, _line = self._rhs(p)
        assert rhs == "True", "marker 命中就跳过——右值没修对也算「已打过」，空补丁复活"

    def test_second_run_is_idempotent_and_does_not_rewrite(self, tmp_path):
        p = self._mk(tmp_path, self.UNPATCHED)
        B._patch_pywebview_contextmenu(p)
        before = io.open(p, "rb").read()
        assert B._patch_pywebview_contextmenu(p) is True
        assert io.open(p, "rb").read() == before, "每次打包都重写 site-packages（不该）"

    def test_missing_assignment_line_is_reported_not_silent(self, tmp_path, capsys):
        p = self._mk(tmp_path, self.NO_LINE)
        before = io.open(p, "rb").read()
        assert B._patch_pywebview_contextmenu(p) is False, "找不到赋值行还装成功"
        out = capsys.readouterr().out
        assert "找不到" in out and "右键菜单" in out
        assert io.open(p, "rb").read() == before, "既然没改，就不该动过文件"

    def test_crlf_file_keeps_its_line_endings(self, tmp_path):
        """pywebview 那个文件在这台机器上是 CRLF——补丁不能顺手把整文件换成 LF。"""
        p = self._mk(tmp_path, self.UNPATCHED, crlf=True)
        assert B._patch_pywebview_contextmenu(p) is True
        raw = io.open(p, "rb").read()
        assert b"settings.AreDefaultContextMenusEnabled = True" in raw
        assert raw.count(b"\n") == raw.count(b"\r\n"), \
            "混进裸 LF 了：一次补丁把整文件行尾改掉，diff 里看不出、回溯也难"

    def test_patch_runs_before_pyinstaller_in_main(self):
        """补丁在打包后才跑＝白打（PyInstaller 已经把未改的文件收进包里了）。"""
        src = io.open(os.path.join(ROOT, "build.py"), encoding="utf-8").read()
        main_src = src[src.index("def main():"):]
        assert main_src.index("_patch_pywebview_contextmenu()") < main_src.index("cmd = [PYINSTALLER,"), \
            "右键菜单补丁排在 PyInstaller 调用之后，包内不会带上改动"

    def test_this_machines_pywebview_is_effectively_patched(self):
        """本机 env 的现状也锁一下：这台机器打出来的包必须带右键菜单。"""
        cand = os.path.normpath(os.path.join(os.path.dirname(B.PYINSTALLER) or "", os.pardir,
                                             "Lib", "site-packages", "webview", "platforms",
                                             "edgechromium.py"))
        if not os.path.isfile(cand):
            pytest.skip("本机定位不到 pywebview（换环境时由上一条行为锁负责）")
        rhs, line = self._rhs(cand)
        assert rhs == "True", \
            "site-packages 里赋值仍是 %r（只有注释没有改动）——重新打包会自动修好" % rhs


# ============================================================ v1.0.632 巨型函数棘轮
class TestGiantFunctionRatchet:
    """巨型函数棘轮（v1.0.632 B9）。

    >100 行的函数不是"代码洁癖"问题：本项目历次静默失效都发生在**没人敢通读**的地方
    （agent_loop 950 行里的权限口径手抄、click_element 271 行里的判据算完不用、
    _stream_body 359 行里的看门狗层层嵌套）。一次拆完不现实，也不该为拆而拆，
    所以只立一条可执行、可验证的规矩：**只许少、不许多**。

    棘轮的失败方式很特定——基线写成一个"松到永不触发"的数字，于是它看起来有、
    实际永远绿。所以本文件第一条就断言基线**等于当天实测值**。
    """

    def _rows(self, n):
        """造 n 条假记录（只要长度与文件名可打印，内容不影响判定）。"""
        return [(120 + i, os.path.join(ROOT, "agent", "loop.py"), "fake_%d" % i) for i in range(n)]

    # ---------------------------------------------------------------- 基线诚实性
    def test_baseline_equals_measured_today(self):
        n = len(B.scan_giant_functions())
        assert n == B.GIANT_FUNC_BASELINE, (
            f"实测 {n} 个 vs 基线 {B.GIANT_FUNC_BASELINE} 个。"
            "基线高于实测＝棘轮松了一格（新增巨型函数不会再被拦住）；"
            "基线低于实测＝本次打包会被拦住，请把数字改成当天真值并注明原因。")

    def test_scan_is_over_the_gate_target_list(self):
        """扫描范围就是门禁清单本身（含 build.py/tools——门禁脚本自己也在闸门里）。"""
        assert "build.py" in B._LINT_TARGETS and "tools" in B._LINT_TARGETS
        files = list(B._iter_lint_files())
        assert any(p.endswith("build.py") for p in files)
        assert any("check_frontend.py" in p for p in files)
        assert not any("__pycache__" in p for p in files)
        assert not any(os.sep + "dist" + os.sep in p for p in files)

    # ---------------------------------------------------------------- 棘轮判定
    def test_growth_blocks_the_build(self, monkeypatch, capsys):
        monkeypatch.setattr(_G, "scan_giant_functions", lambda files=None: self._rows(B.GIANT_FUNC_BASELINE + 1))
        assert B.check_giant_functions() == 1, "多出一个巨型函数却没拦住＝棘轮不存在"
        out = capsys.readouterr().out
        assert "❌" in out and "涨到" in out and "已中止打包" in out, out

    def test_shrinking_passes_and_tells_you_to_lower_the_baseline(self, monkeypatch, capsys):
        monkeypatch.setattr(_G, "scan_giant_functions", lambda files=None: self._rows(B.GIANT_FUNC_BASELINE - 2))
        assert B.check_giant_functions() == 0
        out = capsys.readouterr().out
        assert "下调" in out, "拆掉了却不让把基线改小，等于白拆——下一次增长又是免费的"

    def test_exactly_at_baseline_passes_without_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(_G, "scan_giant_functions", lambda files=None: self._rows(B.GIANT_FUNC_BASELINE))
        assert B.check_giant_functions() == 0
        assert "未增长" in capsys.readouterr().out

    def test_gate_is_wired_into_main(self):
        src = io.open(os.path.join(ROOT, "build.py"), encoding="utf-8").read()
        body = src[src.find("def main("):]
        # v1.0.654 A11：调用点收进 _run_gate；锁「执行器把非零返回（含 -1）当失败」
        assert '_run_gate("巨型函数棘轮检查", check_giant_functions' in body, "只在单测里活的门禁等于没有门禁"
        assert "return not fn()" in src, "调用点了返回值却没当失败处理——门禁会打印❌然后继续打包"

    # ---------------------------------------------------------------- 度量口径本身
    def test_scan_counts_function_span(self, tmp_path):
        """120 行的函数要算进来，50 行的不算。"""
        big = "\n".join("        x%d = %d" % (i, i) for i in range(120))
        src = ("def huge():\n%s\n\n\ndef small():\n    return 2\n" % big)
        p = tmp_path / "sample.py"
        p.write_text(src, encoding="utf-8")
        rows = B.scan_giant_functions(files=[str(p)])
        assert [n for _s, _f, n in rows] == ["huge"], rows
        assert rows[0][0] > B.GIANT_FUNC_MAX_LINES

    def test_nested_function_also_counted_by_own_span(self, tmp_path):
        """嵌套的内层函数按自己的行数单独计——外层的跨度含内层，所以两者都算。
        记下来是为了将来有人把"拆出去"做成"再套一层"时，棘轮不会被骗过去。"""
        big = "\n".join("        x%d = %d" % (i, i) for i in range(120))
        src = "def outer():\n    def inner():\n%s\n    return inner\n" % big
        p = tmp_path / "nest.py"
        p.write_text(src, encoding="utf-8")
        rows = B.scan_giant_functions(files=[str(p)])
        assert sorted(n for _s, _f, n in rows) == ["inner", "outer"], rows

    def test_syntax_error_file_is_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "broken.py"
        p.write_text("def oops(:\n    pass\n", encoding="utf-8")
        assert B.scan_giant_functions(files=[str(p)]) == []


# ============================================================ v1.0.632 静态噪声清零
class TestStaticNoise:
    """静态噪声清零 + 进门禁（v1.0.632 B8）。

    三类噪声此前只在门禁里"汇总计数不拦截"，6 个月攒到 24 条没人回头看。它们各自都指向
    一段**已经不成立的代码**：
      · F841 赋值后从不使用 —— 要么是真死代码，要么是"判据算出来了却没进判定"（本项目
        已实测过多次后者，例如安全判定算完不用）；
      · F811 同名重复定义/重复 import —— 后一份遮蔽前一份，测试 patch 前一份直接失效
        （routes/chat.py 里 `from agent.session import _is_aborted` 就是这一类）；
      · F541 无占位符的 f-string —— 插值被删过，剩下的 f 只会骗过静态检查。

    本文件两条腿：① 断言**今天的全仓噪声为 0**（新增一条就红）；② 断言门禁真的会为非 0
    中止打包（不靠"人记得看输出"）。
    """

    NOISE_SAMPLES = {
        "F841": b"agent/loop.py:10:4: local variable 'verdict' is assigned to but never used\n",
        "F811": b"routes/chat.py:110:5: redefinition of unused '_is_aborted' from line 27\n",
        "F541": b"services/rule_check.py:103:38: f-string is missing placeholders\n",
    }

    class _Out:
        def __init__(self, payload, rc=1):
            self.returncode = rc
            self.stdout = payload
            self.stderr = b""

    @pytest.mark.parametrize("code,payload", sorted(NOISE_SAMPLES.items()))
    def test_each_noise_class_blocks_the_build(self, monkeypatch, capsys, code, payload):
        monkeypatch.setattr(B.subprocess, "run", lambda *a, **k: self._Out(payload))
        assert B.check_undefined_names() != 0, f"{code} 仍然只打印不拦截"
        out = capsys.readouterr().out
        assert "静态噪声" in out and "中止打包" in out, out

    def test_f401_is_still_soft(self, monkeypatch, capsys):
        """未用 import 里有大量刻意的 re-export（`# noqa: F401 路径函数属性查找`），
        把它们升级成硬拦截只会逼人加 noqa 注释，不在这次范围里。"""
        monkeypatch.setattr(B.subprocess, "run", lambda *a, **k: self._Out(
            b"core/paths.py:4:1: 'os' imported but unused\n"))
        assert B.check_undefined_names() == 0
        assert "不拦截" in capsys.readouterr().out

    def test_clean_tree_still_returns_zero(self, monkeypatch):
        monkeypatch.setattr(B.subprocess, "run", lambda *a, **k: self._Out(b"", rc=0))
        assert B.check_undefined_names() == 0

    # ---------------------------------------------------------------- 实测：今天真是 0
    def _real_findings(self):
        r = subprocess.run([sys.executable, "-m", "pyflakes"] + B._LINT_TARGETS,
                           capture_output=True, cwd=ROOT, timeout=300)
        if r.returncode not in (0, 1):
            pytest.skip("pyflakes 不可用（缺开发依赖），本条不适用")
        text = (r.stdout or b"").decode("utf-8", "replace")
        return [ln for ln in text.splitlines()
                if any(k in ln for k in B._NOISE_KEYS)]

    def test_shipped_code_has_zero_static_noise(self):
        """门禁清单（_LINT_TARGETS）里的噪声必须实测为 0——这条红了就是"新增了噪声"。"""
        bad = self._real_findings()
        assert not bad, "\n".join(bad)

    def test_gate_scans_the_gate_itself(self):
        """build.py 与 tools/ 也要进扫描范围：门禁脚本自己坏了，表现是"闸门静默放行"。"""
        assert "build.py" in B._LINT_TARGETS
        assert "tools" in B._LINT_TARGETS

    def test_removed_noise_stays_removed(self):
        """本轮清掉的几处不能被"顺手改回去"：逐条按当前源码复核。"""
        def src(*rel):
            with open(os.path.join(ROOT, *rel), encoding="utf-8") as f:
                return f.read()

        chat = src("routes", "chat.py")
        body = chat[chat.find("def _plan_mode_gen"):chat.find("def _plan_mode_gen") + 4000]
        assert "from agent.session import _is_aborted" not in body, \
            "函数内重复 import 会遮蔽模块级同名——patch 模块级就失效"
        disp = src("agent_tools", "dispatch.py")
        i = disp.find("def build_agent_tools")
        assert "enabled = cfg.get(\"enabled_skills\")" not in disp[i:disp.find("def ", i + 10)], \
            "build_agent_tools 里的 enabled 是从不使用的死赋值（技能筛选已搬进 _split_skills）"
        hub = src("agent_tools", "skill_hub_data.py")
        assert "global _HUB_CACHE" not in hub, "只做 .update() 原地写入，global 是多余的"


# ============================================================ v1.0.648 静默 except 两道闸门
class TestSilentExceptGate:
    """同一类事故的第三次，所以这次把「软提醒」直接换成「硬拦截 + 显式声明」。

    事故的形状（每次都一样）：门禁在、判据松，于是"看起来有、实际永远绿"。
      · v1.0.405 立的裸 except-pass 计数，docstring 写着"目标单调递减"，
        实测却从基线 194 一路涨到 257——因为它只 print 不拦。
      · v1.0.646 立的 tools/scan_silent_except.py，能扫出 ★★★ 数据路径静默，
        但**没接进 build.py**，只能人手跑，等于没有。
    v1.0.648 两道闸门：
      ① 裸 except-pass 棘轮（check_silent_pass）：超过基线即中止，减少则提示下调；
      ② 数据安全路径未声明静默 = 0（check_silent_except）：要么 log_event 留痕，
         要么写 `pass  # silent-ok: <理由>`。
    第二条刻意不按条数设基线：工具按上下文关键词分级，`except FileNotFoundError: pass`
    这类"文件不存在即正常"的路径本来就该静默，逼它们留痕只会把日志灌成噪声；
    真正的风险是**没人看过的静默**，所以判据定为"未声明"而不是"存在"。
    """

    # ---------------------------------------------------------------- 基线诚实性
    def test_bare_baseline_equals_measured_today(self, capsys):
        """与巨型函数棘轮同一条：基线必须等于实测——
        高了棘轮松一格（新增静默不再被拦），低了本次打包直接红。"""
        import ast
        n = B.check_silent_pass()
        capsys.readouterr()
        count = 0
        skip = {"dist", "build", ".pytest_cache", "产物库", "__pycache__", "docs"}
        for dp, dns, fns in os.walk(ROOT):
            dns[:] = [d for d in dns if d not in skip]
            for fn in fns:
                if not fn.endswith(".py"):
                    continue
                try:
                    tree = ast.parse(io.open(os.path.join(dp, fn), encoding="utf-8").read())
                except Exception:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 \
                            and isinstance(node.body[0], ast.Pass):
                        count += 1
        assert count == B.BARE_EXCEPT_BASELINE, (
            f"实测 {count} 条 vs 基线 {B.BARE_EXCEPT_BASELINE} 条。"
            "基线高于实测＝棘轮松了一格（新增静默不会再被拦住）；"
            "基线低于实测＝本次打包会被拦住，请把数字改成当天真值并注明原因。")
        assert n == 0

    def test_growth_blocks_the_build(self, monkeypatch, capsys):
        """涨一条就必须是非零返回——否则这条检查又退回"只 print"。"""
        real = B.check_silent_pass
        monkeypatch.setattr(_G, "BARE_EXCEPT_BASELINE", 0)
        assert B.check_silent_pass() >= 1
        out = capsys.readouterr().out
        assert "❌" in out and "已中止打包" in out, out
        monkeypatch.undo()
        assert real() == 0

    # ---------------------------------------------------------------- ★★★ 声明机制
    def test_undeclared_critical_silent_is_zero_today(self):
        """今天：数据安全路径上不允许存在"既不留痕也不声明"的静默。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import scan_silent_except as sc
        bad = sc.violations()
        assert not bad, "\n".join(f"{p}:{ln}  {t}" for _l, p, ln, t, _d in bad)

    def test_silent_ok_marker_is_recognized(self, tmp_path):
        """标记必须真被认出来——认不出来，这条闸门就退化成"不许有任何 ★★★"，
        于是大家只能删代码或写假日志，声明机制白立。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import scan_silent_except as sc
        # 注意：工具的既有口径是看 except 行**之后**的上下文行定级，
        # 所以关键词必须出现在 except 那一行的注释里，否则造不出 ★★★。
        f = tmp_path / "x.py"
        f.write_text("def a():\n    try:\n        load_config()\n"
                     "    except Exception:  # config 读取失败就用默认值\n        pass\n",
                     encoding="utf-8")
        old_root, sc.ROOT = sc.ROOT, str(tmp_path)
        try:
            assert len(sc.violations()) == 1, "没标记时必须算违规"
            f.write_text("def a():\n    try:\n        load_config()\n"
                         "    except Exception:  # config 读取失败就用默认值\n"
                         "        pass  # silent-ok: 配置不存在就用默认值，属预期分支\n",
                         encoding="utf-8")
            assert sc.violations() == [], "写了 silent-ok 仍被判违规＝标记没生效"
        finally:
            sc.ROOT = old_root

    def test_marker_only_counts_its_own_handler(self, tmp_path):
        """标记只认本 handler 的行：邻居写了不算——
        否则一处声明能给整片代码免检，声明就变成了新形式的静默。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import scan_silent_except as sc
        f = tmp_path / "y.py"
        f.write_text("def a():\n    try:\n        save_config({})\n"
                     "    except Exception:  # config 写入失败\n        pass\n"
                     "    try:\n        save_config({})\n"
                     "    except Exception:  # config 写入失败\n"
                     "        pass  # silent-ok: 这条有理由\n", encoding="utf-8")
        old_root, sc.ROOT = sc.ROOT, str(tmp_path)
        try:
            bad = sc.violations()
            assert len(bad) == 1 and bad[0][2] == 4, f"应只认未声明的那一条（第 4 行），实际：{bad}"
        finally:
            sc.ROOT = old_root

    # ---------------------------------------------------------------- 接线
    def test_both_gates_are_wired_into_main(self):
        """两条闸门都要在 main 里被调用**且返回值当失败处理**——
        只调用不 return，等于打印个 ❌ 然后照常打包（v1.0.405 就这么绿了 20 多个版本）。
        v1.0.654 A11：调用点收进 _run_gate（return not fn() 对 -1 同样拦截）。"""
        src = io.open(os.path.join(ROOT, "build.py"), encoding="utf-8").read()
        body = src[src.find("def main("):]
        assert '_run_gate("静默 pass 棘轮", check_silent_pass' in body, "check_silent_pass 的返回值没当失败处理"
        assert '_run_gate("数据安全静默声明检查", check_silent_except' in body, "check_silent_except 的返回值没当失败处理"
        assert "return not fn()" in src, "门禁执行器没把返回值当失败处理"

    def test_gate_tool_runs_standalone(self):
        """tools/scan_silent_except.py --gate 必须自己能跑出退出码，
        否则它只能活在 import 里，连"人手跑一遍"的机会都没有。"""
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "scan_silent_except.py"), "--gate"],
                           capture_output=True, cwd=ROOT)
        out = (r.stdout or b"").decode("utf-8", "replace")
        assert r.returncode == 0, out[-800:]
        assert "未声明 0 处" in out


# ============================================================ 来自 test_v613_cleanup.py（v1.0.613）


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.portable as P   # noqa: E402

@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(P, "log_event", lambda *a, **k: None)
    P._STATE.clear()
    # _install_worker 成功路径会调 apply_component_path()，把假 git 目录顶进
    # 本进程真实 PATH——不走 monkeypatch，必须手动还原，否则毒到后面所有
    # 用 shutil.which("git") 的测试（test_rebuild 曾因此 WinError 216）。
    saved_path = os.environ.get("PATH", "")
    yield tmp_path
    os.environ["PATH"] = saved_path
    P._STATE.clear()


def test_portable_failure_removes_staging(ws, monkeypatch):
    """失败路径必须当场清 staging——"下次安装开头再清"等于永远不清。"""
    stage = ws / "组件" / "git" / "_staging"
    (stage / "cmd").mkdir(parents=True)
    (stage / "cmd" / "git.exe").write_bytes(b"half-written")

    monkeypatch.setattr(P, "_pick_url", lambda name: (_ for _ in ()).throw(RuntimeError("索引 503")))
    P._STATE["git"] = {"running": True, "phase": "", "pct": 0, "ok": None, "error": "", "version": ""}
    P._install_worker("git")
    assert not stage.exists(), "失败后 staging 还在，下次就又是几百 MB 垃圾"
    assert P.install_status("git")["ok"] is False


def test_portable_success_leaves_no_staging_or_zip(ws, monkeypatch):
    """成功路径：staging 被 rename 走、下载的 zip 要删掉，目录里只留 current/ 与标记。"""
    payload = ws / "payload.zip"
    import zipfile
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("MinGit-2.56.0/cmd/git.exe", b"x")

    def _dl(url, dest, name):
        import shutil as _s
        _s.copyfile(payload, dest)
        return url

    monkeypatch.setattr(P, "_pick_url", lambda name: ("https://m/MinGit-2.56.0-64-bit.zip",
                                                      "MinGit-2.56.0-64-bit.zip"))
    monkeypatch.setattr(P, "_download", _dl)
    P._STATE["git"] = {"running": True, "phase": "", "pct": 0, "ok": None, "error": "", "version": ""}
    P._install_worker("git")
    assert P.install_status("git")["ok"] is True
    left = sorted(os.listdir(ws / "组件" / "git"))
    assert "_staging" not in left, left
    assert not any(f.endswith(".zip") for f in left), left


def _asr_job_body():
    src = io.open(os.path.join(ROOT, "services", "asr.py"), encoding="utf-8").read()
    i = src.find("def _job():")
    return src[i:src.find("        threading.Thread(target=_job", i)]


def test_asr_failure_cleans_staging_source_lock():
    body = _asr_job_body()
    assert 'rmtree(component_dir() + "_staging"' in body, \
        "语音组件失败路径没清 staging（本机曾因此躺了 258.7MB 的 asr_staging）"


def test_asr_removes_pip_bootstrap_source_lock():
    body = _asr_job_body()
    assert "rmtree(pip_dir" in body, \
        "pip 引导目录没删——它会随原子提交搬进正式组件目录（实测 组件/asr/_pip 5.4MB）"


def test_dsh_row_reports_real_footprint(monkeypatch):
    """体检口径要写真数：dsh 装完实测落盘约 800MB（我原先估 90MB，差一个数量级）。"""
    import routes.b_ocr_admin as RO
    import agent_tools.dsh_bridge as DB
    monkeypatch.setattr(DB, "_which_dsh",
                        lambda: r"C:\app\组件\node\current\node-v22.20.0-win-x64\dsh.cmd")
    # v1.0.614 批1：体检行的判据从「找到 dsh」升级为 dsh_check 的 ready（node + 凭据齐备
    # 才算跑得起来），所以这里要供齐另外两项，否则测的就不是本用例想测的口径。
    monkeypatch.setattr(DB, "_node_info", lambda: {"ok": True, "version": "v22.20.0",
                                                   "path": "C:/node/node.exe", "detail": ""})
    monkeypatch.setattr(RO, "load_config", lambda: {"dsh": {"base_url": "http://127.0.0.1:8100",
                                                            "api_key": "sk-x"}})
    c = RO._probe_component("dsh")
    assert c["status"] == "ok", c
    assert c["version"] == "dsh.cmd @ node-v22.20.0-win-x64", c["version"]   # 不被 60 字截断
    assert "800MB" in c["hint"], c["hint"]

# ============================================================ 来自 test_v619_batch1.py（v1.0.619）


from core import paths as PATHS      # noqa: E402
from agent_tools.registry import TOOLS as REGISTRY_TOOLS   # noqa: E402
from agent_tools.skills import SKILLS                      # noqa: E402


# ---------- ① 鉴权 fail-closed ----------

def test_api_denies_when_token_unavailable(monkeypatch):
    import backend
    monkeypatch.setattr(backend, "_AUTH_DISABLED", False)
    monkeypatch.setattr(backend, "_api_token", lambda: "")     # 模拟 token 拿不到
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as c:
        r = c.get("/api/workspace/list")
        assert r.status_code == 503, (
            f"token 为空却放行了（{r.status_code}）——鉴权静默全关比拒绝服务严重得多")


def test_gate_source_is_not_fail_open():
    src = io.open(os.path.join(ROOT, "backend.py"), encoding="utf-8").read()
    assert "if want and not _secrets_mod.compare_digest" not in src, "又回到 fail-open 写法"


# ---------- ② token 持久化等闸门 ----------

def test_token_not_persisted_before_gate(tmp_path, monkeypatch):
    """首启闸门未开：token 仍要在内存里生成（界面照常能用），但**不许**为此 makedirs
    产物库——那正是"归位前的旧位置冒出产物库"的一条真实路径。"""
    import backend
    PATHS._reset_workspace_gate_for_tests()
    monkeypatch.setattr(PATHS, "_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(PATHS, "workspace_root", lambda: os.path.join(str(tmp_path), "产物库"))
    monkeypatch.setattr(backend, "_harden_token_acl", lambda p: None)  # 别在测试里跑 icacls
    monkeypatch.setattr(backend, "_API_TOKEN", "")
    monkeypatch.setattr(backend, "_TOKEN_PERSISTED", False)
    try:
        tok = backend._api_token()
        assert len(tok) == 64, f"token 长度不对：{tok!r}"
        assert not os.path.exists(os.path.join(str(tmp_path), "产物库")), "为写 token 提前建了产物库"
        # 闸门开了之后，下一次访问 /api 自动补持久化
        PATHS.allow_workspace_creation("unit-test")
        assert backend._api_token() == tok, "开闸后换了 token（界面 cookie 会突然 401）"
        tokfile = os.path.join(str(tmp_path), "产物库", "系统", "api_token.txt")
        assert os.path.isfile(tokfile), "开闸后没补持久化，跨进程读不到 token"
        assert io.open(tokfile, encoding="utf-8").read().strip() == tok
    finally:
        PATHS.allow_workspace_creation("teardown")   # 别把闸门关着留给后面的测试


# ---------- ③ 技能：提示词要用的工具必须在自己的 tools 名单里 ----------

def test_skill_prompt_tools_are_declared():
    names = {t["function"]["name"] for t in REGISTRY_TOOLS}
    import re
    # v1.0.623：study-solver 的提示词里现在内联了「静音三件套」JS（单源
    # core.study_js.STUDY_MUTE_EXPR）。那段是**代码资产**不是工具清单，里面的
    # `function(` / `JSON.stringify(` 会被下面的正则当成"提示词点名了不存在的工具"
    # （第一版就是这么假红的）。它自有两道锁：tests/test_v623_mute_sync.py（三段齐备、
    # 无第二份副本）与 test_v555_batch3 的 run_js 防火墙判定，所以这里先摘掉再扫。
    from core.study_js import STUDY_MUTE_EXPR
    missing, unknown = [], []
    for s in SKILLS:
        declared = set(s.get("tools") or [])
        prompt = (s.get("prompt") or "").replace(STUDY_MUTE_EXPR, " ")
        for called in set(re.findall(r"\b([a-z][a-z0-9_]{5,30})\(", prompt)):
            if called not in names:
                unknown.append((s["id"], called))
            elif called not in declared:
                missing.append((s["id"], called))
    assert not unknown, f"技能提示了注册表里不存在的工具：{unknown}"
    assert not missing, (f"技能提示要用、却没给自己声明该工具（单开这个技能时模型无工具可用）："
                         f"{missing}")


def test_webhook_skill_points_at_webhook_tool():
    """messaging 技能描述是「经群机器人 webhook 发群」，工具却曾是 send_wecom_message
    （那是操作企微窗口给联系人打字发消息的另一个工具）——发不出去还看不出为什么。"""
    by_id = {s["id"]: s for s in SKILLS}
    assert "send_wecom_webhook" in by_id["messaging"]["tools"], by_id["messaging"]["tools"]
    assert "send_wecom_message" not in json.dumps(by_id["messaging"], ensure_ascii=False)
    oa = by_id["oa-approval"]
    assert "send_wecom_webhook" in oa["tools"], "一键汇报写着推群，却没给工具"


# ---------- ④ 状态类 JSON 原子写 ----------

def test_progress_and_ledger_use_atomic_writer(monkeypatch, tmp_path):
    from services import batch_flow as BF
    from services import wecom_archive as WA
    calls = []

    def spy(path, obj, indent=2, ensure_ascii=False):
        calls.append(str(path))
        return None

    monkeypatch.setattr(BF, "atomic_write_json", spy)
    monkeypatch.setattr(WA, "atomic_write_json", spy)
    monkeypatch.setattr(BF, "_progress_path", lambda *a: str(tmp_path / "月报进度.json"))
    monkeypatch.setattr(WA, "_ledger_path", lambda: str(tmp_path / "台账.json"))
    BF._save_progress([{"index": 1}])
    WA._save_ledger({"a": 1})
    assert len(calls) == 2, f"没走单点原子写（calls={calls}）：半截 JSON 会让断点/台账整本作废"


def test_save_progress_writes_valid_json(tmp_path, monkeypatch):
    from services import batch_flow as BF
    monkeypatch.setattr(BF, "_progress_path", lambda *a: str(tmp_path / "台账" / "月报进度.json"))
    BF._save_progress([{"index": 1, "title": "甲营业部"}])
    pp = tmp_path / "台账" / "月报进度.json"
    assert json.loads(pp.read_text(encoding="utf-8"))["results"][0]["title"] == "甲营业部"
    assert not (tmp_path / "台账" / "月报进度.json.tmp").exists(), ".tmp 没被替换掉"


def test_dead_page_snapshot_removed():
    """AST 级锁（不是 grep）：`_base_titles` 那份 title 快照被 `_base_ids` 取代后无人再读，
    却仍按每份月报多打一次 CDP /json 请求——注释里提到这个名字不该算红。"""
    tree = ast.parse(io.open(os.path.join(ROOT, "services", "batch_flow.py"),
                             encoding="utf-8").read())
    hit = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "_base_titles":
                    hit.append(node.lineno)
    assert not hit, f"死算赋值又回来了（行 {hit}）"
    # 反向确认这条锁不是空的：同函数里必须仍有那个真正在用的 targetId 快照
    src = io.open(os.path.join(ROOT, "services", "batch_flow.py"), encoding="utf-8").read()
    assert "_base_ids = {t.get(\"id\")" in src


# ---------- ⑤ 同名测试遮蔽（全仓 lint） ----------

def test_env_dependent_cases_skip_not_fail():
    """依赖 wecom venv 真装了 faster-whisper 的那两条用例：缺组件时必须 **skip**，不能 fail。
    否则全量里永远挂着 2 条"环境性红"，真回归混在固定噪音里就看不出来了。"""
    src = io.open(os.path.join(ROOT, "tests", "test_asr.py"), encoding="utf-8").read()
    assert "skipif" in src, "环境依赖用例没有 skipif"
    assert src.count("@_NEED_ENGINE") == 2, src.count("@_NEED_ENGINE")


def test_no_duplicate_test_function_names():
    """同一文件里两个同名 `def test_*`：后定义覆盖前定义，前一条**静默从不执行**。
    test_asr.py 曾把整份内容粘了 4 遍（30 个 def / 收集 8 个）。"""
    bad = []
    tdir = os.path.join(ROOT, "tests")
    for fn in sorted(os.listdir(tdir)):
        if not fn.startswith("test_") or not fn.endswith(".py"):
            continue
        try:
            tree = ast.parse(io.open(os.path.join(tdir, fn), encoding="utf-8").read())
        except SyntaxError:
            continue
        seen = {}
        for node in tree.body:                      # 只认模块级（类里的方法不同作用域）
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("test_"):
                seen.setdefault(node.name, []).append(node.lineno)
        for name, lns in seen.items():
            if len(lns) > 1:
                bad.append(f"{fn}:{name}@{lns}")
    assert not bad, "同名测试函数会静默吞掉前一个：" + "，".join(bad)

# ============================================================ 来自 test_v619_batch4.py（v1.0.619）


import core.config_store as cs   # noqa: E402


# ---------- ① 只写不读的变量 ----------

def _assigned_names(rel):
    tree = ast.parse(io.open(os.path.join(ROOT, rel), encoding="utf-8").read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            tg = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tg:
                if isinstance(t, ast.Name):
                    out.add(t.id)
                elif isinstance(t, ast.Tuple):
                    out |= {e.id for e in t.elts if isinstance(e, ast.Name)}
    return out


@pytest.mark.parametrize("rel,var", [
    ("services/batch_flow.py", "_resumed"),
    ("services/self_repair.py", "used"),
    ("agent/team.py", "_buf"),
    ("agent_tools/dsh_bridge.py", "timed_out"),
])
def test_write_only_locals_removed(rel, var):
    assert var not in _assigned_names(rel), f"{rel} 里 {var} 又变成只写不读的赋值了"


# ---------- ② 注释不许讲错机制 ----------

def test_bind_status_comment_matches_reality():
    """`/api/wecom/status` 死端点已删除（前端零调用）——路由表不许复活它，注释也不能说前端轮询它。"""
    import backend as _B
    rules = {str(r) for r in _B.app.url_map.iter_rules()}
    assert "/api/wecom/status" not in rules, \
        "死端点 /api/wecom/status 已删除（前端零调用），不许重新注册——真实通道是 /api/status 的 wecom 段"
    bsrc = io.open(os.path.join(ROOT, "backend.py"), encoding="utf-8").read()
    for m in re.finditer(r"/api/wecom/status", bsrc):
        seg = bsrc[max(0, m.start() - 260):m.start() + 260]
        assert "轮询" not in seg, "又回到『前端轮询 /api/wecom/status』的错误说明"
    js_all = "".join(io.open(os.path.join(ROOT, "js", f), encoding="utf-8").read()
                     for f in os.listdir(os.path.join(ROOT, "js")) if f.endswith(".js"))
    assert "/api/wecom/status" not in js_all     # 前端零调用（前提事实，保持）
    assert "/api/status" in js_all               # 真实通道还在


# ---------- ③ 行为锁：权限档必须真的落盘 ----------

def test_permission_level_roundtrip(app_client):
    c, _cfg = app_client
    r = c.post("/api/security/config", json={"security": {"permission_level": "ask_every"}})
    assert r.status_code == 200, r.get_json()
    got = (cs.load_config().get("security") or {}).get("permission_level")
    assert got == "ask_every", f"接口回 ok，配置里却是 {got!r}——按白名单逐键写入最容易漏这一键"
    # 非法值必须拒
    r2 = c.post("/api/security/config", json={"security": {"permission_level": "god_mode"}})
    assert r2.status_code == 400, r2.get_json()
    assert (cs.load_config().get("security") or {}).get("permission_level") == "ask_every", \
        "非法值没被拒，把已存的合法值改坏了"
    # 不带该键时不得被清空（omit != clear，与 webhook 那起事故同型）
    c.post("/api/security/config", json={"security": {"command_mode": "block"}})
    assert (cs.load_config().get("security") or {}).get("permission_level") == "ask_every"


# ---------- ④ 前端调用的端点必须存在 ----------

def _known_api_paths():
    import backend as B
    out = set()
    for rule in B.app.url_map.iter_rules():
        p = rule.rule
        if not p.startswith("/api/"):
            continue
        out.add(p.split("<", 1)[0].rstrip("/"))
    return out


def test_js_calls_only_existing_endpoints():
    known = _known_api_paths()
    bad = []
    jsdir = os.path.join(ROOT, "js")
    for fn in sorted(os.listdir(jsdir)):
        if not fn.endswith(".js"):
            continue
        txt = io.open(os.path.join(jsdir, fn), encoding="utf-8").read()
        for m in re.finditer(r"""['"](/api/[A-Za-z0-9_\-/]+)""", txt):
            p = m.group(1).rstrip("/")
            if "${" in p or "{" in p:        # 动态拼接的路径不在本锁覆盖面内
                continue
            # 前缀命中即可（/api/x/<id> 这类带参数的路由已按 < 截断）
            if not any(p == k or p.startswith(k + "/") or k.startswith(p + "/") for k in known):
                bad.append("%s:%s" % (fn, p))
    assert not bad, "前端调了不存在的端点（点了没反应那种）：" + ", ".join(bad)


# ---------- ⑤ 打包收尾的资源一致性核对已接线 ----------

def test_asset_verify_wired_into_build():
    tree = ast.parse(io.open(os.path.join(ROOT, "build.py"), encoding="utf-8").read())
    main_fn = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main_fn is not None
    called = {(getattr(n.func, "attr", None) or getattr(n.func, "id", None))
              for n in ast.walk(main_fn) if isinstance(n, ast.Call)}
    assert "verify_packaged_assets" in called, "包内资源一致性核对没接进打包流程"


# ---------- ⑥ 跑批临时目录会被会话级回收 ----------

def test_conftest_reclaims_mkdtemp():
    """不按模块对象比身份（pytest 加载的 conftest 与 `import tests.conftest` 是两份），
    改成从被替换下来的函数自己的全局域里取登记表。"""
    import tempfile
    assert getattr(tempfile.mkdtemp, "__name__", "") == "_tracked_mkdtemp", \
        "会话级 mkdtemp 回收没生效（被谁覆盖掉了？）"
    registry = tempfile.mkdtemp.__globals__.get("_SESSION_DIRS")
    assert isinstance(registry, list), "回收登记表不见了"
    d = tempfile.mkdtemp(prefix="wec_reclaim_probe_")
    try:
        assert d in registry
        assert os.path.isdir(d), "会话期内必须仍然可见（跨用例先写后读要靠它）"
    finally:
        if d in registry:
            registry.remove(d)
        import shutil
        shutil.rmtree(d, ignore_errors=True)

# ============================================================ 来自 test_v619_bundle_imports.py（v1.0.619）


PROD_TARGETS = ("backend.py", "core_utils.py", "llm_client.py", "security.py",
                "security_ops.py", "core", "services", "desktop", "routes",
                "agent", "agent_tools", "wecom_bridge")
DEV_ONLY_ROOTS = ("tools", "tests", "docs", "js", "css")


def _prod_offenders():
    hits = []
    for tgt in PROD_TARGETS:
        p = os.path.join(ROOT, tgt)
        files = []
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                files += [os.path.join(dirpath, f) for f in filenames if f.endswith(".py")]
        elif os.path.isfile(p):
            files = [p]
        for fp in files:
            tree = ast.parse(io.open(fp, encoding="utf-8").read())
            for node in ast.walk(tree):
                roots = []
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    roots = [node.module.split(".")[0]]
                for r in roots:
                    if r in DEV_ONLY_ROOTS:
                        hits.append("%s:%d → %s" % (os.path.relpath(fp, ROOT), node.lineno, r))
    return hits


def test_production_code_does_not_import_dev_dirs():
    assert not _prod_offenders(), (
        "生产代码引用了冻结包里不存在的开发期目录（运行必静默失效）：" + "; ".join(_prod_offenders()))


def test_build_gate_is_wired():
    """buildlib.gates 里这条门禁必须既存在又被 main() 调用，且失败即中止（硬拦不是提醒）。
    v1.0.654 A11：调用点收进 _run_gate。v1.0.65x 拆分：实现移入 buildlib/gates.py，
    「定义存在」改锁实现文件，「调用接线」仍锁 build.py 源码。"""
    src = io.open(os.path.join(ROOT, "build.py"), encoding="utf-8").read()
    gates_src = io.open(os.path.join(ROOT, "buildlib", "gates.py"), encoding="utf-8").read()
    assert "def check_dev_only_imports()" in gates_src
    i = src.find("def main(")
    assert i > 0
    body = src[i:]
    assert '_run_gate("开发期目录引用检查", check_dev_only_imports' in body, "门禁定义了却没接进打包流程"
    assert "return not fn()" in src, "命中后没中止打包（成了软提醒）"


def test_frontend_check_implementation_is_bundled():
    from core import frontend_check as FC
    for fn in ("check_div_balance", "check_dup_ids", "check_css_braces",
               "check_script_style_close", "check_js_file", "run_all"):
        assert callable(getattr(FC, fn)), fn


def test_cli_shell_reexports_instead_of_duplicating():
    """tools/check_frontend.py 只能是壳：同一批函数对象，不是复制的第二份实现。
    （两份实现 = 改了壳、包内那份还旧着，正是这次事故的另一种形态。）"""
    import importlib
    FC = importlib.import_module("core.frontend_check")
    shell = importlib.import_module("tools.check_frontend")
    for fn in ("check_div_balance", "check_dup_ids", "check_css_braces",
               "check_script_style_close", "check_js_file", "run_all"):
        assert getattr(shell, fn) is getattr(FC, fn), fn


def test_strict_js_check_does_not_pass_unverified(tmp_path):
    from core import portable
    from core.frontend_check import check_js_file
    bad = tmp_path / "bad.js"
    bad.write_text("function ( { var var var;", encoding="utf-8")
    ok, tail = check_js_file(str(bad), strict=True)
    assert ok is False, tail
    good = tmp_path / "good.js"
    good.write_text("const a = 1;\n", encoding="utf-8")
    if portable.find_node():
        assert check_js_file(str(good), strict=True)[0] is True
    # 非 strict（打包门禁那条路，人在看输出）缺 node 时跳过不拦，但仍要说清原因
    ok2, tail2 = check_js_file(str(bad), strict=False)
    if not portable.find_node():
        assert ok2 is True and "node" in tail2


def test_js_node_lookup_uses_portable_single_source():
    """check_js_file 不许再假设 `node` 在 PATH：要与 npm/dsh 同一套定位（系统 → 便携运行时）。
    AST 级：只认函数体里真的调了 find_node()，注释/文档串里提一句不算（第一版用 grep，
    自检时把 `node = _pb.find_node()` 改成 `node = "node"` 锁仍绿——被自己的文档串骗过）。"""
    tree = ast.parse(io.open(os.path.join(ROOT, "core", "frontend_check.py"),
                             encoding="utf-8").read())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "check_js_file"), None)
    assert fn is not None, "check_js_file 不在了"
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "find_node" in called, f"没走 core.portable.find_node()，实际调用：{sorted(called)}"
    src = io.open(os.path.join(ROOT, "core", "frontend_check.py"), encoding="utf-8").read()
    assert 'subprocess.run(["node"' not in src, "又退回硬写 node 的写法了"

# ============================================================ 来自 test_v619_doc_truth.py（v1.0.619）


from conftest import _read  # noqa: E402


README = _read("README.md")
# 公开发布版不含 CHANGELOG.md（迭代记录不外传）——相关断言按缺失跳过，内部版照常生效。
_CHANGELOG_PATH = os.path.join(ROOT, "CHANGELOG.md")
CHANGELOG = _read("CHANGELOG.md") if os.path.isfile(_CHANGELOG_PATH) else None


# ---------- 依赖项数 ----------

def _req_count():
    lines = [l.strip() for l in io.open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8")]
    return len([l for l in lines if l and not l.startswith("#")])


def test_readme_requirements_count_matches_file():
    n = _req_count()
    claims = [int(m.group(1)) for m in re.finditer(r"requirements\.txt[^\n]{0,40}?(\d+)\s*项", README)]
    assert claims, "README 里 requirements 的项数说法不见了（改动结构请同步本锁）"
    bad = [c for c in claims if c != n]
    assert not bad, f"README 写 {bad} 项，requirements.txt 实数 {n} 项"


# ---------- 路由条数：注释里不许再写数 ----------

def test_no_hardcoded_route_counts_in_comments():
    """只抓"路由/接口条数"这类会被代码改动过期的计数；`最近 20 条上下文` 那种是别的量。"""
    pat = re.compile(r"(\d+)\s*条\s*(?:路由|接口|端点)|(?:路由|接口|端点)[^0-9\n]{0,12}?(\d+)\s*条")
    bad = []
    rdir = os.path.join(ROOT, "routes")
    for fn in sorted(os.listdir(rdir)):
        if not fn.endswith(".py"):
            continue
        for ln, line in enumerate(_read(os.path.join("routes", fn)).splitlines(), 1):
            if "#" not in line:
                continue
            if pat.search(line.split("#", 1)[1]):
                bad.append("routes/%s:%d %s" % (fn, ln, line.strip()[:70]))
    assert not bad, "注释里写死了路由条数（实测会变，要数字就 len(app.url_map.iter_rules())）：" + "; ".join(bad)


# ---------- 回退版数：把 js/ 也纳入 ----------

def test_rollback_version_count_copy_covers_js():
    """test_v614_copy_truth 只扫 index.html/README，js/ 里那句"最近 8 个版本"因此活到 v1.0.615。
    同一把尺子量全部承载文案的位置：静态文件 + 后端 hint。"""
    from services import version_store
    keep = version_store._KEEP
    bad = []
    sources = [("index.html", _read("index.html")), ("README.md", README)]
    if CHANGELOG is not None:
        sources.append(("CHANGELOG.md", CHANGELOG))
    sources.append(("services/version_store.py", _read("services/version_store.py")))
    jsdir = os.path.join(ROOT, "js")
    for fn in sorted(os.listdir(jsdir)):
        if fn.endswith(".js"):
            sources.append(("js/" + fn, _read(os.path.join("js", fn))))
    for rel, text in sources:
        s = re.sub(r"<[^>]+>", "", text)
        for m in re.finditer(r"(?:近|保留最近|最近)\s*(\d+)\s*(?:个)?(?:版本|版)", s):
            if int(m.group(1)) != keep:
                bad.append("%s:%s版（代码 _KEEP=%d）" % (rel, m.group(1), keep))
    assert not bad, "回退版数文案与代码不一致：" + "; ".join(bad)


# ---------- 日志位置 ----------

def test_readme_log_path_is_current():
    assert not re.search(r"日志[：:]\s*`?dist/companion\.log", README), \
        "README 又把日志说成 dist 顶层（v1.0.275 起在 产物库/日志/）"
    for m in re.finditer(r"dist/companion\.log", README):
        i = m.start()
        assert "废弃" in README[i - 220:i + 220], "出现裸的 dist/companion.log 说法：%s" % README[i - 60:i + 60]


# ---------- 版本记录事实源 ----------

def test_version_log_pointer_is_honest():
    """README 顶部 blockquote 是 v1.0.545 起的事实源；CHANGELOG 是更早的归档。
    两边必须各自把这件事说清楚——以前 README 把人指向 CHANGELOG，CHANGELOG 停在 554，
    看起来像 70 多个版本没写记录。"""
    if CHANGELOG is None:
        import pytest
        pytest.skip("公开发布版不含 CHANGELOG.md（迭代记录不外传）")
    assert "事实源" in README and "CHANGELOG" in README
    assert "归档边界" in CHANGELOG and "README.md" in CHANGELOG


def test_shipped_versions_have_readme_entry():
    """versions.json 里**已发布**的每个版本号，README 顶部都得有对应条目。
    （v1.0.619 打包后若忘了写发版说明，这条会红——比"记得写"这种口头约定可靠。）"""
    db = os.path.join(ROOT, "产物库", "版本", "versions.json")
    if not os.path.isfile(db):
        import pytest
        pytest.skip("本机还没打过包（versions.json 不存在）")
    import json
    recs = json.load(io.open(db, encoding="utf-8"))
    recs = recs if isinstance(recs, list) else recs.get("versions", [])
    missing = [r.get("version") for r in recs
               if r.get("version") and ("v" + str(r["version"])) not in README]
    assert not missing, "这些已发布版本在 README 里没有记录：%s" % missing


# ---------- spec 由自动重写接管 ----------

def test_readme_does_not_ask_manual_spec_edit():
    assert "新增模块必须同步补登 spec" not in README, \
        "v1.0.555 起 hiddenimports 由 build._autogen_hiddenimports() 自动重写，别再教手工双写"
    assert "自动重写" in README

# ============================================================ 来自 test_v619_release_gate.py（v1.0.619）


import unicodedata

from routes import b_repair as BR    # noqa: E402


def _mk_src(tmp_path, ts):
    """造一棵假源码树 + versions.json（ts=上次构建时间）。"""
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "paths.py").write_text("# x\n", encoding="utf-8")
    (src / "backend.py").write_text("# x\n", encoding="utf-8")
    vd = src / "产物库" / "版本"
    vd.mkdir(parents=True)
    if ts is not None:
        (vd / "versions.json").write_text(
            json.dumps([{"version": "1.0.1", "ts": ts, "hash": ""}], ensure_ascii=False),
            encoding="utf-8")
    return str(src)


def test_no_git_no_hash_still_detects_edits(tmp_path):
    """这台机器的真实形态：git 可定位但 versions.json 里 hash 是空串（非仓库）。"""
    past = int(time.time()) - 3600
    src = _mk_src(tmp_path, past)
    time.sleep(0.02)
    os.utime(os.path.join(src, "core", "paths.py"), None)      # 模拟构建后又改了源码
    changed, reason = BR._source_has_changes(src)
    assert changed is True, reason
    assert "core/paths.py" in reason.replace("\\", "/"), reason


def test_no_changes_after_build_is_blocked(tmp_path):
    built = int(time.time()) + 60          # 构建时间戳在未来＝源码都比它旧
    src = _mk_src(tmp_path, built)
    changed, reason = BR._source_has_changes(src)
    assert changed is False, "零改动仍放行——空转打包没被拦住：" + reason
    assert "没有任何改动" in reason


def test_missing_build_record_falls_through_open(tmp_path):
    """没有版本记录时无从比对——放行，但话要说清是"没有记录"而不是"无法比对（git 不可用）"。"""
    src = _mk_src(tmp_path, None)
    changed, reason = BR._source_has_changes(src)
    assert changed is True
    assert "构建记录" in reason, reason


def test_vacuous_pass_branch_removed():
    """AST 级：`_source_has_changes` 的任何 return 都不许再给出「保守放行」那种无条件放行。
    （第一版用 grep 判这句话，被我自己写的解释注释判红了——注释里讲历史是允许的。）"""
    import ast
    tree = ast.parse(io.open(os.path.join(ROOT, "routes", "b_repair.py"),
                             encoding="utf-8").read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_source_has_changes"), None)
    assert fn is not None, "_source_has_changes 不在了"
    msgs = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            msgs += [e.value for e in node.value.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    bad = [m for m in msgs if "保守放行" in m]
    assert not bad, f"又回到无条件放行：{bad}"
    called = {(getattr(n.func, "attr", None) or getattr(n.func, "id", None))
              for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "_newest_source_mtime" in called, "mtime 比对分支没被调用：" + str(sorted(x for x in called if x))


# ---------- 剥离数据累计可见 + 只清旧份 ----------

def test_release_stats_printed_after_strip():
    """release_mode_clean 收尾必须报累计份数/体积——不报就等于让用户看不见家底。
    v1.0.65x 拆分：实现移入 buildlib/release.py，AST 锁随之改盯实现文件。"""
    import ast
    tree = ast.parse(io.open(os.path.join(ROOT, "buildlib", "release.py"), encoding="utf-8").read())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "release_mode_clean"), None)
    assert fn is not None
    called = {(getattr(n.func, "attr", None) or getattr(n.func, "id", None))
              for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "_print_release_excluded_stats" in called, sorted(x for x in called if x)


def test_prune_keeps_newest_and_never_touches_backups(tmp_path, monkeypatch):
    import build as B
    root = tmp_path / "build"
    exc = root / "_release_excluded"
    for name, big in (("20260101_000000", 1), ("20260102_000000", 1), ("20260103_000000", 1)):
        d = exc / name
        d.mkdir(parents=True)
        (d / "产物库" ).mkdir()
        (d / "产物库" / "companion.log").write_text("x" * 100, encoding="utf-8")
    # 三处禁止触碰的目录，各放一个文件当诱饵
    for guard in ("_dist_backup", "_rollback", "_chat_archive"):
        g = root / guard
        g.mkdir(parents=True)
        (g / "keep.txt").write_text("do not delete", encoding="utf-8")
    monkeypatch.setattr(_REL, "BASE", str(tmp_path))

    rc = B.prune_release_excluded(auto_yes=True)     # 未确认时不得删（见下一条）
    assert rc == 0
    left = os.listdir(str(exc))
    assert left == ["20260103_000000"], left
    for guard in ("_dist_backup", "_rollback", "_chat_archive"):
        assert os.path.isfile(os.path.join(str(root), guard, "keep.txt")), guard + " 被动了"


def test_prune_requires_confirmation(tmp_path, monkeypatch):
    import build as B
    exc = tmp_path / "build" / "_release_excluded"
    for name in ("20260101_000000", "20260102_000000"):
        (exc / name).mkdir(parents=True)
    monkeypatch.setattr(_REL, "BASE", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    assert B.prune_release_excluded(auto_yes=False) == 1
    assert sorted(os.listdir(str(exc))) == ["20260101_000000", "20260102_000000"], \
        "没确认也删了——清理类命令必须显式同意"


def test_build_cli_has_prune_flag():
    src = io.open(os.path.join(ROOT, "build.py"), encoding="utf-8").read()
    assert '"--prune-release"' in src
    assert "if prune_release:" in src


def test_release_strip_needs_its_own_confirm():
    """--release 的剥离动作发生在"打包成功之后"，版本确认覆盖不到它：
    必须自己再问一次，且**回答不是 y 就真的不剥**（AST 里要看到 release=False 的赋值）。"""
    import ast
    tree = ast.parse(io.open(os.path.join(ROOT, "build.py"), encoding="utf-8").read())
    main_fn = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main_fn is not None
    ok = False
    for node in ast.walk(main_fn):
        if not isinstance(node, ast.If):
            continue
        has_input = any(isinstance(c, ast.Call)
                        and getattr(c.func, "id", "") == "input" for c in ast.walk(node))
        stops_release = any(
            isinstance(a, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "release" for t in a.targets)
            and isinstance(a.value, ast.Constant) and a.value.value is False
            for a in ast.walk(node))
        if has_input and stops_release:
            ok = True
    assert ok, "release 剥离没有独立二次确认（或确认后仍照剥）"


# ============================================================ 来自 test_v621_zero_width.py（v1.0.621）


from core.strings import strip_invisible, clean_url, url_missing_scheme   # noqa: E402

# 用 chr() 构造：源码里保持纯 ASCII，肉眼可审；写 \u 转义会被编辑器/粘贴悄悄变成字面字符
ZWSP = chr(0x200B)              # ZERO WIDTH SPACE——用户那条地址的开头
BOM = chr(0xFEFF)               # BOM
LRO = chr(0x202A)               # 双向文本控制符（左到右覆写）
RLO = chr(0x202C)
IDEOGRAPHIC_SPACE = chr(0x3000)  # 全角空格（这个 strip() 认）
ZWJ = chr(0x200D)

ZWSP_URL = ZWSP + "http://example.com:8080/v1"   # v1.0.657 发布去痕：用户实例域名已中性化


# ---------- 清洗本身 ----------

def test_strip_invisible_covers_paste_pollutants():
    assert strip_invisible(ZWSP_URL) == "http://example.com:8080/v1"
    assert strip_invisible("http://x" + ZWSP + ZWJ + BOM + ".com") == "http://x.com"
    assert strip_invisible(LRO + "https://z/v1" + RLO) == "https://z/v1"
    assert strip_invisible(BOM + "sk-abc") == "sk-abc"                 # BOM 开头的 api_key
    assert strip_invisible(IDEOGRAPHIC_SPACE + "http://a/v1 ") == "http://a/v1"
    assert strip_invisible(None) is None                                  # 非字符串原样回


def test_cleaner_does_not_touch_visible_text():
    """反向确认它不是"清洗过头"：正常中文/空格/连字符必须原样保留。"""
    s = "http://a.com/v1  "
    assert strip_invisible(s) == "http://a.com/v1"
    assert strip_invisible("示例公司甲-模型") == "示例公司甲-模型"


def test_clean_url_and_scheme_judgement():
    assert clean_url("http://a/v1///") == "http://a/v1"
    assert url_missing_scheme("example.com:8080/v1") is True
    assert url_missing_scheme(ZWSP_URL) is False, "清完就有协议头，不该误报给用户"
    assert url_missing_scheme("") is False, "空值由调用方按『未配置』处理，不是缺协议"
    assert url_missing_scheme("HTTPS://a/v1") is False


# ---------- 探测：不能再把 InvalidSchema 甩给用户 ----------

def test_probe_survives_zero_width_url():
    """主机换成 127.0.0.1:1（必连接失败）：拿到的是网络类错误，
    而**不能**是 InvalidSchema——那是"零宽字符没被清掉"的唯一外部特征。"""
    from llm_client import probe_remote_models
    url = ZWSP + "http://127.0.0.1:1/v1"
    models, err = probe_remote_models(url, timeout=4)
    assert models is None
    text = err or ""
    assert "InvalidSchema" not in text and "connection adapters" not in text, text


def test_probe_gives_actionable_hint_when_scheme_missing():
    from llm_client import probe_remote_models
    models, err = probe_remote_models("example.com:8080/v1", timeout=4)
    assert models is None and "http://" in err, err


def test_reachable_probe_cleans_stored_polluted_config():
    """config 里**已经**存了脏地址的既有用户（本次事故的真实形态）：使用侧也必须清。"""
    from llm_client import probe_model_reachable
    st = probe_model_reachable({"base_url": ZWSP + "http://127.0.0.1:1/v1", "api_key": ""})
    assert st["state"] in ("down", "auth"), st
    assert "InvalidSchema" not in st["detail"] and "connection adapters" not in st["detail"], st
    st2 = probe_model_reachable({"base_url": "example.com:8080/v1"})
    assert st2["state"] == "down" and "http://" in st2["detail"], st2


# ---------- 写入侧：存进 config 的必须是干净的 ----------
# 注意：这些用例**不联网**——保存与校验发生在发请求之前，探测类断言只打 127.0.0.1:1。

@pytest.fixture()
def app_client_v621(tmp_path, monkeypatch):
    import core.config_store as cs
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cs, "config_path", lambda: str(cfg))
    cs._CFG_CACHE["cfg"] = None
    cs._CFG_CACHE["mtime"] = None
    import backend as backend_mod
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        yield c, cs
    cs._CFG_CACHE["cfg"] = None
    cs._CFG_CACHE["mtime"] = None


def test_save_model_stores_clean_url(app_client_v621):
    c, cs = app_client_v621
    r = c.post("/api/models", json=[{
        "id": "m1", "name": "中转", "base_url": ZWSP_URL,
        "api_key": BOM + "sk-test", "model": "gpt-x"}])
    assert r.status_code == 200, r.get_json()
    saved = cs.load_config()["models"][0]
    assert saved["base_url"] == "http://example.com:8080/v1", repr(saved["base_url"])
    assert saved["api_key"] == "sk-test", repr(saved["api_key"])


def test_save_model_rejects_schemeless_with_clear_message(app_client_v621):
    c, _cs = app_client_v621
    r = c.post("/api/models", json=[{"id": "m2", "base_url": "example.com:8080/v1"}])
    assert r.status_code == 400, r.get_json()
    assert "http://" in r.get_json()["error"]


# ---------- 防回归：使用侧不许再退回裸 strip ----------

def test_llm_client_uses_cleaner_on_request_paths():
    """只清洗写入侧不够——config 里的历史脏值会一直报错。
    AST 判：真正发请求的入口函数体里必须调 clean_url。"""
    tree = ast.parse(io.open(os.path.join(ROOT, "llm_client.py"), encoding="utf-8").read())
    want = {"probe_remote_models", "probe_reasoning_effort", "probe_model_reachable",
            "_chat_url", "_ollama_native_base"}
    missing = set()
    seen = set()
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in want:
            seen.add(fn.name)
            called = {(getattr(n.func, "id", None)) for n in ast.walk(fn) if isinstance(n, ast.Call)}
            if "clean_url" not in called:
                missing.add(fn.name)
    assert seen == want, f"这些入口找不到了（改名请同步本锁）：{want - seen}"
    assert not missing, f"这些请求入口没走 clean_url（零宽字符会原样进 requests）：{sorted(missing)}"


def test_no_invisible_literals_in_this_test_file():
    """本文件的自律：不可见字符只许以转义形式存在，否则"看不见"的字符会混进断言。"""
    src = io.open(__file__, encoding="utf-8").read()
    bad = [hex(ord(c)) for c in src if unicodedata.category(c) == "Cf"]
    assert not bad, "源码里出现字面不可见字符（Cf 类）：" + ", ".join(sorted(set(bad)))

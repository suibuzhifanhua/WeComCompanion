# -*- coding: utf-8 -*-
"""test_asr.py — 语音转写域回归锁（v1.0.523-5 主体；v1.0.646 按域并入 3 个版本文件）
覆盖：引擎可用性检测、字节转写的参数校验/大小上限/临时文件清理、API 形状。
真模型推理不打（依赖 75MB 下载与语音样本），管线由 transcribe_file 的 mock 验证。

v1.0.619（测试卫生）：本文件原先是**同一份内容被整块粘了 4 遍**（30 个 `def test_`、
8 个测试名）。后定义覆盖前定义，所以收集数一直是 8——不是"少了 22 个用例"（我复核过
每一遍的函数体，逐字相同，覆盖不到新分支），但 3/4 的行是纯噪音，而且同名遮蔽意味着
**改第一遍等于什么都没改**。现在只留一份；依赖缺失的两条改成 skipif（见下）。

v1.0.646 归并：ASR 域的 3 个「按版本号起的文件」并入本文件（模块级函数仍为 v1.0.523-5
的 8 条，新增部分按出处分为 3 个 class）：

| 原文件                          | 版本出处  | 守护对象                       | 用例数 |
|---------------------------------|-----------|--------------------------------|--------|
| （本文件模块级）                | v1.0.523-5| 转写入口/大小上限/临时文件/API |   8    |
| test_v594_asr_integrity.py      | v1.0.594  | 组件完整性判据（防残缺实例被认证）|   5   |
| test_v595_asr.py                | v1.0.595  | 模型下载走镜像 + 失败冷却重试   |   3    |
| test_v596_asr_triage.py         | v1.0.596  | 语音「0 字」分诊（静音 vs VAD） |   7    |

这三条锁的共同形态值得记一笔：**都表现为"界面只说转写失败"**，而真实根因分别在
依赖树残缺（594）、Xet CAS 401（595）、VAD 把整段判静音（596）——三次都是靠日志
才区分开的，所以它们和转写入口放在一起，改 /api/asr 时能一次看到全部分诊路径。
"""
import os

import pytest

from services import asr

# 这两条依赖 wecom venv 里真的装了 faster-whisper。本机/新机器没装时以前是硬断言，
# 结果是全量里长期挂着 2 条"环境性红"——把环境问题误报成回归。改为明确跳过并说明原因。
_NEED_ENGINE = pytest.mark.skipif(
    asr.asr_available() != "installed",
    reason="wecom venv 未安装 faster-whisper（语音引擎组件未装）",
)


@_NEED_ENGINE
def test_asr_available_installed_in_venv():
    """wecom venv 已装 faster-whisper——三态应为 'installed'（v1.0.536 组件化）。"""
    assert asr.asr_available() == "installed"


def test_transcribe_bytes_empty():
    text, err = asr.transcribe_bytes(b"")
    assert text is None and "空音频" in err


def test_transcribe_bytes_too_large():
    text, err = asr.transcribe_bytes(b"x" * (21 * 1048576))
    assert text is None and "过大" in err


def test_transcribe_bytes_cleans_temp(monkeypatch):
    """临时文件必须被清理（finally 分支）——recorded via fake transcribe_file。"""
    created = []
    import tempfile as _tf
    real_mkstemp = _tf.mkstemp

    def fake_mkstemp(suffix=None):
        fd, p = real_mkstemp(suffix=suffix)
        created.append(p)
        return fd, p

    monkeypatch.setattr(_tf, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(asr, "transcribe_file", lambda p, language="zh": ("你好", {"duration": 1.0}))
    text, info = asr.transcribe_bytes(b"RIFF-fake-audio", ext="webm")
    assert text == "你好"
    assert created and not os.path.exists(created[0])   # 已清理


@_NEED_ENGINE
def test_api_status_and_transcribe_shape(monkeypatch):
    import backend as backend_mod
    import io as _io
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        d = c.get("/api/asr/status").get_json()
        assert d["available"] == "installed" and "progress" in d
        # 缺文件 → 400
        r = c.post("/api/asr/transcribe", data={})
        assert r.status_code == 400
        # 正常路径（mock 引擎）
        monkeypatch.setattr(asr, "transcribe_bytes",
                            lambda data, ext="webm": ("测试语音", {"duration": 1.2}))
        r2 = c.post("/api/asr/transcribe",
                    data={"audio": (_io.BytesIO(b"RIFF-fake"), "voice.webm")},
                    content_type="multipart/form-data")
        d2 = r2.get_json()
        assert d2["ok"] is True and d2["text"] == "测试语音"


def test_model_size_from_config(tmp_path, monkeypatch):
    import core.config_store as cs
    cfg = tmp_path / "config.json"
    cfg.write_text('{"asr": {"model_size": "base"}}', encoding="utf-8")
    monkeypatch.setattr(cs, "config_path", lambda: str(cfg))
    cs._CFG_CACHE["cfg"] = None; cs._CFG_CACHE["mtime"] = None
    assert asr._model_size() == "base"
    cfg.write_text("{}", encoding="utf-8")
    cs._CFG_CACHE["cfg"] = None; cs._CFG_CACHE["mtime"] = None
    assert asr._model_size() == "small"     # 缺省


def test_install_component_no_self_deadlock(monkeypatch, tmp_path):
    """v1.0.539 死锁回归：install_component 持 _INSTALL_LOCK 调 _set_install（同线程
    重入）——RLock 下必须立即返回（threading.Lock 会永久自死锁，实测用户环境）。"""
    import threading
    import core_utils
    monkeypatch.setattr(core_utils, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(asr, "workspace_root", lambda: str(tmp_path))
    # mock pypi 全链路（不真下载）
    class _FakeReq:
        def get(self, url, **kw):
            m = _io_json()
            return type("R", (), {"json": staticmethod(lambda: m),
                                  "status_code": 200})()
    def _io_json():
        return {"urls": [{"filename": "x-py3-none-any.whl",
                          "url": "https://files.pythonhosted.org/x.whl",
                          "size": 10}]}
    fake = _FakeReq()
    monkeypatch.setattr("core_utils._lazy_requests", fake)
    monkeypatch.setattr(asr, "_download", lambda url, dest, label: dest)
    result_holder = {}
    def _call():
        result_holder["r"] = asr.install_component()
    th = threading.Thread(target=_call, daemon=True)
    th.start()
    th.join(timeout=5)   # 死锁则 join 超时
    assert not th.is_alive(), "install_component 自死锁（_INSTALL_LOCK 重入）"
    assert result_holder["r"]["ok"] is True
    assert asr.install_progress()["running"] is True   # 后台 job 已置 running


def test_install_lock_reentrant():
    """RLock 可重入性直接验证（with 块内调 _set_install 的原始死锁序列）。"""
    with asr._INSTALL_LOCK:
        asr._set_install(phase="重入测试")
    assert asr.install_progress()["phase"] == "重入测试"


# ============================================================ v1.0.594 组件完整性
class TestComponentIntegrity:
    """ASR 组件完整性判据回归锁（v1.0.594）。

    真实故障：用户装了语音组件、界面上却只有「转写失败」。日志里是
      [asr] 模型加载失败（语音输入不可用）：No module named 'tqdm'
    根因是 v1.0.544 及更早的「手挑 wheel」安装留下残缺依赖树（实测缺 tqdm/click/
    filelock/fsspec/packaging），而 v1.0.545 换成 pip bootstrap 后，可用性仍只看
    _installed.json 存不存在 → 残缺实例被永久认证为"已安装"，重装也走不到新路径。
    """
    import json as _json

    @pytest.fixture
    def comp(self, tmp_path, monkeypatch):
        d = tmp_path / "组件" / "asr"
        d.mkdir(parents=True)
        monkeypatch.setattr(asr, "component_dir", lambda: str(d))
        monkeypatch.setattr(asr, "_install_marker", lambda: os.path.join(str(d), "_installed.json"))
        return d

    def _write_marker(self, d, payload):
        import json as _json
        with open(os.path.join(str(d), "_installed.json"), "w", encoding="utf-8") as f:
            _json.dump(payload, f)

    def _fill_deps(self, d, skip=()):
        for m in asr._REQUIRED_TOPS:
            if m in skip:
                continue
            (d / m).mkdir(exist_ok=True)

    def test_legacy_handpicked_marker_is_not_complete(self, comp):
        """旧 `wheels` 标记（手挑 wheel 时代）即使目录看着齐也不算已装好。"""
        self._fill_deps(comp)
        self._write_marker(comp, {"ts": "2026-10-12 13:44:09",
                                  "wheels": ["faster-whisper==1.2.1", "av==16.1.0"]})
        assert asr.component_installed() is True, "标记文件判据不该被改动破坏"
        assert asr.component_complete() is False, "旧格式标记必须触发重装"
        assert asr.asr_available() in ("missing", False)

    def test_missing_transitive_dep_is_not_complete(self, comp):
        """新格式标记但缺 tqdm（本次事故的确切形态）→ 判残缺。"""
        self._fill_deps(comp, skip=("tqdm",))
        self._write_marker(comp, {"ts": "2026-10-22 09:00:00", "pkgs": ["faster-whisper==1.2.1", "av==16.1.0"]})
        assert asr.component_complete() is False

    def test_complete_install_passes(self, comp):
        self._fill_deps(comp)
        self._write_marker(comp, {"ts": "2026-10-22 09:00:00", "pkgs": ["faster-whisper==1.2.1", "av==16.1.0"]})
        assert asr.component_complete() is True

    def test_no_marker_is_not_complete(self, comp):
        self._fill_deps(comp)
        assert asr.component_complete() is False

    def test_component_path_not_injected_when_incomplete(self, comp):
        """残缺组件不能被塞进 sys.path——半初始化的 ctranslate2 会把 import 挂死（v1.0.538 教训）。"""
        self._fill_deps(comp, skip=("ctranslate2",))
        self._write_marker(comp, {"ts": "x", "pkgs": ["p"]})
        saved = asr._state.get("_path_ready")
        try:
            asr._state["_path_ready"] = False
            assert asr._ensure_component_path() is False
            assert asr._state["_path_ready"] is not True, "判不完整时不得置 _path_ready"
        finally:
            asr._state["_path_ready"] = saved


# ============================================================ v1.0.595 镜像与冷却重试
class TestModelLoadRetry:
    """ASR 回归锁（v1.0.595）：模型下载必须走镜像，加载失败不能整程拉黑。

    用户现象："装完组件了，录完还是显示转写失败"。日志：
      [asr] 模型加载失败（语音输入不可用）：Task error: File reconstruction error:
      CAS Client Error: HTTP 401 Unauthorized, domain https://cas-server.xethub.hf.co/…
    pip bootstrap 把 huggingface_hub 升到 2.2.0，新版对走 Xet 的仓库默认经 Xet CAS 取文件，
    而 CAS 域名来自仓库元数据、不经过 HF_ENDPOINT 镜像 → 匿名 401（旧 hand-picked 时代的
    hub 版本没有 Xet，所以从没碰到）。缓存里 model.bin 缺失、只有 config/tokenizer 正是证据。
    """
    import time as _time

    def test_xet_disabled_before_hub_import(self):
        """必须在 import huggingface_hub 之前就置上——它是 import 时读环境的。"""
        assert os.environ.get("HF_HUB_DISABLE_XET") == "1"
        assert os.environ.get("HF_ENDPOINT", "").startswith("https://")
        import sys
        if "huggingface_hub" in sys.modules:
            # 已被别处 import 过也不该是坏事，但要提醒：顺序变了这条修复会静默失效
            assert sys.modules["huggingface_hub"].constants.HF_HUB_DISABLE_XET is True

    def test_failure_is_retried_after_cooldown(self, monkeypatch):
        """加载失败只在冷却期内短路，60s 后必须再试——否则一次网络抖动就判死整个会话。"""
        import time as _time
        calls = []

        def fake_path():
            calls.append(_time.time())
            return False                      # 模拟"组件路径都注不上"→ 走失败记账

        monkeypatch.setattr(asr, "_ensure_component_path", fake_path)
        saved = dict(asr._state)
        try:
            asr._state.update({"_model": None, "_size": None, "_failed": False, "_failed_at": 0.0})
            assert asr._get_model() is None
            assert asr._state["_failed"] is True and asr._state["_failed_at"] > 0

            n0 = len(calls)
            assert asr._get_model() is None
            assert len(calls) == n0, "冷却期内就重试了（应当短路）"

            asr._state["_failed_at"] = _time.time() - (asr._ASR_RETRY_COOLDOWN + 5)
            assert asr._get_model() is None
            assert len(calls) == n0 + 1, "冷却期过后仍未重试——用户只能重启软件"
        finally:
            asr._state.update(saved)

    def test_success_clears_failure_flags(self, monkeypatch):
        """重试成功后必须把 _failed 清零，不能留脏标志。"""
        class FakeModel:
            pass

        monkeypatch.setattr(asr, "_ensure_component_path", lambda: True)
        monkeypatch.setattr(asr, "_model_size", lambda: "tiny")
        fake_pkg = __import__("types").ModuleType("faster_whisper")
        fake_pkg.WhisperModel = lambda *a, **k: FakeModel()
        # 必须走 monkeypatch.setitem：直接写 sys.modules 会在整个 pytest 会话里残留一个
        # 假的 faster_whisper，后面的 asr_available() 因此误报 "installed"（实测踩过）。
        monkeypatch.setitem(__import__("sys").modules, "faster_whisper", fake_pkg)
        saved = dict(asr._state)
        try:
            import time as _time
            asr._state.update({"_model": None, "_size": None, "_failed": True,
                               "_failed_at": _time.time() - 999})
            m = asr._get_model()
            assert isinstance(m, FakeModel)
            assert asr._state["_failed"] is False and asr._state["_failed_at"] == 0.0
        finally:
            asr._state.update(saved)


# ============================================================ v1.0.596 零字分诊
class TestZeroLengthTriage:
    """语音「0 字」分诊回归锁（v1.0.596）。

    用户实报：录完提示「没听清」。日志：
      [asr] 转写完成：0 字（0.0s，音频 4.9s）
    0.0 秒跑完 4.9 秒音频 = 模型根本没推理，Silero VAD（默认 threshold=0.5）把整段判成非语音。
    但"数字静音的录音"（麦克风被系统静音/隐私未授权）也是同样表现，两者提示与处置完全不同，
    所以必须靠音频峰值分开，并且 VAD 判空时要关阀重试一次。
    """

    class _Seg:
        def __init__(self, t):
            self.text = t

    class FakeInfo:
        def __init__(self, duration):
            self.duration = duration
            self.language = "zh"

    class FakeModel:
        """按 vad_filter 返回不同结果，并记录调用次序。"""

        def __init__(self, segs_by_vad):
            self.segs_by_vad = segs_by_vad
            self.calls = []

        def transcribe(self, path, language=None, beam_size=3, vad_filter=True):
            self.calls.append(vad_filter)
            segs = [TestZeroLengthTriage._Seg(t) for t in self.segs_by_vad.get(vad_filter, [])]
            return iter(segs), TestZeroLengthTriage.FakeInfo(4.9)

    @pytest.fixture
    def stub_level(self, monkeypatch):
        def _set(rms, peak, dur):
            monkeypatch.setattr(asr, "_audio_level", lambda p: (rms, peak, dur))
        return _set

    def test_digital_silence_is_reported_as_no_sound(self, stub_level, monkeypatch):
        """近乎静音的录音要说"麦克风没收到声音"，而不是"说得稍长一点"，且不该白跑推理。"""
        stub_level(0.0, 0.0002, 4.9)
        fm = self.FakeModel({True: ["不该被用到"], False: ["不该被用到"]})
        monkeypatch.setattr(asr, "_get_model", lambda: fm)
        text, err = asr.transcribe_file("x.webm")
        assert text is None
        assert "麦克风没有收到声音" in err
        assert fm.calls == [], "静音录音不该触发推理"

    def test_probe_failure_must_not_crash_the_retry_log(self, stub_level, monkeypatch):
        """v1.0.596 实伤：能量探测失败返回 None 时，VAD 重试那行日志无条件 peak:.4f 直接抛
        「unsupported format string passed to NoneType.__format__」→ 接口 500。
        探测只是诊断信息，绝不能把整条转写链带崩。"""
        stub_level(None, None, None)
        fm = self.FakeModel({True: [], False: ["救回来了"]})
        monkeypatch.setattr(asr, "_get_model", lambda: fm)
        text, info = asr.transcribe_file("x.webm")
        assert fm.calls == [True, False]
        assert text == "救回来了"
        assert info["duration"] == 4.9

    def test_probe_runs_after_model_load_so_numpy_is_ready(self, monkeypatch):
        """顺序锁：_audio_level 必须在 _get_model 之后跑。
        numpy / faster_whisper 只有 _ensure_component_path() 注入组件目录后才可导入，
        把探测提到前面会让它在 frozen 下永久 No module named 'numpy'（实测过）。"""
        order = []
        fm = self.FakeModel({True: ["喂"], False: []})

        def spy_model():
            order.append("model")
            return fm

        def spy_level(p):
            order.append("level")
            return (0.05, 0.42, 4.9)

        monkeypatch.setattr(asr, "_get_model", spy_model)
        monkeypatch.setattr(asr, "_audio_level", spy_level)
        text, _info = asr.transcribe_file("x.webm")
        assert order == ["model", "level"], f"实际顺序 {order}"
        assert text == "喂"

    def test_vad_empty_retries_without_vad(self, stub_level, monkeypatch):
        """VAD 判空但音频有能量 → 必须关 VAD 再跑一次（轻声短促中文的真实救回路径）。"""
        stub_level(0.05, 0.42, 4.9)
        fm = self.FakeModel({True: [], False: ["帮我打开报销单"]})
        monkeypatch.setattr(asr, "_get_model", lambda: fm)
        text, info = asr.transcribe_file("x.webm")
        assert fm.calls == [True, False], f"应先带 VAD 跑、再关 VAD 重试，实际 {fm.calls}"
        assert text == "帮我打开报销单"
        assert info["duration"] == 4.9

    def test_no_retry_when_first_pass_has_text(self, stub_level, monkeypatch):
        stub_level(0.05, 0.42, 4.9)
        fm = self.FakeModel({True: ["你好"], False: ["不该跑到这里"]})
        monkeypatch.setattr(asr, "_get_model", lambda: fm)
        text, _ = asr.transcribe_file("x.webm")
        assert fm.calls == [True], "第一次就有结果，不该重复推理（白花一倍时间）"
        assert text == "你好"

    def test_no_retry_for_sub_second_clip(self, stub_level, monkeypatch):
        """不足 1 秒的片段不值得重试（whisper 对小片段本就不可靠，重试只会拖慢反馈）。"""
        stub_level(0.05, 0.42, 4.9)
        fm = self.FakeModel({True: []})

        def tr(path, language=None, beam_size=3, vad_filter=True):
            fm.calls.append(vad_filter)
            return iter([]), self.FakeInfo(0.4)
        fm.transcribe = tr
        monkeypatch.setattr(asr, "_get_model", lambda: fm)
        text, _ = asr.transcribe_file("x.webm")
        assert fm.calls == [True]
        assert text == ""

    def test_audio_level_survives_decode_failure(self, monkeypatch):
        """解码探测失败不能把转写带崩——它只是诊断信息，要退回 (None,None,None)。"""
        import types
        bad = types.ModuleType("faster_whisper")

        def boom(*a, **k):
            raise RuntimeError("av 解码炸了")
        bad.decode_audio = boom
        monkeypatch.setitem(__import__("sys").modules, "faster_whisper", bad)
        assert asr._audio_level("x.webm") == (None, None, None)

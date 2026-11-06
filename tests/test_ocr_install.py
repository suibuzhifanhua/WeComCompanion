# -*- coding: utf-8 -*-
"""v1.0.307：OCR 组件在线安装——离线部分（wheel 白名单匹配 / 状态机 / 幂等）。
真实联网下载验证在 build/_probe_ocr_mirrors.py（不做进套件，避免测试依赖网络）。
运行：pytest tests/test_ocr_install.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def cu(tmp_path, monkeypatch):
    """隔离的 core_utils：config_path 指向临时目录、OCR 状态缓存清零。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for stale in [k for k in list(sys.modules) if k == "core_utils"]:
        sys.modules.pop(stale, None)
    import core_utils
    monkeypatch.setattr(core_utils, "config_path", lambda: str(tmp_path / "config.json"))
    monkeypatch.setattr(core_utils, "_OCR_RUNTIME_STATE", None)
    monkeypatch.setattr(core_utils, "_OCR_DL",
                        {"state": "idle", "progress": 0, "msg": "", "error": ""})
    # v1.0.402 归位后：config_path 定义在 core.config，OCR 状态在 core.ocr——patch 定义处
    import core.config as _cc
    import core.ocr as _co
    monkeypatch.setattr(_cc, "config_path", lambda: str(tmp_path / "config.json"))
    monkeypatch.setattr(_co, "config_path", lambda: str(tmp_path / "config.json"))   # ocr 模块内绑定
    monkeypatch.setattr(_co, "config_path", lambda: str(tmp_path / "config.json"))   # ocr 模块内绑定
    monkeypatch.setattr(_co, "_OCR_RUNTIME_STATE", None)
    monkeypatch.setattr(_co, "_OCR_DL",
                        {"state": "idle", "progress": 0, "msg": "", "error": ""})
    return core_utils


def test_pick_wheel_matches_all_supported_tags(cu):
    """三种合法标签（none-any / cp313 / abi3）都应命中，且优先按白名单顺序。"""
    hrefs = [
        f"/packages/xxx/{cu and 'rapidocr_onnxruntime'}-1.2.3-py3-none-any.whl#sha256=aaa",
        "/packages/yyy/pyclipper-1.4.0-cp313-cp313-win_amd64.whl#sha256=bbb",
        "/packages/zzz/opencv_python-5.0.0.93-cp37-abi3-win_amd64.whl#sha256=ccc",
        "/packages/bad/pyclipper-1.4.0-cp314-cp314-win_amd64.whl#sha256=bad",   # 错 Python 版本
        "/packages/bad/pyclipper-1.4.0-cp313-cp313-manylinux.whl#sha256=bad",   # 错平台
        "/packages/bad/pyclipper-1.3.0-cp313-cp313-win_amd64.whl#sha256=bad",   # 错版本
    ]
    anchors = [(h, "") for h in hrefs]        # v1.0.608：入参改成 (href, requires_python) 锚点表
    hit, _req = cu._ocr_pick_wheel(anchors, "pyclipper", "1.4.0")
    assert hit and hit.rsplit("/", 1)[-1] == "pyclipper-1.4.0-cp313-cp313-win_amd64.whl"
    assert cu._ocr_pick_wheel(anchors, "rapidocr-onnxruntime", "1.2.3")[0].endswith(
        "rapidocr_onnxruntime-1.2.3-py3-none-any.whl")
    assert cu._ocr_pick_wheel(anchors, "opencv-python", "5.0.0.93")[0].endswith(
        "opencv_python-5.0.0.93-cp37-abi3-win_amd64.whl")
    assert cu._ocr_pick_wheel(anchors, "numpy", "9.9.9")[0] is None, "钉定版本不存在时必须返回 None（严格不猜）"


def test_needed_pkgs_and_status_all_installed(cu, tmp_path):
    """六个组件目录齐全 → need 为空、install_ocr_async 幂等返回 ready（不发网络请求）。"""
    ocr_dir = tmp_path / "ocr"
    for sub in cu._OCR_SUB2PKG:
        (ocr_dir / sub).mkdir(parents=True, exist_ok=True)
        (ocr_dir / sub / "__init__.py").write_text("", encoding="utf-8")
    need, d = cu._ocr_needed_pkgs()
    assert need == [], "组件齐全时不应有缺失"
    assert str(d) == str(ocr_dir)
    st = cu.install_ocr_async()
    assert st["state"] == "ready" and st["progress"] == 100
    assert cu.ocr_install_status()["installed"] is True


def test_needed_pkgs_missing_all(cu, tmp_path):
    """临时目录没有任何 ocr 子目录 → 6 个包全部列为缺失（此路径不发请求，只列清单）。"""
    need, d = cu._ocr_needed_pkgs()
    assert sorted(need) == sorted(set(cu._OCR_SUB2PKG.values()))
    assert str(d) == str(tmp_path / "ocr")
    assert cu.ocr_install_status()["installed"] is False


def test_pick_wheel_version_scoped(cu):
    """同包不同版本必须严格钉定：只吃 exact 版本，防镜像新版本悄悄混入。"""
    hrefs = [
        "/packages/newer/pyclipper-1.5.0-cp313-cp313-win_amd64.whl#sha256=x",
        "/packages/older/pyclipper-1.3.0-cp313-cp313-win_amd64.whl#sha256=x",
        "/packages/pin/pyclipper-1.4.0-cp313-cp313-win_amd64.whl#sha256=x",
    ]
    assert cu._ocr_pick_wheel([(h, "") for h in hrefs], "pyclipper", "1.4.0")[0].endswith(
        "pyclipper-1.4.0-cp313-cp313-win_amd64.whl")


def test_verkey_ordering(cu):
    """版本比较：按数字段逐位比较，1.2.10 > 1.2.9；位数不足补 0。"""
    vk = cu._verkey
    assert vk("1.2.10") > vk("1.2.9")
    assert vk("1.10.0") > vk("1.9.9")
    assert vk("2.5.2") == vk("2.5.2")
    assert vk("1.2") == vk("1.2.0")


def test_force_update_selects_only_outdated(cu, tmp_path, monkeypatch):
    """force=True：已装且版本==期望 的跳过；缺失/版本落后的才重装（离线：捕获线程参数不真跑）。"""
    ocr_dir = tmp_path / "ocr"
    (ocr_dir / "numpy").mkdir(parents=True)
    (ocr_dir / "shapely").mkdir(parents=True)
    monkeypatch.setattr(cu, "ocr_versions", lambda: {"numpy": "2.5.2", "shapely": "2.1.1"})
    # v1.0.402 归位后 install_ocr_async 用 core.ocr 模块内绑定——同步 patch 定义处
    import core.ocr as _co3
    monkeypatch.setattr(_co3, "ocr_versions", lambda: {"numpy": "2.5.2", "shapely": "2.1.1"})
    captured = {}

    class FakeThread:
        def __init__(self, target=None, daemon=None, kwargs=None):
            captured["target"] = target
            captured.update(kwargs or {})

        def start(self):
            pass

    monkeypatch.setattr(cu.threading, "Thread", FakeThread)
    st = cu.install_ocr_async(versions={"numpy": "2.5.2", "shapely": "2.1.2"}, force=True)
    assert st["state"] == "running"
    assert sorted(captured["need"]) == ["shapely"], "只有版本落后/缺失的组件需要重装"
    assert captured["want"]["shapely"] == "2.1.2"


def test_check_updates_states(cu, tmp_path, monkeypatch):
    """ocr_check_updates 状态判定：missing/outdated/latest/unknown + 可更新计数（最新版解析打桩）。"""
    ocr_dir = tmp_path / "ocr"
    (ocr_dir / "numpy").mkdir(parents=True)          # 有版本记录且已最新
    (ocr_dir / "shapely").mkdir(parents=True)        # 版本落后
    (ocr_dir / "cv2").mkdir(parents=True)            # 无版本记录（unknown）
    # rapidocr / onnxruntime / pyclipper 目录缺失
    monkeypatch.setattr(cu, "ocr_versions", lambda: {"numpy": "2.5.2", "shapely": "2.1.1"})
    monkeypatch.setattr(cu, "_ocr_latest_version", lambda pkg, py="cp313": {
        "numpy": "2.5.2", "shapely": "2.1.2", "cv2": "5.0.1.0",
    }.get(pkg, "1.0.0"))
    # v1.0.402 归位后 ocr_check_updates 用 core.ocr 模块内绑定——同步 patch 定义处
    import core.ocr as _co2
    monkeypatch.setattr(_co2, "ocr_versions", lambda: {"numpy": "2.5.2", "shapely": "2.1.1"})
    monkeypatch.setattr(_co2, "_ocr_latest_version", lambda pkg, py="cp313": {
        "numpy": "2.5.2", "shapely": "2.1.2", "cv2": "5.0.1.0",
    }.get(pkg, "1.0.0"))
    # v1.0.402 归位后 ocr_check_updates 用 core.ocr 模块内绑定——同步 patch 定义处
    import core.ocr as _co2
    monkeypatch.setattr(_co2, "ocr_versions", lambda: {"numpy": "2.5.2", "shapely": "2.1.1"})
    monkeypatch.setattr(_co2, "_ocr_latest_version", lambda pkg, py="cp313": {
        "numpy": "2.5.2", "shapely": "2.1.2", "cv2": "5.0.1.0",
    }.get(pkg, "1.0.0"))
    d = cu.ocr_check_updates()
    states = {p["pkg"]: p["state"] for p in d["packages"]}
    assert states["numpy"] == "latest"
    assert states["shapely"] == "outdated"
    assert states["opencv-python"] == "unknown"   # wheel 包名是 opencv-python（目录名 cv2）
    assert states["rapidocr-onnxruntime"] == "missing"
    assert d["updates_available"] == 2   # outdated + unknown 计入可更新；missing 走「安装」而非「更新」

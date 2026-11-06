# -*- coding: utf-8 -*-
"""v1.0.306：OCR 运行时旁挂（dist/ocr）自足性冒烟。

背景：cv2/onnxruntime/rapidocr/numpy/shapely 不再打进 exe，改放 dist/ocr/，由
core_utils.ensure_ocr_runtime() 在首次 OCR 前插入 sys.path。本测试在【隔离子进程】里
把 venv site-packages 从 sys.path 剔除、只留 dist/ocr + PIL，模拟冻结 exe 的真实加载路径，
再对合成图片跑一次真实 OCR——dist/ocr 缺任何一个依赖都会在这里暴露。
运行：pytest tests/test_ocr_runtime.py   （未执行过新 build、无 dist/ocr 时自动跳过）
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OCR_DIR = os.path.join(BASE, "dist", "ocr")
VENV_PY = sys.executable  # 用当前解释器（跑测试的 venv），不再硬编码开发机路径


def test_ocr_runtime_is_self_sufficient():
    if not os.path.isdir(os.path.join(OCR_DIR, "rapidocr_onnxruntime")):
        pytest_skip = True  # 尚未执行过 1.0.306 build
        if pytest_skip:
            import pytest
            pytest.skip("dist/ocr 不存在（还没跑过 1.0.306 build），跳过")
    assert os.path.isdir(os.path.join(OCR_DIR, "cv2")), "缺 cv2"
    assert os.path.isdir(os.path.join(OCR_DIR, "onnxruntime")), "缺 onnxruntime"
    assert os.path.isdir(os.path.join(OCR_DIR, "numpy")), "缺 numpy"

    code = r'''
import sys, os
ocr = sys.argv[1]
# 模拟冻结 exe：剔除 venv site-packages，只留标准库 + dist/ocr（+PIL 造图用）
sys.path = [p for p in sys.path if "site-packages" not in p.lower()]
import sysconfig
pil_dir = os.path.join(sysconfig.get_paths()["purelib"], "PIL")
if os.path.isdir(os.path.dirname(pil_dir)):
    sys.path.append(os.path.dirname(pil_dir))   # 只补回 PIL（exe 里 PIL 仍在包内）
sys.path.insert(0, ocr)
import numpy as np
from rapidocr_onnxruntime import RapidOCR
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (420, 140), "white")
d = ImageDraw.Draw(img)
try:
    f = ImageFont.truetype("arial.ttf", 44)
except Exception:
    f = None
d.text((24, 42), "HELLO 12345", fill="black", font=f)
eng = RapidOCR()
res, _ = eng(np.asarray(img))
txt = " ".join((x[1] or "") for x in (res or []))
print("OCR_TEXT:" + txt.replace(" ", ""))
assert "12345" in txt.replace(" ", ""), "OCR 未识别出 12345: " + txt
print("OCR_OK")
'''
    r = subprocess.run([VENV_PY, "-c", code, OCR_DIR], capture_output=True, timeout=300)
    out = (r.stdout or b"").decode("utf-8", "ignore")
    err = (r.stderr or b"").decode("utf-8", "ignore")
    assert r.returncode == 0, f"隔离加载 dist/ocr 失败：\n{err[-1200:]}"
    assert "OCR_OK" in out, f"OCR 未通过：{out[-400:]}"


def test_ensure_ocr_runtime_idempotent():
    """ensure_ocr_runtime 幂等且不抛异常（无 dist/ocr 时返回 False，有则 True）。"""
    sys.path.insert(0, BASE)
    for stale in [k for k in list(sys.modules) if k.startswith(("core_utils", "rapidocr", "cv2", "onnxruntime"))]:
        sys.modules.pop(stale, None)
    import core_utils
    r1 = core_utils.ensure_ocr_runtime()
    r2 = core_utils.ensure_ocr_runtime()   # 第二次直接走缓存
    assert r1 == r2, "幂等性破坏：两次调用结果不一致"

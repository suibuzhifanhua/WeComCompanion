# -*- coding: utf-8 -*-
"""core/result.py — 工具/服务返回 dict 的错误契约单源（v1.0.655 B5 保守形态 →
v1.0.656 全量统一）。

契约（v1.0.656 起）：
  * **内部生产面**统一 `{"ok": False, "error": msg}`（`err()` 构造）——服务/桥接/
    工具层不再产出 `{"__error__": ...}`。
  * **模型可见面**（tool message JSON）保持 `{"__error__": msg}` 逐字节不变——
    严格后端 400 敏感面。转换只在 agent/loop.py 的序列化点做一次（`to_model`）。
  * **读方**统一 `is_err(d)` / `err_msg(d)`——双轨都认（存量 __error__ 形状的
    存档/入参 fixture 不用改，逐兼容）。

历史：v1.0.655 B5 保守批只收了读方双读（is_err/err_msg 单谓词）；
v1.0.656 本批把生产面 22 文件统一到 {ok,error}，loop.py 单点转 __error__。"""


def err(msg, **extra):
    """内部规范错误结果：{"ok": False, "error": msg, ...}。extra 保留附加键。"""
    d = {"ok": False, "error": str(msg)}
    if extra:
        d.update(extra)
    return d


def is_err(d):
    """错误判定单谓词：任一错误轨（__error__ / ok=False）为真即错。"""
    return isinstance(d, dict) and (bool(d.get("__error__")) or d.get("ok") is False)


def err_msg(d):
    """错误文案单出口：两轨取其一，无错返回空串。"""
    if not isinstance(d, dict):
        return ""
    return str(d.get("__error__") or d.get("error") or "")


def to_model(d):
    """模型可见转换（loop.py 序列化点单点调用）：
    内部错误轨 {ok,error} → {"__error__": msg, ...其余键原样}；
    已是 __error__ 形状或非错误 dict 原样返回（恒等，零拷贝路径）。"""
    if not isinstance(d, dict) or not is_err(d):
        return d
    if d.get("__error__"):
        return d
    out = {"__error__": err_msg(d)}
    for k, v in d.items():
        if k not in ("ok", "error"):
            out[k] = v
    return out

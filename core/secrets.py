# -*- coding: utf-8 -*-
"""
core.secrets — 企微伴侣密钥保护模块（v1.0.63 拆分自 backend.py）
跨系统兜底策略：
  - Windows：DPAPI（当前用户可见，CryptProtectData / CryptUnprotectData）
  - 非 Windows：降级为弱保护（base64 混淆）并明确告警，不再静默降级
  - 任一路径失败：自动降级并记录日志，绝不因加密失败导致配置无法读写
v1.0.647 归位：原顶层 security.py → core/secrets.py（不叫 core/security.py——
v1.0.646 刚因与原 core/security.py 同名不同义改过名）。它只 import 三个标准库，
却被 core/config.py、core/config_store.py 反向依赖——归位后 core 包不再依赖顶层模块。
"""
import base64
import ctypes
import sys

_ENCRYPT_PREFIX = "enc:v1:"
_WEAK_PREFIX = "enc:weak:"
IS_WINDOWS = sys.platform == "win32"

# 敏感 key 命中提示词（与 backend 原 _SENSITIVE_KEY_HINTS 保持一致）
# v1.0.373 审查修复（P3-8）：补 private_key/secret_key/pwd 变体——原命中词漏掉
# 非标准命名，此类密钥会明文落盘 config.json
# v1.0.510 优化（P1-4）：补 bank_account/bank_name——个人报销 PII（银行账号/开户行）
# 原先不在加密名单，config.json 明文落盘；加入后 DPAPI 加密 + 工具参数日志脱敏
# （core/config._mask_args 共用本名单）自动覆盖。注意：name 故意不加——
# 模型配置/连接器里大量正常 "name" 字段会被误加密。
_SENSITIVE_KEY_HINTS = ("api_key", "apikey", "webhook", "password", "passwd", "pwd",
                        "token", "secret", "authorization", "cookie", "access_key",
                        "private_key", "secret_key", "client_secret",
                        "bank_account", "bank_name")

# v1.0.588（P1）：按「路径」而非「键名」判定的敏感项。reimburse.name 是报销人真实
# 姓名（PII，原先明文落 config.json 且随 dist 一起分发），但不能把 "name" 加进上面的
# 命中词——模型配置/连接器里大量正常 name 字段会被误加密。故仅在 reimburse 段内生效。
_SENSITIVE_PATHS = {("reimburse", "name")}

# DPAPI DATA_BLOB 结构（protect/unprotect 共用，定义一次）
if IS_WINDOWS:
    from ctypes import wintypes as _wt

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", _wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

# 默认静默；由 backend 模块注入 log_event，避免跨模块循环依赖
_log = lambda msg: None


def set_logger(fn):
    """注入日志函数（backend 的 log_event），用于加密/解密告警。"""
    global _log
    _log = fn if callable(fn) else (lambda m: None)


def _is_sensitive_key(key):
    k = str(key).lower()
    return any(h in k for h in _SENSITIVE_KEY_HINTS)


def _walk_secrets(obj, fn, _path=()):
    """递归遍历 dict/list，对敏感 key 的字符串值应用 fn。
    _path 为自配置根的键路径，用于 _SENSITIVE_PATHS 的路径级判定。

    已带 enc: 前缀的值一律跳过（v1.0.590）：解密失败时我们会原样保留密文，若保存前
    再套一层 _protect_secret 就变成 enc:v1:<enc:v1:...>，越滚越长且永远解不开。
    加密必须是幂等的。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            p = _path + (str(k).lower(),)
            if isinstance(v, (dict, list)):
                out[k] = _walk_secrets(v, fn, p)
            elif (_is_sensitive_key(k) or p in _SENSITIVE_PATHS) and isinstance(v, str) \
                    and v and not v.startswith("enc:"):
                out[k] = fn(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_walk_secrets(x, fn, _path) for x in obj]
    return obj


def _dpapi_protect(plain):
    """Windows DPAPI 加密（当前用户可见）。返回 base64 串。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    data = plain.encode("utf-16-le")
    buf = ctypes.create_string_buffer(data)
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI CryptProtectData failed, err={ctypes.GetLastError()}")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
    return base64.b64encode(raw).decode("ascii")


def _dpapi_unprotect(b64):
    """Windows DPAPI 解密。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    raw = base64.b64decode(b64)
    buf = ctypes.create_string_buffer(raw)
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI CryptUnprotectData failed, err={ctypes.GetLastError()}")
    try:
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
    return out.decode("utf-16-le")


def _protect_secret(plain):
    """对单个敏感值加密；Windows 用 DPAPI，非 Windows 降级弱保护并明确告警。"""
    if IS_WINDOWS:
        try:
            return _ENCRYPT_PREFIX + _dpapi_protect(plain)
        except Exception as e:  # noqa: BLE001
            _log(f"[config] DPAPI 加密失败，降级为弱保护存储：{e}")
    else:
        _log(f"[config] 当前系统({sys.platform})无 DPAPI，敏感字段将以弱保护(base64)存储，请注意保管配置文件")
    return _WEAK_PREFIX + base64.b64encode(plain.encode("utf-8")).decode("ascii")


def _unprotect_secret(val):
    """解密带 enc: 前缀的敏感值。

    ⚠️ 解密失败必须**原样保留密文**，绝不能返回空串。原因（v1.0.590 修掉我自己引入的
    数据丢失）：load_config 解密 → 运行期改配置 → save_config 再加密，是一条回写链；
    一旦解密失败把值变成 ""，`_walk_secrets` 会跳过空值不再加密，下一次落盘就把这个
    字段写成真正的空字符串——**唯一的一份密文被销毁**。而 DPAPI 绑定 Windows 用户+机器，
    配置文件跨机/跨用户拷过来时必然解不开（实测 0x8009002B），这是常见情形而非异常。
    保留密文的代价是运行期拿到一串 base64（表现为远端 401），但那可诊断、可恢复；
    写空是不可恢复的。两种失败都落一条可操作日志提示重填。"""
    if not isinstance(val, str) or not val.startswith("enc:"):
        return val
    try:
        if val.startswith(_ENCRYPT_PREFIX):
            return _dpapi_unprotect(val[len(_ENCRYPT_PREFIX):])
        if val.startswith(_WEAK_PREFIX):
            return base64.b64decode(val[len(_WEAK_PREFIX):]).decode("utf-8")
        _log(f"[config] ⚠️ 未知加密格式（前缀 {val[:8]}…），按原值保留")
        return val
    except Exception as e:  # noqa: BLE001
        _log(f"[config] ⚠️ 敏感字段解密失败，已原样保留密文（不会丢失）；"
             f"DPAPI 绑定当前 Windows 用户与机器，配置文件跨机/跨用户拷贝过来必然解不开，"
             f"请在设置页重新填写该项。错误：{e}")
        return val

# -*- coding: utf-8 -*-
"""core.instance_lock — 单实例 mutex（原 core/security.py，v1.0.646 改名）（自 core_utils 拆分 v1.0.402）
"""
# -*- coding: utf-8 -*-
import time

# ---------------- v1.0.373 单实例 mutex（管理员重启竞态修复） ----------------
def acquire_single_instance_mutex(name, wait_grace=False, max_wait=15.0, k32=None, sleep_fn=None):
    """创建/获取命名 mutex（单实例锁）。返回 (ok, handle)。
    wait_grace=True（管理员重启 --admin 场景）：mutex 被旧实例持有（正在 1.2s 退出
    倒计时中）时轮询等待最多 max_wait 秒再判定，避免新实例被误杀——
    原实现"旧实例未退即检测 → 新旧全部消失"（用户实测）。
    wait_grace=False（普通启动）：持有即返回 False。
    k32/sleep_fn：测试注入（默认 kernel32 真实现 / time.sleep）。"""
    import ctypes as _ct
    if k32 is None:
        k32 = _ct.WinDLL("kernel32", use_last_error=True)
    _sleep = sleep_fn or time.sleep
    k32.CreateMutexW.argtypes = [_ct.c_void_p, _ct.c_int, _ct.c_wchar_p]
    k32.CreateMutexW.restype = _ct.c_void_p
    ERROR_ALREADY_EXISTS = 183
    ERROR_ACCESS_DENIED = 5
    waited = 0.0
    while True:
        _ct.set_last_error(0)
        handle = k32.CreateMutexW(None, 0, name)
        err = _ct.get_last_error()
        if handle and err != ERROR_ALREADY_EXISTS and err != ERROR_ACCESS_DENIED:
            return True, handle   # 本进程新建成功
        # 到这里 = mutex 已被持有，两种情况：
        #   err=183 已存在（同权限实例）；
        #   err=5 无权打开（持锁者是提权实例——v1.0.373 修复：普通双击遇管理员
        #   实例运行时 CreateMutex 被拒，此前误判 err=5 为"拿到锁"导致双实例双图标）
        if handle:
            # v1.0.652（审计 P2-F4）：失败路径关掉"打开已有 mutex"返回的句柄——
            # 旧实现直接丢弃，wait_grace 轮询最多 ~50 次泄漏 50 个内核句柄。
            try:
                k32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
        if not wait_grace or waited >= max_wait:
            return False, None
        _sleep(0.3)
        waited += 0.3

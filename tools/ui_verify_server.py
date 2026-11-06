# -*- coding: utf-8 -*-
"""tools/ui_verify_server.py — UI 验证实例（常驻工具）：独立端口 8790 + 单实例锁直通。
供 playwright 前端验证；不影响正在运行的用户实例；用完结束该 python 进程。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config_store as CS
CS.APP_PORT = 8790                      # backend import 时绑定，须先改

import core_utils
core_utils.acquire_single_instance_mutex = lambda *a, **k: (True, None)   # 不撞用户实例

os.environ["WECOM_NO_GUI"] = "1"
import threading
import backend

threading.Thread(target=lambda: backend.app.run(
    host="127.0.0.1", port=8790, threaded=True, use_reloader=False), daemon=True).start()
print("READY on 8790", flush=True)
import time
while True:
    time.sleep(60)   # 常驻；验证完结束进程

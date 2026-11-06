# -*- coding: utf-8 -*-
"""core.wecom_state — 跨层共享的企微绑定状态（v1.0.510 拆出）
====================================================================
v1.0.510 优化（P1-7 短期项）：_BIND_CACHE 原定义在 routes/wecom.py，backend.py
再 `from routes.wecom import _BIND_CACHE` 反向抓路由层私有名——入口层依赖路由层
内部符号，是 backend⇄routes 软循环的一部分。现下沉到 core/（两层共同依赖的最底层），
routes/wecom.py 与 backend.py 改为各自 import 同一 dict 对象，消除入口层→路由层的
私有符号依赖。注意：共享的是同一个 dict 对象（引用语义），`.clear()`/`.update()`
在任一侧修改对另一侧可见，行为与原实现完全一致。
"""

# 企微绑定状态缓存（bind_status 结果；routes/wecom 写，backend 启动绑定也写）
_BIND_CACHE = {}

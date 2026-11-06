# -*- coding: utf-8 -*-
"""tools/import_profile.py — 冷启动 import 耗时画像（自耗时，非累计）。

用法：
    python tools/import_profile.py                 # 默认画像 `import backend`
    python tools/import_profile.py --module agent.loop
    python tools/import_profile.py --project       # 只看本项目模块（core/agent/routes/...）

为什么要有它：v1.0.648 之前有一句"冷启动 3.3s，把 wecom_bridge/windows(1565 行)
改成惰性导入"的判断。实测（本工具）出来的数是：
    · `import backend` 端到端 ≈ 0.7s（首次冷盘那一次 3.3s 是文件缓存因素，重跑 0.68/0.69/0.70）
    · 本项目全部模块自耗合计 ≈ 0.05s，其中 wecom_bridge.windows **10.6ms**、
      desktop.close_guard 4.9ms、agent.loop 11.9ms
    · 真正的大头是 stdlib（typing/subprocess/inspect/urllib.request/zipfile/enum…）
结论：按"文件大 → 懒加载"去动 wecom_bridge 是拿 1% 的收益换整套回归风险。
行数不等于导入成本——**先量再改**，这个工具就是那把尺。
"""
import argparse
import re
import subprocess
import sys

ROOT_MODULES = ("backend", "core", "core_utils", "agent", "agent_tools", "routes",
                "services", "wecom_bridge", "desktop", "llm_client", "security_ops",
                "tools", "version")


def profile(module="backend", project_only=False, top=15):
    out = subprocess.run([sys.executable, "-X", "importtime", "-c", "import %s" % module],
                         capture_output=True, text=True).stderr
    rows = []
    for line in out.splitlines():
        m = re.match(r"^(\s*)import time:\s+(\d+)\s+\|\s+(\d+)\s+\|\s*(.+)$", line)
        if m:
            rows.append((len(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).strip()))
    if not rows:
        print("没有抓到 importtime 输出（Python 版本不支持 -X importtime？）")
        return 1
    self_map, cum_map = {}, {}
    for i, (ind, own, cum, name) in enumerate(rows):
        child = sum(c2 for ind2, _o2, c2, _n2 in rows[i + 1:] if ind2 == ind + 1)
        self_map[name] = max(own - child, 0)
        cum_map.setdefault(name, cum)
    items = [(n, s) for n, s in self_map.items()
             if not project_only or n.split(".")[0] in ROOT_MODULES]
    items.sort(key=lambda kv: -kv[1])
    total_self = sum(s for _n, s in self_map.items())
    print("模块：import %s    （自耗时 top%d%s）"
          % (module, top, "，仅本项目模块" if project_only else ""))
    for name, s in items[:top]:
        print("  %8.1f ms  cum=%8.1f ms  %s" % (s / 1000.0, cum_map[name] / 1000.0, name))
    if project_only:
        print("  —— 本项目模块自耗合计：%.1f ms / 全部 %.1f ms（%.1f%%）"
              % (sum(s for _n, s in items) / 1000.0, total_self / 1000.0,
                 100.0 * sum(s for _n, s in items) / max(total_self, 1)))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", default="backend")
    ap.add_argument("--project", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    sys.exit(profile(a.module, a.project, a.top))

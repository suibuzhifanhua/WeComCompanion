"""wecom_bridge/js/ 内 JS 片段加载器（v1.0.656：read.py 内嵌 JS 外置）。

开发态从 <包目录>/js/ 读；PyInstaller 冻结态与 core.paths.resource_path 同口径——
取 sys._MEIPASS 为基目录，数据文件按 WeComCompanion.spec 的 datas 条目
('wecom_bridge/js', 'wecom_bridge/js') 落在 <_MEIPASS>/wecom_bridge/js/。
读取 UTF-8、newline='' 原样返回（JS 片段按 \n 拼接进 eval 表达式，禁止换行翻译）。"""
import os
import sys


def load_js(name):
    base = getattr(sys, "_MEIPASS", None)
    if base:
        path = os.path.join(base, "wecom_bridge", "js", name)
    else:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", name)
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()

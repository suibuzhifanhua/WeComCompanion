# version.py — 应用版本号（唯一可信来源：build.py 打包时自动 patch +1）
#
# 版本约定（强制）：
#   永远通过 python build.py（或 --yes / --version=x.y.z）打包，版本号由 build.py
#   自动递增并写回本文件——不要手动编辑 __version__。
#   每次代码改动 / 功能变更（含 bug 修复、新增功能、行为调整）后必须重新打包，
#   禁止只改源码不打包（否则 dist 中的 exe 仍是旧版本）。
__version__ = "1.0.657"


def get(default="0.0.0"):
    """v1.0.510 优化（P2-13）：统一版本读取入口——原 8 个文件各自复制
    try: from version import __version__ / except: VERSION="0.0.0" 兜底块，
    现收敛为本函数（version.py 随源码分发，实际不会缺失；default 仅防御）。"""
    return __version__

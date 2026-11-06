# -*- coding: utf-8 -*-
"""buildlib.verify — 包内前端资源一致性核对（自 build.py 逐字迁入）。"""
import io
import os

from buildlib.env import BASE


def verify_packaged_assets(exe_path):
    """v1.0.619（审查⑧·打包一致性）：把**包内**的 index.html/css/*.js 与源码树逐字节比对。

    起因：spec 的 datas 打进 exe 后从不核对，历史上出过「dist 落后 4 个文件」——
    代码改了、exe 里还是旧前端，用户看到的是"改了没生效"，而排查方向会被带偏到 JS 逻辑。
    --smoke 只验 import，验不了资源新旧。这里直接读 PyInstaller 归档（onefile 的
    CArchive 里就有 datas），不需要运行 exe、不产生副作用。

    返回不一致的条目数；-1 表示这次没法比对（读不了归档/缺 PyInstaller），只警告不拦。"""
    src_assets = ["index.html"]
    for sub in ("css", "js"):
        d = os.path.join(BASE, sub)
        if os.path.isdir(d):
            src_assets += [os.path.join(sub, f) for f in sorted(os.listdir(d))
                           if f.endswith(".css" if sub == "css" else ".js")]
    try:
        from PyInstaller.archive.readers import CArchiveReader
        arc = CArchiveReader(exe_path)
    except Exception as e:  # noqa: BLE001
        print(f"[assets] 警告：读不了 exe 归档，跳过包内资源一致性核对（{type(e).__name__}: {e}）")
        return -1
    diffs = []
    for rel in src_assets:
        src_file = os.path.join(BASE, rel)
        key = rel.replace("/", os.sep)
        try:
            got = arc.extract(key)
        except Exception:  # noqa: BLE001
            diffs.append(f"{rel}（包内没有这一项）")
            continue
        want = io.open(src_file, "rb").read()
        if got != want:
            diffs.append("%s（包内 %d 字节 / 源码 %d 字节）" % (rel, len(got), len(want)))
    if diffs:
        print(f"[assets] ❌ 包内前端资源与源码树不一致 {len(diffs)} 处（共比对 {len(src_assets)} 项）：")
        for d in diffs[:20]:
            print("[assets]   " + d)
        print("[build] ❌ 已判定本次产物不可发——请确认 spec 的 datas 覆盖到这些文件后重打")
        return len(diffs)
    print(f"[assets] 包内资源一致性：{len(src_assets)} 项与源码树逐字节一致 ✓")
    return 0

# -*- coding: utf-8 -*-
"""buildlib.versioning — 版本号读取/递增/写入与 exe 版本资源生成（自 build.py 逐字迁入）。"""
import os
import re

from buildlib.env import BASE, VPATH


def current_version():
    with open(VPATH, "r", encoding="utf-8") as f:
        txt = f.read()
    m = re.search(r'__version__\s*=\s*["\'](\d+)\.(\d+)\.(\d+)["\']', txt)
    return ".".join(m.groups()) if m else "0.0.0"


def next_version(v):
    a, b, c = map(int, v.split("."))
    c += 1
    return f"{a}.{b}.{c}"


def bump_version(explicit=None):
    """P3-4：显式传参指定版本则不自动 +1；未指定时 patch +1。返回 (旧版本, 新版本)。"""
    cur = current_version()
    new = explicit or next_version(cur)
    with open(VPATH, "r", encoding="utf-8") as f:
        txt = f.read()
    txt = re.sub(r'__version__\s*=\s*["\']\d+\.\d+\.\d+["\']',
                 f'__version__ = "{new}"', txt)
    with open(VPATH, "w", encoding="utf-8") as f:
        f.write(txt)
    return cur, new


def make_version_info(v):
    a, b, c, d = (list(map(int, v.split("."))) + [0, 0, 0, 0])[:4]
    path = os.path.join(BASE, "version_info.txt")
    content = f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, {d}),
    prodvers=({a}, {b}, {c}, {d}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u''),
         StringStruct(u'FileDescription', u'企业微信伴侣'),
         StringStruct(u'FileVersion', u'{a}.{b}.{c}.{d}'),
         StringStruct(u'InternalName', u'WeComCompanion'),
         StringStruct(u'LegalCopyright', u''),
         StringStruct(u'OriginalFilename', u'WeComCompanion.exe'),
         StringStruct(u'ProductName', u'企业微信伴侣'),
         StringStruct(u'ProductVersion', u'{a}.{b}.{c}.{d}')])]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path

# -*- coding: utf-8 -*-
"""core.strings — 用户输入里的不可见字符清理（v1.0.621，单点）。

起因（用户实测）：添加模型时从别处复制的接口地址带了一个 **U+200B 零宽空格**，
本软件报 `InvalidSchema: No connection adapters were found for
'\\u200bhttp://example.com:8080/v1/models'`，而其他软件同一个地址能识别。
根因两条：
  · `str.strip()` 只去 **空白**，而 `'\\u200b'.isspace()` 是 False → 零宽字符原样留着；
  · requests 拿到 `\\u200bhttp://…` 认不出协议 → 抛的是 InvalidSchema，
    界面上看不出"地址其实是对的"，用户只会反复重贴同一个地址。
所以清理要在**写入侧**（保存/探测入口）和**使用侧**（真正发请求前）两边都做——
只做写入侧的话，config.json 里已经存进去的脏值仍然会一直报错。
"""
import re

# 零宽空格/非连字/连接符、词连接符、BOM、软连字符，以及双向文本控制符
# （写成 \u 转义而不是字面字符：这些字符**本来就看不见**，源码里贴字面量等于埋雷）
_INVISIBLE_RE = re.compile(
    "[\\u200b\\u200c\\u200d\\u2060\\ufeff\\u00ad\\u202a-\\u202e\\u2066-\\u2069]"
)


def strip_invisible(s):
    """去掉不可见字符并 strip() 首尾（NBSP/全角空格 strip() 已能处理），不改动正常字符。
    非字符串原样返回（调用方常传 None）。"""
    if not isinstance(s, str):
        return s
    return _INVISIBLE_RE.sub("", s).strip()


def clean_url(s):
    """接口地址专用：清理不可见字符 + 首尾空白/斜杠，并顺手把"看起来像地址但没有协议头"
    的情况判出来（`_url_missing_scheme`），供调用方给出人话提示而不是让 requests 抛 InvalidSchema。"""
    u = strip_invisible(s)
    if isinstance(u, str):
        u = u.rstrip("/")
    return u


def url_missing_scheme(s):
    """True = 清理后仍不像 http(s) 地址（缺协议头是最常见的一种）。
    空串不算（由调用方按"未配置"处理）。"""
    u = strip_invisible(s)
    if not u:
        return False
    return not u.lower().startswith(("http://", "https://"))

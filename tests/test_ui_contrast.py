# -*- coding: utf-8 -*-
"""tests/test_ui_contrast.py — UI 对比度域回归锁合集（v1.0.646 按域归并）。

两条锁共用同一套 WCAG 2.1 相对亮度计算（原本各写一份，合并后收敛到底座的
`_rgb` / `_lum` / `_ratio_rgb` / `_ratio_hex` / `_mix`），守的都是同一类失效：
**通用令牌扫描器看不见的那一层**。

| 原文件                        | 版本出处  | 守护对象                         | 用例数 |
|-------------------------------|-----------|----------------------------------|--------|
| test_v614_err_contrast.py     | v1.0.614  | 报错态对比度 + 扫描器两处盲区    |   6    |
| test_v618_judge_tag.py        | v1.0.618  | 「判断模型」徽章 tint 底对比度   |   4    |

共同形态：通用令牌测试只把前景色与 `--bg/--panel/--panel2` 比，而真实渲染时
前景压在**混出来的 tint 底**上（judge 的 `color-mix(ink 14%)`）或**只在异常时才
进 DOM 的节点**上（.msg.err）。两边都是"扫描器全绿、用户看得清清楚楚不达标"。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------- WCAG 2.1 底座
def _rgb(h):
    """十六进制色 → (r,g,b) 元组（支持 3/6 位）。"""
    h = str(h).strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb):
    """WCAG 2.1 相对亮度（输入 (r,g,b)）。"""
    def f(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio_rgb(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _ratio_hex(a, b):
    return _ratio_rgb(_rgb(a), _rgb(b))


def _mix(fg, bg, p):
    """color-mix(in srgb, fg p%, transparent) 叠在 bg 上的实际颜色。"""
    return tuple(round(f * p + b * (1 - p)) for f, b in zip(_rgb(fg), _rgb(bg)))


def _css():
    return io.open(os.path.join(ROOT, "css", "style.css"), encoding="utf-8").read()


def _strip_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


# ============================================================ v1.0.614 报错态
class TestErrorStateContrast:
    """报错态对比度 + 扫描器两处盲区（v1.0.614，审查报告 ⑳㉑）。

    实测数字（WCAG 2.1 相对亮度公式，色值直接从 css/style.css 的令牌定义里解析）：
      · `.msg.err` 深色：改前 **#ff8a80 on #fff5f4 = 2.13:1** → 改后 on #2a1618 = **7.50:1**
      · 模型探测错误块（js/07 内联 #fff1f0/#ffccc7）深色：改前 **2.08:1** → 改后走 .err-note 同 7.50:1
      浅色侧本来就在 4.6~4.8，问题全在深色——浅底是硬编码的，文字却随主题翻亮。

    为什么扫描器一直没抓到：① 它把祖先 opacity 只合成进底色、**没合成进前景**；
    ② `.msg.err` 这类节点只在真报错时才进 DOM，正常遍历 13 个视图时根本采不到。
    """

    def _err_tokens(self, dark=False):
        """从 css 里取 --err-bg/--err-fg 的两个主题值。
        顺序即主题顺序：第一处是 :root（浅色），第二处是 body.dark（深色）——
        按选择器切片会被注释里的 "body.dark" 骗到（第一版就是这么错的）。"""
        out = {}
        css = _css()
        for name in ("--err-bg", "--err-fg"):
            vals = re.findall(re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{3,6})", css)
            out[name] = vals[1 if dark else 0] if len(vals) > (1 if dark else 0) else None
        return out

    def test_error_bubble_uses_tokens_not_hex(self):
        m = re.search(r"\.msg\.err\s*\{([^}]*)\}", _css())
        assert m, ".msg.err 规则找不到了"
        body = m.group(1)
        assert "#" not in body, "报错气泡又回到硬编码色值：%s" % body.strip()[:120]
        for t in ("--err-bg", "--err-bd", "--err-fg"):
            assert "var(%s)" % t in body, "缺令牌 %s：%s" % (t, body.strip()[:120])

    def test_error_bubble_contrast_passes_in_both_themes(self):
        for dark in (False, True):
            t = self._err_tokens(dark)
            assert t["--err-bg"] and t["--err-fg"], t
            r = _ratio_hex(t["--err-fg"], t["--err-bg"])
            assert r >= 4.5, "%s主题 .msg.err 只有 %.2f:1" % ("深色" if dark else "浅色", r)

    def test_dark_theme_actually_differs_from_light(self):
        """防"深浅两套其实同值"——那等于深色没做，前两条断言会假绿。"""
        a, b = self._err_tokens(False), self._err_tokens(True)
        assert a["--err-bg"] != b["--err-bg"], "深浅底色一样，说明深色覆盖丢了"

    def test_js_probe_panel_no_longer_inlines_error_surface(self):
        j = io.open(os.path.join(ROOT, "js", "07-usage-models.js"), encoding="utf-8").read()
        assert "#fff1f0" not in j and "#ffccc7" not in j, "内联浅底又回来了"
        assert 'class="err-note"' in j
        assert ".err-note" in _css(), "js 在用 .err-note，样式里没有这个类"

    def test_scanner_composites_opacity_into_foreground(self):
        """扫描器盲区之一：祖先 opacity 必须合成进前景，否则弱化文字全部漏检。"""
        s = io.open(os.path.join(ROOT, "tools", "contrast_scan.py"), encoding="utf-8").read()
        i = s.find("fgRaw = parse(c.color)")
        seg = s[max(0, i - 260):i + 700]
        assert "parseFloat(getComputedStyle(q).opacity)" in seg, "前景没乘祖先 opacity"
        assert re.search(r"fgRaw\w*\[3\]\s*\*\s*op", seg), "算出来的 opacity 没用到前景上"

    def test_scanner_can_measure_error_states(self):
        """扫描器盲区之二：异常态节点正常遍历时 DOM 里没有，要能合成出来测。"""
        s = io.open(os.path.join(ROOT, "tools", "contrast_scan.py"), encoding="utf-8").read()
        assert "--states" in s and "SYNTH_INJECT" in s
        for cls in ("msg err", "pill err", "pp-err", "st-err"):
            assert cls in s, "合成样本没覆盖 .%s" % cls
        assert "SYNTH_REMOVE" in s, "注入后没有摘除，会污染后续视图采样"


# ============================================================ v1.0.618 judge 徽章
class TestJudgeBadgeContrast:
    """「判断模型」徽章的对比度锁（v1.0.618，正式包扫描器抓出来的那一条）。

    病：`.tag.judge` 原先把 `#4f46e5`/`#6366f1` 写死在规则里。文字压在
    `color-mix(ink 14%)` 混出来的**淡紫底**上，浅档实测 **3.61:1**、深档 **3.37:1**，
    两边都不达 AA——而通用令牌测试只把 fg 与 `--bg/--panel/--panel2` 比，
    **看不见"背景是半透明色混出来的 tint"这一层**，所以一直绿。这里补的就是这一层。
    """

    def _tokens(self, block_re):
        m = re.search(block_re, _strip_comments(_css()), re.S)
        assert m, "找不到令牌块：%s" % block_re
        return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1)))

    def _css_rule(self, sel):
        m = re.search(r"([^\n]*%s\s*\{[^}]*\})" % re.escape(sel), _css())
        return m.group(1) if m else ""

    def test_judge_rule_has_no_bare_hex(self):
        """三条 judge 规则里不许再出现写死的十六进制（注释里的历史值不算）。"""
        for sel in (".tag.judge", "tr.judge-row td", "tr.judge-row td:first-child"):
            rule = self._css_rule(sel)
            assert rule, "找不到规则 %s（改名了？那这把锁要跟着改）" % sel
            body = _strip_comments(rule)
            bad = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
            assert not bad, "%s 里还有硬编码色 %s（深色模式一定翻不了亮）" % (sel, bad)
            assert "var(--judge-" in body, "%s 没吃 judge 令牌" % sel

    def test_judge_tokens_defined_in_both_themes(self):
        light = self._tokens(r":root\s*\{(.*?)\n\s*\}")
        dark = self._tokens(r"body\.dark\s*\{(.*?)\n\s*\}")
        for name, v in (("light", light), ("dark", dark)):
            for tok in ("--judge-ink", "--judge-fg"):
                assert v.get(tok), "%s 主题缺 %s" % (name, tok)
        assert light["--judge-fg"].lower() != dark["--judge-fg"].lower(), \
            "深浅两档 fg 一模一样 = 深色下必然不够亮"

    def test_judge_fg_meets_aa_on_its_real_tinted_background(self):
        """关键一条：fg 要压得住 **它自己那层 14% 混合底**，不是面板色。"""
        cases = []
        for tok_block, surf_tok, label in (
                (r":root\s*\{(.*?)\n\s*\}", "--panel", "light"),
                (r"body\.dark\s*\{(.*?)\n\s*\}", "--panel", "dark")):
            v = self._tokens(tok_block)
            if surf_tok not in v:
                base = self._tokens(r":root\s*\{(.*?)\n\s*\}")
                v = dict(base, **v)
            cases.append((label, v))
        for label, v in cases:
            bg = v["--panel"] if v["--panel"].startswith("#") else self._tokens(r":root\s*\{(.*?)\n\s*\}")["--panel"]
            ink = v["--judge-ink"]
            tint = _mix(ink, bg, 0.14)                    # .tag.judge 的实际底
            r_tint = _ratio_rgb(_rgb(v["--judge-fg"]), tint)
            r_panel = _ratio_rgb(_rgb(v["--judge-fg"]), _rgb(bg))
            assert r_tint >= 4.5, "%s 主题 judge 字压在淡紫底上只有 %.2f:1" % (label, r_tint)
            assert r_panel >= 4.5, "%s 主题 judge 字压在面板上只有 %.2f:1" % (label, r_panel)

    def test_no_other_bare_indigo_left(self):
        """全 CSS 里 #4f46e5/#6366f1 只允许活在注释里（本项目注释会记录被替换掉的旧值）。"""
        code = _strip_comments(_css())
        left = [h for h in ("#4f46e5", "#6366f1") if re.search(r"\b%s\b" % h, code, re.I)]
        assert not left, "还有硬编码靛蓝在代码行里：%s" % left

# -*- coding: utf-8 -*-
"""agent.loop_guard — 主循环死循环熔断状态机（v1.0.345 自 loop.py 内联状态抽出）。

三路独立的「工具名+参数签名」连续重复检测：
  写循环  同一写操作（点击/输入/滚动）连续成功重复           warn=4   kill=8
  读循环  同一读操作（读页面/读字段）连续成功重复             warn=5   kill=9
  错循环  同一写操作连续报错仍盲目重试                        warn=6   kill=10
导航白名单：「上一题/下一题」类翻题点击用独立更高阈值（warn=10 kill=20）——
合法翻题（改答案回退连点数次）曾被 4 次阈值误杀（2026-09-01 实测，v1.0.247）。

计数复位语义（与 v1.0.344 及之前 loop.py 内联实现逐位一致）：
  · 任何写操作（无论成败）复位读循环计数（"点下一题→读→点下一题→读"是正常推进）
  · 写操作报错 → 错循环计数累计、成功写循环计数复位（偶发 error 重试不误杀）
  · 写操作成功 → 错循环计数复位
  · 读操作报错 → 读循环计数复位（CDP 抖动属合理重试）
  · 同一签名【连续】出现才累计——中间穿插任何其它工具调用即复位对应计数

loop.py 职责边界：本类只做【判定】（返回 ok/warn/kill + 连续次数），干预消息构造、
上下文回退手术、judge 升级、SSE 事件仍由 loop.py 执行——保持行为逐位不变的前提下
让状态机可被单测覆盖（tests/test_loop_guard.py）。
"""
import json

# 阈值单一来源：沿用 session.py 的实证常量（避免双处维护漂移）
from .session import (
    LOOP_WARN_AT, LOOP_KILL_AT, LOOP_READ_WARN_AT, LOOP_READ_KILL_AT,
    LOOP_ERR_WARN_AT, LOOP_ERR_KILL_AT, LOOP_NAV_WARN_AT, LOOP_NAV_KILL_AT,
)

DEFAULT_THRESHOLDS = {
    "warn": LOOP_WARN_AT, "kill": LOOP_KILL_AT,
    "read_warn": LOOP_READ_WARN_AT, "read_kill": LOOP_READ_KILL_AT,
    "err_warn": LOOP_ERR_WARN_AT, "err_kill": LOOP_ERR_KILL_AT,
    "nav_warn": LOOP_NAV_WARN_AT, "nav_kill": LOOP_NAV_KILL_AT,
    # v1.0.515（审计 P1-7）：A/B 交替循环阈值——"click X(成功但无效) → read Y →
    # click X → read Y" 每路 streak 恒为 1，三路同签名检测全部失明。交替计数
    # 按"完成一次 [A,B] 循环"累计（每次调用 +1），warn=6 ≈ 循环 8 次，kill=10 ≈ 循环 12 次
    "alt_warn": 6, "alt_kill": 10,
}
_NAV_TARGET_KEYS = ("上一题", "下一题", "上一道", "下一道")
_READ_PREFIXES = ("capture_", "list_", "read_", "search_", "query_", "get_")

# v1.0.470（P1-4）：不带读类前缀的只读工具显式清单——此前 Ask 模式拦截判定
# 与并行预取白名单（_PARALLEL_SAFE_TOOLS）两套口径互相打架：web_search/fetch_webpage
# 等纯只读工具因前缀不命中被误拦，而拦截结果又被 prefetch 无条件覆盖，形同虚设。
# 现统一以本函数为单一事实来源（LoopGuard 读循环检测同样适用——它们确实只读）。
_EXTRA_READ_TOOLS = frozenset({
    "web_search", "fetch_webpage", "run_rule_check",
    "pc_screenshot", "pc_system_info", "pc_clipboard",
    "dsh_status", "ask_judge", "ask_user",
})


def is_read_tool(name):
    """读类/查询/收尾工具判定（与 loop.py 原内联判定一致）。
    v1.0.470：并入 _EXTRA_READ_TOOLS，Ask 拦截/并行预取/熔断三方共用本判定。"""
    return name.startswith(_READ_PREFIXES) or name == "finish" or name in _EXTRA_READ_TOOLS


def make_sig(name, args):
    """工具调用签名（工具名+排序后参数 JSON）——循环检测的最小单元。"""
    return name + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True)


def is_nav_click(name, args):
    """「上一题/下一题」类合法翻题导航点击（v1.0.247 白名单）。"""
    tgt = args.get("target", "") if isinstance(args, dict) else ""
    return name == "click_wecom_element" and any(k in str(tgt) for k in _NAV_TARGET_KEYS)


class LoopGuard:
    """三路熔断状态机。on_write/on_read 返回 (action, streak)：
    action ∈ "ok" | "warn" | "kill"；warn 仅首次触发（给模型一次重新决策机会），
    warned 置位后同路只报 kill——与原内联实现一致。"""

    def __init__(self, thresholds=None):
        self.thr = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self.thr.update(thresholds)
        self.reset()

    def reset(self):
        self.write_sig = None
        self.write_streak = 0
        self.write_warned = False
        self.read_sig = None
        self.read_streak = 0
        self.read_warned = False
        self.err_sig = None
        self.err_streak = 0
        self.err_warned = False
        # v1.0.515（审计 P1-7）：A/B 交替循环检测状态
        self._recent = []          # 最近若干工具签名（不分读写类）
        self.alt_streak = 0        # 连续完成 [A,B,A,B] 循环的次数
        self.alt_warned = False

    def _track_alt(self, sig):
        """交替循环检测（v1.0.515）：维护最近签名窗口，出现 [A,B,A,B]（A≠B）模式
        即累计 alt_streak，模式打破（出现第三个不同签名/纯重复）即清零。
        返回 ("warn"/"kill", streak) 或 None——类内检测（写/读/错循环）优先级更高，
        仅当类内判定为 ok 且交替触发阈值时才由调用方采用本结果。"""
        self._recent.append(sig)
        if len(self._recent) > 8:
            self._recent.pop(0)
        if len(self._recent) >= 4:
            a, b, c, d = self._recent[-4:]
            if a == c and b == d and a != b:
                self.alt_streak += 1
                if self.alt_warned and self.alt_streak >= self.thr["alt_kill"]:
                    return ("kill", self.alt_streak)
                if not self.alt_warned and self.alt_streak >= self.thr["alt_warn"]:
                    self.alt_warned = True
                    return ("warn", self.alt_streak)
                return ("ok", self.alt_streak)
        self.alt_streak = 0
        return ("ok", 0)

    # ---- 写工具（点击/输入/滚动/上传/关页等） ----
    def on_write(self, name, args, is_err):
        sig = make_sig(name, args)
        # v1.0.515：交替循环检测先跑（窗口不分读写类；类内 warn/kill 优先级更高）
        alt_act = self._track_alt(sig)
        # 任何写操作都打断读循环（页面上的实际行动=状态已可能变化）
        self.read_sig = None
        self.read_streak = 0
        if is_err:
            # 错循环：同一写操作持续报错仍重试（读操作不重置错计数——read 后仍点同一
            # error 元素说明 read 没帮它脱困，继续累计；P1-5 实测连点 60+ 次靠此拦截）
            if sig == self.err_sig:
                self.err_streak += 1
            else:
                self.err_sig = sig
                self.err_streak = 1
            if self.err_warned and self.err_streak >= self.thr["err_kill"]:
                return ("kill", self.err_streak)
            if not self.err_warned and self.err_streak >= self.thr["err_warn"]:
                self.err_warned = True
                return ("warn", self.err_streak)
            # 偶发 error 重试：不累计成功写循环（P1-3 保留）
            self.write_sig = None
            self.write_streak = 0
            return alt_act if alt_act[0] != "ok" else ("ok", self.err_streak)
        # 成功写：错循环计数复位（模型已能推进）
        self.err_sig = None
        self.err_streak = 0
        if sig == self.write_sig:
            self.write_streak += 1
        else:
            self.write_sig = sig
            self.write_streak = 1
        nav = is_nav_click(name, args)
        warn_at = self.thr["nav_warn"] if nav else self.thr["warn"]
        kill_at = self.thr["nav_kill"] if nav else self.thr["kill"]
        if self.write_warned and self.write_streak >= kill_at:
            return ("kill", self.write_streak)
        if not self.write_warned and self.write_streak >= warn_at:
            self.write_warned = True
            return ("warn", self.write_streak)
        return alt_act if alt_act[0] != "ok" else ("ok", self.write_streak)

    # ---- 读工具（读页面/读字段/列元素/搜索等；finish 由 loop.py 自行 continue，不经此处） ----
    def on_read(self, name, args, is_err):
        sig = make_sig(name, args)
        # v1.0.515：交替循环检测先跑
        alt_act = self._track_alt(sig)
        # 读操作复位写循环计数（"读→点击→读"是正常推进）
        self.write_sig = None
        self.write_streak = 0
        if is_err:
            # 读工具报错（CDP 抖动/页面未就绪）→ 合理重试，复位读计数
            self.read_sig = None
            self.read_streak = 0
            return alt_act if alt_act[0] != "ok" else ("ok", 0)
        sig2 = sig
        if sig2 == self.read_sig:
            self.read_streak += 1
        else:
            self.read_sig = sig2
            self.read_streak = 1
        if self.read_warned and self.read_streak >= self.thr["read_kill"]:
            return ("kill", self.read_streak)
        if not self.read_warned and self.read_streak >= self.thr["read_warn"]:
            self.read_warned = True
            return ("warn", self.read_streak)
        return alt_act if alt_act[0] != "ok" else ("ok", self.read_streak)

    # ---- finish：复位全部（收尾工具不参与任何循环检测） ----
    def on_finish(self):
        self.write_sig = None
        self.write_streak = 0
        self.read_sig = None
        self.read_streak = 0
        # v1.0.361 审查修复（O5）：补复位 err_*——原注释称"复位全部"实际漏了三项，
        # finish 被拒后继续执行时 err 状态残留，会让无关写操作提前触发 warn/kill。
        self.err_sig = None
        self.err_streak = 0
        self.err_warned = False
        # v1.0.515（P1-7）：交替循环状态一并复位
        self._recent = []
        self.alt_streak = 0
        self.alt_warned = False

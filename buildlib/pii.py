# -*- coding: utf-8 -*-
"""buildlib.pii — dist 内 PII 分发风险扫描（自 build.py 逐字迁入）。"""
import os
import re as _re

from buildlib.env import BASE


def scan_dist_pii():
    """v1.0.510 优化（P0-1）：打包后扫描 dist 内的个人信息（PII）分发风险。
    背景：DIST_PRESERVE 为「用户升级不丢配置」刻意保留 config.json/产物库——这对
    本机自用是正确的，但意味着 dist 整目录拷给他人 = 把对话记录/模型备份里可能的
    银行账号等 PII 一起发出去（2026-10-08 审计实测：dist/产物库/对话记录 存在含
    完整银行卡号的对话存档）。--release 发布模式（build.py --release）会剥离这些
    数据，但它是 opt-in——本扫描作为兜底提醒：每次打包后检查 dist 产物库/与根目录
    备份文件里是否有 16-19 位疑似银行卡号，命中即显著告警（不拦截打包——本机
    自用含自己的数据是预期行为）。"""
    dist = os.path.join(BASE, "dist")
    if not os.path.isdir(dist):
        return
    # 16-19 位数字串（银行卡 16/19 位主流；14 位时间戳、13 位毫秒时间戳不命中）
    card_re = _re.compile(r"(?<!\d)\d{16,19}(?!\d)")
    targets = []
    # v1.0.652（审计 P0-5→P0-6）：改 os.walk 全量递归——旧实现只扫 3 个目录的**一层**，
    # 漏掉 产物库/会话/（对话正文主要存放处）与所有子目录，命中 0 还打"未发现 ✓"，
    # 假绿灯会强化"直接把 dist 整目录发出去"的错误行为（正是本函数 docstring 自述
    # 事故的复发面）。现在：dist/产物库/ 整棵树递归 + dist 根的模型配置备份。
    _scan_root = os.path.join(dist, "产物库")
    if os.path.isdir(_scan_root):
        for _r, _dirs, _files in os.walk(_scan_root):
            for fn in _files:
                if fn.lower().endswith((".md", ".json", ".txt", ".jsonl", ".csv")):
                    targets.append(os.path.join(_r, fn))
    for fn in os.listdir(dist):
        if fn.startswith("模型配置备份") and fn.endswith(".json"):
            targets.append(os.path.join(dist, fn))
    hits = []
    scanned = 0
    for p in targets:
        try:
            if os.path.getsize(p) > 20 * 1048576:
                continue
            with open(p, encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            scanned += 1
        except OSError:
            continue
        m = card_re.search(txt)
        if m:
            hits.append((os.path.relpath(p, dist), m.group(0)[:4] + "****" + m.group(0)[-4:]))
    # 模型配置备份含明文 api_key（v1.0.510：与卡号告警并列提示，不重复扫卡号）
    model_backups = [fn for fn in os.listdir(dist)
                     if fn.startswith("模型配置备份") and fn.endswith(".json")]
    if hits or model_backups:
        print("[build] " + "=" * 62)
    if hits:
        print("[build] ⚠️ PII 分发风险提醒：dist 内检测到疑似银行卡号明文：")
        for rel, masked in hits[:10]:
            print(f"[build]   - dist/{rel}（{masked}）")
        more = len(hits) - 10
        if more > 0:
            print(f"[build]   … 及另外 {more} 个文件")
    for fn in model_backups:
        print(f"[build] ⚠️ 分发风险提醒：dist/{fn} 含明文 api_key（导出备份），勿随 dist 分发")
    if hits or model_backups:
        print("[build] 本机自用无碍；但【不要直接把 dist 目录发给别人】——")
        print("[build] 分发前请运行: python build.py --release （自动剥离 config/产物库/日志）")
        print("[build] " + "=" * 62)
    else:
        # v1.0.652：文案去绝对化——只说"扫描范围内未命中"并报扫描量，不再打
        # 无条件的"未发现 ✓"（旧版漏扫 会话/ 时这个 ✓ 就是假绿灯）。
        print(f"[check] dist PII 扫描：已扫描 {scanned} 个文本文件（产物库/ 全树递归 + 模型配置备份）"
              f"，范围内未命中疑似银行卡号")

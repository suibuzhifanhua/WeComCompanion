"""
rule_check.py — 企微伴侣 · 脚本判定引擎（v1.0.94 新增，v1.0.95 全模块接入）
==========================================================
设计原则：机械判定不耗 LLM。
  - 判定型/机械型模块的规则是"确定性规则"（精确/包含/非空/存在性/统计/分类），
    全部由本模块脚本判定，<1ms、零成本、100% 可复现。
  - 输入统一为 data 结构（各模块自行消费）：
      finance-check:  read_wecom_fields 的 fields 数组
      finance-audit:  列表行 rows 或列表页正文 body
      data-organizer / data-analysis:  headers + rows 二维表
      file-organizer:  files 文件清单
      reimburse-filler: fields 表单字段名
  - 输出统一为结构化判定结果：
      {"ok": True, "module": "...", "result": "green"|"red"|"ok",
       "verdict": "...", "checks": [逐项], "fails": [...], ...模块专属字段, "reason": "摘要"}
  - 新增模块：在 RULES 注册表加一个判定函数（输入 data，输出上述结构）即可。
  - 模型优先模块（不注册本引擎）：exam-solver 答题答案判断主观，保持模型判定；
    其余理解型模块（process-analyzer/doc-review/oa-approval/meeting-minutes/
    periodic-report/writing-draft/info-research）判定依赖语义理解，保持模型判定。
"""
import re
from collections import Counter

# ---------- 字段值提取工具 ----------
_NEG_WORDS = ("无", "未填写", "暂无", "空白", "为空", "没有", "不适用")


# 单据编号格式（v1.0.650 F6 提到模块级）：列表页筛「已付款单据」与新的财务脚本
# 流水线认「哪条列表链接算一张单据」共用这一条口径，两处各写一份正则必漂。
DOC_NO_RE = re.compile(r'BX\w{4}-\w+|[A-Z]{2}\d{4}-\d+')


def _field_map(data):
    """把 read_wecom_fields 的 fields 数组转成 {key: field}，便于按关键词取片段。
    也兼容扁平 dict 输入（agent 有时会直接传 {"支出类别": "专管费用", ...}）。"""
    # 先尝试从 fields 数组构造
    fields = (data or {}).get("fields") or []
    fm = {f.get("key", ""): f for f in fields if isinstance(f, dict)}
    # 兼容：如果 data 本身就是扁平 dict（key=value），直接合并进去
    if not fm and isinstance(data, dict):
        fm = {k: {"value": str(v), "snippets": [str(v)], "count": 1}
              for k, v in data.items()
              if k not in ("ok", "module", "result", "verdict", "checks", "fails",
                           "reason", "hits", "evidence", "title", "url",
                           "body_len", "waited", "retries", "body")}
    # 兜底：fields 数组中元素可能有 nested "value" 字段（非标准但出现过）
    if not fm:
        return fm
    return fm


def _extract_val(snippet, kw):
    """从片段中提取关键词后的字段值（兼容 冒号/空格/无分隔 三种写法）。"""
    if not snippet:
        return ""
    i = snippet.find(kw)
    if i < 0:
        return ""
    tail = snippet[i + len(kw):].lstrip()
    if tail.startswith(("：", ":")):
        tail = tail[1:].lstrip()
    # 取到下一个明显分隔符为止
    m = re.match(r'([^\n\r；;，,。]*?)(?:\s*$)', tail)
    val = m.group(1).strip() if m and m.group(1) else tail.strip()
    return val.strip(" \t\n\r：:，,。;；")


def _pick_attachments(field):
    """从附件字段片段中提取附件文件名（常见办公/图片/压缩格式）。"""
    if not field:
        return []
    names = []
    for snip in field.get("snippets") or []:
        for m in re.finditer(
                r'([\w\u4e00-\u9fa5][\w\u4e00-\u9fa5\-\.]*\.(?:pdf|docx?|xlsx?|pptx?|jpg|jpeg|png|ofd|zip|rar|wps))',
                snip, re.I):
            n = m.group(1).strip()
            if n and n not in names:
                names.append(n)
    return names


def _mk(item, expect, actual, ok, note=""):
    return {"item": item, "expect": expect, "actual": actual or "（未取到）", "pass": ok, "note": note}


# ---------- 各模块判定函数 ----------
def check_finance(data):
    """财务审核规则（逐张读取 → 脚本判定 → 红绿报告）：
      - 专管部门 = 信息技术部
      - 明细项目 = 非空
      - 业务类型 = 费用类付款（电子）
      - 支出类别 = 专管费用
      - 预算执行单（PMP项目）= 非空
      - 有附件（记录附件名称）
      全部符合 → green，任一不符 → red。"""
    fm = _field_map(data)
    checks = []

    # 规则 1：专管部门 = 信息技术部
    f = fm.get("专管部门")
    snip = (f.get("snippets") or [""])[0] if f else ""
    actual = _extract_val(snip, "专管部门") if f else ""
    if not actual and "信息技术部" in snip:
        actual = "信息技术部"
    ok = bool(actual) and ("信息技术部" in actual)
    checks.append(_mk("专管部门", "信息技术部", actual, ok,
                      "" if ok else ("未读到该字段" if (not f or f.get("count", 0) == 0) else f"实际值：{actual}")))

    # 规则 2：明细项目 = 非空
    f = fm.get("明细项目")
    if f and f.get("count", 0) > 0:
        snip = (f.get("snippets") or [""])[0]
        actual = _extract_val(snip, "明细项目")
        empty = (not actual) or any(w in actual for w in _NEG_WORDS) or len(actual.replace(" ", "")) < 2
        if empty and snip and len(snip.replace(" ", "")) > 10:
            empty = False
            actual = actual or "有内容"
        checks.append(_mk("明细项目", "有内容", actual, not empty,
                          "" if not empty else ("内容为空" if not actual else "疑似空内容")))
    else:
        checks.append(_mk("明细项目", "有内容", "（未读到）", False, "页面无该字段/未填写"))

    # 规则 3：业务类型 = 费用类付款（电子）
    f = fm.get("业务类型")
    snip = (f.get("snippets") or [""])[0] if f else ""
    actual = _extract_val(snip, "业务类型") if f else ""
    # v1.0.652（审计 P0-5）：旧判定 `("电子" in actual)` 是子串匹配——"费用类付款
    # （非电子类）"含子串"电子"被判符合（假绿），而报销默认值表里恰好就填这个值。
    # 改否定词优先：非电子/纸质/线下 任一命中即不符合（明确的互斥形态）。
    _biz_neg = ("非电子", "不是电子", "纸质", "线下")
    if not actual and "费用类付款" in snip and "电子" in snip and not any(w in snip for w in _biz_neg):
        actual = "费用类付款（电子）"
    ok = (bool(actual) and ("费用类付款" in actual) and ("电子" in actual)
          and not any(w in actual for w in _biz_neg))
    checks.append(_mk("业务类型", "费用类付款（电子）", actual, ok,
                      "" if ok else ("未读到该字段" if (not f or f.get("count", 0) == 0) else f"实际值：{actual}")))

    # 规则 4：支出类别 = 专管费用
    f = fm.get("支出类别")
    snip = (f.get("snippets") or [""])[0] if f else ""
    actual = _extract_val(snip, "支出类别") if f else ""
    if not actual and "专管费用" in snip:
        actual = "专管费用"
    ok = bool(actual) and ("专管费用" in actual)
    checks.append(_mk("支出类别", "专管费用", actual, ok,
                      "" if ok else ("未读到该字段" if (not f or f.get("count", 0) == 0) else f"实际值：{actual}")))

    # 规则 5：预算执行单（PMP项目）非空
    f = fm.get("预算执行单") or fm.get("PMP项目")
    if f and f.get("count", 0) > 0:
        snip = (f.get("snippets") or [""])[0]
        kw = "预算执行单" if fm.get("预算执行单") else "PMP项目"
        actual = _extract_val(snip, kw)
        empty = (not actual) or any(w in actual for w in _NEG_WORDS) or len(actual.replace(" ", "")) < 2
        if empty and snip and len(snip.replace(" ", "")) > len(kw) + 2:
            empty = False
            actual = actual or "有内容"
        checks.append(_mk(kw, "有内容", actual, not empty,
                          "" if not empty else ("内容为空" if not actual else "疑似空内容")))
    else:
        checks.append(_mk("预算执行单/PMP项目", "有内容", "（未读到）", False, "页面无该字段/未填写"))

    # 规则 6：有附件（记录附件名称）
    f = fm.get("附件") or fm.get("附件名称")
    atts = _pick_attachments(f) if f else []
    has_att = bool(f) and f.get("count", 0) > 0
    checks.append(_mk("附件", "至少 1 个", "、".join(atts) if atts else ("有附件" if has_att else "无附件"),
                      has_att, "" if has_att else "无附件"))

    fails = [c["item"] for c in checks if not c["pass"]]
    green = not fails

    # 全部目标字段都未读到 → 疑似详情页未加载完成
    target_kws = ("专管部门", "明细项目", "业务类型", "支出类别", "预算执行单", "PMP项目", "附件")
    all_miss = all((fm.get(k) is None or fm.get(k).get("count", 0) == 0)
                   for k in target_kws)
    reason = ("全部符合，标绿" if green else
              "不符合：" + "、".join(fails))
    if all_miss:
        reason = "⚠️ 全部字段未读到，疑似详情页尚未加载完成，请先确认页面已加载后重读"
    return {
        "ok": True,
        "module": "finance-check",
        "result": "green" if green else "red",
        "verdict": "符合" if green else "不符合",
        "checks": checks,
        "fails": fails,
        "attachments": atts,
        "all_fields_missed": all_miss,
        "field_values": {c["item"]: c["actual"] for c in checks},
        "reason": reason,
    }


# ---------- 脚本优先模块（机械处理不耗 LLM，模型只做业务解读/兜底） ----------
def check_finance_audit(data):
    """财务单据核查：列表页筛「已付款」单据（确定性规则）。
    输入 data：
      rows: [{单据编号/编号, 单据状态/状态, ...}, ...]（优先，结构化行）
      body: 列表页正文文本（无 rows 时正则解析表格行）
    输出：已付款/其他清单 + 状态分布统计。"""
    d = data or {}
    rows = d.get("rows") or []
    body = d.get("body") or ""
    docs = []
    if rows:
        for r in rows:
            if not isinstance(r, dict):
                continue
            no = r.get("单据编号") or r.get("编号") or r.get("单号") or r.get("doc_id") or ""
            st = r.get("单据状态") or r.get("状态") or r.get("status") or ""
            if no:
                docs.append((str(no).strip(), str(st).strip()))
    elif body:
        for m in DOC_NO_RE.finditer(body):
            no = m.group(0)
            line = body[m.end():m.end() + 80].splitlines()
            seg = line[0] if line else ""
            st = "已付款" if ("已付款" in seg or "已付" in seg) else "其他"
            docs.append((no, st))
    seen, uniq = set(), []
    for no, st in docs:
        if no not in seen:
            seen.add(no)
            uniq.append((no, st))
    paid = [no for no, st in uniq if "已付款" in st or "已付" in st]
    other = [no for no, st in uniq if "已付款" not in st and "已付" not in st]
    dist = Counter(st for _, st in uniq)
    checks = [
        _mk("单据状态筛选", "只保留已付款", f"共 {len(uniq)} 张，已付款 {len(paid)} 张",
            bool(uniq), "" if uniq else "列表无单据"),
        _mk("单据清单去重", "编号唯一", f"{len(docs)} 条原始 → {len(uniq)} 张去重后",
            len(uniq) > 0, ""),
    ]
    fails = []
    # 金额一致性校验（可选）：data 传 total（应付款总额）+ items=[{金额}] 或 {amounts:[...]}
    _total = d.get("total")
    _amounts = d.get("amounts") or []
    if not _amounts and isinstance(d.get("items"), list):
        for it in d["items"]:
            if isinstance(it, dict):
                v = it.get("金额") or it.get("amount")
            else:
                v = it
            _amounts.append(v)
    if _amounts:
        nums = [_to_num(a) for a in _amounts]
        nums = [n for n in nums if n is not None]
        if nums:
            s = sum(nums)
            checks.append(_mk("单据金额合计", "已筛单据金额求和",
                              f"合计 {s:,.2f} 元（{len(nums)} 条）", True, ""))
            if _total is not None:
                t = _to_num(_total)
                if t is None:
                    checks.append(_mk("总额核对", "明细合计 vs 应付款总额",
                                      "无法解析应付款总额，需模型核对", True, ""))
                elif abs(s - t) > 0.01:
                    fails.append(f"金额不一致：单据合计 {s:,.2f} ≠ 应付款总额 {t:,.2f}（差 {abs(s - t):,.2f}）")
                    checks.append(_mk("总额核对", "明细合计 vs 应付款总额", f"不一致！{s:,.2f} vs {t:,.2f}", False,
                                      "可能漏单或金额填错，需复核"))
                else:
                    checks.append(_mk("总额核对", "明细合计 vs 应付款总额", f"一致 {s:,.2f} = {t:,.2f}", True, ""))
    return {
        "ok": True, "module": "finance-audit", "result": "red" if fails else "ok",
        "verdict": "金额核对不通过，需复核" if fails else "已完成列表筛选",
        "checks": checks, "fails": fails,
        "paid_docs": paid, "other_docs": other,
        "stats": {"total": len(uniq), "paid": len(paid), "unpaid": len(other),
                  "status_dist": dict(dist)},
        "reason": f"共 {len(uniq)} 张单据，已付款 {len(paid)} 张、其他 {len(other)} 张"
                  + ("" if paid else "（无已付款单据）")
                  + (f"；{'；'.join(fails)}" if fails else ""),
    }


def _to_num(v):
    """宽容转数值：去千分位/货币符号/单位后缀，转不了返回 None。"""
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.replace(",", "").replace("，", "").replace("¥", "").replace("￥", "").replace("元", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def check_data_organizer(data):
    """数据整理：去重 + 分类汇总（确定性规则）。
    输入：headers=[列名...], rows=[[...], ...], group_col=分类列名/序号（默认第 1 列）。
    输出：去重统计 + 分类分布。"""
    d = data or {}
    headers = d.get("headers") or []
    rows = d.get("rows") or []
    if not rows:
        return {"ok": True, "module": "data-organizer", "result": "no_data",
                "verdict": "无数据可整理", "checks": [], "fails": [], "reason": "未提供 rows 数据"}
    ncol = max(len(r) for r in rows)
    cols = list(headers) if len(headers) >= ncol else [str(i) for i in range(ncol)]
    seen, dup_rows = set(), []
    for r in rows:
        t = tuple(r)
        if t in seen:
            dup_rows.append(r)
        else:
            seen.add(t)
    deduped = [list(t) for t in seen]
    gc = d.get("group_col")
    gi = None
    if isinstance(gc, int):
        gi = gc if 0 <= gc < ncol else None
    elif isinstance(gc, str):
        gi = cols.index(gc) if gc in cols else (0 if cols else None)
    else:
        gi = 0
    gstat = Counter()
    for r in deduped:
        if gi is not None and 0 <= gi < len(r):
            gstat[str(r[gi])] += 1
    gname = cols[gi] if cols and gi is not None and gi < len(cols) else "第1列"
    checks = [
        _mk("去重", "无重复行", f"{len(rows)} 行 → 去重后 {len(deduped)} 行（剔除 {len(dup_rows)} 行重复）",
            True, ""),
        _mk("分类汇总", f"按列「{gname}」分类",
            "、".join(f"{k}({v})" for k, v in gstat.most_common(20)) or "（空）", bool(gstat), ""),
    ]
    return {
        "ok": True, "module": "data-organizer", "result": "ok",
        "verdict": "已完成去重与分类汇总",
        "checks": checks, "fails": [],
        "row_count": len(rows), "dedup_count": len(deduped),
        "duplicate_count": len(dup_rows), "duplicates": dup_rows[:50],
        "group_stats": dict(gstat),
        "reason": f"{len(rows)} 行去重后剩 {len(deduped)} 行，按「{gname}」分类 {len(gstat)} 类",
    }


def check_data_analysis(data):
    """数据报表分析：数值列统计 + 分类分布 + 异常识别（确定性规则）。"""
    d = data or {}
    headers = d.get("headers") or []
    rows = d.get("rows") or []
    if not rows:
        return {"ok": True, "module": "data-analysis", "result": "no_data",
                "verdict": "无数据", "checks": [], "fails": [], "reason": "未提供 rows"}
    ncol = max(len(r) for r in rows)
    cols = list(headers) if len(headers) >= ncol else [str(i) for i in range(ncol)]
    col_stats = []
    for ci, name in enumerate(cols):
        vals = [r[ci] for r in rows if ci < len(r)]
        nums = [v for v in (_to_num(x) for x in vals) if v is not None]
        missing = sum(1 for r in rows if ci >= len(r) or r[ci] in ("", None))
        if nums:
            mean = sum(nums) / len(nums)
            sd = (sum((x - mean) ** 2 for x in nums) / len(nums)) ** 0.5
            outliers = [x for x in nums if sd and abs(x - mean) > 3 * sd]
            col_stats.append({
                "column": name, "type": "numeric",
                "count": len(nums), "sum": round(sum(nums), 4),
                "mean": round(mean, 4), "min": min(nums), "max": max(nums),
                "missing": missing, "zeros": sum(1 for x in nums if x == 0),
                "negatives": sum(1 for x in nums if x < 0),
                "outliers": [round(x, 4) for x in outliers[:20]],
            })
        else:
            col_stats.append({
                "column": name, "type": "category",
                "count": len(vals), "missing": missing,
                "dist": dict(Counter(str(v) for v in vals).most_common(20)),
            })
    seen, dup = set(), 0
    for r in rows:
        t = tuple(r)
        if t in seen:
            dup += 1
        seen.add(t)
    num_cols = sum(1 for c in col_stats if c["type"] == "numeric")
    checks = [
        _mk("数值统计", "各数值列 和/均值/极值", f"{num_cols} 个数值列已统计", True, ""),
        _mk("异常识别", "缺失/零值/负值/离群", f"缺失 {sum(c['missing'] for c in col_stats)} 处、重复行 {dup} 行", True, ""),
    ]
    return {
        "ok": True, "module": "data-analysis", "result": "ok",
        "verdict": "已完成统计汇总与异常识别",
        "checks": checks, "fails": [],
        "row_count": len(rows), "columns": col_stats,
        "duplicate_rows": dup,
        "reason": f"{len(rows)} 行 {len(cols)} 列，数值列 {num_cols} 个已统计、分类列 {len(col_stats) - num_cols} 个已分布",
    }


def _file_type(name):
    n = str(name or "").lower()
    if n.endswith((".xlsx", ".xls", ".csv", ".et")):
        return "报表"
    if n.endswith((".pptx", ".ppt")):
        return "演示"
    if n.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")):
        return "图片"
    if n.endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
        return "压缩包"
    if n.endswith((".pdf", ".ofd")):
        return "发票" if ("发票" in n or "专票" in n or "普票" in n) else "文档"
    if n.endswith((".docx", ".doc", ".txt", ".md", ".wps")):
        return "文档"
    return "其他"


def check_file_organizer(data):
    """归档整理：按类型/日期归类统计 + 疑似重复检测（确定性规则）。
    输入：files=[{name, size, mtime, path}, ...]（name 必填，mtime/date 可选）。"""
    files = (data or {}).get("files") or []
    if not files:
        return {"ok": True, "module": "file-organizer", "result": "no_data",
                "verdict": "无文件", "checks": [], "fails": [], "reason": "未提供 files"}
    by_type = Counter(_file_type(f.get("name", "")) for f in files)
    by_month = Counter()
    for f in files:
        mt = f.get("mtime") or f.get("date") or ""
        if mt:
            s = str(mt)
            if len(s) >= 7:
                by_month[s[:7]] += 1
    dup_by_name = Counter(str(f.get("name", "")) for f in files)
    size_cnt = Counter()
    for f in files:
        sz = f.get("size")
        if isinstance(sz, (int, float)) and sz > 0:
            size_cnt[sz] += 1
    dup_list = []
    for n, c in dup_by_name.items():
        if c > 1 and n:
            dup_list.append({"kind": "同名", "key": n, "count": c})
    for sz, c in size_cnt.items():
        if c > 1:
            dup_list.append({"kind": "同大小", "key": f"{sz}B", "count": c})
    checks = [
        _mk("类型归类", "按扩展名分类",
            "、".join(f"{k}({v})" for k, v in by_type.most_common()) or "（空）", bool(by_type), ""),
        _mk("疑似重复", "同名/同大小", f"{len(dup_list)} 组", True, ""),
    ]
    return {
        "ok": True, "module": "file-organizer", "result": "ok",
        "verdict": "已完成归档分类统计",
        "checks": checks, "fails": [],
        "total": len(files), "by_type": dict(by_type),
        "by_month": dict(by_month), "duplicates": dup_list[:50],
        "reason": f"共 {len(files)} 个文件，分类 {len(by_type)} 类、疑似重复 {len(dup_list)} 组",
    }


def _load_reimburse_pii():
    """v1.0.470 修复（P0-4）：个人报销信息（姓名/费用承担公司/银行账号/开户行）从 config.json
    的 reimburse 段读取——原硬编码在源码里，agent 自动填报销单会带出，源码随
    导出/自修复报告/仓库泄露即 PII 事故。未配置时对应字段返回空 → 判定为
    「需用户确认」，由模型询问用户，绝不编造。
    v1.0.510 优化（P1-4）：config.json 的 reimburse 段已纳入 DPAPI 加密名单
    （security._SENSITIVE_KEY_HINTS 补 bank_account/bank_name），本函数经
    load_config 读到的已是解密后的运行期明文，落盘侧不再明文。"""
    try:
        from core.config_store import load_config
        r = (load_config() or {}).get("reimburse")
        return r if isinstance(r, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _mask_account(acc):
    """银行账号脱敏（P1-4）：保留前 4 后 4，中间 ****。过短（<=8 位）整体打码。"""
    acc = str(acc or "").strip()
    if not acc:
        return acc
    if len(acc) <= 8:
        return "****"
    return acc[:4] + "****" + acc[-4:]


def check_reimburse_filler(data):
    """报销单填写：给出表单字段的确定性默认值（用户常规报销场景）。
    输入：fields=[字段名...] 或 [{key, count, snippets}, ...]。
    可选增强：items=[{name, amount}, ...] 费用明细行 + total=总金额 → 做金额一致性校验
    （明细合计 vs 表单总金额，差异>0.01 判定 red，防止报销金额填错）。
    输出：字段 → 默认值映射；未识别字段标记 unknown 由模型兜底。"""
    d = data or {}
    fields = d.get("fields") or []
    if fields and isinstance(fields[0], dict):
        names = [f.get("key", "") for f in fields]
    else:
        names = [str(x) for x in fields]
    # 确定性默认值规则表（来自 reimburse-filler 技能）。
    # v1.0.470（P0-4）：姓名/银行账号/开户行等个人字段改从 config.reimburse 读取，
    # 源码只保留业务无关的通用默认值。
    _pii = _load_reimburse_pii()
    _pname = str(_pii.get("name") or "").strip()
    _company = str(_pii.get("company") or "").strip()   # v1.0.657：费用承担公司同样外置（公开发布匿名化）
    # v1.0.510 优化（P1-4）：银行账号属高敏 PII——默认原值下发（自动填报需要真实值，
    # 改动会打断现有填报流程，保持行为兼容），但工具结果会随 messages 发给 LLM 端点
    # （当前默认本地 Ollama，远程端点为明文 HTTP 时有泄露面）。用户可在 config.json
    # 设 reimburse.mask_for_llm=true，把银行账号脱敏后再进 LLM 上下文（由用户手输补全）。
    _acct = str(_pii.get("bank_account") or "").strip()
    _acct_note = "config reimburse.bank_account"
    try:
        from core.config_store import load_config as _lc
        if ((_lc() or {}).get("reimburse") or {}).get("mask_for_llm"):
            _acct_note = "已按 mask_for_llm 脱敏，请在表单手动输入完整账号"
            _acct = _mask_account(_acct)
    except Exception as e:  # noqa: BLE001
        # v1.0.648：这条静默的代价是**可能把未脱敏的银行账号喂给大模型**——
        # 读不到 mask_for_llm 时代码走的是"不脱敏"分支，却什么都不说。
        from core.logging import log_event
        log_event(f"[rule] reimburse.mask_for_llm 判定失败，账号按未脱敏处理：{e}")
    DEFAULTS = [
        ("报销分类", "报销", "报销场景选「报销」，对外付款选「付款」"),
        ("实报人职位", "公司员工", ""),
        ("实报日期", "今天", "用页面日期控件今天或输入 YYYY-MM-DD"),
        ("实报人", _pname, "用户本人（config reimburse.name）"),
        ("费用承担部门", "信息技术部", ""),
        ("费用承担公司", _company, "页面已有默认值则跳过（config reimburse.company）"),
        ("支出类别", "直接费用", ""),
        ("费用分摊", "不勾选", "默认不勾选"),
        ("业务类型", "费用类付款（非电子类）", ""),
        ("收款人类型", "个人", "收款人为用户本人"),
        ("收款人", _pname, "用户本人（config reimburse.name）"),
        ("银行账号", _acct, _acct_note),
        ("账户名称", _pname, "config reimburse.name"),
        ("开户银行", str(_pii.get("bank_name") or "").strip(), "config reimburse.bank_name"),
        ("支付方式", "电汇", ""),
    ]
    defaults = []
    for name in names:
        if not name:
            continue
        # v1.0.470：精确匹配优先，双向子串只做兜底——原实现"实报人"字段会先命中
        # 规则表里靠前的"实报人职位"（"实报人" in "实报人职位"），被填成"公司员工"。
        hit = None
        for kw, val, note in DEFAULTS:
            if name == kw:
                hit = (kw, val, note)
                break
        if not hit:
            for kw, val, note in DEFAULTS:
                if kw in name or name in kw:
                    hit = (kw, val, note)
                    break
        if hit:
            kw, val, note = hit
            if val:
                defaults.append({"field": name, "value": val, "rule": kw, "note": note})
            else:
                # 个人信息未配置（或通用默认值为空）→ 明确交给用户确认，不产空值
                defaults.append({"field": name, "value": None, "rule": "",
                                 "note": f"「{kw}」未在设置（config.reimburse）中配置，需用户确认"})
        else:
            defaults.append({"field": name, "value": None, "rule": "", "note": "无默认值，需用户确认或模型推断"})
    hit_n = len([x for x in defaults if x["value"]])
    checks = [
        _mk("默认值覆盖", "已知字段给默认值", f"{hit_n}/{len(defaults)} 个字段命中确定性默认值", True, ""),
    ]
    fails = []
    # 金额一致性校验（明细合计 vs 总金额）
    items = d.get("items")
    total = d.get("total")
    if isinstance(items, list) and items:
        amounts = []
        for it in items:
            amt = None
            if isinstance(it, dict):
                v = it.get("amount") or it.get("金额")
                amt = _to_float(v)
            else:
                amt = _to_float(it)
            if amt is not None:
                amounts.append(amt)
        if amounts:
            s = sum(amounts)
            checks.append(_mk("明细金额合计", "费用明细行金额求和",
                              f"明细合计 {s:.2f} 元（{len(amounts)} 行）", True, ""))
            if total is not None:
                t = _to_float(total)
                if t is None:
                    checks.append(_mk("总金额校验", "明细合计 vs 表单总金额",
                                      "无法解析表单总金额，需模型核对", True, ""))
                elif abs(s - t) > 0.01:
                    fails.append(f"金额不一致：明细合计 {s:.2f} ≠ 表单总金额 {t:.2f}，差 {abs(s - t):.2f} 元")
                    checks.append(_mk("总金额校验", "明细合计 vs 表单总金额",
                                      f"不一致！明细 {s:.2f} vs 总金额 {t:.2f}（差 {abs(s - t):.2f}）", False,
                                      "报销金额可能填错，需修正明细或总金额后再提交"))
                else:
                    checks.append(_mk("总金额校验", "明细合计 vs 表单总金额",
                                      f"一致：明细 {s:.2f} = 总金额 {t:.2f}", True, ""))
    return {
        "ok": True, "module": "reimburse-filler", "result": "red" if fails else "ok",
        "verdict": "金额校验不通过，需修正" if fails else "已给出表单默认值",
        "checks": checks, "fails": fails,
        "defaults": defaults,
        "reason": (f"；{'；'.join(fails)}" if fails else
                   f"{len(defaults)} 个字段中 {hit_n} 个命中确定性默认值，其余由模型/用户兜底"),
    }


def _to_float(v):
    """把金额值（数字/字符串/含逗号/含¥）转 float；失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("¥", "").replace("元", "")
    try:
        return float(s)
    except Exception:  # noqa: BLE001
        return None


def check_monthly_report(data):
    """信息技术运维月报审核（v1.0.201）：确定性字段比对，不耗 LLM。
    输入 data（模型从详情页 read_fields + Excel read_workspace_file 提取后传入）：
      expect_days:   本月应运维天数
      actual_days:   实际运维天数
      list_found:    正文/附件区是否出现设备清单 xlsx（机构到底传没传）
      list_ok:       清单是否成功静默取到并解析出数据行（机制层）
      page_pcs / excel_pcs: v1.0.604 起**只作展示不参与判定**（字段仍可传入，兼容旧调用）
    判定（v1.0.604 按用户口径取消在用设备数比对——页面口径与清单口径常年对不上，
    误判成「不通过」让机构白整改）：
      1) 应运维天数 == 实际运维天数
      2) 设备清单已上传：正文没有 xlsx → red「未上传设备清单」；
         传了但取不到/解析不了 → 数据缺失 → pending（机制问题不误伤机构）
    输出：checks / verdict / fails / result(green|red|pending)。"""
    d = data or {}

    def _num(v):
        if isinstance(v, (int, float)):
            return float(v)
        s = re.sub(r"[^\d.\-]", "", str(v or ""))
        try:
            return float(s)
        except Exception:  # noqa: BLE001
            return None

    exp, act = _num(d.get("expect_days")), _num(d.get("actual_days"))
    checks = []
    if exp is not None and act is not None:
        checks.append(_mk("运维天数一致", "应运维==实际", f"应 {d.get('expect_days')} / 实际 {d.get('actual_days')}",
                          exp == act, "天数不一致"))
    else:
        # v1.0.555（P1-1 修复）：原双侧双 None 不追加任何 check——只要计算机数对上
        # 就判 green 放行，运维天数一致性实际从未校验（假绿）。单侧缺与双侧缺统一
        # 记「数据缺失」走 pending 交人工确认。
        checks.append(_mk("运维天数", "应运维==实际", f"应 {d.get('expect_days')} / 实际 {d.get('actual_days')}",
                          False, "天数数据缺失，无法比对"))
    lf, lo = d.get("list_found"), d.get("list_ok")
    _info = f"（{d.get('list_detail')}）" if (lo and d.get("list_detail")) else ""
    if lf is False:
        checks.append(_mk("设备清单", "已上传设备清单", "正文未见 xlsx 清单附件", False, "未上传设备清单"))
    elif lo is True:
        checks.append(_mk("设备清单", "已上传设备清单", f"清单已取到并解析{_info}", True))
    elif lf is True:
        # 机构传了，是我们取不到/读不懂——归「数据缺失」交人工，不判机构不合规
        checks.append(_mk("设备清单", "已上传设备清单", f"清单已上传但静默取文件/解析未成功：{str(d.get('list_note') or '')[:60]}",
                          False, "清单数据缺失，无法确认内容"))
    else:
        checks.append(_mk("设备清单", "已上传设备清单", "清单是否存在未探明", False, "清单数据缺失，无法确认是否已上传"))
    if not checks:
        # v1.0.555（P1-2 修复）：原判 red"不通过"与 v1.0.224 确立的"数据缺失→
        # 待定而非不通过"设计自相矛盾——机构会因脚本读不到字段被要求整改。
        # 全缺是"无法判定"，归 pending 交人工/LLM 确认。
        return {"ok": True, "module": "monthly-report", "result": "pending",
                "verdict": "待定", "checks": [], "fails": [],
                "reason": "⚠️ 缺少可比对数据，请确认页面字段与 Excel 内容已提取"}

    fails = [c["item"] for c in checks if not c["pass"]]
    # v1.0.224：数据缺失（值没提取到）→ 判「待定」而非「不通过」——
    # 实测某分支 page=None+excel=None 被误判 green（缺数据不算 fail 就通过），
    # 语义上"无法比对"既不是通过也不是不通过，交人工/LLM 确认。
    missing = any("数据缺失" in (c.get("note") or "") for c in checks if not c["pass"])
    if missing:
        return {"ok": True, "module": "monthly-report", "result": "pending",
                "verdict": "待定", "checks": checks, "fails": fails,
                "field_values": {c["item"]: c["actual"] for c in checks},
                "reason": "⚠️ 数据缺失，无法判定：" + "、".join(fails)}
    return {
        "ok": True, "module": "monthly-report",
        "result": "green" if not fails else "red",
        "verdict": "符合" if not fails else "不符合",
        "checks": checks, "fails": fails,
        "field_values": {c["item"]: c["actual"] for c in checks},
        # v1.0.604：红判理由改用 note（item 是判定项名，读起来像"不符合：设备清单"这种半截话）
        "reason": ("全部符合" if not fails else
                   "不符合：" + "、".join((c.get("note") or c["item"]) for c in checks if not c["pass"])),
    }


# ---------- 规则注册表（新增模块在此登记） ----------
RULES = {
    "finance-check": check_finance,
    "finance-audit": check_finance_audit,
    "data-organizer": check_data_organizer,
    "data-analysis": check_data_analysis,
    "file-organizer": check_file_organizer,
    "reimburse-filler": check_reimburse_filler,
    "monthly-report": check_monthly_report,
}


def run_rule(module, data):
    """按模块名路由到脚本判定函数。未知模块返回结构化错误（由调用方决定回落 LLM）。
    判定返回统一附带 hits（命中的检查项）与 evidence（各检查项实际值/证据明细），
    供 agent 直接引用命中的规则与证据，避免把整段 checks 全量塞进上下文（P4-3）。"""
    fn = RULES.get(module)
    if fn is None:
        return {"ok": False, "module": module,
                "error": f"rule_check: 未注册的模块 {module!r}，可用：{sorted(RULES)}"}
    try:
        out = fn(data or {})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "module": module, "error": f"rule_check: {module} 判定异常：{e}"}
    if isinstance(out, dict):
        checks = out.get("checks")
        if isinstance(checks, list):
            out["hits"] = [c.get("item") for c in checks if isinstance(c, dict) and c.get("pass")]
            out["evidence"] = {c.get("item", f"#{i}"): c.get("actual", "")
                               for i, c in enumerate(checks) if isinstance(c, dict)}
    return out


def list_rules():
    """返回已注册的脚本判定模块清单。"""
    return sorted(RULES)


if __name__ == "__main__":
    # 自检：用一条绿单 + 一条红单验证判定逻辑
    demo_green = {"ok": True, "body_len": 8000, "fields": [
        {"key": "支出类别", "count": 1, "snippets": ["支出类别：专管费用"]},
        {"key": "专管部门", "count": 1, "snippets": ["专管部门：信息技术部"]},
        {"key": "业务类型", "count": 1, "snippets": ["业务类型：费用类付款（电子）"]},
        {"key": "预算执行单", "count": 1, "snippets": ["预算执行单：预算执行单号 YS-2026-0801，内容已填写"]},
        {"key": "附件", "count": 1, "snippets": ["附件：发票.pdf 合同.docx"]},
    ]}
    demo_red = {"ok": True, "body_len": 8000, "fields": [
        {"key": "支出类别", "count": 1, "snippets": ["支出类别：办公费"]},
        {"key": "专管部门", "count": 1, "snippets": ["专管部门：信息技术部"]},
        {"key": "业务类型", "count": 1, "snippets": ["业务类型：费用类付款（电子）"]},
        {"key": "预算执行单", "count": 1, "snippets": ["预算执行单："]},
    ]}
    import json as _j
    print("绿单:", _j.dumps(run_rule("finance-check", demo_green), ensure_ascii=False))
    print("红单:", _j.dumps(run_rule("finance-check", demo_red), ensure_ascii=False))
    # 新模块自检
    print("模块清单:", list_rules())
    print("单据核查:", _j.dumps(run_rule("finance-audit", {"rows": [
        {"单据编号": "BX2608-04334", "单据状态": "已付款"},
        {"单据编号": "BX2608-04335", "单据状态": "待审批"},
        {"单据编号": "BX2608-04334", "单据状态": "已付款"},
    ]}), ensure_ascii=False)[:400])
    print("数据整理:", _j.dumps(run_rule("data-organizer", {"headers": ["科目", "金额"],
        "rows": [["差旅费", "100"], ["差旅费", "100"], ["办公费", "50"]]}), ensure_ascii=False)[:400])
    print("数据报表:", _j.dumps(run_rule("data-analysis", {"headers": ["科目", "金额"],
        "rows": [["差旅费", "100"], ["办公费", "50"], ["培训费", ""], ["差旅费", "-5"]]}), ensure_ascii=False)[:400])
    print("归档整理:", _j.dumps(run_rule("file-organizer", {"files": [
        {"name": "发票.pdf", "size": 100}, {"name": "发票.pdf", "size": 100},
        {"name": "汇总.xlsx", "size": 200}, {"name": "方案.docx", "size": 300},
        {"name": "截图.png", "size": 50}, {"name": "资料.zip", "size": 400}]}), ensure_ascii=False)[:400])
    print("报销填写:", _j.dumps(run_rule("reimburse-filler", {"fields": [
        "报销分类", "费用承担部门", "银行账号", "备注", "收款人"]}), ensure_ascii=False)[:400])

# 技能提示词 · 月报表单直读核对（EKP xform）

来源：2026-10-22 人工录制 `build/op_trace_manual1/trace.jsonl`（18 事件 / 208 秒）+ 详情页字段实取
`build/xform_map.json`（甲分公司 2026 年 9 月信息技术运维月报，40 字段）。
观察通道 `tools/op_recorder.py`（只记录，不拦截不派发），字段推断 `tools/xform_fields.py`。

## 一、这次录制揭示的关键事实

1. **月报详情不是正文页，是 OA 的 xform 表单**：
   `https://oa.example.com/ekp/km/review/km_review_main/kmReviewMain.do?method=view&fdId=<32位>&fdTaskInstanceId=<32位>`
   数值住在 `input[name="extendDataFormInfo.value(fd_xxxxxx)"]` 里，正文段落**没有**「在用计算机数量：23」这种句子。
2. 所以现有两条取数通道都会 miss：
   - `read_wecom_fields`（按关键词抓正文片段）→ `count=0`；
   - `services/batch_flow.py` 的正文正则（代码里那句「某分支等页面值在表单控件，正文无值」的注脚就是这个坑）→ 只能落 pending。
3. `read_wecom_page` 其实**能**读到这些 input 的 `name` 和 `value`，但 `label` 是空的——EKP 的标签在表格
   `<td>` 单元格里而不是 `<label>` 元素上，`wecom_bridge/read.py` 的 `findFieldLabel` 那套（label[for]/aria/placeholder/
   closest label/MUI 兄弟节点）一条都不命中。**模型拿到 `fd_3d11ce00e2be8a = 115` 却不知道 115 是什么。**
4. `fd_` id 是**模板级**的，不是实例级的：同一张月报模板下所有机构、所有月份的 id 完全相同。
   → 一张映射表可以喂全部机构，这正是"直读字段代替正文正则"能固化的前提。
   （目前只有 1 张样本，第二批建议换一家机构复核 id 是否一致。）

## 二、已验证的 fd_ → 业务字段映射

| fd_ key | 业务字段 | 样例值 | 取数注意 |
|---|---|---|---|
| `fd_3d11ce00e2be8a` | 设备总数量 | 115 | text |
| `fd_3d11ce01d95ff2` | 本月报废数量 | 0 | text |
| `fd_3d11ce0359c6d6` | 本月新增设备数 | 0 | text |
| `fd_3d11ce87e6770a` | 在用计算机数量（仅限办公场地计算机） | 23 | text，同行右侧有公式列「安装率 100.0%」 |
| `fd_3d11ce88bce0b4` | 未安装终端安全软件数量 | 0 | text，同上 |
| `fd_3d11a34cd4f510` | 机构名称 | 示例期货公司_甲分公司 | hidden，同 key 两条：第一条是行 id，**第二条才是文本值** |
| `fd_3d11c7691a895c` / `fd_3d11a372400154` | 月报所示月份 年 / 月 | 2026 / 9 | 两个独立 hidden，年在前 |
| `fd_3d11a625443a74` | 本月信息技术人员是否发生过离职或工作岗位交接 | 2 | 选项编码。该格选项文本依次为「有离职 / 有工作交接 / 无」，正文写「9月1日至9月4日因胡小平休假，网络巡检工作交接至肖斯琦」→ **2 ≈ 有工作交接**（推断，待第二张确认；脚本按选项文本判更稳） |
| `fd_3d11cb03e32a54` | 该月是否发生异常事件记录 | 2 | 选项文本「是 / 否」→ **2 ≈ 否**（同上待确认） |
| `fd_3d11cb1592f08a` | 明细表（`TABLE_DL_` 行 id） | — | 行内 `fd_3d11cb4d225100` 本次为空 |
| `fd_3d11d012b7d804` | 费用明细行 id | 10 行 | 每行 label 落在那行的「*电信服务*基础/增值电信服务费」 |
| `fd_3d11d0a78d883e` | 费用明细「单一采购」日期 | 2026-01-26 … 2026-09-23 | 行间重复出现，按顺序对应明细行 |
| `fd_3d11d03fe3a9fc` | 明细「日期」 | 空 | 本次未填 |
| `fd_3d11d0d17662e6` / `fd_3d11d046a7bbc8` | 应急演练类型 / 记录 | 空 / uid | 本次未填 |

## 三、只读取数 JS（可读版，喂 `wecom_run_js` 的 `expression`，函数体、以 return 结尾）

已过 `agent_tools/executors.py` 的 `wecom_run_js` 危险 API 黑名单（22 条模式 0 命中：无 fetch、无跨帧、
无字符串字面量拼接、`location` 只读），所以不会被打回人工审批。
第四节技能正文里嵌的是这段的**等价压缩版**（12 行 1.1 KB，装进 prompt 才带得动）——
两版在同一张真实详情页各跑一次，40 个字段的 key/value/label **逐条一致**（diff 0）。

```javascript
const clean = (s) => String(s || '').trim().replace(/\s+/g, ' ');
const isNum = (t) => !t || /^[0-9.,%\s-]+$/.test(t);
const labelOf = (el) => {
  const td = el.closest('td');
  if (!td) return clean(el.getAttribute('aria-label') || el.name);
  const r = td.getBoundingClientRect();
  for (let p = td.previousElementSibling, i = 0; p && i < 3; p = p.previousElementSibling, i++) {
    const t = clean(p.innerText);
    if (!isNum(t) && t.length <= 40) return t;
  }
  const tr = td.closest('tr');
  const table = tr && tr.closest('table');
  if (table) {
    let row = tr.previousElementSibling;
    for (let up = 0; row && up < 6; up++, row = row.previousElementSibling) {
      let best = '', bestW = 0;
      for (const c of Array.from(row.cells || [])) {
        const cr = c.getBoundingClientRect();
        const w = Math.min(r.right, cr.right) - Math.max(r.left, cr.left);
        const t = clean(c.innerText);
        if (w > bestW && !isNum(t) && t.length <= 40) { best = t; bestW = w; }
      }
      if (best) return best;
    }
  }
  return '';
};
const rows = [];
document.querySelectorAll('input, textarea, select').forEach((e) => {
  if (rows.length >= 120) return;
  const nm = e.name || e.id || '';
  const ids = nm.match(/fd_[0-9a-f]{8,}/g) || [];
  if (!ids.length) return;
  rows.push({key: ids[ids.length - 1], type: (e.type || e.tagName).toLowerCase(),
             value: clean(e.value).slice(0, 60), label: labelOf(e)});
});
return JSON.stringify({url: location.href, title: document.title, fields: rows});
```

标签推断为什么这样写：EKP 表是「表头行在上、值行在下」，且表头第一格常是小节标题（列数比值行多一格）——
按「上一行同列」取标签会整体错位一格（第一版就把 115 标成了小节名「设备管理总体情况」）。
改成取**与本列水平投影重叠最大**的非数字表头单元格后，与 colspan/错位无关，实测 40 字段标签全对。

## 四、技能提示词正文（可整段粘进「技能中心 → 新建技能」）

```
【月报表单直读核对】核对分支机构「信息技术运维月报」（OA km_review / xform 表单）的取数与判定。
批量任务（"把本月月报跑一遍"）第一步仍必须调用 run_audit_flow，本技能只用于三种场景：
①流水线把某张判成「待定/数据问题」需要深挖；②校准判定口径；③详情页正文取不到数值时补位取数。

一、定位
1. list_wecom_windows 找标题含「月报-流程管理」的详情页（URL 形如 oa.example.com/ekp/km/review/
   km_review_main/kmReviewMain.do?method=view&fdId=…）。后续每个工具都带 pick=该标题——窗口序号会随开关漂移，标题才稳定。
2. 需要自己打开某张时：列表页在 oa.example.com/tpp-web 待办事宜，用 list_wecom_links 取真实 href，
   或点标题文本「<机构>提交的<年月>月的信息技术运维月报」（div.main>div.main_title>a.work_desc>span）。
   点击必然新开独立窗口；8 秒内标题没变成「…月报-流程管理」就回列表重开一次，
   不要改用 pc_takeover / dsh / 外部浏览器（脱离 CDP 控制必失败）。

二、取数（关键：值在表单控件里，正文没有这些数字）
3. wecom_run_js（pick=标题「…月报-流程管理」）原样执行下面这段 expression，返回 {url,title,fields:[{key,label,value}]}：
   const c=(s)=>String(s||'').trim().replace(/\s+/g,' ');
   const n=(t)=>!t||/^[0-9.,%\s-]+$/.test(t);
   const L=(el)=>{const td=el.closest('td');if(!td)return c(el.getAttribute('aria-label')||el.name);const r=td.getBoundingClientRect();
   for(let p=td.previousElementSibling,i=0;p&&i<3;p=p.previousElementSibling,i++){const t=c(p.innerText);if(!n(t)&&t.length<=40)return t;}
   const tr=td.closest('tr'),tb=tr&&tr.closest('table');if(tb){let row=tr.previousElementSibling;
   for(let u=0;row&&u<6;u++,row=row.previousElementSibling){let b='',bw=0;
   for(const x of Array.from(row.cells||[])){const q=x.getBoundingClientRect(),w=Math.min(r.right,q.right)-Math.max(r.left,q.left),t=c(x.innerText);
   if(w>bw&&!n(t)&&t.length<=40){b=t;bw=w;}}if(b)return b;}}return '';};
   const out=[];document.querySelectorAll('input, textarea, select').forEach((e)=>{if(out.length>=120)return;
   const ids=(e.name||e.id||'').match(/fd_[0-9a-f]{8,}/g)||[];if(!ids.length)return;
   out.push({key:ids[ids.length-1],type:(e.type||e.tagName).toLowerCase(),value:c(e.value).slice(0,60),label:L(e)});});
   return JSON.stringify({url:location.href,title:document.title,fields:out});
   label 为空的按 fd_ 映射表补业务名：
   设备总数量=fd_3d11ce00e2be8a，本月报废数量=fd_3d11ce01d95ff2，本月新增设备数=fd_3d11ce0359c6d6，
   在用计算机数量=fd_3d11ce87e6770a，未安装终端安全软件数量=fd_3d11ce88bce0b4，
   机构名称=fd_3d11a34cd4f510（同 key 取第二条文本值），月报年=fd_3d11c7691a895c 月=fd_3d11a372400154，
   人员离职或交接=fd_3d11a625443a74，异常事件=fd_3d11cb03e32a54。
   fd_ 是模板级 id，全部机构共用这张表；换一个表单模板才需要重建映射。
4. 交叉校验：read_wecom_fields ["应运维天数","实际运维天数","在用计算机数量","设备清单","终端安全软件"] 取正文段落与附件名。
   正文与表单不一致时以表单字段为准，并把「两个值各是多少」原样写进疑问项，不擅自裁决。
5. 附件对账（v1.0.604 起只判"传没传"）：download_wecom_attachment 静默直传取清单
   （返回 silent_only=true 就说明这页取不到直链——记「清单已上传但无法静默获取」，改判待定，**不许点下载按钮补齐**）；
   取到后用 analyze_monthly_attachment 拿 pcs_in_use / total_rows 只作参考展示。

三、判定（v1.0.604 用户口径：**取消在用设备数比对**，只看两条）
R1 正文取不到「本月应运维天数」或为 0 → 数据缺失 → 待定（不是不通过）。
R2 实际运维天数 ≠ 应运维天数 → 不通过（写明两个数字；正文有休假/交接/节假日解释的，把解释一并写上）。
R3 设备清单是否上传：正文/附件区有 xlsx 且能静默取到 → 通过；正文根本没有 xlsx → 不通过「未上传设备清单」；
   传了但取不到文件（页面结构取不到直链）→ 待定，**不算机构不合规**，也不许改点击补齐。
表单里的设备总数量 / 本月报废数量 / 本月新增设备数 / 在用计算机数量 / 未安装终端安全软件数量
——v1.0.604 起**全部只作参考展示，不参与红绿**（页面口径与清单口径常年对不上，比对出来的"不通过"多是假警）。
要恢复任何一项比对，需要用户明确口径后再改 `services/rule_check.py::check_monthly_report`。

四、纪律（硬约束）
- 绝不点击「提交/同意/通过/退回/保存/用印」等任何改变流程状态的按钮，本技能只读只核对；
  run_audit_flow 的 auto_approve 只在用户明确要求时才传 true。
- 不修改任何表单字段。wecom_run_js 只允许读 DOM：禁止 click()/dispatchEvent()/赋值 .value/.checked、禁止 fetch。
- **不点页面里的「下载」按钮**（v1.0.604）：企微 Electron 自管下载且无视 CDP 下载行为设置，点击必弹原生
  「另存为」挂在前台。附件只走页内 fetch 静默直传；取不到就按待定处理。
- 自己开出来的详情页读完必须 close_wecom_page 关掉，再回列表；不要动用户自己打开的窗口，不要抢前台焦点。
- 数字只能来自读到的字段和脚本返回值：报告里没有的机构绝不写「通过/不通过/待定」，取不到就写「未取到 + 原因」。
- 每处理完一项立即 append_progress(key="月报核对", content=机构/年月/关键数/结论/依据字段名)，
  全部处理完再 read_workspace_file 读进度文件汇总，别把中间结果全压在上下文里。
```

## 五、稳定后要固化的一处（另一处已随 v1.0.604 落地）

1. `wecom_bridge/read.py` 的 `findFieldLabel`：加「表格表头水平重叠」兜底（第三节那段 JS 已验证），
   让 `read_wecom_page` 对 EKP 页面直接给出带业务标签的字段——所有技能与流水线一起受益，不用再带映射表。
2. ~~月报判定改用 xform 字段主取~~ → v1.0.604 按用户口径先收口成「天数一致 + 清单是否上传」两条，
   `page_pcs` 提取链（正文正则 / 字段 value / DOM 表格三通道）与 `在用计算机数一致` 判定项已整体删除；
   以后若要把设备总数、终端安全软件安装率纳入红绿，需要用户逐条给口径，再改 `services/rule_check.py`。

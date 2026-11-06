  // 增强 body：对 MUI/AntD 组件，innerText 读不到值
  // 先提取组件值，注入到 body 中供 agent 读取
  function extractMUISelectValues() {
    const cands = __wbcMuiCandidates();
    const pairs = [];
    cands.combos.forEach((c) => {
      const labelText = c.byLabel;
      const altLabel = c.ariaLabel;
      let value = c.dataValue || c.ariaVal;
      if (!value) value = c.selText;
      if (!value) value = c.inputVal;
      if ((labelText || altLabel) && value) {
        pairs.push({label: (labelText || altLabel).trim(), value});
      }
    });
    cands.elems.forEach((el) => {
      if (el.value && el.label) {
        pairs.push({label: el.label, value: el.value});
      }
    });
    return pairs;
  }

  // 提取 MUI Select 值并注入到 body 中
  const muiValues = extractMUISelectValues();
  let body = (document.body ? document.body.innerText : '').replace(/\s+/g,' ').trim().slice(0, 30000);
  // 将 MUI 组件值注入到 body 文本中（格式：【MUI 专管部门 = 信息技术部】）
  for (const pv of muiValues) {
    const injection = `【MUI ${pv.label} = ${pv.value}】`;
    body += ' ' + injection;
  }

  // v1.0.242：提取考试选项【选中状态】（学习平台考试页）。
  // 此前 agent 读不到哪个选项被选中 → 平台恢复上轮进度后「逐题盲点检查」，
  // 实测死循环从未走到交卷（v1.0.241 事故）。现在：
  //  1) 返回 exam_options（按题分组，每选项 label/text/selected）
  //  2) body 尾部注入 【当前题已选：A C】（单题页）或 【已选状态N：... | 首选项摘要…】（多题页）
  // 这是 agent 查看选中状态的唯一合法途径——禁止盲点选项「验证」。
  function extractExamOptions() {
    const res = [];
    // 路径 1：标准 radio/checkbox input
    const inputs = Array.from(document.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
    if (inputs.length >= 2) {
      inputs.slice(0, 12).forEach((inp) => {
        let label = '';
        const lb = (inp.closest && inp.closest('label')) || null;
        if (lb) label = (lb.innerText || '').trim();
        if (!label && inp.parentElement) label = (inp.parentElement.innerText || '').trim();
        if (label) res.push([{label: label.slice(0, 30), text: '', selected: !!inp.checked}]);
      });
      return res.length ? [res] : res;
    }
    // 路径 2：div 式选项（实测 DOM：题目框 > 选项组容器 > 每选项两层 div，
    // 选中态 = 选项行上叠加一个动态 hash class（如 emotion 的 jss1247），无语义名！）
    // 文本以选项字母 A-F 或「正确/错误」开头
    const pageText = document.body ? document.body.innerText : '';
    if (!/已答|未答|交卷|答题卡|选项/.test(pageText)) return res;
    const optRe = /^([A-F][\s.、:：)）]*|正确|错误)/;
    const rawMatches = [];
    Array.from(document.querySelectorAll('div,li')).forEach((el) => {
      const t = (el.innerText || '').trim();
      if (t.length < 2 || t.length > 200) return;  // 裸字母标签（长度1）不是选项行
      if (!optRe.test(t)) return;
      rawMatches.push(el);
    });
    // 最内层行：不包含其它匹配元素的匹配项（外层容器 innerText 同样以字母开头会误报）
    const rows = rawMatches.filter((el) => !rawMatches.some((r) => r !== el && el.contains && el.contains(r)));
    if (rows.length < 2) return res;
    // 分组：向上找到【包含 >=2 行的最低祖先】= 该题的选项容器（行的直接父级是
    // 每选项独立的 wrapper，按直接父分组会散成单行——必须按公共容器分）
    const groupOf = (r) => {
      let cur = r;
      for (let i = 0; i < 6 && cur; i++) {
        cur = cur.parentElement;
        if (!cur) return null;
        let n = 0;
        rows.forEach((x) => { if (x !== r && cur.contains(x)) n++; });
        if (n >= 1) return cur;
      }
      return null;
    };
    const groups = new Map();
    rows.forEach((r) => {
      const key = groupOf(r) || r;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    });
    const valid = [];
    groups.forEach((g) => {
      if (g.length >= 2 && g.length <= 8) valid.push(g);
    });
    valid.sort((a, b) => b.length - a.length);
    const mkOpt = (el, i, selected) => {
      const t = (el.innerText || '').trim();
      const m = t.match(/^([A-F])|^(正确|错误)/);
      return {label: m ? m[0] : String(i + 1), text: t.slice(0, 40), selected: !!selected};
    };
    // 选中标记判定（自身+2 级祖先）
    const ariaVal = (el) => {
      let cur = el;
      for (let i = 0; i < 3 && cur; i++) {
        const a = cur.getAttribute && (cur.getAttribute('aria-checked') || cur.getAttribute('aria-selected') || cur.getAttribute('aria-pressed'));
        if (a) return a;
        cur = cur.parentElement;
      }
      return null;
    };
    const semMarked = (el) => {
      let cur = el;
      for (let i = 0; i < 3 && cur; i++) {
        if (/select|check|active|current|chosen|highlight/i.test(String(cur.className || ''))) return true;
        cur = cur.parentElement;
      }
      return false;
    };
    const clsSetOf = (el) => {
      const s = new Set();
      let cur = el;
      for (let i = 0; i < 3 && cur; i++) {
        String(cur.className || '').split(/\s+/).forEach((c) => { if (c) s.add(c); });
        cur = cur.parentElement;
      }
      return s;
    };
    // 语义选中 class（v1.0.247 修复 Q5 盲点）：实测选中态 = 选项行父级叠加
    // question-module__selected--2UBRn（CSS Modules「__selected--hash」BEM 修饰符）。
    // 差异法（下方）的盲区：【全部选项都选中】时选中 class 落入组交集、行差全空，
    // 会被误判「全部未选」（实测 Q5=ABCDE 显示【当前题已选：无】，Agent 据此乱点）。
    // 语义层用精确 token 匹配（独立 class 词 selected/checked/is-*，或 __selected--/
    // __checked-- BEM 修饰符），不做裸子串匹配（避免 checkbox-container 之类误报）。
    // 全部行命中 = 全部选中；部分命中 = 按命中判；无命中 = 落回差异法。
    // 组内各行检查相同的 3 级祖先链，公共祖先 class 对全组一致，部分命中只可能来自
    // 行自身/行级 class——即真正的选中态 class。
    const semCls = (el) => {
      let cur = el;
      for (let i = 0; i < 3 && cur; i++) {
        const c = String(cur.className || '');
        if (/(^|\s)(is-)?(selected|checked)(\s|$)/i.test(c) || /__(selected|checked)--/i.test(c)) return true;
        cur = cur.parentElement;
      }
      return false;
    };
    valid.slice(0, 3).forEach((g) => {
      // 1) aria 优先（部分平台用 aria-checked/selected/pressed）
      const ariaFlags = g.map(ariaVal);
      if (ariaFlags.some((a) => a !== null)) {
        res.push(g.map((el, i) => mkOpt(el, i, ariaFlags[i] === 'true')));
        return;
      }
      // 2) 语义选中 class（selected/checked 精确 token 或 __selected--hash BEM 修饰符）
      const semFlags = g.map(semCls);
      if (semFlags.some(Boolean)) {
        res.push(g.map((el, i) => mkOpt(el, i, semFlags[i])));
        return;
      }
      // 3) 组内 class 差异：class 集（自身+2 级祖先）减去组交集 = 该行独有的差异 class。
      //    选中行会多出状态 class（实测 jss1247），未选行没有——对动态 hash class 有效。
      const sets = g.map(clsSetOf);
      const inter = new Set([...sets[0]]);
      sets.forEach((s) => { [...inter].forEach((c) => { if (!s.has(c)) inter.delete(c); }); });
      const extra = sets.map((s) => [...s].filter((c) => !inter.has(c)));
      if (extra.every((e) => e.length > 0)) {
        // 每行都有独有 class = class 是逐实例生成的，无法区分选中态 →
        // 退回语义 class 检查；仍分不出来就不输出该组（宁缺毋假）
        const sem = g.map(semMarked);
        if (!sem.some(Boolean) || sem.every(Boolean)) return;
        res.push(g.map((el, i) => mkOpt(el, i, sem[i])));
        return;
      }
      res.push(g.map((el, i) => mkOpt(el, i, extra[i].length > 0)));
    });
    return res;
  }
  const examOpts = extractExamOptions();
  if (examOpts.length === 1) {
    const sel1 = examOpts[0].filter((o) => o.selected).map((o) => o.label);
    body += ' 【当前题已选：' + (sel1.length ? sel1.join(' ') : '无') + '】';
  } else if (examOpts.length > 1) {
    examOpts.forEach((g, gi) => {
      const sel = g.filter((o) => o.selected).map((o) => o.label);
      const anchor = (g[0].text || '').slice(0, 12);
      body += ' 【已选状态' + (gi + 1) + '：' + (sel.length ? sel.join(' ') : '无') + ' | ' + anchor + '…】';
    });
  }

  const out = {
    title: document.title,
    url: location.href,
    body: body
  };
  // 提取答题状态（考试/问卷页常见格式：「已答：X 未答：Y」），供 agent 交卷前核对漏填
  const _stm = out.body.match(/已答[:：]\s*(\d+)\s*未答[:：]\s*(\d+)/);
  if (_stm) {
    out.answered = parseInt(_stm[1], 10);
    out.unanswered = parseInt(_stm[2], 10);
    out.answer_total = out.answered + out.unanswered;
  }
  const fields = [];
  const fieldEls = Array.from(document.querySelectorAll('input,textarea,select,[role="textbox"],[role="combobox"],[role="searchbox"]'));
  // 增强：建立 label->field 映射
  function findFieldLabel(el) {
    // 0. 标准 HTML 关联：label[for=id]（最可靠，OA 表单常用）
    if (el.id) {
      const forLbl = document.querySelector('label[for="' + el.id + '"]');
      if (forLbl && forLbl.innerText && forLbl.innerText.trim()) {
        return forLbl.innerText.trim().slice(0, 80);
      }
    }
    // 1. 已有的：aria-label, placeholder, name, closest label
    const ariaLabel = el.getAttribute('aria-label') || '';
    const placeholder = el.placeholder || '';
    const name = el.name || '';
    const closestLabel = (el.closest('label') && el.closest('label').innerText) || '';
    // 2. 查找兄弟元素中的 label 文本（MUI 常用 formControl 结构）
    const parent = el.parentElement;
    let siblingLabel = '';
    if (parent) {
      // 查找父元素中所有包含「专管部门」「支出类别」等中文关键词的文本节点
      const textNodes = [];
      const walker = document.createTreeWalker(parent, NodeFilter.SHOW_TEXT, null);
      let node;
      while (node = walker.nextNode()) {
        const txt = (node.textContent || '').trim();
        // 只取长度 2-20 的中文字符串（可能是字段名）
        if (/[\u4e00-\u9fa5]{2,20}$/.test(txt) && txt.length <= 20 && txt.length >= 2) {
          textNodes.push(txt);
        }
      }
      // 取最后一个可能的 label
      siblingLabel = textNodes[textNodes.length - 1] || '';
    }
    // 3. 拼接所有可用标签
    const allLabels = [ariaLabel, placeholder, name, closestLabel, siblingLabel].filter(Boolean).join(' ');
    return allLabels.trim().slice(0, 80);
  }
  fieldEls.forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const label = findFieldLabel(el);
    const role = el.getAttribute('role') || '';
    // 必填检测：required 属性 / aria-required / label 文本含 *（HTML 常用必填标记）
    const _lblStar = (label.match(/\*/) !== null);
    const required = !!(el.required || el.getAttribute('aria-required') === 'true' || _lblStar);
    if (tag === 'input' && ['text','email','search','number','tel','url','password','date','datetime-local'].includes(el.type)) {
      fields.push({kind:'text', name:el.name||'', label:label, value:el.value||'', required:required});
    } else if (tag === 'textarea') {
      fields.push({kind:'textarea', name:el.name||'', label:label, value:el.value||'', required:required});
    } else if (tag === 'input' && el.type === 'radio') {
      fields.push({kind:'radio', name:el.name||'', label:label, checked:!!el.checked, value:el.value||'', required:required});
    } else if (tag === 'input' && el.type === 'checkbox') {
      fields.push({kind:'checkbox', name:el.name||'', label:label, checked:!!el.checked, value:el.value||'', required:required});
    } else if (tag === 'select') {
      fields.push({kind:'select', name:el.name||'', label:label, value:el.value||'', options:Array.from(el.options).map(o=>o.value+'|'+o.text.trim()), required:required});
    } else if (tag === 'input' && el.type === 'file') {
      fields.push({kind:'file', name:el.name||'', label:label, required:required});
    } else if (role.includes('textbox') || role.includes('combobox') || role.includes('searchbox')) {
      // MUI/自定义组件用 role 标识的可输入元素（常为 contenteditable 的 div）
      fields.push({kind:'text', name:el.name||'', label:label, value:(el.innerText||el.textContent||'').trim().slice(0,200), required:required});
    }
  });
  out.fields = fields;
  out.mui_values = muiValues;
  const actsAll = [];
  document.querySelectorAll('button,a[href], [role="button"]').forEach((el) => {
    const t = (el.innerText||el.textContent||'').trim();
    if (t) actsAll.push({tag:el.tagName.toLowerCase(), text:t.slice(0,50), href:el.href||''});
  });
  out.actions = actsAll.slice(0, 250);
  // v1.0.196/197：links = __extractLinks()（按 href 去重，每行只留摘要，索引空间
  // 与 click_element 的数字 target 完全一致）
  out.links = __extractLinks();
  // v1.0.242：考试选项选中状态（数组按题分组：[[{label,text,selected},...],...]）
  if (examOpts.length) out.exam_options = examOpts;
  return JSON.stringify(out);
})()

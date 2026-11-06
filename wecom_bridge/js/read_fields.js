(() => {
  // 增强版 body 提取：对 MUI/AntD 等组件库，innerText 读不到值
  // 需要额外探测：data-value, aria-valuetext, role="combobox" 的子元素
  // （v1.0.653 瘦身：零调用的 extractComponentValue/findFieldContext 两个死 JS 函数已删）
  // 从 body 中提取所有「字段名：值」对（增强版）
  function extractFieldPairs(body) {
    const pairs = [];
    // 匹配常见标签格式：label：value / label: value / label 值（无分隔符）
    const patterns = [
      /([\u4e00-\u9fa5A-Za-z（）()\-]+)[:：]\s*([^\n\r，,。；;]{1,100})/g,
      /([\u4e00-\u9fa5A-Za-z（）()\-]+)\s+([^\n\r，,。；;]{1,100})/g
    ];
    for (const pat of patterns) {
      let m;
      while ((m = pat.exec(body)) !== null) {
        const label = m[1].trim();
        const value = m[2].trim();
        if (label.length > 1 && value.length > 0 && !/^(https?:|http:|\/)/.test(value)) {
          pairs.push({label, value});
        }
      }
    }
    return pairs;
  }

  // 从 DOM 中探测 MUI/AntD 组件的字段值（innerText 读不到的）
  function extractMUIFields() {
    const cands = __wbcMuiCandidates();
    const muiFields = [];
    cands.combos.forEach((c) => {
      if (c.byLabel && c.selText) {
        muiFields.push({label: c.byLabel, value: c.selText, source: 'mui-combobox'});
      }
      if (c.dataValue && c.formLabel) {
        muiFields.push({label: c.formLabel, value: c.dataValue, source: 'mui-data-value'});
      }
    });
    cands.antds.forEach((a) => {
      if (a.label && a.value) {
        muiFields.push({label: a.label, value: a.value, source: 'antd-select'});
      }
    });
    return muiFields;
  }

  const body = (document.body ? document.body.innerText : '').replace(/\s+/g,' ').trim();
  const pairs = extractFieldPairs(body);
  const muiFields = extractMUIFields();
  return {
    title: document.title,
    url: location.href,
    body: body.slice(0, 30000),  // 与 _READ_PAGE_JS 一致，避免 CDP 传 500KB+ 大 JSON
    body_len: body.length,
    field_pairs: pairs.slice(0, 200),  // 前 200 个「字段名：值」对
    mui_fields: muiFields,  // MUI/AntD 组件字段
  };
})()

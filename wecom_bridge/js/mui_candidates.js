function __wbcMuiCandidates() {
  const combos = [];
  document.querySelectorAll('[role="combobox"]').forEach((cb) => {
    const ariaLabelledBy = cb.getAttribute('aria-labelledby');
    const byLabel = ariaLabelledBy ? ((document.getElementById(ariaLabelledBy)?.innerText || '').trim()) : '';
    const ariaLabel = (cb.getAttribute('aria-label') || '');
    const selected = cb.querySelector('[role="option"][aria-selected="true"]');
    const selText = selected ? (selected.innerText || '').trim() : '';
    const dataValue = cb.getAttribute('data-mui-value') || '';
    const ariaVal = cb.getAttribute('aria-valuetext') || '';
    const inputVal = (cb.querySelector('input[data-value]')?.getAttribute('data-value') || '');
    const formLabel = ((cb.closest('[class*="MuiFormControl"]')?.querySelector('label')?.innerText || '').trim());
    combos.push({byLabel: byLabel, ariaLabel: ariaLabel, selText: selText, dataValue: dataValue, ariaVal: ariaVal, inputVal: inputVal, formLabel: formLabel});
  });
  const elems = [];
  document.querySelectorAll('[data-mui-value]').forEach((el) => {
    const labelEl = el.closest('.MuiFormControl-root, .MuiFormControl-fullWidth')?.querySelector('label, [class*="MuiFormLabel"]');
    elems.push({value: el.getAttribute('data-mui-value') || '', label: labelEl ? (labelEl.innerText || '').trim() : ''});
  });
  const antds = [];
  document.querySelectorAll('[class*="ant-select"]').forEach((sel) => {
    const selected = sel.querySelector('[class*="ant-select-selection-item"]');
    antds.push({label: (sel.previousElementSibling?.innerText || '').trim(), value: selected ? (selected.innerText || '').trim() : ''});
  });
  return {combos: combos, elems: elems, antds: antds};
}

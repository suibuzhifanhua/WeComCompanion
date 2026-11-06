  const norm = s => String(s||'').replace(/\s+/g,'');
  const ntx = norm(tx);
  const _match = e => {
    const txt = ((e.innerText||e.textContent||'') + ' ' + (e.getAttribute('aria-label')||'')).trim();
    return txt.includes(tx) || (e.placeholder||'').includes(tx) || (e.name||'').includes(tx);
  };
  const _matchN = e => {
    const txt = ((e.innerText||e.textContent||'') + ' ' + (e.getAttribute('aria-label')||'')).trim();
    return norm(txt).includes(ntx) || norm(e.placeholder||'').includes(ntx) || norm(e.name||'').includes(ntx);
  };

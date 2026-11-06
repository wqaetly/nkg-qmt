(() => {
  const search = document.getElementById('research-search');
  if (!search) return;
  const rows = [...document.querySelectorAll('.research-stock')];
  const previous = document.getElementById('research-prev'), next = document.getElementById('research-next');
  let page = 0;
  function show() {
    const query = search.value.trim().toLowerCase();
    const matched = rows.filter(row => row.dataset.search.toLowerCase().includes(query));
    const pages = Math.max(1, Math.ceil(matched.length / 5));
    page = Math.min(page, pages - 1);
    rows.forEach(row => { row.hidden = true; });
    matched.slice(page * 5, page * 5 + 5).forEach(row => { row.hidden = false; });
    document.getElementById('research-count').textContent = `匹配 ${matched.length} / ${rows.length} 只`;
    document.getElementById('research-page').textContent = `${page + 1} / ${pages} 页 · 每页 5 只`;
    document.getElementById('research-empty').hidden = matched.length > 0;
    previous.disabled = page === 0;
    next.disabled = page >= pages - 1;
  }
  search.addEventListener('input', () => { page = 0; show(); });
  previous.addEventListener('click', () => { page--; show(); });
  next.addEventListener('click', () => { page++; show(); });
  show();
})();

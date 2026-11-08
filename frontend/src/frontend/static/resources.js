"use strict";
(() => {
  const root = document.getElementById('resources-view'), inventory = document.getElementById('resources-inventory');
  const error = document.getElementById('resources-error'), status = document.getElementById('resources-status');
  const el = (tag, text) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = String(text); return item; };
  function render(data) {
    const fragment = document.createDocumentFragment();
    const card = el('section'); card.className = 'card'; card.dataset.key = 'resources';
    const table = el('table'), head = el('thead'), headings = el('tr');
    const columns = [['Name', 'name'], ['ID', 'id'], ['State / scope', 'state'], ['Driver', 'driver'], ['Labels', 'labels']];
    if (root.dataset.kind === 'services') columns.push(['Logs', 'logs']);
    for (const [label] of columns) headings.append(el('th', label)); head.append(headings); table.append(head);
    const body = el('tbody'); table.append(body);
    for (const item of data.resources) {
      const row = el('tr'); row.dataset.key = item.id;
      for (const [, field] of columns) {
        const cell = el('td', item[field] || '—');
        if (field === 'labels') cell.replaceChildren(dockerLabels(item.labels));
        if (field === 'logs') cell.replaceChildren(dockerLogsLink({scope: 'service', kind: 'swarm', service_id: item.id, service: item.name}));
        if (item._matches?.['resource.' + field]?.length || (field === 'labels' && Object.keys(item._matches || {}).some(key => key.startsWith('label.')))) cell.classList.add('filter-match');
        row.append(cell);
      }
      body.append(row);
    }
    if (!data.resources.length) { const row = el('tr'), cell = el('td', 'No matching resources.'); cell.colSpan = columns.length; row.append(cell); body.append(row); }
    card.append(table); fragment.append(card); reconcileChildren(inventory, fragment);
  }
  let loading = false, again = false;
  async function refresh() {
    const generation = liveRefresh.generation;
    if (loading) { again = true; return; } loading = true;
    try {
      const response = await authenticatedFetch('/api/docker/resources/' + encodeURIComponent(root.dataset.kind) + window.location.search, {cache: 'no-store'});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || `Resources unavailable (HTTP ${response.status})`);
      if (generation !== liveRefresh.generation) return;
      render(data); error.hidden = true; liveRefresh.updated(status);
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { loading = false; if (again) { again = false; if (liveRefresh.enabled) refresh(); } }
  }
  refresh();
  if (root.dataset.live === 'true') {
    const events = liveRefresh.events('/api/events?component=docker'); let timer;
    events.addEventListener('ready', () => { status.textContent = 'Live · connected'; refresh(); });
    events.addEventListener('change', () => { clearTimeout(timer); timer = liveRefresh.schedule(refresh, 300); });
    events.addEventListener('session-expired', checkSession);
    events.addEventListener('shutdown', () => { events.close(); status.textContent = 'Server is shutting down.'; });
    events.addEventListener('unavailable', () => { status.textContent = 'Live connection unavailable · reconnecting'; });
    events.onerror = () => { status.textContent = 'Live connection interrupted · reconnecting'; checkSession(); };
    window.addEventListener('pagehide', () => { clearTimeout(timer); events.close(); });
  } else status.textContent = 'Snapshot · live events disabled';
})();

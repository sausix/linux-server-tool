"use strict";
(() => {
  const root = document.getElementById('labels-view'), inventory = document.getElementById('labels-inventory');
  const error = document.getElementById('labels-error'), status = document.getElementById('labels-status');
  const el = (tag, text) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = String(text); return item; };
  function render(data) {
    const fragment = document.createDocumentFragment();
    for (const message of data.errors || []) { const warning = el('p', message); warning.className = 'alert warning'; fragment.append(warning); }
    const note = el('p', data.note); note.className = 'muted'; fragment.append(note);
    const groups = new Map();
    for (const item of data.labels) {
      if (!groups.has(item.key)) groups.set(item.key, []);
      groups.get(item.key).push(item);
    }
    for (const [key, items] of groups) {
      const card = el('details'); card.className = 'label-group'; card.dataset.key = key;
      card.open = Boolean(window.location.search);
      const summary = el('summary', `${key} (${items.length} values)`);
      if (items.some(item => item._matches?.['label.key']?.length)) summary.classList.add('filter-match');
      card.append(summary);
      const table = el('table'), head = el('thead'), headings = el('tr');
      const columns = [['Value', 'value'], ['Stacks', 'stacks'], ['Containers', 'containers'], ['Images', 'images'], ['Services', 'services'], ['Container templates', 'container_templates'], ['Networks', 'networks'], ['Volumes', 'volumes'], ['Nodes', 'nodes'], ['Engines', 'engines']];
      for (const [label, field] of columns) {
        const heading = el('th'); heading.scope = 'col';
        if (field === 'value') heading.textContent = label;
        else {
          heading.title = label; heading.setAttribute('aria-label', label); heading.className = 'label-count-heading';
          const accessible = el('span', label); accessible.className = 'visually-hidden';
          heading.append(dockerIcon(field), accessible);
        }
        headings.append(heading);
      }
      head.append(headings); table.append(head);
      const body = el('tbody'); table.append(body);
      for (const item of items) {
      const row = el('tr'); row.dataset.key = JSON.stringify([item.key, item.value]);
      for (const [, field] of columns) {
        const cell = el('td', item[field] ?? 0);
        const filters = new URLSearchParams({'label.key': item.key, 'label.value': item.value});
        if (field === 'value') {
          const link = el('a', item[field] || '(empty)');
          link.href = '/docker/labels?' + filters;
          cell.replaceChildren(link);
        } else if (item[field] > 0) {
          const link = el('a', item[field]);
          const page = ['stacks', 'containers', 'images', 'volumes'].includes(field) ? '/docker/' + field : '/docker/resources/' + field;
          link.href = page + '?' + filters; cell.replaceChildren(link);
        }
        if (item._matches?.['label.' + field]?.length) cell.classList.add('filter-match'); row.append(cell);
      }
      body.append(row);
      }
      card.append(table); fragment.append(card);
    }
    if (!data.labels.length) fragment.append(el('p', 'No matching labels.'));
    reconcileChildren(inventory, fragment);
  }
  let loading = false, again = false;
  async function refresh() {
    const generation = liveRefresh.generation;
    if (loading) { again = true; return; } loading = true;
    try {
      const response = await authenticatedFetch('/api/docker/labels' + window.location.search, {cache: 'no-store'});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || `Labels unavailable (HTTP ${response.status})`);
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

"use strict";
(() => {
  const view = document.getElementById('containers-view');
  const inventory = document.getElementById('containers-inventory');
  const error = document.getElementById('containers-error');
  const status = document.getElementById('containers-status');
  const el = (tag, text) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = String(text); return item; };
  const link = (text, url) => { const item = el('a', text); item.href = url; return item; };
  const stackLink = item => item.stack ? link(item.stack, `/docker/stacks/${encodeURIComponent(item.stack_kind)}/${encodeURIComponent(item.stack)}`) : document.createTextNode('—');
  function table(items, columns, key) {
    const grid = el('table'); const head = el('thead'); const titles = el('tr');
    columns.forEach(([title]) => titles.append(el('th', title))); head.append(titles); grid.append(head);
    const body = el('tbody'); grid.append(body);
    for (const item of items) {
      const row = el('tr'); row.dataset.key = String(item[key]);
      if (item.display_state) row.dataset.state = item.display_state;
      for (const [, field] of columns) {
        const cell = el('td', item[field] ?? '—');
        if (field === 'name' && item.id) cell.replaceChildren(link(item.name, '/docker/containers/' + encodeURIComponent(item.id)));
        if (field === 'name' && item.type === 'volume' && item.name) cell.replaceChildren(dockerVolumeLink(item.name));
        if (field === 'stack') cell.replaceChildren(stackLink(item));
        if (field === 'image' && item.image) cell.replaceChildren(dockerImageLink(item.image_id || item.image, item.image));
        if (field === 'logs') cell.replaceChildren(dockerLogsLink({scope: 'container', id: item.id}));
        if (['state', 'health'].includes(field) && item[field]) cell.replaceChildren(link(item[field], '/docker/containers?' + new URLSearchParams({['container.' + field]: item[field]})));
        if (item._matches?.['container.' + field]?.length) cell.classList.add('filter-match');
        row.append(cell);
      }
      body.append(row);
    }
    if (!items.length) { const row = el('tr'); const cell = el('td', 'No matching resources.'); cell.colSpan = columns.length; row.append(cell); body.append(row); }
    return grid;
  }
  function card(key, title) { const item = el('section'); item.className = 'card'; item.dataset.key = key; if (title) item.append(el('h2', title)); return item; }
  function render(data) {
    const fragment = document.createDocumentFragment();
    if (!view.dataset.id) {
      const list = card('containers');
      list.append(table(data.containers, [['Name', 'name'], ['Image', 'image'], ['Stack', 'stack'], ['Service', 'service'], ['State', 'state'], ['Health', 'health'], ['Status', 'status'], ['Logs', 'logs']], 'id'));
      fragment.append(list);
    } else {
      const summary = card('summary', data.name); summary.dataset.state = data.display_state;
      const actions = el('div'); actions.className = 'stack-controls'; actions.append(dockerLogsLink({scope: 'container', id: data.id})); summary.append(actions);
      const fields = el('dl');
      for (const [title, field] of [['ID', 'id'], ['Image', 'image'], ['Image ID', 'image_id'], ['State', 'state'], ['Health', 'health'], ['Stack', 'stack'], ['Created', 'created'], ['Started', 'started'], ['Finished', 'finished'], ['Exit code', 'exit_code'], ['Restart count', 'restart_count'], ['Restart policy', 'restart_policy']]) {
        const row = el('div'); const value = el('dd', data[field] ?? '—');
        if (field === 'stack') value.replaceChildren(stackLink(data));
        if (['image', 'image_id'].includes(field) && data[field]) value.replaceChildren(dockerImageLink(data.image_id || data.image, data[field]));
        row.append(el('dt', title), value); fields.append(row);
      }
      summary.append(fields); fragment.append(summary);
      if (Object.keys(data.labels || {}).length) {
        const labels = card('labels', 'Labels'); labels.append(dockerLabels(data.labels)); fragment.append(labels);
      }
      const networks = card('networks', 'Networks');
      networks.append(dockerEndpointTable(data.networks, {showNetwork: true}));
      for (const message of data.network_errors || []) { const warning = el('p', message); warning.className = 'alert warning'; networks.append(warning); }
      fragment.append(networks);
      const mounts = card('mounts', 'Mounts');
      mounts.append(table(data.mounts, [['Type', 'type'], ['Name', 'name'], ['Destination', 'destination'], ['Writable', 'writable']], 'destination'));
      fragment.append(mounts);
      const volumes = card('volumes', 'Volumes');
      if (data.volume_error) volumes.append(el('p', data.volume_error));
      volumes.append(dockerVolumeTable(data.volumes || [], view.dataset.write === 'true')); fragment.append(volumes);
    }
    reconcileChildren(inventory, fragment);
  }
  let loading = false, again = false;
  async function refresh() {
    const generation = liveRefresh.generation;
    if (loading) { again = true; return; }
    loading = true;
    try {
      const response = await authenticatedFetch('/api/docker/containers' + (view.dataset.id ? '/' + encodeURIComponent(view.dataset.id) : window.location.search), {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Container inventory unavailable (HTTP ${response.status})`);
      if (view.dataset.id) {
        try { const volumes = await dockerVolumeInventory({'volume.container': data.id}); data.volumes = volumes.volumes; data.volume_error = volumes.errors?.join('; '); }
        catch (failure) { data.volume_error = failure.message; }
      }
      if (generation !== liveRefresh.generation) return;
      render(data); error.hidden = true; liveRefresh.updated(status);
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { loading = false; if (again) { again = false; if (liveRefresh.enabled) refresh(); } }
  }
  refresh();
  if (view.dataset.live === 'true') {
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

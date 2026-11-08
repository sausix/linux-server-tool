"use strict";
(() => {
  const view = document.getElementById('volumes-view'), inventory = document.getElementById('volumes-inventory');
  const status = document.getElementById('volumes-status'), error = document.getElementById('volumes-error');
  const writable = view.dataset.write === 'true';
  const selectable = writable && !view.dataset.reference;
  const selected = new Set();
  let current, loading = false, again = false, storage = {}, storageLoading = false, storageStarted = false, storageInfo = 'Storage statistics not requested';
  const el = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  function review(mode, ids = []) {
    const form = el('form'); form.method = 'post'; form.action = '/docker/volumes/delete';
    for (const [name, value] of [['csrf_token', view.dataset.csrf], ['mode', mode], ...ids.map(name => ['ids', name])]) {
      const input = el('input'); input.type = 'hidden'; input.name = name; input.value = value; form.append(input);
    }
    document.body.append(form); form.submit();
  }
  function render() {
    if (selectable) {
      const visible = new Set(current.volumes.map(item => item.name));
      for (const name of selected) if (!visible.has(name)) selected.delete(name);
    }
    const fragment = document.createDocumentFragment();
    for (const message of current.errors || []) { const warning = el('p', message); warning.className = 'alert warning'; fragment.append(warning); }
    fragment.append(el('p', current.note));
    const toolbar = () => {
      const bar = el('div'); bar.className = 'image-toolbar';
      if (selectable) {
        const selection = el('div');
        for (const [text, action] of [['Select all', 'all'], ['Select none', 'none']]) {
          const button = el('button', text); button.type = 'button'; button.dataset.action = action; selection.append(button);
        }
        bar.append(selection);
      }
      const info = el('span', storageInfo); info.setAttribute('role', 'status'); bar.append(info);
      const actions = el('div'); actions.className = 'image-delete-actions';
      if (selectable) {
        const button = el('button', `Delete selected (${selected.size})`); button.type = 'button'; button.dataset.action = 'selected'; button.hidden = !selected.size; actions.append(button);
      }
      const refresh = el('button', storageLoading ? 'Loading statistics…' : 'Refresh storage'); refresh.type = 'button'; refresh.disabled = storageLoading;
      refresh.dataset.action = 'storage'; actions.append(refresh);
      if (writable && !view.dataset.reference && current.volumes.some(item => item.usage === 'unused')) {
        const button = el('button', 'Delete unused'); button.type = 'button'; button.title = 'Review all unused volumes on this endpoint, including volumes outside the current filter';
        button.dataset.action = 'unused'; actions.append(button);
      }
      bar.append(actions); return bar;
    };
    fragment.append(toolbar());
    const items = view.dataset.reference ? [current] : current.volumes;
    fragment.append(dockerVolumeTable(items.map(item => ({...item, size: storage[item.name] ?? item.size})), writable, {selectable, selected}));
    if (view.dataset.reference) {
      const summary = el('section'); summary.className = 'card'; summary.dataset.key = 'volume-details'; summary.append(el('h2', current.name));
      const fields = el('dl');
      const raw = current.inspect || {};
      for (const [title, value] of [['Name', current.name], ['Driver', current.driver], ['Scope', current.scope],
        ['Created', raw.CreatedAt ?? current.created], ['Mountpoint', raw.Mountpoint], ['Usage', current.usage], ['Anonymous', current.anonymous ? 'Yes' : 'No']]) {
        const row = el('div'); row.append(el('dt', title), el('dd', value ?? 'Unknown')); fields.append(row);
      }
      summary.append(fields); fragment.append(summary);
      for (const [title, value] of [['Labels', raw.Labels || current.labels], ['Driver options', raw.Options || current.options], ['Driver status', raw.Status], ['Usage data', raw.UsageData], ['Cluster volume', raw.ClusterVolume]]) {
        if (!value || !Object.keys(value).length) continue;
        const section = el('section'); section.className = 'card'; section.dataset.key = title; section.append(el('h2', title));
        const pre = el('pre', JSON.stringify(value, null, 2)); section.append(pre); fragment.append(section);
      }
      const refs = el('section'); refs.className = 'card'; refs.dataset.key = 'mounts'; refs.append(el('h2', 'Container mounts'));
      for (const container of current.container_items || []) {
        const row = el('p'), link = el('a', container.name || container.id); link.href = '/docker/containers/' + encodeURIComponent(container.id);
        row.append(link, document.createTextNode(` · ${container.state} · ${(container.destinations || []).join(', ')}`)); refs.append(row);
      }
      if (!current.container_items?.length) refs.append(el('p', 'No container mounts observed.'));
      fragment.append(refs);
      const inspect = el('details'); inspect.dataset.key = 'inspect'; inspect.append(el('summary', 'Complete Docker volume inspect'), el('pre', JSON.stringify(raw, null, 2))); fragment.append(inspect);
    }
    fragment.append(toolbar());
    reconcileChildren(inventory, fragment);
    syncSelection();
  }
  inventory.addEventListener('click', event => {
    const action = event.target.closest('button[data-action]')?.dataset.action;
    if (action === 'storage') loadStorage();
    else if (action === 'unused' && writable) review('unused');
    else if (action === 'selected' && selectable && selected.size) review('selected', [...selected]);
    else if (action === 'all' && selectable) { current.volumes.forEach(item => selected.add(item.name)); syncSelection(); }
    else if (action === 'none' && selectable) { selected.clear(); syncSelection(); }
  });
  function syncSelection() {
    inventory.querySelectorAll('input[data-volume-name]').forEach(input => { input.checked = selected.has(input.dataset.volumeName); });
    inventory.querySelectorAll('[data-action="selected"]').forEach(button => { button.hidden = !selected.size; button.textContent = `Delete selected (${selected.size})`; });
  }
  inventory.addEventListener('change', event => {
    const input = event.target.closest('input[data-volume-name]'); if (!input || !selectable) return;
    if (input.checked) selected.add(input.dataset.volumeName); else selected.delete(input.dataset.volumeName);
    syncSelection();
  });
  async function loadStorage() {
    if (storageLoading || !current) return;
    storageStarted = true; storageLoading = true; storageInfo = 'Loading storage statistics…'; render();
    try {
      const response = await authenticatedFetch('/api/docker/volumes/storage', {cache: 'no-store'});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Storage unavailable');
      storage = data.volumes || {}; storageInfo = `Storage snapshot: ${new Date().toLocaleTimeString()}`;
    } catch (failure) { storageInfo = 'Storage statistics: ' + failure.message; }
    finally { storageLoading = false; render(); }
  }
  async function refresh() {
    if (loading) { again = true; return; } loading = true;
    const generation = liveRefresh.generation;
    try {
      const query = view.dataset.reference ? '/detail?' + new URLSearchParams({volume: view.dataset.reference}) : window.location.search;
      const response = await authenticatedFetch('/api/docker/volumes' + query, {cache: 'no-store'});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || `Volumes unavailable (HTTP ${response.status})`);
      if (generation !== liveRefresh.generation) return;
      current = data; render(); error.hidden = true; liveRefresh.updated(status);
      if (!storageStarted) loadStorage();
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { loading = false; if (again) { again = false; if (liveRefresh.enabled) refresh(); } }
  }
  refresh();
  if (view.dataset.live === 'true') {
    const events = liveRefresh.events('/api/events?component=docker'); let timer;
    events.addEventListener('change', () => { clearTimeout(timer); timer = liveRefresh.schedule(refresh, 300); });
    events.addEventListener('ready', refresh);
    events.addEventListener('session-expired', checkSession);
    events.addEventListener('shutdown', () => { events.close(); status.textContent = 'Server is shutting down.'; });
    events.onerror = () => { status.textContent = 'Live connection interrupted · reconnecting'; checkSession(); };
    window.addEventListener('pagehide', () => { clearTimeout(timer); events.close(); });
  }
})();

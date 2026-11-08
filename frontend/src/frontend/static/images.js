"use strict";
(() => {
  const view = document.getElementById('images-view');
  const inventory = document.getElementById('images-inventory');
  const error = document.getElementById('images-error');
  const status = document.getElementById('images-status');
  const loadingIndicator = document.getElementById('images-loading');
  let currentData, storageData = null, storageLoading = false, storageStarted = false, storageError = '';
  let inventoryFingerprint = '', storageFingerprint = '', storageUpdated = null;
  const writable = view.dataset.write === 'true';
  const selected = new Set();
  let visibleIds = [];
  const el = (tag, text) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = String(text); return item; };
  function size(value) {
    if (value === null || value === undefined) return 'Unknown';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']; let index = 0, amount = value;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++; }
    return `${amount.toFixed(index ? 2 : 0)} ${units[index]} (${value.toLocaleString()} bytes)`;
  }
  const link = (text, href) => { const item = el('a', text); item.href = href; return item; };
  const stackLink = item => link(item.kind ? item.name : item.stack, `/docker/stacks/${encodeURIComponent(item.kind || item.stack_kind)}/${encodeURIComponent(item.kind ? item.name : item.stack)}`);
  function card(key, title) { const item = el('section'); item.className = 'card'; item.dataset.key = key; if (title) item.append(el('h2', title)); return item; }
  function references(title, items, renderer) {
    const section = card(title, title); const list = el('ul');
    for (const item of items) { const row = el('li'); row.dataset.key = item.id || (item.kind + ':' + item.name); renderer(row, item); list.append(row); }
    if (!items.length) list.append(el('li', 'None observed.'));
    section.append(list); return section;
  }
  function action(label, name) {
    const button = el('button', label); button.type = 'button'; button.dataset.action = name; return button;
  }
  function storageHeader(title) {
    const header = el('div'); header.className = 'image-storage-header'; header.append(el('h2', title));
    const controls = el('div'); controls.className = 'image-storage-controls';
    const info = el('span'); info.setAttribute('role', 'status');
    if (storageLoading) {
      if (!storageData) {
        const spinner = el('span'); spinner.className = 'loading-spinner'; spinner.setAttribute('aria-hidden', 'true'); controls.append(spinner);
      }
      info.textContent = storageData ? 'Refreshing…' : 'Loading statistics…';
    } else if (storageError) info.textContent = 'Statistics unavailable';
    else if (storageUpdated) {
      info.textContent = inventoryFingerprint !== storageFingerprint ? 'Image inventory changed' : `Updated ${storageUpdated.toLocaleTimeString()}`;
      info.title = `Storage snapshot: ${storageUpdated.toLocaleString()}. Refresh to recalculate.`;
    } else info.textContent = 'Statistics pending';
    const button = action('Refresh', 'storage-retry'); button.disabled = storageLoading;
    button.setAttribute('aria-label', 'Refresh storage statistics'); controls.append(info, button); header.append(controls);
    return header;
  }
  function storageWarning(section) {
    if (storageError) { const warning = el('p', storageError); warning.className = 'alert warning'; section.append(warning); }
  }
  function toolbar(unused, key) {
    const bar = el('div'); bar.className = 'image-toolbar'; bar.dataset.key = key;
    const selection = el('div'); selection.append(action('Select all', 'all'), action('Select none', 'none'));
    const deletion = el('div'); deletion.className = 'image-delete-actions';
    const chosen = action('Delete selected', 'selected'); chosen.hidden = !selected.size; deletion.append(chosen);
    if (unused) { const button = action(`Delete unused (${unused})`, 'unused'); button.title = 'Review all unused images on this endpoint, including images outside the current filter'; deletion.append(button); }
    bar.append(selection, deletion); return bar;
  }
  function syncSelection() {
    inventory.querySelectorAll('input[data-image-id]').forEach(input => { input.checked = selected.has(input.dataset.imageId); });
    inventory.querySelectorAll('[data-action="selected"]').forEach(button => { button.hidden = !selected.size; button.textContent = `Delete selected (${selected.size})`; });
  }
  function review(ids, mode) {
    const form = el('form'); form.method = 'post'; form.action = view.dataset.deleteUrl; form.hidden = true;
    for (const [name, value] of [['csrf_token', view.dataset.csrf], ['mode', mode], ...ids.map(id => ['ids', id])]) {
      const input = el('input'); input.type = 'hidden'; input.name = name; input.value = value; form.append(input);
    }
    document.body.append(form); form.submit();
  }
  inventory.addEventListener('change', event => {
    const input = event.target.closest('input[data-image-id]'); if (!input) return;
    if (input.checked) selected.add(input.dataset.imageId); else selected.delete(input.dataset.imageId);
    syncSelection();
  });
  inventory.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]'); if (!button) return;
    const kind = button.dataset.action;
    if (kind === 'storage-retry') { loadStorage(); return; }
    if (!writable) return;
    if (kind === 'all') visibleIds.forEach(id => selected.add(id));
    else if (kind === 'none') selected.clear();
    else if (kind === 'selected' && selected.size) review([...selected], 'selected');
    else if (kind === 'unused') review([], 'unused');
    else if (kind === 'delete') window.location.assign(view.dataset.deleteUrl + '?' + new URLSearchParams({image: button.dataset.imageId}));
    syncSelection();
  });
  function render(data) {
    if (storageData) {
      if (data.images) data = {...data, images: data.images.map(image => ({...image, ...(storageData.images[image.id] || {})}))};
      else data = {...data, ...(storageData.images[data.id] || {})};
    }
    const fragment = document.createDocumentFragment();
    for (const message of data.errors || []) { const warning = el('p', message); warning.className = 'alert warning'; fragment.append(warning); }
    const note = el('p', data.note); note.className = 'muted'; fragment.append(note);
    if (!view.dataset.reference) {
      visibleIds = data.images.map(image => image.id);
      for (const id of selected) if (!visibleIds.includes(id)) selected.delete(id);
      const stats = card('storage'); stats.append(storageHeader('Image storage · all images')); storageWarning(stats);
      const totals = el('dl');
      const storage = {...(data.storage || {})};
      if (storageData) {
        storage.layers_size = storageData.layers_size;
        // Statistics include all images, regardless of the inventory search filter.
        const values = Object.values(storageData.images);
        storage.unique_size = values.length === storage.images && values.every(image => image.unique_size != null) ? values.reduce((sum, image) => sum + image.unique_size, 0) : null;
      }
      for (const [label, value] of [['Images', storage.images ?? 'Unknown'], ['Active', storage.active ?? 'Unknown'],
        ['Referenced', storage.referenced ?? 'Unknown'], ['Unused', storage.unused ?? 'Unknown'], ['Unknown usage', storage.unknown ?? 'Unknown'],
        ['Image layer storage (Engine)', size(storage.layers_size)], ['Logical image sizes (shared layers counted repeatedly)', size(storage.logical_size)],
        ['Exclusive layers across images', size(storage.unique_size)]]) {
        const row = el('div'); row.append(el('dt', label), el('dd', value)); totals.append(row);
      }
      const unused = el('div'); unused.dataset.key = 'unused-storage';
      const unusedValue = el('dd', size(storage.unused_size)); unusedValue.className = 'unused-image-storage';
      if (writable && storage.unused > 0) unusedValue.append(action('Delete unused', 'unused'));
      const unusedIds = storage.unused_image_ids || [];
      const exclusive = storageData && unusedIds.length === storage.unused && unusedIds.every(id => storageData.images[id]?.unique_size != null)
        ? unusedIds.reduce((sum, id) => sum + storageData.images[id].unique_size, 0) : storage.unused === 0 ? 0 : null;
      const detail = el('small', `Exclusive layers: ${size(exclusive)}. Logical size can include shared layers; deletion may free less.`);
      detail.className = 'muted'; unusedValue.append(detail);
      unused.append(el('dt', 'Unused images (logical size)'), unusedValue); totals.append(unused);
      stats.append(totals); fragment.append(stats);
      const section = card('images'); const legend = el('p', 'Active: blue · Referenced: amber · Unused: gray · Unknown: standard background');
      section.append(legend);
      if (writable) section.append(toolbar(storage.unused || 0, 'toolbar-top'));
      const grid = el('table'), head = el('thead'), titles = el('tr');
      const columns = [['Name / tags', 'name'], ['Usage', 'usage'], ['Size', 'size'], ['Shared', 'shared_size'], ['Unique', 'unique_size'], ['Containers', 'containers'], ['Active containers', 'active'], ['Services', 'services'], ['Child images', 'children']];
      if (writable) titles.append(el('th', 'Select'));
      for (const [title] of columns) titles.append(el('th', title));
      if (writable) titles.append(el('th', 'Action'));
      head.append(titles); grid.append(head);
      const body = el('tbody'); grid.append(body);
      for (const image of data.images) {
        const row = el('tr'); row.dataset.key = image.id; row.dataset.usage = image.usage;
        if (writable) {
          const cell = el('td'); const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.dataset.imageId = image.id;
          checkbox.setAttribute('aria-label', 'Select ' + image.name); cell.append(checkbox); row.append(cell);
        }
        for (const [, field] of columns) {
          const cell = el('td', ['size', 'shared_size', 'unique_size'].includes(field) ? size(image[field]) : image[field]);
          if (field === 'name') {
            cell.replaceChildren();
            for (const name of image.tags.length ? image.tags : [image.name]) { if (cell.childNodes.length) cell.append(el('br')); cell.append(dockerImageLink(image.id, name)); }
          }
          if (field === 'usage') cell.replaceChildren(link(image.usage, '/docker/images?' + new URLSearchParams({'image.usage': image.usage})));
          if (image._matches?.['image.' + field]?.length || (field === 'name' && image._matches?.['image.tags']?.length)) cell.classList.add('filter-match');
          row.append(cell);
        }
        if (writable) { const cell = el('td'); const button = action('Delete', 'delete'); button.dataset.imageId = image.id; cell.append(button); row.append(cell); }
        body.append(row);
      }
      if (!data.images.length) { const row = el('tr'), cell = el('td', 'No matching images.'); cell.colSpan = columns.length + (writable ? 2 : 0); row.append(cell); body.append(row); }
      section.append(grid);
      if (writable) section.append(toolbar(storage.unused || 0, 'toolbar-bottom'));
      fragment.append(section);
    } else {
      const section = card('summary'); section.append(storageHeader(data.name)); storageWarning(section); section.dataset.usage = data.usage;
      const fields = el('dl');
      for (const [label, value] of [['ID', data.id], ['Usage', data.usage], ['Tags', data.tags.join(', ') || 'None'], ['Digests', data.digests.join(', ') || 'None'], ['Size', size(data.size)], ['Shared size', size(data.shared_size)], ['Unique size', size(data.unique_size)], ['Created', data.created], ['OS', data.os], ['Architecture', data.architecture], ['Variant', data.variant || '—'], ['Layers', data.layers]]) {
        const row = el('div'); row.append(el('dt', label), el('dd', value ?? 'Unknown')); fields.append(row);
      }
      section.append(fields); fragment.append(section);
      if (Object.keys(data.labels || {}).length) {
        const labels = card('labels', 'Labels'); labels.append(dockerLabels(data.labels)); fragment.append(labels);
      }
      if (data.parent_id) {
        const parent = card('parent', 'Parent image'); parent.append(dockerImageLink(data.parent_id)); fragment.append(parent);
      }
      fragment.append(references('Dependent child images', data.child_items || [], (row, item) => row.append(dockerImageLink(item.id, item.name))));
      fragment.append(references('Containers', data.container_items, (row, item) => {
        row.append(link(item.name || item.id, '/docker/containers/' + encodeURIComponent(item.id)), document.createTextNode(` · ${item.state}`));
        if (item.stack) row.append(document.createTextNode(' · Stack: '), stackLink(item));
      }));
      fragment.append(references('Services', data.service_items, (row, item) => {
        row.append(document.createTextNode(item.name));
        if (item.stack) row.append(document.createTextNode(' · Stack: '), stackLink(item));
      }));
      fragment.append(references('Stacks', data.stack_items, (row, item) => row.append(stackLink(item))));
    }
    reconcileChildren(inventory, fragment);
    syncSelection();
  }
  let loading = false, again = false;
  async function loadStorage() {
    if (!currentData) return;
    if (storageLoading) return;
    const requestedFingerprint = inventoryFingerprint;
    storageStarted = true; storageLoading = true; storageError = ''; render(currentData);
    try {
      const response = await authenticatedFetch('/api/docker/images/storage', {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Storage statistics unavailable (HTTP ${response.status})`);
      storageData = data; storageUpdated = new Date(); storageFingerprint = requestedFingerprint;
    } catch (failure) { storageError = failure.message; }
    finally {
      storageLoading = false; if (currentData) render(currentData);
    }
  }
  async function refresh() {
    const generation = liveRefresh.generation;
    if (loading) { again = true; return; } loading = true;
    view.setAttribute('aria-busy', 'true');
    loadingIndicator.hidden = Boolean(currentData);
    try {
      const query = view.dataset.reference ? '/detail?' + new URLSearchParams({image: view.dataset.reference}) : window.location.search;
      const response = await authenticatedFetch('/api/docker/images' + query, {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Images unavailable (HTTP ${response.status})`);
      if (generation !== liveRefresh.generation) return;
      inventoryFingerprint = JSON.stringify(data.images ? data.images.map(image => [image.id, image.size]) : [data.id, data.size]);
      currentData = data; render(data); error.hidden = true; liveRefresh.updated(status);
      if (!storageStarted) loadStorage();
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally {
      loading = false; view.setAttribute('aria-busy', 'false'); loadingIndicator.hidden = true;
      if (again) { again = false; if (liveRefresh.enabled) refresh(); }
    }
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

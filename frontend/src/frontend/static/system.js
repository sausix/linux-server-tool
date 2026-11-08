"use strict";
(() => {
  const inventory = document.getElementById('system-inventory'), status = document.getElementById('system-status'), error = document.getElementById('system-error');
  const el = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  const size = value => value == null ? 'Unknown' : `${(value / 1073741824).toLocaleString(undefined, {maximumFractionDigits: 2})} GiB`;
  const percent = value => value == null ? 'Sampling…' : `${value.toFixed(1)}%`;
  function stackedBar(segments, total, label, background = 'chart-off') {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 1000 24');
    svg.setAttribute('preserveAspectRatio', 'none'); svg.setAttribute('role', 'img'); svg.classList.add('resource-bar');
    svg.setAttribute('aria-label', label + ': ' + segments.map(segment => `${segment.label} ${segment.text}`).join(', '));
    const track = document.createElementNS(ns, 'rect'); track.setAttribute('width', '1000'); track.setAttribute('height', '24'); track.setAttribute('class', background); svg.append(track);
    let position = 0;
    if (Number.isFinite(total) && total > 0) for (const segment of segments) {
      if (!Number.isFinite(segment.value) || segment.value <= 0) continue;
      const width = Math.min(1000 - position, segment.value / total * 1000);
      const rect = document.createElementNS(ns, 'rect'); rect.setAttribute('x', position); rect.setAttribute('width', width); rect.setAttribute('height', '24'); rect.setAttribute('class', segment.color);
      rect.dataset.value = segment.value;
      const title = document.createElementNS(ns, 'title'); title.textContent = `${segment.label}: ${segment.text}`; rect.append(title); svg.append(rect); position += width;
    }
    return svg;
  }
  function chart(section, label, segments, total, background) {
    const row = el('div'); row.className = 'resource-chart'; row.append(el('p', label), stackedBar(segments, total, label, background)); section.append(row);
  }
  const segment = (label, value, color) => ({label, value, color, text: size(value)});
  function card(title, fields) {
    const section = el('section'); section.className = 'card'; section.dataset.key = title; section.append(el('h2', title));
    const list = el('dl');
    for (const [name, value, color] of fields) { const row = el('div'); if (color) row.className = color; row.append(el('dt', name), el('dd', value ?? 'Unknown')); list.append(row); }
    section.append(list); return section;
  }
  function table(title, items, columns) {
    const section = card(title, []), grid = el('table'), head = el('thead'), headings = el('tr');
    columns.forEach(([title]) => headings.append(el('th', title))); head.append(headings); grid.append(head);
    const body = el('tbody'); grid.append(body);
    for (const item of items) { const row = el('tr'); row.dataset.key = item.name || item.mountpoint;
      for (const [, field, formatter, color] of columns) {
        const cell = el('td'); if (color) cell.className = color;
        const value = formatter ? formatter(item[field]) : item[field] ?? '—'; cell.append(value instanceof Node ? value : document.createTextNode(String(value))); row.append(cell);
      }
      body.append(row);
    }
    if (!items.length) { const row = el('tr'), cell = el('td', 'No entries visible to the daemon.'); cell.colSpan = columns.length; row.append(cell); body.append(row); }
    section.append(grid); return section;
  }
  function render(data) {
    const fragment = document.createDocumentFragment(), cards = el('div'); cards.className = 'dashboard-cards';
    const uptime = data.uptime_seconds == null ? 'Unknown' : `${Math.floor(data.uptime_seconds / 86400)}d ${Math.floor(data.uptime_seconds % 86400 / 3600)}h ${Math.floor(data.uptime_seconds % 3600 / 60)}m`;
    cards.append(card('Operating system', [['Hostname', data.os.hostname], ['OS', data.os.name], ['Kernel', data.os.kernel], ['Architecture', data.os.architecture], ['Uptime', uptime]]));
    const memory = data.memory;
    const accounting = data.memory_accounting;
    const memoryCard = card('Memory', [['Total', size(memory.MemTotal)], ['Used (excluding cache / buffers)', size(accounting?.used), 'chart-used'],
      ['Buffers', size(accounting?.buffers), 'chart-buffers'], ['Page cache + reclaimable slab', size(accounting?.cache), 'chart-cache'], ['Free', size(accounting?.free), 'chart-free'], ['Available (estimate)', size(memory.MemAvailable)]]);
    if (accounting) chart(memoryCard, 'Physical memory', [segment('Used', accounting.used, 'chart-used'), segment('Buffers', accounting.buffers, 'chart-buffers'), segment('Cache', accounting.cache, 'chart-cache'), segment('Free', accounting.free, 'chart-free')], accounting.total);
    memoryCard.append(el('p', 'Available is a kernel estimate that overlaps free and reclaimable memory; it is not an additional segment.'));
    if (memory.SwapTotal > 0 && memory.SwapFree != null) {
      const used = memory.SwapTotal - memory.SwapFree;
      const details = el('dl');
      for (const [label, value, color] of [['Swap used', used, 'chart-swap'], ['Swap free', memory.SwapFree, 'chart-swap-free']]) { const row = el('div'); row.className = color; row.append(el('dt', label), el('dd', size(value))); details.append(row); }
      memoryCard.append(details); chart(memoryCard, 'Swap', [segment('Used', used, 'chart-swap'), segment('Free', memory.SwapFree, 'chart-swap-free')], memory.SwapTotal);
    }
    cards.append(memoryCard);
    const count = data.cpus.filter(cpu => cpu.name !== 'cpu').length;
    const loads = data.load.map(Number), labels = ['1 min', '5 min', '15 min'], colors = ['chart-used', 'chart-buffers', 'chart-cache'];
    const cpuCard = card('CPU', [['Model', data.cpu_model], ['Logical CPUs', count], ...loads.map((load, index) => [`Load · ${labels[index]}`, load.toFixed(2), colors[index]])]);
    const scale = Math.max(count, ...loads.filter(Number.isFinite), 1);
    loads.forEach((load, index) => chart(cpuCard, `Load · ${labels[index]}`, [{label: 'Load', value: load, text: load.toFixed(2), color: colors[index]}], scale, colors[index] + '-off'));
    cpuCard.append(el('p', `Shared scale: 0–${scale.toFixed(2)}; ${count} logical CPUs. Load counts runnable and uninterruptible tasks; it is not CPU usage. Time windows are shown separately, not added.`)); fragment.append(cards, cpuCard);
    const usageBar = value => {
      const row = el('div'); row.className = 'cpu-usage-bar'; row.append(el('span', percent(value)), stackedBar([{label: 'Busy', value, text: percent(value), color: 'chart-used'}], 100, 'CPU usage', 'chart-used-off')); return row;
    };
    const coreCard = table('CPU core usages', data.cpus.filter(cpu => cpu.name !== 'cpu'), [['CPU', 'name'], ['Usage', 'usage', usageBar]]); coreCard.classList.add('cpu-core-usages'); fragment.append(coreCard);
    fragment.append(table('Disks and partitions', data.devices, [['Device', 'device'], ['Type', 'type'], ['Parent', 'parent'], ['Size', 'size', size], ['Read only', 'read_only', value => value ? 'Yes' : 'No']]));
    const mounts = data.mounts.map(mount => ({...mount, reserved: mount.total != null && mount.used != null && mount.available != null ? Math.max(0, mount.total - mount.used - mount.available) : null}));
    const mountCard = card('Block-device mounts', []), deviceTable = el('table'), deviceHead = el('thead'), deviceTitles = el('tr');
    deviceTitles.append(el('th', 'Block device')); deviceHead.append(deviceTitles); deviceTable.append(deviceHead);
    const devices = new Map();
    for (const mount of mounts) { if (!devices.has(mount.device)) devices.set(mount.device, []); devices.get(mount.device).push(mount); }
    for (const [device, entries] of [...devices].sort(([left], [right]) => left.localeCompare(right))) {
      const deviceBody = el('tbody'); deviceBody.dataset.key = device; deviceTable.append(deviceBody);
      const deviceRow = el('tr'); deviceRow.dataset.key = device;
      const title = el('th', device); title.scope = 'rowgroup'; deviceRow.append(title); deviceBody.append(deviceRow);
      const detailRow = el('tr'); detailRow.dataset.key = device + ':mounts'; const detail = el('td'); detail.className = 'block-device-mounts';
      const group = table('', entries.sort((left, right) => left.mountpoint.localeCompare(right.mountpoint)), [['Mount', 'mountpoint'], ['Filesystem', 'filesystem'], ['Total', 'total', size], ['Used', 'used', size, 'chart-used'], ['Reserved / unavailable', 'reserved', size, 'chart-swap'], ['Available', 'available', size, 'chart-free'], ['Usage', 'usage', value => value == null ? 'Unknown' : percent(value)]]);
      // Keep the shared table renderer while avoiding a nested card or empty title.
      detail.append(group.querySelector('table'));
      for (const mount of entries) if (mount.total > 0 && mount.used != null && mount.available != null) chart(detail, mount.mountpoint,
        [segment('Used', mount.used, 'chart-used'), segment('Reserved / unavailable', mount.reserved, 'chart-swap'), segment('Available', mount.available, 'chart-free')], mount.total);
      detailRow.append(detail); deviceBody.append(detailRow);
    }
    if (!devices.size) { const body = el('tbody'), row = el('tr'); row.append(el('td', 'No block-device mounts visible to the daemon.')); body.append(row); deviceTable.append(body); }
    mountCard.append(deviceTable);
    fragment.append(mountCard);
    fragment.append(el('p', data.note));
    for (const message of data.errors) { const warning = el('p', displayHostPath(message)); warning.className = 'alert warning'; fragment.append(warning); }
    reconcileChildren(inventory, fragment);
  }
  let loading = false, timer;
  async function refresh() {
    if (loading) return; loading = true; const generation = liveRefresh.generation;
    try {
      const response = await authenticatedFetch('/api/system', {cache: 'no-store'}), data = await response.json();
      if (!response.ok) throw new Error(data.detail || `System unavailable (HTTP ${response.status})`);
      if (generation !== liveRefresh.generation) return;
      render(data); error.hidden = true; liveRefresh.updated(status);
    } catch (failure) { error.textContent = displayHostPath(failure.message); error.hidden = false; }
    finally { loading = false; if (liveRefresh.enabled) timer = liveRefresh.schedule(refresh, 5000); }
  }
  refresh();
  liveRefresh.onChange(() => { clearTimeout(timer); if (liveRefresh.enabled) refresh(); });
  window.addEventListener('pagehide', () => clearTimeout(timer));
})();

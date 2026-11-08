"use strict";
function dockerIcon(name) { return appIcon(name); }
function dockerVolumeLink(name) {
  const link = document.createElement('a'); link.textContent = name;
  link.href = '/docker/volumes/detail?' + new URLSearchParams({volume: name}); return link;
}
async function dockerVolumeInventory(filters) {
  const response = await authenticatedFetch('/api/docker/volumes?' + new URLSearchParams(filters), {cache: 'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `Volumes unavailable (HTTP ${response.status})`);
  return data;
}
function dockerVolumeTable(items, writable = false, {selectable = false, selected = new Set()} = {}) {
  const el = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  const link = (text, href) => { const node = el('a', text); node.href = href; return node; };
  const grid = el('table'); grid.className = 'volume-table';
  const head = el('thead'), titles = el('tr');
  const columns = [...(selectable ? [['Select', 'select']] : []), ['Name', 'name'], ['Anonymous', 'anonymous'], ['Driver', 'driver'], ['Usage', 'usage'], ['Size', 'size'], ['Containers', 'container'], ['Services', 'service'], ['Stacks', 'stack'], ['Labels', 'labels']];
  if (writable) columns.push(['Actions', 'actions']);
  for (const [title] of columns) titles.append(el('th', title));
  head.append(titles); grid.append(head); const body = el('tbody'); grid.append(body);
  const sorted = [...items].sort((left, right) => Number(Boolean(left.anonymous)) - Number(Boolean(right.anonymous)) || left.name.toLowerCase().localeCompare(right.name.toLowerCase()) || left.name.localeCompare(right.name));
  function reference(cell, field, ref, destination, icon) {
    if (cell.childNodes.length) cell.append(el('br'));
    cell.append(link(ref.name || ref.id, '/docker/volumes?' + new URLSearchParams({['volume.' + field]: field === 'container' ? ref.id : ref.name})));
    if (destination) {
      const jump = link('', destination); jump.className = 'resource-jump'; jump.title = `Open ${field}: ${ref.name || ref.id}`;
      jump.setAttribute('aria-label', jump.title); jump.append(dockerIcon(icon)); cell.append(document.createTextNode(' '), jump);
    }
  }
  for (const item of sorted) {
    const row = el('tr'); row.dataset.key = item.name; row.dataset.usage = item.usage;
    for (const [, field] of columns) {
      const cell = el('td');
      if (field === 'select') {
        const input = el('input'); input.type = 'checkbox'; input.dataset.volumeName = item.name;
        input.checked = selected.has(item.name); input.setAttribute('aria-label', 'Select ' + item.name); cell.append(input);
      }
      else if (field === 'name') cell.append(dockerVolumeLink(item.name));
      else if (field === 'anonymous') {
        const anonymous = Boolean(item.anonymous);
        const badge = link(anonymous ? 'Yes' : 'No', '/docker/volumes?' + new URLSearchParams({'volume.anonymous': String(anonymous)}));
        badge.title = anonymous ? 'Docker marks this volume with com.docker.volume.anonymous' : 'Docker does not mark this volume as anonymous'; cell.append(badge);
      }
      else if (field === 'usage') cell.append(link(item[field], '/docker/volumes?' + new URLSearchParams({['volume.' + field]: item[field]})));
      else if (field === 'size') cell.textContent = Number.isFinite(item.size) && item.size >= 0 ? `${(item.size / 1048576).toLocaleString(undefined, {maximumFractionDigits: 2})} MiB (${item.size.toLocaleString()} bytes)` : 'Unknown';
      else if (field === 'container' || field === 'stack') {
        const refs = field === 'container' ? item.container_items : item.stack_items;
        for (const ref of refs || []) {
          reference(cell, field, ref, field === 'container' ? '/docker/containers/' + encodeURIComponent(ref.id) : `/docker/stacks/${encodeURIComponent(ref.kind)}/${encodeURIComponent(ref.name)}`, field === 'container' ? 'containers' : 'stacks');
        }
        if (!cell.childNodes.length) cell.textContent = '—';
      } else if (field === 'service') {
        for (const ref of item.service_items || []) {
          reference(cell, field, ref, ref.stack ? `/docker/stacks/${encodeURIComponent(ref.stack_kind)}/${encodeURIComponent(ref.stack)}#service-${encodeURIComponent(ref.name)}` : '/docker/resources/services?' + new URLSearchParams({'resource.id': ref.id}), 'services');
        }
        if (!cell.childNodes.length) cell.textContent = '—';
      } else if (field === 'labels') cell.append(dockerLabels(item.labels));
      else if (field === 'actions') { const button = link('Delete', '/docker/volumes/delete?' + new URLSearchParams({volume: item.name})); button.className = 'button secondary'; cell.append(button); }
      else cell.textContent = item[field] ?? '—';
      if (item._matches?.['volume.' + field]?.length) cell.classList.add('filter-match');
      row.append(cell);
    }
    body.append(row);
  }
  if (!items.length) { const row = el('tr'), cell = el('td', 'No matching volumes.'); cell.colSpan = columns.length; row.append(cell); body.append(row); }
  return grid;
}
function dockerLogsLink(target, label = 'Logs') {
  const link = document.createElement('a'); link.textContent = label; link.className = 'button secondary';
  link.href = '/docker/logs?' + new URLSearchParams(target); link.target = '_blank'; link.rel = 'noopener noreferrer';
  link.dataset.logViewer = 'true'; return link;
}
document.addEventListener('click', event => {
  const link = event.target.closest?.('[data-log-viewer]');
  if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault(); window.open(link.href, '_blank', 'popup,width=1200,height=780,noopener,noreferrer');
});
function dockerEndpointTable(endpoints, {showNetwork = false, showContainer = false} = {}) {
  const element = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  const anchor = (text, href) => { const node = element('a', text); node.href = href; return node; };
  const stackURL = (kind, name) => `/docker/stacks/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`;
  const table = element('table'); table.className = 'endpoint-table';
  const head = element('thead'), headings = element('tr');
  const columns = [...(showNetwork ? [['Network', 'name']] : []),
    ...(showContainer ? [['Container', 'container'], ['Service', 'service']] : []),
    ['Interface', 'interface'], ...(showNetwork ? [['Bridge / parent', 'bridge_interface']] : []),
    ['Internal MAC', 'mac'], ['Port MAC', 'port_mac'], ['IPv4 / prefix', 'ip4'], ['IPv4 gateway', 'gateway4'], ['IPv6 / prefix', 'ip6'], ['IPv6 gateway', 'gateway6']];
  for (const [title] of columns) headings.append(element('th', title));
  head.append(headings); table.append(head);
  const body = element('tbody'); table.append(body);
  for (const endpoint of endpoints || []) {
    const row = element('tr'); row.dataset.key = `${endpoint.container_id}:${endpoint.network_id || endpoint.name}`;
    for (const [, field] of columns) {
      const value = endpoint[field], cell = element('td', value || '—');
      if (field === 'name' && value && endpoint.stack) cell.replaceChildren(anchor(value, stackURL(endpoint.stack_kind, endpoint.stack)));
      if (field === 'container' && value && endpoint.container_id) cell.replaceChildren(anchor(value, '/docker/containers/' + encodeURIComponent(endpoint.container_id)));
      if (field === 'service' && value && endpoint.service_stack) cell.replaceChildren(anchor(value, stackURL(endpoint.service_stack_kind, endpoint.service_stack)));
      if (['interface', 'bridge_interface'].includes(field) && value) {
        const link = anchor(value, '/network/interfaces?' + new URLSearchParams({'interface.name': value}));
        link.title = field === 'interface' ? endpoint.interface_source || 'Endpoint interface' : 'Network bridge or parent interface';
        cell.replaceChildren(link);
      }
      if (field === 'interface' && !value) cell.title = 'No unique namespace-scoped reciprocal veth pair could be established for this Docker endpoint.';
      if (['ip4', 'ip6', 'gateway4', 'gateway6'].includes(field) && value) {
        const prefix = endpoint[field + '_prefix_length'];
        const cidr = ['ip4', 'ip6'].includes(field) && Number.isInteger(prefix);
        const address = cidr ? `${value}/${prefix}` : value;
        cell.replaceChildren(anchor(address, '/network/interfaces?' + new URLSearchParams({[cidr ? 'interface.subnet' : 'interface.ip']: address})));
      }
      row.append(cell);
    }
    body.append(row);
  }
  if (!endpoints?.length) {
    const row = element('tr'), cell = element('td', 'No network endpoints reported.'); cell.colSpan = columns.length; row.append(cell); body.append(row);
  }
  return table;
}
function dockerStackControls(stack) {
  const controls = document.createElement('div'); controls.className = 'stack-controls';
  controls.setAttribute('role', 'toolbar'); controls.setAttribute('aria-label', 'Stack controls');
  for (const action of ['start', 'stop', 'restart', ...(stack.base_kind === 'swarm' || stack.kind === 'swarm' ? [] : ['apply'])]) {
    const link = document.createElement('a'); link.textContent = action[0].toUpperCase() + action.slice(1); link.className = 'button';
    link.href = `/docker/stack-actions/${encodeURIComponent(stack.kind)}/${encodeURIComponent(stack.name)}/${action}`;
    controls.append(link);
  }
  return controls;
}
function dockerImageLink(reference, label = reference) {
  const link = document.createElement('a');
  link.textContent = label;
  link.href = '/docker/images/detail?' + new URLSearchParams({image: reference});
  return link;
}
function dockerImageLinks(value) {
  const fragment = document.createDocumentFragment();
  for (const reference of (value || '').split(', ').filter(Boolean)) {
    if (fragment.childNodes.length) fragment.append(document.createTextNode(', '));
    fragment.append(dockerImageLink(reference));
  }
  if (!fragment.childNodes.length) fragment.append(document.createTextNode('—'));
  return fragment;
}
function dockerLabels(labels, title = 'Labels') {
  const details = document.createElement('details'); details.className = 'resource-labels';
  const entries = Object.entries(labels || {}).sort(([left], [right]) => left.localeCompare(right));
  const summary = document.createElement('summary'); summary.textContent = `${title} (${entries.length})`; details.append(summary);
  if (!entries.length) {
    const empty = document.createElement('p'); empty.textContent = 'No labels set.'; empty.className = 'muted'; details.append(empty);
    return details;
  }
  const table = document.createElement('table'), head = document.createElement('thead'), row = document.createElement('tr');
  for (const text of ['Key', 'Value']) { const cell = document.createElement('th'); cell.textContent = text; row.append(cell); }
  head.append(row); table.append(head);
  const body = document.createElement('tbody'); table.append(body);
  for (const [key, value] of entries) {
    const row = document.createElement('tr'); row.dataset.key = key;
    for (const text of [key, value]) { const cell = document.createElement('td'); cell.textContent = String(text); row.append(cell); }
    body.append(row);
  }
  details.append(table); return details;
}

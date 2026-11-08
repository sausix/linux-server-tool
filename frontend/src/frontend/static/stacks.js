"use strict";
(() => {
  const menu = document.querySelector('[data-stack-menu]');
  const menuDetails = menu?.closest('details');
  const view = document.getElementById('stacks-view');
  const inflight = new Map();
  const el = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (cls) item.className = cls;
    return item;
  };
  const href = stack => `/docker/stacks/${encodeURIComponent(stack.kind)}/${encodeURIComponent(stack.name)}`;
  function filter(field, value) {
    const link = el('a', value);
    link.href = '/docker/stacks?' + new URLSearchParams({['stack.' + field]: value});
    return link;
  }
  async function load(query = '') {
    const url = '/api/docker/stacks' + query;
    if (!inflight.has(url)) inflight.set(url, authenticatedFetch(url, {cache: 'no-store'}).then(async response => {
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `Stacks unavailable (HTTP ${response.status})`);
      return result;
    }).finally(() => inflight.delete(url)));
    return inflight.get(url);
  }
  async function loadMenu() {
    if (!menu) return;
    try {
      const data = await load();
      const fragment = document.createDocumentFragment();
      for (const stack of data.stacks) {
        const item = el('li');
        const link = el('a', stack.name);
        if (stack.kind === 'compose-meta') link.classList.add('compose-meta');
        link.prepend(dockerIcon('stacks'), document.createTextNode(' '));
        link.href = href(stack);
        if (window.location.pathname === link.pathname) link.setAttribute('aria-current', 'page');
        item.append(link); fragment.append(item);
      }
      if (!data.stacks.length) fragment.append(el('li', data.errors.length ? 'Stacks unavailable' : 'No stacks found', 'muted'));
      menu.replaceChildren(fragment);
    } catch (error) { menu.replaceChildren(el('li', error.message, 'muted')); }
  }
  menuDetails?.addEventListener('toggle', () => { if (menuDetails.open) loadMenu(); });
  if (menuDetails?.open) loadMenu();
  if (!view) return;
  const inventory = document.getElementById('stacks-inventory');
  const status = document.getElementById('stacks-status');
  const error = document.getElementById('stacks-error');
  function matched(cell, stack, field) {
    if (stack._matches?.['stack.' + field]?.length) cell.classList.add('filter-match');
    return cell;
  }
  function table(title, items, columns, keyField) {
    const card = el('section', undefined, 'card'); card.dataset.key = title;
    card.append(el('h2', title));
    const grid = el('table');
    const head = el('thead'); const headings = el('tr');
    for (const [label] of columns) headings.append(el('th', label));
    head.append(headings); grid.append(head);
    const body = el('tbody'); grid.append(body);
    for (const item of items) {
      const row = el('tr'); row.dataset.key = String(item[keyField]);
      for (const [, field] of columns) {
        const cell = el('td', item[field] ?? '—');
        if (field === 'labels') cell.replaceChildren(dockerLabels(item.labels));
        if (title === 'Containers' && field === 'name' && item.id) {
          const link = el('a', item.name);
          link.href = '/docker/containers/' + encodeURIComponent(item.id);
          cell.replaceChildren(link);
        }
        if (field === 'interface' && item[field]) {
          const link = el('a', item[field]);
          link.href = '/network/interfaces?' + new URLSearchParams({'interface.name': item[field]});
          link.title = item.interface_source || '';
          cell.replaceChildren(link);
        }
        row.append(cell);
      }
      body.append(row);
      if (title === 'Networks' && item.endpoints?.length) {
        const child = el('tr'); child.dataset.key = `${item[keyField]}:endpoints`; child.className = 'network-endpoints';
        const cell = el('td'); cell.colSpan = columns.length;
        cell.append(dockerEndpointTable(item.endpoints, {showContainer: true})); child.append(cell); body.append(child);
      }
      const mappedPorts = new Set((item.endpoints || []).map(endpoint => endpoint.interface).filter(Boolean));
      if (title === 'Networks') for (const port of (item.bridge_ports || []).filter(port => !mappedPorts.has(port.name))) {
        const child = el('tr'); child.dataset.key = `${item[keyField]}:port:${port.name}`; child.className = 'network-bridge-port';
        const name = el('td'), link = el('a', port.name);
        link.href = '/network/interfaces?' + new URLSearchParams({'interface.name': port.name});
        name.append(appIcon('interfaces'), document.createTextNode(' '), link);
        const details = el('td'); details.colSpan = columns.length - 1;
        const services = port.docker_services || [];
        if (!services.length) details.textContent = 'Service unknown';
        for (const service of services) {
          if (details.childNodes.length) details.append(document.createTextNode(', '));
          const target = el('a', service.service);
          target.href = `/docker/stacks/${encodeURIComponent(service.kind)}/${encodeURIComponent(service.stack)}`;
          details.append(target);
        }
        child.append(name, details); body.append(child);
      }
    }
    if (!items.length) { const row = el('tr'); const cell = el('td', 'No resources reported.'); cell.colSpan = columns.length; row.append(cell); body.append(row); }
    card.append(grid); return card;
  }
  function serviceTree(stack) {
    const card = el('section', undefined, 'card'); card.dataset.key = 'services-containers';
    card.append(el('h2', 'Services and containers'));
    const grid = el('table', undefined, 'service-tree');
    const head = el('thead'); const headings = el('tr');
    for (const title of ['Name', 'Mode', 'Image', 'State', 'Replicas / status', 'Logs']) headings.append(el('th', title));
    head.append(headings); grid.append(head);
    const assigned = new Set();
    function containerRow(body, item, service) {
      const row = el('tr', undefined, 'service-container'); row.dataset.key = 'container:' + item.id;
      row.dataset.state = item.display_state || item.state;
      const name = el('td'); const link = el('a', item.name);
      link.href = '/docker/containers/' + encodeURIComponent(item.id); name.append(link);
      const inheritedImages = service?.image ? service.image.split(', ') : [];
      const image = el('td');
      if (item.image && !inheritedImages.includes(item.image)) image.append(dockerImageLink(item.image_id || item.image, item.image));
      row.append(name, el('td'), image,
        el('td', item.display_state || item.state), el('td', item.status));
      const logs = el('td'); logs.append(dockerLogsLink({scope: 'container', id: item.id})); row.append(logs);
      body.append(row); assigned.add(item.id);
    }
    for (const service of stack.service_items) {
      const body = el('tbody'); body.dataset.key = 'service:' + (service.id || service.name);
      body.id = 'service-' + service.name;
      const row = el('tr', undefined, 'service-heading'); row.dataset.key = 'heading';
      row.dataset.state = service.state || 'unknown';
      const name = el('th', service.name); name.scope = 'row';
      if (service.mode !== 'compose' && Object.keys(service.labels || {}).length) name.append(dockerLabels(service.labels, 'Service labels'));
      const image = el('td');
      if (service.image_items?.length) {
        for (const item of service.image_items) {
          if (image.childNodes.length) image.append(document.createTextNode(', '));
          image.append(dockerImageLink(item.id || item.name, item.name));
        }
      } else image.append(dockerImageLinks(service.image));
      row.append(name, el('td', service.mode), image, el('td', service.state || 'unknown'),
        el('td', `${service.running ?? 'Unknown'} running · ${service.desired ?? 'Unknown'} desired`));
      const logs = el('td'); logs.append(dockerLogsLink({scope: 'service', kind: stack.base_kind || stack.kind, name: stack.name, service: service.name})); row.append(logs);
      body.append(row);
      const children = stack.container_items.filter(item => !assigned.has(item.id) &&
        ((service.id && service.id === item.service_id) || service.name === item.service));
      for (const item of children) containerRow(body, item, service);
      if (!children.length) {
        const empty = el('tr'); const cell = el('td', stack.kind === 'swarm' ? 'No containers visible on this Docker endpoint.' : 'No containers reported.');
        cell.colSpan = 6; empty.append(cell); body.append(empty);
      }
      grid.append(body);
    }
    const unassigned = stack.container_items.filter(item => !assigned.has(item.id));
    if (unassigned.length || !stack.service_items.length) {
      const body = el('tbody'); body.dataset.key = 'unassigned';
      const heading = el('tr'); const title = el('th', 'Containers without a known service'); title.colSpan = 6;
      heading.append(title); body.append(heading);
      for (const item of unassigned) containerRow(body, item, null);
      if (!unassigned.length) { const row = el('tr'); const cell = el('td', 'No services or containers reported.'); cell.colSpan = 6; row.append(cell); body.append(row); }
      grid.append(body);
    }
    card.append(grid); return card;
  }
  function metadataCard(stack) {
    const meta = stack.metadata;
    if (!meta?.present) return null;
    const card = el('section', undefined, 'card'); card.dataset.key = 'metadata';
    card.append(el('h2', 'Stack metadata · x-stackmeta'));
    if (meta.present) {
      if (Object.keys(meta.labels || {}).length) card.append(dockerLabels(meta.labels, 'Stack labels'));
      if (meta.description) card.append(el('p', meta.description, 'stack-description'));
      const settings = el('dl');
      for (const [label, value] of [['Autostart', meta.autostart ? 'Requested' : 'Disabled'],
        ['Extended startup', meta.eligible ? 'Eligible metadata' : 'Excluded']]) {
        const row = el('div'); row.append(el('dt', label), el('dd', value)); settings.append(row);
      }
      card.append(settings);
      card.append(el('p', 'Autostart intent is stored; manual controls use saved configuration and require confirmation.', 'muted'));
      for (const field of meta.invalid_fields || []) card.append(el('p', `${field.field}: ${field.reason}`, 'alert error'));
      for (const conflict of meta.restart_conflicts || []) card.append(el('p', `${conflict.service}: ${conflict.source} = ${JSON.stringify(conflict.policy)}`, 'alert warning'));
      if (meta.requires?.length) {
        const grid = el('table'), head = el('thead'), titles = el('tr');
        for (const title of ['Required stack', 'Required state', 'Assessment']) titles.append(el('th', title));
        head.append(titles); grid.append(head); const body = el('tbody'); grid.append(body);
        for (const required of meta.requires) {
          const row = el('tr'); row.dataset.key = required.stack; row.dataset.state = required.target_state;
          if (required.assessment === 'error') row.classList.add('dependency-error');
          const name = el('td');
          if (required.target_kind) {
            const link = el('a', required.stack); link.href = href({name: required.stack, kind: required.target_kind});
            if (required.target_kind === 'compose-meta') link.classList.add('compose-meta');
            name.append(link);
          } else name.textContent = required.stack;
          row.append(name, el('td', required.state), el('td', `${required.satisfied ? 'Satisfied' : required.assessment || 'Pending'} · ${required.reason || ''}`)); body.append(row);
        }
        card.append(grid);
      }
    }
    return card;
  }
  function definitionCard(stack) {
    if (stack.base_kind === 'swarm' || stack.kind === 'swarm') return null;
    const card = el('section', undefined, 'card'); card.dataset.key = 'compose-definition';
    card.append(el('h2', 'Compose definition'));
    const model = stack.compose_model, tree = model?.tree;
    const writable = view.dataset.definitionWrite === 'true';
    const base = '/docker/stack-models/' + encodeURIComponent(stack.name);
    const csrf = document.querySelector('input[name="csrf_token"]')?.value || '';
    const actionForm = (action, label, node = '') => {
      const form = el('form'); form.method = 'post'; form.action = base + '/change';
      for (const [key, value] of Object.entries({action, node, revision: tree?.revision || 0,
        expected_hash: model?.definition_hash || '', csrf_token: csrf})) {
        const field = el('input'); field.type = 'hidden'; field.name = key; field.value = value; form.append(field);
      }
      const button = el('button', label, 'secondary'); button.type = 'submit'; form.append(button);
      return form;
    };
    if (!tree) {
      if (writable && model?.definition_hash) card.append(actionForm('initialize', 'Initialize stack management'));
      else card.append(el('p', 'Stack management has not been initialized.', 'muted'));
    } else {
      const children = new Map();
      for (const node of tree.nodes) {
        const key = node.parent || ''; if (!children.has(key)) children.set(key, []);
        children.get(key).push(node);
      }
      const renderNodes = parent => {
        const list = el('ul', undefined, 'definition-tree');
        for (const node of children.get(parent) || []) {
          const item = el('li'), row = el('div', undefined, 'definition-node'); item.dataset.key = node.id;
          row.append(el('strong', displayHostPath(node.label)));
          if (tree.active === node.id) row.append(el('span', 'Active', 'definition-marker active'));
          if (tree.running === node.id && stack.running > 0) {
            const marker = el('span', 'Running', 'definition-marker running');
            marker.title = model.baseline_evidence === 'observed-unverified' ? 'Observed source; the original deployment definition cannot be verified.' : 'Last successfully applied definition.';
            row.append(marker);
          }
          if (view.dataset.definitionRead === 'true') {
            const edit = el('a', 'Edit', 'button secondary');
            edit.href = node.file ? '/docker/stack-files/edit?' + new URLSearchParams({name: stack.name, path: node.file}) : base + '/edit/' + node.id;
            row.append(edit);
          }
          if (writable) {
            const duplicate = el('a', 'Duplicate', 'button secondary'); duplicate.href = base + '/duplicate/' + node.id; row.append(duplicate);
            const derive = el('a', 'Derive', 'button secondary'); derive.href = base + '/edit/' + node.id + '?mode=derive'; row.append(derive);
            if (tree.active !== node.id) row.append(actionForm('activate', 'Set Active', node.id));
            if (node.can_delete) row.append(actionForm('delete', 'Delete', node.id));
          }
          item.append(row);
          if (children.has(node.id)) item.append(renderNodes(node.id));
          list.append(item);
        }
        return list;
      };
      card.append(renderNodes(''));
      if (writable) {
        const create = el('a', 'New Definition', 'button secondary'); create.href = base + '/new'; card.append(create);
      }
    }
    if (stack.running > 0) {
      const running = tree?.nodes.find(node => node.id === tree.running);
      const info = el('dl');
      for (const [label, value] of [['Running definition', running?.label || 'Unknown'],
        ['Working directory', stack.runtime?.directory || stack.working_directory || 'Unknown'],
        ['Created', stack.runtime?.created || 'Unknown'], ['Started', stack.runtime?.started || 'Unknown']]) {
        const row = el('div'), detail = el('dd', displayHostPath(value));
        if (['Created', 'Started'].includes(label) && value !== 'Unknown') {
          const date = new Date(value); if (!Number.isNaN(date.valueOf())) detail.textContent = date.toLocaleString();
          detail.title = label === 'Created' ? 'Earliest creation of the current containers' : 'Latest start of the running containers';
        }
        row.append(el('dt', label), detail); info.append(row);
      }
      card.append(info);
    }
    if (model?.error) card.append(el('p', model.error, 'alert warning'));
    return card;
  }
  function stackToolbar(position) {
    const toolbar = el('div', undefined, 'image-toolbar'); toolbar.dataset.key = 'stack-toolbar-' + position;
    toolbar.setAttribute('role', 'toolbar'); toolbar.setAttribute('aria-label', 'Stack actions');
    const actions = el('div', undefined, 'image-toolbar-actions');
    if (view.dataset.definitionWrite === 'true') {
      const add = el('a', 'Add stack', 'button'); add.href = '/docker/stack-create'; actions.append(add);
    }
    toolbar.append(actions); return toolbar;
  }
  function render(data) {
    const fragment = document.createDocumentFragment();
    for (const message of data.errors) fragment.append(el('p', message, 'alert warning'));
    fragment.append(el('p', data.note, 'muted'));
    if (!view.dataset.name) {
      const card = el('section', undefined, 'card'); card.dataset.key = 'stack-list';
      const grid = el('table'); const headings = el('tr');
      const columns = [['Name', 'name'], ['Kind', 'kind'], ['State', 'state'], ['Services', 'services'],
        ['Containers', 'containers'], ['Running', 'running'], ['Stopped', 'stopped'],
        ['Unhealthy', 'unhealthy'], ['Networks', 'networks'], ['Volumes', 'volumes']];
      for (const [label] of columns) headings.append(el('th', label));
      headings.append(el('th', 'Actions'));
      const head = el('thead'); head.append(headings); grid.append(head);
      const body = el('tbody'); grid.append(body);
      for (const stack of data.stacks) {
        const row = el('tr'); row.dataset.key = stack.kind + ':' + stack.name;
        row.dataset.state = stack.state;
        for (const [, field] of columns) {
          const cell = matched(el('td'), stack, field);
          if (field === 'name') { const link = el('a', stack.name); link.href = href(stack); cell.append(link); }
          else if (['kind', 'state'].includes(field)) cell.append(filter(field, stack[field]));
          else cell.textContent = stack[field];
          if (stack.kind === 'compose-meta' && ['name', 'kind'].includes(field)) cell.classList.add('compose-meta');
          row.append(cell);
        }
        const controls = el('td');
        const actions = view.dataset.definitionWrite === 'true' ? dockerStackControls(stack) : el('div', undefined, 'stack-controls');
        actions.append(dockerLogsLink({scope: 'stack', kind: stack.base_kind || stack.kind, name: stack.name}));
        controls.append(actions); row.append(controls);
        body.append(row);
      }
      if (!data.stacks.length) { const row = el('tr'); const cell = el('td', 'No matching stacks.'); cell.colSpan = columns.length + 1; row.append(cell); body.append(row); }
      card.append(stackToolbar('top'), grid, stackToolbar('bottom')); fragment.append(card);
    } else {
      const stack = data.stacks[0];
      if (!stack) fragment.append(el('p', 'Stack not found or no labelled resources remain.', 'alert warning'));
      else {
        document.getElementById('stacks-title')?.classList.toggle('compose-meta', stack.kind === 'compose-meta');
        const card = el('section', undefined, 'card'); card.dataset.key = 'summary';
        const actions = view.dataset.definitionWrite === 'true' ? dockerStackControls(stack) : el('div', undefined, 'stack-controls');
        actions.append(dockerLogsLink({scope: 'stack', kind: stack.base_kind || stack.kind, name: stack.name})); card.append(actions);
        card.dataset.state = stack.state;
        const summary = el('dl');
        for (const [label, value] of [['Kind', stack.kind], ['State', stack.state], ['Services', stack.services],
          ['Containers', stack.containers], ['Running', stack.running], ['Stopped', stack.stopped],
          ['Running replicas', stack.running_replicas ?? 'Unknown'], ['Desired replicas', stack.desired_replicas ?? 'Unknown']]) {
          const row = el('div'); const detail = el('dd', value);
          if (['Kind', 'State'].includes(label)) detail.replaceChildren(filter(label.toLowerCase(), value));
          row.append(el('dt', label), detail); summary.append(row);
        }
        card.append(summary); fragment.append(card);
        const definition = definitionCard(stack); if (definition) fragment.append(definition);
        const metadata = metadataCard(stack); if (metadata) fragment.append(metadata);
        fragment.append(serviceTree(stack));
        fragment.append(table('Networks', stack.network_items, [['Name', 'name'], ['Driver', 'driver'], ['Scope', 'scope'], ['Interface', 'interface'], ['Labels', 'labels']], 'name'));
        const volumes = el('section', undefined, 'card'); volumes.dataset.key = 'Volumes'; volumes.append(el('h2', 'Volumes'));
        if (data.volume_error) volumes.append(el('p', data.volume_error, 'alert warning'));
        volumes.append(dockerVolumeTable(data.volumes || stack.volume_items.map(item => ({...item, usage: 'unknown'})), view.dataset.definitionWrite === 'true'));
        fragment.append(volumes);
      }
    }
    reconcileChildren(inventory, fragment);
  }
  let loading = false, again = false, initialAnchor = true;
  async function refresh() {
    const generation = liveRefresh.generation;
    if (loading) { again = true; return; }
    loading = true;
    try {
      const query = view.dataset.name ? '?' + new URLSearchParams({'stack.name': view.dataset.name, 'stack.base_kind': view.dataset.kind === 'swarm' ? 'swarm' : 'compose'}) : window.location.search;
      const data = await load(query);
      if (view.dataset.name) {
        try { const volumes = await dockerVolumeInventory({'volume.stack': view.dataset.name}); data.volumes = volumes.volumes; data.volume_error = volumes.errors?.join('; '); }
        catch (failure) { data.volume_error = failure.message; }
      }
      if (generation !== liveRefresh.generation) return;
      render(data); error.hidden = true; liveRefresh.updated(status);
      if (initialAnchor && view.dataset.name) {
        initialAnchor = false;
        try { document.getElementById(decodeURIComponent(window.location.hash.slice(1)))?.scrollIntoView({block: 'start'}); }
        catch { /* Ignore malformed URL fragments. */ }
      }
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { loading = false; if (again) { again = false; if (liveRefresh.enabled) refresh(); } }
  }
  refresh();
  if (view.dataset.live === 'true') {
    const events = liveRefresh.events('/api/events?component=docker');
    let timer;
    events.addEventListener('ready', () => { status.textContent = 'Live · connected'; refresh(); });
    events.addEventListener('change', () => {
      clearTimeout(timer); timer = liveRefresh.schedule(() => { refresh(); if (menuDetails?.open) loadMenu(); }, 300);
    });
    events.addEventListener('session-expired', checkSession);
    events.addEventListener('shutdown', () => { events.close(); status.textContent = 'Server is shutting down. Reload after restart.'; });
    events.addEventListener('unavailable', () => { status.textContent = 'Live connection unavailable · reconnecting'; });
    events.onerror = () => { status.textContent = 'Live connection interrupted · reconnecting'; checkSession(); };
    window.addEventListener('pagehide', () => events.close());
  } else status.textContent = 'Snapshot · live events disabled';
})();

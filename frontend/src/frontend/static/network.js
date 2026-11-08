/* Render system data as text nodes; daemon-provided values never become HTML. */
"use strict";
const root = document.getElementById("network-view");
const inventory = document.getElementById("inventory");
const statusLine = document.getElementById("live-status");
const errorLine = document.getElementById("load-error");
let lastData;
let loading = false;
let reloadRequested = false;
let interfaceUI = {}, addressAccents = [];
function interfaceColor(element, name) {
  const accent = interfaceUI[name]?.accentcolor;
  if (accent) { element.classList.add('ui-accent'); element.style.setProperty('--ui-accent', accent); element.style.setProperty('--interface-accent', accent); }
  else element.classList.add('interface-system');
  return element;
}
function addressColor(element, value) {
  const match = addressAccents.find(item => addressInSubnets(value, [item.subnet]));
  if (match) { element.classList.add('ui-accent'); element.style.setProperty('--ui-accent', match.accentcolor); }
  return element;
}
function extension(item, shortcut = false) {
  const element = node(item.href ? 'a' : 'span', shortcut ? '' : item.text, shortcut ? 'category-shortcut' : 'ui-extension');
  if (item.href) element.href = item.href;
  element.style.setProperty('--extension-accent', item.accentcolor);
  element.title = item.text;
  if (shortcut) { element.setAttribute('aria-label', item.text); element.append(appIcon(item.icon || 'interfaces')); }
  return element;
}
function extensions(parent, name) {
  const ui = interfaceUI[name] || {};
  for (const item of [...(ui.links || []), ...(ui.badges || [])]) parent.append(extension(item));
}
function stateBadge(link) {
  const up = link.operational_state === 'up';
  const state = link.operational_state || 'unknown';
  const badge = highlight(filterLink('interface.state', state, up ? 'Up' : state === 'down' ? 'Down' : state === 'unknown' ? 'Unknown' : state), link, 'interface.state');
  badge.classList.add('state-badge', up ? 'state-up' : 'state-down'); return badge;
}
function scopeBadge(link) {
  const scope = link.scope || 'unknown';
  const badge = highlight(filterLink('interface.scope', scope, {loopback: 'Loopback', public: 'Public', private: 'Private', unknown: 'No address'}[scope] || scope), link, 'interface.scope');
  badge.classList.add('scope-badge'); badge.dataset.scope = scope; return badge;
}
function scopeAccent(element, link) {
  element.dataset.scope = link.scope || 'unknown';
  const accent = interfaceUI[link.name]?.accentcolor;
  if (accent) element.style.setProperty('--interface-accent', accent);
  return element;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function filterLink(field, value, label = value, view = 'interfaces') {
  const link = node('a', label);
  link.href = `${view === 'sysctl' ? '/system/sysctl' : '/network/' + view}?${new URLSearchParams({[field]: value})}`;
  if (field === 'interface.name') interfaceColor(link, value);
  return link;
}
function linkedAddress(value, label = value) {
  const link = filterLink(value.includes('/') ? 'interface.subnet' : 'interface.ip', value, label);
  return addressColor(link, value);
}
function interfaceShortcut(field, value) {
  const link = filterLink(field, value, '');
  link.classList.add('category-shortcut');
  link.title = 'Show matching interfaces'; link.setAttribute('aria-label', link.title);
  link.append(appIcon('interfaces')); return link;
}
function highlight(element, entry, fields, value) {
  const selected = (Array.isArray(fields) ? fields : [fields]).flatMap(field => entry?._matches?.[field] || []);
  if (selected.length && (value === undefined || selected.some(item => String(item).toLowerCase() === String(value).toLowerCase() || String(item).split('/')[0].toLowerCase() === String(value).split('/')[0].toLowerCase()))) {
    element.classList.add('filter-match');
    element.title = 'Matches the current filter';
  }
  return element;
}
function linkedRule(text) {
  const pre = node('pre');
  // Preserve rule formatting while linking literal addresses, including IPv6.
  for (const token of text.split(/((?:(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F]*:[0-9a-fA-F:]*:[0-9a-fA-F:]*)(?:\/\d{1,3})?)/g)) {
    if (parseSubnetAddress(token)) pre.append(linkedAddress(token));
    else pre.append(document.createTextNode(token));
  }
  return pre;
}
function renderInterfaces(data) {
  for (const link of data.interfaces || []) {
    const card = node("article", undefined, "card interface-card");
    scopeAccent(card, link);
    const heading = node('h2');
    heading.append(appIcon('interfaces'), document.createTextNode(' '));
    heading.append(highlight(filterLink('interface.name', link.name), link, 'interface.name'));
    const scope = scopeBadge(link); scope.classList.add('scope-badge');
    heading.append(document.createTextNode(' '), scope, stateBadge(link));
    extensions(heading, link.name);
    card.append(heading);
    const kind = link.kind || (link.flags?.includes("loopback") ? "loopback" : link.type === 1 ? "Ethernet" : `Link type ${link.type}`);
    const metadata = node('table');
    const labels = node('tr');
    for (const title of ['Type', 'State', 'MTU', 'MAC', 'Master', 'Flags']) labels.append(node('th', title));
    metadata.append(labels);
    const details = node('tr');
    details.append(highlight(node('td', kind), link, 'interface.type'), highlight(node('td', link.operational_state || 'unknown'), link, 'interface.state'), node('td', link.mtu));
    const mac = node('td');
    if (link.mac_address) mac.append(filterLink('interface.mac', link.mac_address)); else mac.textContent = '—';
    highlight(mac, link, 'interface.mac');
    const master = node('td');
    if (link.master) master.append(filterLink('interface.name', link.master)); else master.textContent = '—';
    details.append(mac, master);
    const flags = node('td', undefined, 'flags');
    for (const flag of link.flags || []) {
      flags.append(highlight(filterLink('interface.flag', flag), link, 'interface.flag', flag), document.createTextNode(' · '));
    }
    details.append(flags); metadata.append(details); card.append(metadata);
    if (link.kind === 'bridge') {
      card.append(node('h3', 'Bridge ports'));
      const ports = node('table');
      const titles = node('tr');
      const columns = new Map();
      for (const port of link.ports || []) for (const [key, value] of Object.entries(interfaceUI[port.name]?.columns || {})) columns.set(key, value.label);
      for (const title of ['Interface', 'State', 'MAC', 'Flags', ...columns.values()]) titles.append(node('th', title));
      ports.append(titles);
      for (const port of link.ports || []) {
        const row = node('tr');
        const name = node('td'); name.append(filterLink('interface.name', port.name), document.createTextNode(' '), scopeBadge(port));
        row.append(name, node('td', port.operational_state || 'unknown'), node('td', port.mac_address || '—'), node('td', (port.flags || []).join(', ')));
        for (const [key] of columns) {
          const cell = node('td');
          const items = interfaceUI[port.name]?.columns?.[key]?.items || [];
          for (const item of items) { if (cell.childNodes.length) cell.append(document.createTextNode(', ')); cell.append(extension(item)); }
          if (!items.length) cell.textContent = '—';
          row.append(cell);
        }
        ports.append(row);
      }
      if (!link.ports?.length) card.append(node('p', 'No bridge ports'));
      else card.append(ports);
      if (link.mac_source?.matching_ports?.length) {
        const origin = node('p', 'Bridge MAC source: ');
        for (const name of link.mac_source.matching_ports) origin.append(filterLink('interface.name', name), document.createTextNode(' '));
        if (link.mac_source.note) origin.append(document.createTextNode(link.mac_source.note));
        card.append(origin);
      }
    }
    const config = data.configuration?.[link.name] || {addresses: [], sources: [], complete: false};
    card.append(node("p", config.sources.length ? `Configuration: ${config.sources.map(displayHostPath).join(", ")}${config.dynamic ? " · dynamic addressing enabled" : ""}` : "Persistent configuration source unknown", "muted"));
    const list = node('table');
    const head = node('thead');
    const headings = node('tr');
    for (const title of ['Address / prefix', 'Family', 'Type', 'State / source', 'Action']) headings.append(node('th', title));
    head.append(headings); list.append(head);
    const body = node('tbody'); list.append(body);
    const writable = root.dataset.addressWrite === 'true';
    function addressRow(value, family, source, className, runtime) {
      const row = node('tr');
      const address = node('td'); address.append(linkedAddress(value, value));
      highlight(address, link, ['interface.ip', 'interface.ip4', 'interface.ip6', 'interface.subnet'], value);
      const classification = data.address_types?.[value.split('/')[0]];
      if (classification?.public ?? classification?.type === 'public') address.querySelector('a')?.classList.add('address-public');
      const type = node('td');
      if (classification) {
        type.append(highlight(filterLink('interface.address_type', classification.type, classification.label), link, 'interface.address_type', classification.type));
        if (classification.public ?? classification.type === 'public') type.classList.add('address-public');
        if (classification.scope) type.append(node('p', `Scope: ${classification.scope}`));
        if (classification.embedded_ipv4) type.append(node('p', `Embedded IPv4: ${classification.embedded_ipv4}`));
      } else type.textContent = 'Unknown';
      const state = node('td', source, className);
      if (writable && config.sources.length && runtime && !config.addresses.includes(value)) {
        const persist = node('a', 'Make persistent');
        persist.href = '/network/interfaces/address/persist?' + new URLSearchParams({interface: link.name, address: value});
        state.append(document.createTextNode(' · '), persist);
      }
      row.append(address, node('td', family), type, state);
      const action = node('td');
      if (runtime && writable) {
        const remove = node('a', 'Delete');
        remove.href = '/network/interfaces/address/delete?' + new URLSearchParams({interface: link.name, address: value});
        action.append(remove);
      } else if (!runtime && writable && config.sources.length) {
        const remove = node('a', 'Remove from configuration');
        remove.href = '/network/interfaces/address/remove-config?' + new URLSearchParams({interface: link.name, address: value});
        action.append(remove);
      }
      row.append(action); body.append(row);
    }
    const running = (data.addresses || []).filter(address => address.interface === link.name);
    const runningValues = running.map(address => `${address.address}/${address.prefix_length}`);
    for (const address of running) {
      const value = `${address.address}/${address.prefix_length}`;
      const persistent = config.addresses.includes(value);
      const differs = !persistent && config.complete && !config.dynamic;
      const label = persistent ? "persistent" : differs ? "runtime / not declared" : "runtime · source unknown or dynamic";
      addressRow(value, address.family, label, persistent ? 'persistent' : differs ? 'drift' : 'muted', true);
    }
    for (const value of config.addresses) {
      if (!runningValues.includes(value)) {
        addressRow(value, value.includes(':') ? 'ipv6' : 'ipv4', 'Runtime deleted', 'runtime-deleted', false);
      }
    }
    if (!body.children.length) {
      const row = node('tr'); const cell = node('td', 'No addresses'); cell.colSpan = 5; row.append(cell); body.append(row);
    }
    card.append(list);
    if (writable) {
      const add = node('a', 'Add address', 'button');
      add.href = '/network/interfaces/address/add?' + new URLSearchParams({interface: link.name});
      card.append(add);
    }
    inventory.append(card);
  }
}
function firewallTables(data) {
  if (data.tables) return new Map(data.tables.map((table, index) => [index, {...table, chains: new Map(table.chains.map(chain => [chain.name, chain]))}]));
  const tables = new Map();
  function table(family, name, backend = 'nftables') {
    const key = `${backend}:${family}:${name}`;
    if (!tables.has(key)) tables.set(key, {family, name, backend, chains: new Map()});
    return tables.get(key);
  }
  for (const entry of data.ruleset?.nftables || []) {
    const value = entry.table || entry.chain || entry.rule;
    if (!value) continue;
    const group = table(value.family, entry.table ? value.name : value.table);
    if (entry.table) group._matches = value._matches;
    if (entry.chain) group.chains.set(value.name, {...value, rules: group.chains.get(value.name)?.rules || []});
    if (entry.rule) {
      if (!group.chains.has(value.chain)) group.chains.set(value.chain, {name: value.chain, rules: []});
      group.chains.get(value.chain).rules.push({text: JSON.stringify(value.expr, null, 2), _matches: value._matches});
    }
  }
  for (const [family, raw] of [["ip", data.ipv4_rules], ["ip6", data.ipv6_rules]]) {
    let current;
    for (const line of (raw || "").split("\n")) {
      if (line.startsWith("*")) current = table(family, line.slice(1), data.backends?.find(name => name !== 'nftables') || data.backend);
      else if (current && line.startsWith(":")) {
        const [name, policy] = line.slice(1).split(/\s+/);
        const matches = data._legacy_matches?.[`${family}:${current.name}:${name}`];
        current.chains.set(name, {name, policy, rules: [], _matches: matches});
        if (matches?.['firewall.table'] || matches?.['firewall.family']) current._matches = matches;
      } else if (current && line.startsWith("-A ")) {
        const name = line.split(/\s+/)[1];
        if (!current.chains.has(name)) current.chains.set(name, {name, rules: []});
        const matches = data._legacy_matches?.[`${family}:${current.name}:${line}`];
        current.chains.get(name).rules.push({text: line, _matches: matches});
        if (matches?.['firewall.table'] || matches?.['firewall.family']) current._matches = matches;
      }
    }
  }
  return tables;
}
function renderFirewall(data, destination = inventory) {
  destination.append(node("p", `Backend: ${data.backend}`));
  if (data.tools) destination.append(node('p', `Tools: ${Object.values(data.tools).join(', ')}`, 'muted'));
  for (const error of data.errors || []) destination.append(node("p", error, "alert warning"));
  const tables = firewallTables(data);
  if (!tables.size) destination.append(node("p", data.errors?.length ? "Rules could not be read completely." : "No tables reported."));
  for (const table of tables.values()) {
    const card = node("article", undefined, "card");
    card.dataset.key = JSON.stringify([table.backend, table.family, table.name]);
    const heading = node('h2', `${table.backend} · ${table.family} · `);
    heading.prepend(appIcon('firewall'), document.createTextNode(' '));
    heading.append(highlight(filterLink('firewall.table', table.name, table.name, 'firewall'), table, ['firewall.table', 'firewall.family']));
    card.append(heading);
    if (!table.chains.size) card.append(node("p", "No chains"));
    for (const chain of table.chains.values()) {
      const section = node("section", undefined, "chain");
      section.dataset.key = chain.name;
      const chainHeading = node('h3');
      const chainLink = filterLink('firewall.chain', chain.name, chain.name, 'firewall');
      chainLink.href += `&${new URLSearchParams({'firewall.table': table.name, 'firewall.family': table.family, 'firewall.backend': table.backend})}`;
      chainHeading.append(highlight(chainLink, chain, 'firewall.chain'));
      section.append(chainHeading);
      section.append(highlight(node("p", [chain.type, chain.hook && `hook ${chain.hook}`, chain.prio !== undefined && `priority ${chain.prio}`, chain.policy && `policy ${chain.policy}`].filter(Boolean).join(" · "), "muted"), chain, 'firewall.policy'));
      if (!chain.rules.length) section.append(node("p", "No rules"));
      if (chain.policy_counter) section.append(node('p', `Policy counters [packets:bytes]: ${chain.policy_counter}`, 'muted'));
      const rules = node('table', undefined, 'firewall-rules');
      const headers = node('tr');
      for (const title of ['# / Handle', 'Match expression', 'Action / target', 'Packets', 'Bytes', 'Details']) headers.append(node('th', title));
      const head = node('thead'); head.append(headers); rules.append(head);
      const body = node('tbody'); rules.append(body);
      for (const [index, rule] of chain.rules.entries()) {
        const row = node('tr');
        row.dataset.key = rule.id || String(index);
        row.append(node('td', rule.handle || index + 1));
        const match = node('td');
        const text = node('pre');
        for (const part of rule.expression_parts || [{text: rule.expression || rule.text || 'any'}]) {
          if (part.interface) text.append(highlight(interfaceColor(filterLink('interface.name', part.interface, part.text), part.interface), rule, 'firewall.interface', part.interface));
          else {
            const addresses = linkedRule(part.text);
            text.append(...addresses.childNodes);
          }
        }
        text.classList.add('rule-expression');
        if (rule.compatibility_notes?.length) text.title = rule.compatibility_notes.join('\n');
        match.append(highlight(text, rule, ['firewall.rule', 'firewall.expression']));
        row.append(match);
        const verdict = rule.action === 'jump' && rule.targets?.length === 1 && !rule.jump_targets?.length;
        const action = highlight(node('td', verdict ? rule.targets[0].toLowerCase() : rule.action || '—'), rule, 'firewall.action');
        for (const target of verdict ? [] : rule.targets || []) {
          action.append(document.createTextNode(' '));
          if ((rule.jump_targets || []).includes(target)) {
            const link = filterLink('firewall.chain', target, target, 'firewall');
            link.href += '&' + new URLSearchParams({'firewall.table': table.name, 'firewall.family': table.family, 'firewall.backend': table.backend});
            action.append(highlight(link, rule, 'firewall.target', target));
          } else action.append(node('span', target));
        }
        row.append(action);
        for (const key of ['packets', 'bytes']) row.append(node('td', rule.counters?.length ? rule.counters.map(counter => counter[key] ?? (counter.name ? `named: ${counter.name}` : '—')).join(' / ') : '—'));
        const raw = node('td');
        raw.dataset.key = 'raw';
        const details = node('details'); details.append(node('summary', 'Raw'), node('pre', rule.raw || rule.text || ''));
        for (const note of rule.compatibility_notes || []) details.append(node('p', note, 'muted'));
        raw.append(details); row.append(raw); body.append(row);
      }
      if (chain.rules.length) section.append(rules);
      card.append(section);
    }
    destination.append(card);
  }
}
function renderRouting(data) {
  inventory.append(node('p', data.source_note, 'muted'));
  for (const error of data.errors || []) inventory.append(node('p', error, 'alert warning'));
  const routesCard = node('article', undefined, 'card');
  routesCard.append(node('h2', 'Routing tables'));
  const table = node('table');
  const headings = node('tr');
  for (const heading of ['Family', 'Table', 'Destination', 'Gateway', 'Interface', 'Metric', 'Type']) headings.append(node('th', heading));
  table.append(headings);
  for (const route of data.routes || []) {
    const row = node('tr');
    for (const value of [route.family, route.table_name || route.table,
      route.destination === 'default' ? 'default' : `${route.destination}/${route.prefix_length}`,
      route.gateway, route.interface, route.metric, route.type].entries()) {
      const [index, content] = value;
      const cell = node('td');
      const field = ['route.family', 'route.table', 'route.destination', 'route.gateway', 'route.interface', 'route.metric', 'route.type'][index];
      if ([0, 1, 6].includes(index) && content !== undefined) cell.append(filterLink(field, String(index === 1 ? route.table : content), content, 'routing'));
      else if (index === 4 && content) {
        cell.append(interfaceColor(filterLink('route.interface', content, content, 'routing'), content),
          document.createTextNode(' '), interfaceShortcut('interface.name', content));
        for (const item of interfaceUI[content]?.links || []) cell.append(document.createTextNode(' '), extension(item, true));
      }
      else if ((index === 3 || index === 2) && content) {
        const localField = index === 2 && content !== 'default' ? 'route.subnet' : field;
        const localLink = filterLink(localField, String(content), content, 'routing');
        addressColor(localLink, String(content));
        cell.append(localLink);
        if (content !== 'default') cell.append(document.createTextNode(' '),
          interfaceShortcut(index === 2 ? 'interface.subnet' : 'interface.ip', String(content)));
      }
      else cell.textContent = content ?? '—';
      highlight(cell, route, index === 4 ? [field, 'interface.name', 'interface.ip', 'interface.ip4', 'interface.ip6', 'interface.subnet'] : index === 2 ? [field, 'route.subnet'] : field);
      row.append(cell);
    }
    table.append(row);
  }
  routesCard.append(table);
  if (root.dataset.view === 'routing') inventory.append(routesCard);
  const groups = new Map();
  for (const option of data.options || []) {
    const title = `${option.family.toUpperCase()} · ${option.interface ? `Interface ${option.interface}` : 'Global'}`;
    if (!groups.has(title)) groups.set(title, []);
    groups.get(title).push(option);
  }
  for (const [title, options] of groups) {
    const card = node('details', undefined, 'card');
    card.open = title.endsWith('Global');
    const summary = node('summary', `${title} (${options.length})`);
    if (options[0].interface) {
      summary.replaceChildren(document.createTextNode(`${options[0].family.toUpperCase()} · Interface `),
        interfaceColor(node('span', options[0].interface), options[0].interface), document.createTextNode(` (${options.length})`));
    }
    for (const option of options) highlight(summary, option, ['sysctl.family', 'option.family', 'sysctl.interface', 'option.interface', 'interface.name']);
    card.append(summary);
    if (options[0].interface && !['all', 'default'].includes(options[0].interface)) card.append(filterLink('interface.name', options[0].interface, 'Show interface'));
    const table = node('table');
    const headings = node('tr');
    for (const heading of ['Kernel option / classification', 'Kernel default', 'Live value', 'Persistent candidates / source']) headings.append(node('th', heading));
    table.append(headings);
    for (const option of options) {
      const row = node('tr');
      const keyCell = node('td');
      keyCell.append(filterLink('sysctl.key', option.key, option.key, 'sysctl'));
      if (option.interface) interfaceColor(keyCell.firstChild, option.interface);
      keyCell.append(document.createElement('br'), filterLink('prefix', option.prefix || option.key.split('.').slice(0, -1).join('.'), 'Prefix', 'sysctl'));
      for (const classification of option.classes || []) keyCell.append(document.createTextNode(' · '), filterLink('class', classification, classification, 'sysctl'));
      highlight(keyCell, option, ['sysctl.key', 'option.key', 'sysctl.prefix', 'option.prefix', 'sysctl.classes', 'option.classes', 'sysctl.state', 'option.state']);
      row.append(keyCell);
      const defaultCell = node('td', option.kernel_default ?? '?');
      if (option.kernel_default_source) {
        const source = option.kernel_default_source;
        defaultCell.append(node('p', `${source.config_key} — ${displayHostPath(source.file)}:${source.line}`, 'muted'));
        defaultCell.append(node('p', `Kernel ${source.kernel_release}`, 'muted'));
      }
      defaultCell.title = option.default_note || 'The original kernel default is not exposed by procfs.';
      row.append(defaultCell);
      const candidates = [option.systemd_candidate, option.procps_candidate].filter(Boolean);
      const normalized = value => String(value).trim().split(/\s+/).join(' ');
      const comparable = candidates.length ? candidates.map(candidate => candidate.value) : option.kernel_default != null ? [option.kernel_default] : [];
      const differs = comparable.length && comparable.every(value => normalized(value) !== normalized(option.value));
      const matches = comparable.length && comparable.every(value => normalized(value) === normalized(option.value));
      const liveValue = highlight(node('td', option.value, differs ? 'drift' : matches ? 'persistent' : 'muted'), option, ['sysctl.value', 'option.value']);
      if (option.unit) liveValue.append(node('small', ` · ${option.unit}`, 'muted'));
      row.append(liveValue);
      const source = node('td');
      for (const [label, candidate] of [['systemd', option.systemd_candidate], ['procps', option.procps_candidate]]) {
        if (candidate) source.append(node('p', `${label}: ${candidate.value} — ${displayHostPath(candidate.file)}:${candidate.line}`));
      }
      if (!candidates.length) source.append(node('span', 'No persistent assignment found', 'muted'));
      for (const runtime of option.configuration_sources || []) {
        const label = node('p', `Configuration Source: ${runtime.name} (${runtime.confidence})`); label.title = runtime.description; source.append(label);
        source.append(node('small', runtime.description, 'muted'));
      }
      if (option.state === 'default') source.append(node('p', option.kernel_default !== null && option.kernel_default !== undefined ? 'default: matches local build default' : 'default: unconfigured; original kernel default unverified', 'muted'));
      if (option.ambiguous) source.append(node('p', 'Configuration candidates disagree', 'warning'));
      if (option.sources.length) {
        const history = node('details');
        history.append(node('summary', 'Declarations'));
        for (const item of option.sources) history.append(node('p', `${item.key} = ${item.value ?? '(excluded from wildcard assignment)'} — ${displayHostPath(item.file)}:${item.line}`));
        source.append(history);
      }
      row.append(source);
      highlight(source, option, ['sysctl.source', 'option.source']);
      table.append(row);
    }
    card.append(table);
    inventory.append(card);
  }
}
function render(data) {
  lastData = data;
  interfaceUI = data.ui?.interfaces || {};
  addressAccents = data.ui?.address_accents || [];
  if (root.dataset.view === 'firewall') {
    const fresh = document.createDocumentFragment();
    renderFirewall(data, fresh);
    reconcileChildren(inventory, fresh);
    return;
  }
  inventory.replaceChildren();
  if (root.dataset.view === 'overview') {
    const links = [...(data.interfaces || [])].sort((left, right) =>
      Number(right.scope === 'loopback') - Number(left.scope === 'loopback') || left.name.localeCompare(right.name));
    const byName = new Map(links.map(link => [link.name, link]));
    const isBridgePort = link => byName.get(link.master)?.kind === 'bridge';
    function box(link, ancestors = new Set()) {
      const section = scopeAccent(node('article', undefined, 'interface-box'), link); section.dataset.name = link.name;
      if (link.kind === 'bridge') section.classList.add('bridge-box');
      const heading = node('div', undefined, 'interface-box-heading');
      const name = filterLink('interface.name', link.name); name.className = 'interface-name';
      name.prepend(appIcon('interfaces'), document.createTextNode(' '));
      const scope = scopeBadge(link); scope.classList.add('scope-badge');
      heading.append(name, scope, stateBadge(link));
      section.append(heading, node('span', link.mac_address || 'No MAC', 'muted'));
      if (link.kind === 'bridge' && !ancestors.has(link.name)) {
        const ports = node('div', undefined, 'bridge-port-boxes');
        const seen = new Set([...ancestors, link.name]);
        for (const port of links.filter(port => port.master === link.name && !seen.has(port.name))) ports.append(box(port, seen));
        if (ports.children.length) section.append(ports);
      }
      return section;
    }
    for (const link of links.filter(link => link.kind !== 'bridge' && !isBridgePort(link))) inventory.append(box(link));
    for (const link of links.filter(link => link.kind === 'bridge' && !isBridgePort(link))) inventory.append(box(link));
    if (!inventory.children.length) inventory.append(node('p', 'No interfaces reported.', 'muted'));
  }
  else if (root.dataset.view === 'interfaces') renderInterfaces(data);
  else if (['routing', 'sysctl'].includes(root.dataset.view)) renderRouting(data);
  else renderFirewall(data);
}
if (root.dataset.view === 'firewall') document.addEventListener('selectionchange', () => {
  if (lastData && window.getSelection()?.isCollapsed) render(lastData);
});
async function refresh() {
  const generation = liveRefresh.generation;
  if (loading) { reloadRequested = true; return; }
  loading = true;
  try {
    const response = await authenticatedFetch(`${root.dataset.view === 'sysctl' ? '/api/system/sysctl' : '/api/network/' + root.dataset.view}${window.location.search}`, {cache: "no-store"});
    if (!response.ok) {
      const failure = await response.json().catch(() => ({}));
      throw new Error(failure.detail || `Inventory unavailable (HTTP ${response.status})`);
    }
    const data = await response.json();
    if (generation !== liveRefresh.generation) return;
    render(data);
    liveRefresh.updated(statusLine);
    errorLine.hidden = true;
  } catch (error) { errorLine.textContent = displayHostPath(error.message); errorLine.hidden = false; }
  finally { loading = false; if (reloadRequested) { reloadRequested = false; if (liveRefresh.enabled) refresh(); } }
}
refresh();
if (root.dataset.live === "true") {
  const events = liveRefresh.events(root.dataset.view === 'sysctl' ? '/api/events?component=system' : '/api/events');
  let timer;
  events.addEventListener("ready", () => { statusLine.textContent = "Live · connected"; refresh(); });
  events.addEventListener("change", () => {
    statusLine.textContent = `Live · updated ${new Date().toLocaleTimeString()}`;
    clearTimeout(timer); timer = liveRefresh.schedule(refresh, 200);
  });
  events.addEventListener("unavailable", () => { statusLine.textContent = "Live connection unavailable · reconnecting"; });
  events.addEventListener("shutdown", () => {
    events.close();
    statusLine.textContent = "Server is shutting down. Reload this page after restart.";
  });
  events.addEventListener('session-expired', () => { checkSession(); });
  events.onerror = () => { statusLine.textContent = "Live connection interrupted · reconnecting"; checkSession(); };
  window.addEventListener("pagehide", () => events.close());
  for (const topic of JSON.parse(root.dataset.extensionTopics || '[]')) {
    const source = liveRefresh.events('/api/events?' + new URLSearchParams({component: topic}));
    source.addEventListener('change', () => { clearTimeout(timer); timer = liveRefresh.schedule(refresh, 200); });
    source.addEventListener('ready', refresh);
    source.addEventListener('session-expired', checkSession);
    source.addEventListener('shutdown', () => source.close());
    window.addEventListener('pagehide', () => source.close());
  }
} else statusLine.textContent = "Snapshot · live events disabled in service";

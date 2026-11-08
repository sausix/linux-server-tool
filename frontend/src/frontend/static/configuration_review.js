"use strict";
(() => {
  const root = document.getElementById('configuration-review'), inventory = document.getElementById('configuration-inventory');
  const status = document.getElementById('configuration-status'), error = document.getElementById('configuration-error');
  const el = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  function render(data) {
    const fragment = document.createDocumentFragment(); fragment.append(el('p', data.note));
    for (const message of data.errors || []) { const warning = el('p', displayHostPath(message)); warning.className = 'alert warning'; fragment.append(warning); }
    for (const item of data.items) {
      const card = el('section'); card.className = 'card configuration-setting'; card.dataset.key = item.key;
      card.append(el('h2', item.title), el('code', item.key));
      const current = el('p', `Current value: ${item.value ?? 'Unavailable'} · ${item.state}`); current.className = 'configuration-current'; card.append(current);
      if (item.error) { const warning = el('p', displayHostPath(item.error)); warning.className = 'alert warning'; card.append(warning); }
      const choices = el('div'); choices.className = 'configuration-choices'; choices.setAttribute('role', 'list');
      const values = [...item.choices];
      if (item.value != null && !values.some(choice => choice.value === item.value)) values.push({value: item.value, explanation: 'This value is not covered by the documented choices. Consult the running kernel and distribution documentation; no generic meaning is assumed.'});
      for (const choice of values) {
        const box = el('div'); box.className = 'configuration-choice'; box.dataset.key = choice.value; box.setAttribute('role', 'listitem');
        const selected = item.value != null && choice.value === item.value;
        if (selected) { box.classList.add('is-current'); box.setAttribute('aria-current', 'true'); }
        box.append(el('h3', `${choice.value}${selected ? ' · Current' : ''}`), el('p', choice.explanation)); choices.append(box);
      }
      card.append(choices);
      if (item.note) card.append(el('p', item.note));
      if (item.assessment) { const analysis = el('p', item.assessment); analysis.className = 'alert warning'; card.append(analysis); }
      for (const runtime of item.configuration_sources || []) {
        card.append(el('h3', 'Configuration Source'), el('p', `${runtime.name} (${runtime.confidence})`), el('p', runtime.description));
      }
      const sources = el('details'); sources.dataset.key = 'sources'; sources.append(el('summary', 'Live path and persistent configuration'));
      sources.append(el('p', displayHostPath(item.path)));
      for (const [name, candidate] of [['systemd candidate', item.systemd_candidate], ['procps candidate', item.procps_candidate]]) if (candidate) sources.append(el('p', `${name}: ${candidate.value} — ${displayHostPath(candidate.file)}:${candidate.line}`));
      for (const source of item.sources || []) sources.append(el('p', `${source.key} = ${source.value ?? '(excluded)'} — ${displayHostPath(source.file)}:${source.line}`));
      if (!item.sources?.length) sources.append(el('p', 'No persistent declaration found in the inspected files.'));
      card.append(sources);
      const documentation = el('a', 'Kernel documentation'); documentation.href = item.documentation; documentation.target = '_blank'; documentation.rel = 'noopener noreferrer'; card.append(documentation); fragment.append(card);
    }
    reconcileChildren(inventory, fragment);
  }
  let loading = false, timer;
  async function refresh() {
    if (loading) return; loading = true; const generation = liveRefresh.generation;
    try {
      const response = await authenticatedFetch(`/api/${root.dataset.category}/configuration`, {cache: 'no-store'}), data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Configuration unavailable (HTTP ${response.status})`);
      if (generation !== liveRefresh.generation) return;
      render(data); error.hidden = true; liveRefresh.updated(status);
    } catch (failure) { error.textContent = displayHostPath(failure.message); error.hidden = false; }
    finally { loading = false; if (liveRefresh.enabled) timer = liveRefresh.schedule(refresh, 5000); }
  }
  refresh(); liveRefresh.onChange(() => { clearTimeout(timer); if (liveRefresh.enabled) refresh(); });
  window.addEventListener('pagehide', () => clearTimeout(timer));
})();

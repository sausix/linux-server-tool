"use strict";
(() => {
  const target = JSON.parse(document.getElementById('log-target').textContent);
  const controls = document.querySelector('[data-log-controls]'), limit = document.getElementById('line-limit');
  const liveButton = document.querySelector('[data-log-live]'), status = document.querySelector('[data-log-status]');
  const error = document.querySelector('[data-log-error]'), note = document.querySelector('[data-log-note]');
  const lines = document.querySelector('.log-lines'), colors = new Map();
  let events = null, fetching = false, live = false, generation = 0, bufferLimit = Number(limit.value), checkpoint = null;
  const query = () => new URLSearchParams({...target, limit: bufferLimit});
  function warning(message) { error.textContent = message; error.hidden = !message; }
  function trim() { while (lines.childElementCount > bufferLimit) lines.firstElementChild.remove(); }
  function applyLimit() { if (!limit.reportValidity()) return false; bufferLimit = Number(limit.value); trim(); return true; }
  function append(record, autoScroll = true) {
    const atEnd = autoScroll && lines.scrollHeight - lines.scrollTop - lines.clientHeight < 50;
    const key = record.container_id || record.name;
    if (!colors.has(key)) colors.set(key, (colors.size * 137.507764 + 25) % 360);
    const row = document.createElement('div'); row.className = 'log-line';
    row.style.setProperty('--log-source-color', `hsl(${colors.get(key)} 70% 73%)`);
    const time = document.createElement('time'); time.textContent = record.timestamp || '—';
    if (record.timestamp) time.dateTime = record.timestamp;
    if (record.stream === 'stderr') time.className = 'log-stderr';
    time.title = record.stream === 'combined' ? 'TTY: stdout and stderr are combined' : record.stream;
    const name = document.createElement('span'); name.className = 'log-source';
    name.textContent = record.service ? `${record.service} / ${record.name}` : record.name;
    name.title = record.container_id || '';
    const text = document.createElement('span'); text.className = 'log-text';
    text.textContent = record.text + (record.truncated ? ' [line truncated]' : '');
    row.append(time, name, text); lines.append(row); trim();
    if (atEnd) lines.scrollTop = lines.scrollHeight;
    if (live) status.textContent = `Live · ${lines.childElementCount} buffered lines`;
  }
  function disconnect() { events?.close(); events = null; }
  function connect() {
    disconnect(); if (!live) return;
    status.textContent = 'Connecting live logs…';
    const params = query(); if (checkpoint) params.set('checkpoint', checkpoint);
    const stream = new EventSource('/api/docker/logs/live?' + params); events = stream; checkpoint = null;
    stream.addEventListener('ready', () => { if (events === stream) status.textContent = 'Live · connected'; });
    stream.addEventListener('log', event => { if (events === stream) append(JSON.parse(event.data)); });
    stream.addEventListener('log-warning', event => { if (events === stream) warning(JSON.parse(event.data).message); });
    stream.addEventListener('log-error', event => { if (events === stream) { warning(JSON.parse(event.data).message); stop(); } });
    stream.addEventListener('shutdown', () => { if (events === stream) { warning('Server is shutting down. Reopen Live after restart.'); stop(); } });
    stream.addEventListener('session-expired', async () => {
      if (events !== stream) return;
      disconnect();
      try { await authenticatedFetch('/api/session', {cache: 'no-store'}); if (live) fetchRecent(); } catch (_) { stop(); }
    });
    stream.onerror = async () => {
      if (events !== stream) return;
      disconnect(); warning('Live log connection interrupted. Fetch recent lines before reconnecting.'); stop();
      await checkSession();
    };
  }
  function stop() { live = false; disconnect(); liveButton.setAttribute('aria-pressed', 'false'); status.textContent = `${lines.childElementCount} buffered lines`; }
  async function fetchRecent() {
    if (fetching || !applyLimit()) return;
    fetching = true; const version = ++generation; disconnect(); warning(''); status.textContent = 'Loading recent lines…';
    const button = controls.querySelector('[type="submit"]'); button.disabled = true; liveButton.disabled = true;
    try {
      const response = await authenticatedFetch('/api/docker/logs?' + query(), {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Logs unavailable');
      if (version !== generation) return;
      checkpoint = data.checkpoint || null;
      lines.replaceChildren();
      for (const record of data.lines) append(record, false);
      lines.scrollTop = lines.scrollHeight;
      note.textContent = data.note || ''; warning((data.errors || []).join('\n'));
      status.textContent = `${lines.childElementCount} lines · ${data.sources.length} containers`;
      if (live) connect();
    } catch (failure) { warning(failure.message); stop(); }
    finally { fetching = false; button.disabled = false; liveButton.disabled = false; }
  }
  controls.addEventListener('submit', event => { event.preventDefault(); fetchRecent(); });
  limit.addEventListener('change', applyLimit);
  liveButton.addEventListener('click', () => {
    if (!applyLimit()) return;
    if (live) stop(); else { live = true; warning(''); liveButton.setAttribute('aria-pressed', 'true'); fetchRecent(); }
  });
  window.addEventListener('pagehide', () => { ++generation; stop(); });
  fetchRecent();
})();

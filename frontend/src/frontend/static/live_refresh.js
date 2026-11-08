"use strict";
// One session preference controls inventory subscriptions and scheduled refreshes.
const liveRefresh = (() => {
  const changes = new EventTarget(), timers = new Set(), statuses = new Map();
  let enabled = document.body.dataset.liveRefresh !== 'false', generation = 0;
  function paintStatus(element, time) {
    const serviceLive = element.closest('[data-live]')?.dataset.live === 'true';
    element.textContent = `${enabled && serviceLive ? 'Live · ' : ''}Updated ${time}`;
  }
  const api = {
    get enabled() { return enabled; },
    get generation() { return generation; },
    updated(element) {
      const time = new Date().toLocaleTimeString(); statuses.set(element, time); paintStatus(element, time);
    },
    onChange(callback) { changes.addEventListener('change', callback); },
    schedule(callback, delay) {
      if (!enabled) return;
      const timer = setTimeout(() => { timers.delete(timer); if (enabled) callback(); }, delay);
      timers.add(timer); return timer;
    },
    events(url) {
      const target = new EventTarget(); let source = null, closed = false;
      const sync = () => {
        source?.close(); source = null;
        if (!enabled || closed) return;
        const current = new EventSource(url); source = current;
        for (const type of ['ready', 'change', 'unavailable', 'shutdown', 'session-expired']) {
          current.addEventListener(type, event => {
            if (enabled && source === current) target.dispatchEvent(new MessageEvent(type, {data: event.data}));
          });
        }
        current.onerror = event => { if (enabled && source === current) target.onerror?.(event); };
      };
      target.close = () => { closed = true; source?.close(); source = null; changes.removeEventListener('change', sync); };
      changes.addEventListener('change', sync); sync(); return target;
    },
    setEnabled(value) {
      if (enabled === value) return;
      generation++;
      enabled = value; document.body.dataset.liveRefresh = String(value);
      if (!enabled) { for (const timer of timers) clearTimeout(timer); timers.clear(); }
      for (const [element, time] of statuses) paintStatus(element, time);
      if (!enabled) for (const element of document.querySelectorAll('#live-status, #stacks-status, #containers-status, #images-status, #labels-status, #resources-status')) {
        if (!statuses.has(element)) element.textContent = 'Refresh paused';
      }
      changes.dispatchEvent(new Event('change'));
    },
  };
  const form = document.querySelector('[data-refresh-toggle]');
  form?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button'), error = form.querySelector('[data-refresh-error]');
    const previous = enabled, next = !enabled;
    button.disabled = true; error.hidden = true;
    // Stop streams immediately, before persisting the preference.
    if (!next) api.setEnabled(false);
    try {
      const body = new URLSearchParams({enabled: String(next), csrf_token: form.elements.csrf_token.value});
      const response = await authenticatedFetch(form.action, {method: 'POST', body});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Unable to save refresh preference');
      api.setEnabled(result.enabled);
    } catch (failure) {
      api.setEnabled(previous); error.textContent = failure.message; error.hidden = false;
    } finally {
      button.disabled = false; button.textContent = enabled ? 'Live refresh' : 'No refresh';
      button.setAttribute('aria-pressed', String(enabled));
    }
  });
  return api;
})();

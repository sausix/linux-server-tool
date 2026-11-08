"use strict";
(() => {
  const form = document.querySelector('[data-compose-editor]');
  if (!form || form.dataset.confirmation === 'true') return;
  const text = form.querySelector('[data-editor-text]'), name = form.querySelector('[data-stack-name]');
  const base = form.querySelector('[data-base-input]'), revisionInput = form.querySelector('[data-draft-revision]');
  const status = form.querySelector('[data-draft-status]'), discard = form.querySelector('[data-discard-draft]');
  const rebase = form.querySelector('[data-rebase-draft]');
  const endpoint = '/api/docker/stack-drafts/' + encodeURIComponent(form.dataset.draftKey);
  let revision = 0, baseHash = base.value || 'new', saved = '', saving = null, timer, bypass = false, initialized = false;
  const state = () => JSON.stringify([text.value, name.value, baseHash]);
  const params = () => new URLSearchParams({content: text.value, stack_name: name.value, base_hash: baseHash,
    revision: String(revision), csrf_token: form.elements.csrf_token.value});
  function update() {
    revisionInput.value = revision || '';
    if (discard) discard.hidden = !revision;
    if (rebase) rebase.hidden = baseHash === form.dataset.sourceHash;
  }
  async function flush() {
    clearTimeout(timer);
    if (!initialized) { status.textContent = 'Loading draft before review…'; await restored; }
    if (text.readOnly) return true;
    if (saving) { try { await saving; } catch (_) { return false; } return flush(); }
    if (state() === saved) return true;
    const snapshot = state(), body = params();
    status.textContent = 'Saving draft…';
    saving = (async () => {
      const response = await authenticatedFetch(endpoint, {method: 'POST', body, keepalive: new Blob([body.toString()]).size < 60000});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Draft could not be saved');
      revision = data.revision; saved = snapshot; update(); status.textContent = 'Draft saved · ' + new Date().toLocaleTimeString();
    })();
    try { await saving; return true; }
    catch (failure) { status.textContent = failure.message + ' Keep this page open; the edited text is still here.'; return false; }
    finally { saving = null; }
  }
  async function restore() {
    saved = state();
    text.disabled = true; name.disabled = true;
    try {
      const response = await authenticatedFetch(endpoint, {cache: 'no-store'});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Draft lookup failed');
      if (data.draft) {
        text.value = data.draft.content; baseHash = data.draft.base_hash; revision = data.draft.revision;
        base.value = baseHash === 'new' ? '' : baseHash;
        if (!name.readOnly && name.type !== 'hidden') name.value = data.draft.stack_name;
        saved = state(); status.textContent = baseHash === form.dataset.sourceHash ? 'Saved draft restored.' : 'Draft restored. The source changed; review it and explicitly rebase before saving.';
      }
      update();
    } catch (failure) { status.textContent = failure.message + ' Existing drafts have not been overwritten.'; text.readOnly = true; }
    finally { initialized = true; text.disabled = false; name.disabled = false; }
  }
  const changed = () => { if (!initialized || text.readOnly) return; bypass = false; status.textContent = 'Draft has unsaved changes…'; clearTimeout(timer); timer = setTimeout(flush, 700); };
  text.addEventListener('input', changed); name.addEventListener('input', changed);
  form.querySelector('[data-save-draft]')?.addEventListener('click', flush);
  rebase?.addEventListener('click', async () => {
    baseHash = form.dataset.sourceHash; base.value = baseHash === 'new' ? '' : baseHash; update(); await flush();
  });
  discard?.addEventListener('click', async () => {
    if (!window.confirm('Discard this saved draft and reload the source?')) return;
    if (!await flush()) return;
    try {
      const response = await authenticatedFetch(endpoint, {method: 'DELETE', body: new URLSearchParams({revision: String(revision), csrf_token: form.elements.csrf_token.value})});
      if (!response.ok) throw new Error((await response.json()).detail || 'Draft was not discarded');
      saved = state(); bypass = true; window.location.reload();
    } catch (failure) { status.textContent = failure.message; }
  });
  let reviewing = false;
  form.addEventListener('submit', async event => {
    if (bypass || text.readOnly) return;
    event.preventDefault();
    if (reviewing) return;
    reviewing = true;
    const submitter = event.submitter;
    try {
      if (await flush()) {
        // Native submit dispatch may still be active at the microtask checkpoint.
        // A new task lets the browser clear its reentrant submission guard.
        await new Promise(resolve => setTimeout(resolve, 0));
        bypass = true;
        form.requestSubmit(submitter || undefined);
      }
    } finally { reviewing = false; }
  });
  window.addEventListener('beforeunload', event => {
    if (!bypass && initialized && (saving || state() !== saved)) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => {
    clearTimeout(timer);
    if (!text.readOnly && initialized && state() !== saved && !saving && navigator.sendBeacon) navigator.sendBeacon(endpoint, params());
  });
  const restored = restore();
})();

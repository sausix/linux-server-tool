"use strict";
(() => {
  const root = document.getElementById('stack-job'); if (!root) return;
  const status = root.querySelector('[data-job-status]'), error = root.querySelector('[data-job-error]');
  let timer, closed = false;
  async function refresh() {
    const generation = liveRefresh.generation;
    if (closed) return;
    try {
      const response = await authenticatedFetch('/api/docker/stack-jobs/' + encodeURIComponent(root.dataset.id), {cache: 'no-store'});
      const job = await response.json(); if (!response.ok) throw new Error(job.detail || 'Operation status unavailable');
      if (generation !== liveRefresh.generation) return;
      status.textContent = `${job.action}: ${job.status}`; error.textContent = job.error || ''; error.hidden = !job.error;
      if (['succeeded', 'failed'].includes(job.status)) return;
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    if (!closed) timer = liveRefresh.schedule(refresh, 1000);
  }
  liveRefresh.onChange(() => { clearTimeout(timer); if (liveRefresh.enabled && !closed) refresh(); });
  refresh(); window.addEventListener('pagehide', () => { closed = true; clearTimeout(timer); });
})();

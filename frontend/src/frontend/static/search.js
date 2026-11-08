"use strict";
for (const form of document.querySelectorAll('[data-inventory-search]')) {
  const input = form.querySelector('input[name="q"]');
  const clear = form.querySelector('.search-clear');
  const toggle = form.querySelector('.search-help-toggle');
  const help = form.querySelector('.search-help');
  const initial = input.value.trim();
  function markCurrent() {
    input.classList.toggle('filter-current', Boolean(initial) && input.value.trim() === initial);
  }
  markCurrent();
  let navigating = false;
  function apply() {
    if (navigating || input.value.trim() === initial) return;
    navigating = true;
    const url = new URL(form.action, window.location.href);
    const value = input.value.trim();
    if (value) url.searchParams.set('q', value);
    window.location.assign(url.pathname + url.search);
  }
  form.addEventListener('submit', event => { event.preventDefault(); apply(); });
  form.addEventListener('focusout', () => {
    // Allow the clear/help buttons to handle their clicks before applying edits.
    queueMicrotask(() => { if (!form.contains(document.activeElement)) apply(); });
  });
  input.addEventListener('input', () => { clear.hidden = !input.value; markCurrent(); });
  input.addEventListener('search', () => { if (!input.value) apply(); });
  clear.hidden = !input.value;
  clear.addEventListener('click', () => {
    input.value = '';
    markCurrent();
    clear.hidden = true;
    input.focus();
    apply();
  });
  toggle.addEventListener('click', () => {
    const expanded = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(expanded));
    help.hidden = !expanded;
  });
}

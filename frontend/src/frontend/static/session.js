"use strict";
let loginRedirectPending = false;
function redirectToLogin() {
  if (loginRedirectPending) return;
  loginRedirectPending = true;
  const next = window.location.pathname + window.location.search;
  window.location.replace('/login?' + new URLSearchParams({reason: 'session', next}));
}
async function authenticatedFetch(url, options) {
  const response = await fetch(url, options);
  if (response.status === 403 && response.headers.get('X-Profile-Required') === 'true') {
    window.location.replace('/settings/user');
    throw new Error('Complete your profile security setup before continuing.');
  }
  if (response.status === 401) {
    redirectToLogin();
    throw new Error('Session expired. Redirecting to sign in…');
  }
  return response;
}
async function checkSession() {
  if (loginRedirectPending) return;
  try {
    const response = await fetch('/api/session', {cache: 'no-store'});
    if (response.status === 401) redirectToLogin();
  } catch (_) { /* A network outage is not proof of session expiration. */ }
}
// Renew while the user interacts, without keeping an untouched tab alive by timer.
(() => {
  const interval = Number(document.body.dataset.sessionRenewSeconds) * 1000;
  if (!interval) return;
  let lastRenewal = Date.now();
  const activity = () => {
    if (document.visibilityState === 'hidden' || Date.now() - lastRenewal < interval) return;
    lastRenewal = Date.now(); checkSession();
  };
  for (const type of ['pointerdown', 'keydown', 'scroll']) document.addEventListener(type, activity, {passive: true, capture: true});
})();

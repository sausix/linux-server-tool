"use strict";
(() => {
  const decode = value => Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4)), char => char.charCodeAt(0)).buffer;
  const encode = buffer => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const options = value => {
    value.challenge = decode(value.challenge);
    if (value.user) value.user.id = decode(value.user.id);
    for (const key of ['allowCredentials', 'excludeCredentials']) for (const item of value[key] || []) item.id = decode(item.id);
    return value;
  };
  const serialize = credential => {
    const response = {clientDataJSON: encode(credential.response.clientDataJSON)};
    for (const key of ['attestationObject', 'authenticatorData', 'signature', 'userHandle']) {
      if (credential.response[key]) response[key] = encode(credential.response[key]);
    }
    if (credential.response.getTransports) response.transports = credential.response.getTransports();
    return {id: credential.id, rawId: encode(credential.rawId), type: credential.type, response};
  };
  const post = async (url, body) => {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Security-key request failed');
    return data;
  };
  const supported = () => {
    if (!window.isSecureContext || !window.PublicKeyCredential) throw new Error('Security keys require HTTPS and a browser with WebAuthn support.');
  };
  const login = document.querySelector('[data-webauthn-login]');
  login?.addEventListener('submit', async event => {
    event.preventDefault(); const button = login.querySelector('button'), status = login.querySelector('[data-key-status]');
    button.disabled = true; const csrf_token = login.elements.csrf_token.value;
    try {
      supported(); status.textContent = 'Waiting for your security key…';
      const challenge = await post('/login/webauthn/options', {csrf_token});
      const credential = await navigator.credentials.get({publicKey: options(challenge.publicKey)});
      const result = await post('/login/webauthn/verify', {csrf_token, challenge_id: challenge.challenge_id, credential: serialize(credential)});
      window.location.assign(result.redirect);
    } catch (failure) {
      status.textContent = failure.message;
      if (failure.name === 'NotAllowedError') post('/login/webauthn/cancel', {csrf_token}).catch(() => {});
    } finally { button.disabled = false; }
  });
  const area = document.querySelector('[data-security-keys]'); if (!area) return;
  const form = area.querySelector('[data-register-key]'), status = form.querySelector('[data-key-status]');
  let busy = false;
  async function change(operation, key_id) {
    if (busy) return; busy = true;
    const buttons = [...area.querySelectorAll('button')]; for (const button of buttons) button.disabled = true;
    const common = {csrf_token: form.elements.csrf_token.value, current_password: form.elements.current_password.value, operation, key_id};
    try {
      supported(); status.textContent = 'Verifying security-key change…';
      if (!common.current_password) throw new Error('Enter your current password.');
      if (area.dataset.hasKeys === 'true' && area.dataset.mode !== 'off') {
        const challenge = await post('/settings/user/security-keys/authorize/options', common);
        const credential = await navigator.credentials.get({publicKey: options(challenge.publicKey)});
        common.grant = (await post('/settings/user/security-keys/authorize/verify', {...common, challenge_id: challenge.challenge_id, credential: serialize(credential)})).grant;
      }
      let result;
      if (operation === 'register') {
        const challenge = await post('/settings/user/security-keys/register/options', {...common, name: form.elements.name.value});
        const credential = await navigator.credentials.create({publicKey: options(challenge.publicKey)});
        result = await post('/settings/user/security-keys/register/verify', {csrf_token: common.csrf_token, challenge_id: challenge.challenge_id, credential: serialize(credential)});
      } else result = await post('/settings/user/security-keys/remove', common);
      form.elements.current_password.value = ''; window.location.assign(result.redirect);
    } catch (failure) {
      status.textContent = failure.message;
      if (failure.name === 'NotAllowedError') post('/settings/user/security-keys/cancel', {csrf_token: common.csrf_token}).catch(() => {});
    } finally { busy = false; for (const button of buttons) button.disabled = false; }
  }
  form.addEventListener('submit', event => { event.preventDefault(); change('register'); });
  for (const button of area.querySelectorAll('[data-remove-key]')) button.addEventListener('click', () => {
    if (window.confirm('Remove this security key?')) change('remove', Number(button.dataset.removeKey));
  });
})();

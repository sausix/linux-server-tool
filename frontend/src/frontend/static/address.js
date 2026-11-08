"use strict";
const input = document.getElementById('address');
const error = document.getElementById('address-error');
let pending;
let generation = 0;
async function validate() {
  const current = ++generation;
  if (!input.value.trim()) return;
  try {
    const response = await authenticatedFetch('/api/address/validate?' + new URLSearchParams({address: input.value, prefix_policy: 'required'}), {cache: 'no-store'});
    const info = await response.json();
    if (current !== generation) return;
    if (!response.ok) throw new Error(info.detail || 'Unable to validate address');
    error.textContent = '';
    input.setCustomValidity('');
  } catch (failure) {
    if (current !== generation) return;
    error.textContent = failure.message;
  }
}
input.addEventListener('input', () => { ++generation; clearTimeout(pending); pending = setTimeout(validate, 250); });
input.addEventListener('blur', validate);

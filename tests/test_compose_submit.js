// Run with: node --test tests/test_compose_submit.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('the first review click waits for pending draft restoration and submits once', async () => {
  class Field extends EventTarget {}
  const text = Object.assign(new Field(), {value: 'services: {}', readOnly: false});
  const name = Object.assign(new Field(), {value: 'demo', type: 'hidden'});
  const base = {value: ''}, revision = {value: ''}, status = {};
  const fields = {'[data-editor-text]': text, '[data-stack-name]': name,
    '[data-base-input]': base, '[data-draft-revision]': revision, '[data-draft-status]': status};
  const form = new EventTarget(); form.dataset = {draftKey: 'test', sourceHash: 'new'};
  form.querySelector = selector => fields[selector] || null;
  form.elements = {csrf_token: {value: 'test'}};
  let submitted = 0, resolveDraft;
  const response = new Promise(resolve => { resolveDraft = resolve; });
  form.requestSubmit = submitter => {
    const event = new Event('submit', {cancelable: true}); event.submitter = submitter;
    if (form.dispatchEvent(event)) submitted++;
  };
  const context = vm.createContext({Event, URLSearchParams, Blob, clearTimeout, setTimeout,
    document: {querySelector: () => form}, window: new EventTarget(), navigator: {},
    authenticatedFetch: () => response});
  vm.runInContext(fs.readFileSync('frontend/src/frontend/static/compose_editor.js', 'utf8'), context);
  const button = {};
  const event = new Event('submit', {cancelable: true}); event.submitter = button;
  form.dispatchEvent(event);
  assert.equal(submitted, 0);
  resolveDraft({ok: true, json: async () => ({draft: null})});
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(submitted, 1);
  assert.equal(text.disabled, false);
});

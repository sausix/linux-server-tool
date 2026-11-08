// Run with: node --test tests/test_live_refresh.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('pausing closes streams, discards scheduled work and preserves snapshot timestamps', () => {
  const sources = [], timers = new Map(); let counter = 0;
  class Source extends EventTarget {
    constructor(url) { super(); this.url = url; this.closed = false; sources.push(this); }
    close() { this.closed = true; }
  }
  const status = {textContent: '', closest: () => ({dataset: {live: 'true'}})};
  const context = vm.createContext({EventTarget, Event, MessageEvent, Date, EventSource: Source,
    document: {body: {dataset: {liveRefresh: 'true'}}, querySelector: () => null, querySelectorAll: () => [status]},
    setTimeout: callback => { timers.set(++counter, callback); return counter; }, clearTimeout: id => timers.delete(id)});
  vm.runInContext(fs.readFileSync('frontend/src/frontend/static/live_refresh.js', 'utf8') + '\nglobalThis.controller = liveRefresh;', context);
  const controller = context.controller;
  const events = controller.events('/api/events'); let updates = 0;
  events.addEventListener('change', () => updates++);
  sources[0].dispatchEvent(new MessageEvent('change', {data: '{}'}));
  assert.equal(updates, 1);
  controller.updated(status);
  assert.match(status.textContent, /^Live · Updated /);
  const timestamp = status.textContent.replace('Live · ', '');
  controller.schedule(() => updates++, 200);
  controller.setEnabled(false);
  assert.equal(sources[0].closed, true);
  assert.equal(timers.size, 0);
  assert.equal(status.textContent, timestamp);
  sources[0].dispatchEvent(new MessageEvent('change', {data: '{}'}));
  assert.equal(updates, 1);
  controller.schedule(() => updates++, 200);
  assert.equal(timers.size, 0);
  controller.setEnabled(true);
  assert.equal(sources.length, 2);
  sources[1].dispatchEvent(new MessageEvent('change', {data: '{}'}));
  assert.equal(updates, 2);
  events.close();
  controller.setEnabled(false); controller.setEnabled(true);
  assert.equal(sources.length, 2);
});

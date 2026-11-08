// Run with: node --test tests/test_host_paths.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('host display paths use longest bind prefix and preserve operational strings', () => {
  const mappings = {'/host/root': '/', '/host/etc': '/etc'};
  const context = vm.createContext({document: {body: {dataset: {hostPaths: JSON.stringify(mappings)}}}});
  vm.runInContext(fs.readFileSync('frontend/src/frontend/static/host_paths.js', 'utf8'), context);
  for (const [actual, expected] of [
    ['/host/etc/sysctl.d/test.conf', '/etc/sysctl.d/test.conf'],
    ['/host/root', '/'], ['/host/root/etc/a', '/etc/a'],
    ['Source: /host/etc/a:3 and /host/root/run/b', 'Source: /etc/a:3 and /run/b'],
    ['/host/etc-other/a', '/host/etc-other/a'], ['/other/host/etc/a', '/other/host/etc/a'],
  ]) assert.equal(context.displayHostPath(actual), expected);
  const operational = {path: '/host/etc/sysctl.d/test.conf'};
  assert.equal(context.displayHostPath(operational.path), '/etc/sysctl.d/test.conf');
  assert.equal(operational.path, '/host/etc/sysctl.d/test.conf');
});

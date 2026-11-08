"""The actual interface renderer consumes extensions without Docker scripts."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import shutil
import subprocess
import threading
import time
import urllib.request

import pytest


def test_interface_extension_styles_links_badges_and_removal_in_browser(tmp_path):
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    if not browser:
        pytest.skip('Chromium is required')
    websocket = pytest.importorskip('websockets.sync.client')
    assets = Path(__file__).resolve().parents[1]/'frontend/src/frontend/static'
    ui = {'interfaces': {'br0': {'accentcolor': '#77bdff', 'links': [{'text': 'Stack: demo',
        'href': '/docker/stacks/compose/demo', 'accentcolor': '#77bdff'}], 'badges': [{'text': 'Managed', 'accentcolor': '#77bdff'}]}}}
    data = {'interfaces': [{'name': 'br0', 'kind': 'bridge', 'operational_state': 'up', 'scope': 'private', 'ports': [], 'flags': []}], 'addresses': [], 'ui': ui}
    page = '''<!doctype html><html><head><link rel="stylesheet" href="/app.css">
<style>:root{--category-network:#55d6a9;--interface-public:#51eb00;--interface-loopback:#87929f}</style>
<script>const liveRefresh={generation:0,enabled:false,updated(){}};const authenticatedFetch=fetch;const displayHostPath=x=>x;</script>
<script src="/icons.js" defer></script><script src="/ip_subnets.js" defer></script><script src="/network.js" defer></script></head>
<body><main data-category="network"><section id="network-view" data-view="interfaces" data-live="false">
<p id="live-status"></p><p id="load-error" hidden></p><div id="inventory"></div></section></main></body></html>'''
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            name = self.path.lstrip('/').split('?', 1)[0]
            if name.startswith('api/'):
                body, mime = json.dumps(data).encode(), 'application/json'
            elif name in {'app.css', 'icons.js', 'ip_subnets.js', 'network.js', 'icons.svg'}:
                body = (assets/name).read_bytes(); mime = 'text/css' if name.endswith('.css') else 'image/svg+xml' if name.endswith('.svg') else 'text/javascript'
            else:
                body, mime = page.encode(), 'text/html'
            self.send_response(200); self.send_header('Content-Type', mime); self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True); worker.start()
    process = subprocess.Popen([browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--no-proxy-server',
        '--no-first-run', '--user-data-dir='+str(tmp_path/'browser'), '--remote-debugging-port=0', 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        active = tmp_path/'browser/DevToolsActivePort'; deadline = time.monotonic()+8
        while not active.exists():
            assert process.poll() is None and time.monotonic()<deadline
            time.sleep(.02)
        with urllib.request.urlopen('http://127.0.0.1:'+active.read_text().splitlines()[0]+'/json/list') as response:
            target = next(item for item in json.load(response) if item['type']=='page')
        with websocket.connect(target['webSocketDebuggerUrl']) as connection:
            identity = 0
            def command(method, params):
                nonlocal identity
                identity += 1; connection.send(json.dumps({'id':identity,'method':method,'params':params}))
                while True:
                    result = json.loads(connection.recv(timeout=5))
                    if result.get('id') == identity:
                        assert 'error' not in result, result
                        return result.get('result', {})
            def evaluate(expression):
                result = command('Runtime.evaluate', {'expression':expression, 'returnByValue':True})
                assert 'exceptionDetails' not in result, result
                return result.get('result', {}).get('value')
            def until(expression):
                deadline = time.monotonic()+5
                while not evaluate(expression):
                    assert time.monotonic()<deadline, evaluate('document.body.innerText')
                    time.sleep(.03)
            command('Page.navigate', {'url':f'http://127.0.0.1:{server.server_port}/'})
            until("!!document.body && document.body.innerText.includes('Managed')")
            assert evaluate("document.querySelector('.ui-extension').getAttribute('href')") == '/docker/stacks/compose/demo'
            assert evaluate("getComputedStyle(document.querySelector('.interface-card')).borderTopColor") == 'rgb(119, 189, 255)'
            assert evaluate("document.querySelector('span.ui-extension').textContent") == 'Managed'
            data['ui'] = {'interfaces': {}}
            evaluate('refresh()')
            until("!document.querySelector('.ui-extension')")
            assert evaluate("getComputedStyle(document.querySelector('.interface-card')).borderTopColor") == 'rgb(85, 214, 169)'
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        server.shutdown(); server.server_close(); worker.join(timeout=2)

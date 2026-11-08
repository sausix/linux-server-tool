"""Exercise native form submission: an unchanged draft must review on one click."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import time
import urllib.request
import shutil
import subprocess
import threading

import pytest


@pytest.mark.parametrize('dirty', [False, True])
def test_compose_review_submits_on_first_click(tmp_path, dirty):
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    if not browser:
        pytest.skip('Chromium is required to test native form submission')
    websocket = pytest.importorskip('websockets.sync.client')
    script = (Path(__file__).parents[1] / 'frontend/src/frontend/static/compose_editor.js').read_bytes()
    reviews = []
    edit = "text.value = 'services: {web: {image: test}}'; text.dispatchEvent(new Event('input', {bubbles: true}));" if dirty else ''
    page = '''<!doctype html><html><head><script>window.authenticatedFetch = (...args) => fetch(...args);</script>
<script src="/compose_editor.js" defer></script></head><body>
<form method="post" action="/review" data-compose-editor data-draft-key="fixture" data-source-hash="1">
<input type="hidden" name="csrf_token" value="test"><input name="name" value="example" data-stack-name>
<input type="hidden" name="revision" value="1" data-base-input><input type="hidden" name="draft_revision" data-draft-revision>
<textarea name="definition_text" data-editor-text>services: {}</textarea><p data-draft-status></p>
<button type="submit" data-review>Review definition</button></form></body></html>'''
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, mime='text/html'):
            body = body.encode() if isinstance(body, str) else body
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/compose_editor.js':
                self.send(script, 'text/javascript')
            elif self.path.startswith('/api/docker/stack-drafts/'):
                self.send('{"draft": null}', 'application/json')
            else:
                self.send(page)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if self.path == '/review':
                reviews.append(body)
                self.send('<!doctype html><html><body>Review reached on first click</body></html>')
            else:
                self.send('{"revision": 1}', 'application/json')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    worker.start()
    process = subprocess.Popen([browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                                '--no-proxy-server', '--no-first-run', '--no-default-browser-check',
                                '--user-data-dir=' + str(tmp_path / 'browser'), '--remote-debugging-port=0', 'about:blank'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        active_port = tmp_path / 'browser/DevToolsActivePort'
        deadline = time.monotonic() + 8
        while not active_port.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert active_port.exists(), 'Chromium debugger did not start'
        port = active_port.read_text().splitlines()[0]
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=3) as response:
            pages = json.load(response)
        target = next(item for item in pages if item['type'] == 'page')
        with websocket.connect(target['webSocketDebuggerUrl'], open_timeout=3) as connection:
            identity = 0
            def command(method, params):
                nonlocal identity
                identity += 1
                connection.send(json.dumps({'id': identity, 'method': method, 'params': params}))
                while True:
                    result = json.loads(connection.recv(timeout=5))
                    if result.get('id') == identity:
                        assert 'error' not in result, result.get('error')
                        return result.get('result', {})

            def evaluate(expression):
                return command('Runtime.evaluate', {'expression': expression, 'returnByValue': True}).get('result', {}).get('value')

            command('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/'})
            deadline = time.monotonic() + 5
            while not evaluate("!!document.querySelector('[data-editor-text]') && !document.querySelector('[data-editor-text]').disabled"):
                assert time.monotonic() < deadline, 'Draft restoration did not finish'
                time.sleep(.02)
            if dirty:
                evaluate("(() => { const text = document.querySelector('[data-editor-text]'); " + edit + " })()")
            point = evaluate("(() => { const r = document.querySelector('[data-review]').getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2}; })()")
            # Native input is essential: a JS button.click() keeps an outer JS frame
            # alive and hides the browser's reentrant-submit guard.
            for event in ('mousePressed', 'mouseReleased'):
                command('Input.dispatchMouseEvent', {'type': event, **point, 'button': 'left', 'clickCount': 1})
            deadline = time.monotonic() + 3
            while not reviews and time.monotonic() < deadline:
                time.sleep(.02)
            assert len(reviews) == 1, 'Review was not reached after one native click'
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=3)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

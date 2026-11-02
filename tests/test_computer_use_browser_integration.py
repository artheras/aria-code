"""Isolated browser acceptance; no user profile or external site is opened.

Run with ARIA_COMPUTER_BROWSER_TEST=1 after installing the browser extra/Chromium.
Native approval is a deterministic test callback; it is never changed in the server.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('ARIA_COMPUTER_BROWSER_TEST') != '1',
                                reason='explicit isolated Chromium test')


def test_real_browser_observe_approve_fill_verify(monkeypatch):
    from playwright.sync_api import BrowserType
    from aria_code.computer_use_server import BrowserSession, dispatch

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'''<!doctype html><title>Aria acceptance fixture</title>
                <input id="note" aria-label="Note"><button id="save"
                onclick="document.querySelector('#output').textContent=document.querySelector('#note').value">Save</button>
                <div id="output"></div>''')
        def log_message(self, *_): pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    real_launch = BrowserType.launch
    def launch_headless(self, **kwargs):
        options = {**kwargs, 'headless': True}
        if os.environ.get('ARIA_COMPUTER_TEST_BROWSER'):
            options['executable_path'] = os.environ['ARIA_COMPUTER_TEST_BROWSER']
        return real_launch(self, **options)
    monkeypatch.setattr(BrowserType, 'launch', launch_headless)
    approvals = []
    origin = f'http://127.0.0.1:{server.server_port}'
    session = BrowserSession([origin], approval=lambda summary: approvals.append(summary) or True)
    try:
        first = session.call('browser_open', {'url': origin})
        page = session.page
        assert first['title'] == 'Aria acceptance fixture'
        assert len(first['elements']) == 2
        assert base64.b64decode(first['_image']).startswith(b'\x89PNG')
        filled = session.call('browser_fill', {'observation_id': first['observation_id'],
                                             'selector': '#note', 'text': '\u4f60\u597d Aria'})
        saved = session.call('browser_click', {'observation_id': filled['observation_id'],
                                             'selector': '#save'})
        assert '\u4f60\u597d Aria' in saved['text']
        assert session.page is page
        assert len(approvals) == 3
        session.page.locator('#output').evaluate("e => e.textContent = 'changed'")
        with pytest.raises(ValueError, match='stale'):
            session.call('browser_click', {'observation_id': saved['observation_id'], 'selector': '#save'})
        with pytest.raises(ValueError, match='not allowed'):
            session.call('browser_open', {'url': 'https://example.com'})
        session.approval = lambda _: False
        current = session.observe()
        with pytest.raises(PermissionError):
            session.call('browser_click', {'observation_id': current['observation_id'], 'selector': '#save'})
        response = dispatch(session, {'jsonrpc':'2.0', 'id':1, 'method':'tools/call',
                                      'params':{'name':'browser_observe'}})
        assert response['result']['content'][1]['type'] == 'image'
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

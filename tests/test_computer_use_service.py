import io
import json
import sys
import types
from unittest.mock import Mock

import pytest
from aria_code.computer_use_server import BrowserSession, dispatch, origin, approve
from aria_code.runtime.agent_loop import record_tool_result, build_next_turn_messages
from aria_code.tools import computer_use_tools as desktop


class Page:
    url = 'https://example.com/'
    html = '<button>Save</button>'

    def __init__(self):
        self.target = Mock()
        self.target.count.return_value = 1
        self.target.is_visible.return_value = True
        self.goto = Mock(side_effect=lambda url, **kw: setattr(self, 'url', url))

    def content(self): return self.html
    def title(self): return 'Example'
    def inner_text(self, selector): return 'Save'
    def screenshot(self): return b'png'
    def locator(self, selector): return self.target


@pytest.fixture
def session():
    s = BrowserSession(['https://example.com'], approval=Mock(return_value=True), clock=lambda: 0)
    s.page = Page()
    return s


def action_args(session):
    return {'observation_id': session.observe()['observation_id'], 'selector': 'button'}


def test_origin_normalization_and_scope():
    assert origin('https://EXAMPLE.com:443/a') == 'https://example.com'
    assert origin('http://[::1]:8080') == 'http://[::1]:8080'
    for url in ['file:///tmp/a', 'javascript:alert(1)', 'https://u:p@example.com']:
        with pytest.raises(ValueError): origin(url)
    empty = BrowserSession([], approval=Mock())
    with pytest.raises(ValueError): empty.call('browser_open', {'url': 'https://example.com'})
    empty.approval.assert_not_called()


def test_denial_prevents_browser_launch():
    s = BrowserSession(['https://example.com'], approval=lambda _: False)
    with pytest.raises(PermissionError): s.call('browser_open', {'url': 'https://example.com'})
    assert s.page is None


def test_same_session_reuses_page_and_returns_new_observation(session):
    page = session.page
    first = session.call('browser_open', {'url': 'https://example.com/a'})
    second = session.call('browser_open', {'url': 'https://example.com/b'})
    assert session.page is page
    assert first['observation_id'] != second['observation_id']
    assert page.goto.call_count == 2


@pytest.mark.parametrize('name,extra', [('browser_click', {}), ('browser_fill', {'text': '你好 Aria'}), ('browser_press', {'key': 'Enter'})])
def test_mutations_require_individual_approval(session, name, extra):
    args = {**action_args(session), **extra}
    session.approval.return_value = False
    with pytest.raises(PermissionError): session.call(name, args)
    session.page.target.click.assert_not_called()
    session.page.target.fill.assert_not_called()
    session.page.target.press.assert_not_called()


def test_unicode_fill_does_not_disclose_text_in_approval(session):
    args = {**action_args(session), 'text': '秘密你好'}
    session.call('browser_fill', args)
    session.page.target.fill.assert_called_once_with('秘密你好')
    assert '秘密你好' not in session.approval.call_args.args[0]


@pytest.mark.parametrize('change', ['dom', 'clock', 'url', 'token'])
def test_stale_observation_never_acts(session, change):
    args = action_args(session)
    if change == 'dom': session.page.html = '<button>Delete</button>'
    if change == 'clock': session.clock = lambda: 61
    if change == 'url': session.page.url = 'https://example.com/other'
    if change == 'token': args['observation_id'] = 'wrong'
    with pytest.raises(ValueError): session.call('browser_click', args)
    session.page.target.click.assert_not_called()
    session.approval.assert_not_called()


def test_changes_during_approval_invalidate_action(session):
    args = action_args(session)
    def change(_):
        session.page.html = 'Changed'
        return True
    session.approval.side_effect = change
    with pytest.raises(ValueError): session.call('browser_click', args)
    session.page.target.click.assert_not_called()


def test_ambiguous_selector_never_prompts_or_acts(session):
    args = action_args(session)
    session.page.target.count.return_value = 2
    with pytest.raises(ValueError): session.call('browser_click', args)
    session.approval.assert_not_called()


def test_redirect_document_blocked(session):
    route = Mock()
    route.request.is_navigation_request.return_value = True
    route.request.url = 'https://evil.example/'
    session._route(route)
    route.abort.assert_called_once()
    route.continue_.assert_not_called()


def test_mcp_protocol_images_and_notifications(session):
    def msg(method, params=None):
        return dispatch(session, {'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
    assert msg('initialize')['result']['capabilities'] == {'tools': {}}
    assert len(msg('tools/list')['result']['tools']) == 7
    assert dispatch(session, {'jsonrpc':'2.0','method':'notifications/initialized'}) is None
    result = msg('tools/call', {'name':'browser_observe'})['result']
    assert not result['isError']
    assert result['content'][1] == {'type':'image','mimeType':'image/png','data':'cG5n'}
    assert '_image' not in result['content'][0]['text']
    assert msg('tools/call', {'name':'approve'})['result']['isError']
    assert msg('unknown')['error']['code'] == -32601


def test_stdio_lifecycle_and_bad_input(monkeypatch):
    from aria_code import computer_use_server as server
    requests = '\n'.join([json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize'}), '{', json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'browser_capabilities'}})]) + '\n'
    monkeypatch.setattr(sys, 'stdin', io.StringIO(requests))
    output = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', output)
    monkeypatch.setenv('ARIA_COMPUTER_ORIGINS', '')
    server.main()
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[1]['error']['code'] == -32700
    assert json.loads(replies[2]['result']['content'][0]['text'])['origins'] == []


def test_headless_and_unavailable_approval_deny(monkeypatch):
    from aria_code import computer_use_server as server
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert not approve('test')
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(server.subprocess, 'run', Mock(side_effect=OSError()))
    assert not approve('test')


def test_images_belong_only_to_own_batch():
    first, second = [], []
    formatter = lambda name, result: json.dumps(result)
    raw = {'success':True, 'data':'captured', '_vision_image':'private-image'}
    record_tool_result(first, 'computer_screenshot', raw, formatter)
    record_tool_result(second, 'read_file', {'success':True}, formatter)
    assert 'private-image' not in first[0]['result']
    assert isinstance(build_next_turn_messages('', first)[1]['content'], list)
    assert isinstance(build_next_turn_messages('', second)[1]['content'], str)


def test_tls_verification_in_requests_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', None)
    request = Mock(side_effect=RuntimeError('certificate rejected'))
    monkeypatch.setitem(sys.modules, 'requests', types.SimpleNamespace(get=request))
    result = desktop._tool_browser_navigate({'url':'https://example.com'})
    assert not result['success']
    assert request.call_args.kwargs['verify'] is True


def test_unicode_clipboard_restored_and_scroll_direction(monkeypatch):
    gui = Mock()
    gui.FailSafeException = type('FailSafeException', (Exception,), {})
    clip = Mock()
    clip.paste.return_value = 'previous'
    monkeypatch.setitem(sys.modules, 'pyautogui', gui)
    monkeypatch.setitem(sys.modules, 'pyperclip', clip)
    monkeypatch.setattr(desktop.time, 'sleep', lambda _: None)
    assert desktop._tool_computer_action({'action':'type','text':'你好'})['success']
    assert clip.copy.call_args_list[0].args == ('你好',)
    assert clip.copy.call_args_list[-1].args == ('previous',)
    assert desktop._tool_computer_action({'action':'scroll','dy':3})['success']
    gui.scroll.assert_called_once_with(-3, x=0, y=0)


def test_clipboard_restored_on_input_error(monkeypatch):
    gui = Mock()
    gui.FailSafeException = type('FailSafeException', (Exception,), {})
    gui.hotkey.side_effect = RuntimeError('failure')
    clip = Mock(); clip.paste.return_value = 'original'
    monkeypatch.setitem(sys.modules, 'pyautogui', gui)
    monkeypatch.setitem(sys.modules, 'pyperclip', clip)
    assert not desktop._tool_computer_action({'action':'type','text':'你好'})['success']
    clip.copy.assert_called_with('original')


@pytest.mark.parametrize('params', [{'monitor':-1}, {'max_dim':0}, {'monitor':'bad'}])
def test_screenshot_validation(params):
    assert not desktop._tool_computer_screenshot(params)['success']


@pytest.mark.asyncio
async def test_mcp_image_reaches_next_agent_turn():
    from aria_code.mcp_client import MCPServer
    s = MCPServer('browser', 'unused', [])
    s._running = True
    async def reply(*args):
        return {'content':[{'type':'text','text':'{"url":"https://example.com"}'},
                           {'type':'image','mimeType':'image/png','data':'cG5n'}]}
    s._request = reply
    result = await s.call_tool('browser_observe', {})
    records = []
    record_tool_result(records, 'mcp__browser__browser_observe', result, lambda _, r: json.dumps(r))
    user = build_next_turn_messages('', records)[1]
    assert user['content'][0]['image_url']['url'] == 'data:image/png;base64,cG5n'
    assert 'cG5n' not in records[0]['result']


@pytest.mark.asyncio
async def test_mcp_timeout_from_host_configuration(tmp_path, monkeypatch):
    from aria_code.mcp_client import MCPServer, MCPToolRegistry
    p = tmp_path / 'mcp.json'
    p.write_text(json.dumps({'servers':[{'name':'browser','command':'unused','timeout':120}]}))
    async def start(self): return True
    monkeypatch.setattr(MCPServer, 'start', start)
    reg = MCPToolRegistry(config_path=p)
    assert await reg.start_all() == {'browser':True}
    assert reg._servers['browser'].timeout == 120


@pytest.mark.asyncio
async def test_real_stdio_process_handshake_and_permissions():
    from aria_code.mcp_client import MCPServer
    s = MCPServer('computer', sys.executable, ['-m', 'aria_code.computer_use_server'],
                  env={'ARIA_COMPUTER_ORIGINS':''})
    try:
        assert await s.start()
        assert len(s.tools) == 7
        result = await s.call_tool('browser_capabilities', {})
        assert result['origins'] == []
        assert result['desktop_input'] is False
        denied = await s.call_tool('browser_open', {'url':'https://example.com'})
        assert denied['success'] is False
        assert denied['error_code'] == 'mcp_tool_error'
    finally:
        await s.stop()


def test_image_bytes_not_in_hooks_or_trace():
    from aria_code.runtime.tool_executor import ToolExecutor
    calls = []
    result = {'success':True,'_vision_image':'private-image','data':{}}
    executor = ToolExecutor({'capture':(lambda _:result, 'capture')},
                            hook=lambda event, tool, params, value=None: calls.append(value))
    assert executor.execute_local('capture', {})['_vision_image'] == 'private-image'
    assert '_vision_image' not in calls[-1]
    assert '_vision_image' not in executor.trace.tool_calls[-1].result

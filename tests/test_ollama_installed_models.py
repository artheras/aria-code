"""An Ollama request may use only the model this server currently advertises."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aria_code.providers import ollama_models as models


class Response:
    status = 200
    def __init__(self, data): self.data = data
    def read(self, _limit): return json.dumps(self.data).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass


@pytest.mark.parametrize('requested,installed,expected', [
    ('qwen:3b', ['qwen:3b'], 'qwen:3b'),
    ('qwen', ['qwen:latest'], 'qwen:latest'),
    ('registry:5000/team/qwen', ['registry:5000/team/qwen:latest'], 'registry:5000/team/qwen:latest'),
])
def test_only_exact_model_or_latest_spelling_is_accepted(requested, installed, expected):
    assert models.require_installed_model(installed, requested) == expected


@pytest.mark.parametrize('requested,installed', [
    ('qwen:7b', ['qwen:3b']), ('qwen', ['qwen:3b']),
    ('missing', ['deepseek:1.5b']), ('qwen', []),
])
def test_does_not_substitute_family_or_another_installed_model(requested, installed):
    with pytest.raises(models.OllamaModelError, match='not installed'):
        models.require_installed_model(installed, requested)


def test_discovery_is_fresh_per_endpoint_and_after_removal(monkeypatch):
    inventories={'http://one/api/tags':['chosen:3b'], 'http://two/api/tags':['other:1b']}
    calls=[]
    def fetch(url, **kwargs):
        calls.append(url)
        return Response({'models':[{'name':n} for n in inventories[url]]})
    monkeypatch.setattr(models.urllib.request, 'urlopen', fetch)
    assert models.resolve_model_sync('http://one', 'chosen:3b') == 'chosen:3b'
    with pytest.raises(models.OllamaModelError): models.resolve_model_sync('http://two', 'chosen:3b')
    inventories['http://one/api/tags']=[]
    with pytest.raises(models.OllamaModelError): models.resolve_model_sync('http://one', 'chosen:3b')
    assert calls == ['http://one/api/tags','http://two/api/tags','http://one/api/tags']


@pytest.mark.parametrize('data',[{}, {'models':None}, {'models':{}}, {'models':[{}]}, {'models':[{'name':'bad name'}]}])
def test_invalid_inventory_fails_closed(monkeypatch,data):
    monkeypatch.setattr(models.urllib.request,'urlopen',lambda *a,**kw:Response(data))
    with pytest.raises(models.OllamaModelError,match='Cannot verify'): models.resolve_model_sync('http://test','chosen')


def test_normalizes_each_users_address_without_touching_other_backends(monkeypatch):
    monkeypatch.setenv('OLLAMA_HOST','127.0.0.1:20000')
    assert models.ollama_base_url()== 'http://127.0.0.1:20000'
    assert models.ollama_base_url({'ollama_url':'http://alice:11434'})=='http://alice:11434'
    assert models.ollama_base_url({'ollama_base_url':'http://bob/prefix/v1','ollama_url':'http://alice'})=='http://bob/prefix'
    assert models.ollama_base_url({'local_url':'http://unrelated:8000'})=='http://127.0.0.1:20000'


@pytest.mark.asyncio
async def test_all_python_inference_entrypoints_reject_missing_before_chat_or_cache(monkeypatch):
    monkeypatch.setattr(models.urllib.request,'urlopen',lambda *a,**kw:Response({'models':[]}))
    from aria_code.providers.local_llm_provider import LocalLLMProvider
    local=LocalLLMProvider(backend='ollama',base_url='http://test',model='missing:1b')
    events=[ev async for ev in local.stream([{'role':'user','content':'hello'}])]
    assert events and events[0]['type']=='error' and 'not installed' in events[0]['message']
    from aria_code.providers.llm.ollama import OllamaProvider
    from aria_code.providers.llm.base import ProviderConfig, Message
    direct=OllamaProvider(ProviderConfig(name='ollama',model='missing:1b',base_url='http://test'))
    events=[ev async for ev in direct.stream([Message(role='user',content='hello')])]
    assert events[0]['type']=='error' and 'not installed' in events[0]['message']
    from aria_code.apps.cli.providers.llm import ollama_stream
    monkeypatch.setattr(ollama_stream,'_cache_get',lambda *a:'old cached success')
    result=await ollama_stream.stream_ollama('http://test','hello',[],model='missing:1b',enable_tools=False)
    assert result['success'] is False and 'not installed' in result['error']
    from aria_code.apps.cli.providers.chat_routing import should_fallback
    assert should_fallback('ollama',result,is_placeholder=True) is False
    # The runtime may keep only the error text; that must not allow rescue.
    assert should_fallback('ollama',{'success':False,'error':result['error']},is_placeholder=True) is False


@pytest.mark.asyncio
async def test_selection_refusal_preserves_config_and_does_not_save(monkeypatch):
    from aria_code.apps.cli.commands.model_cmds import ModelCommandsMixin
    monkeypatch.setattr(models.urllib.request,'urlopen',lambda *a,**kw:Response({'models':[{'name':'installed:1b'}]}))
    obj=ModelCommandsMixin()
    cfg={'model':'original','local_provider':'google'}
    obj.terminal=SimpleNamespace(config=cfg)
    obj.context=SimpleNamespace(console=Mock(),has_rich=True,save_config=Mock())
    obj._set_model_by_id('missing:7b',provider='ollama')
    assert cfg=={'model':'original','local_provider':'google'}
    await obj.cmd_model('ollama/missing:7b')
    assert cfg=={'model':'original','local_provider':'google'}
    obj.context.save_config.assert_not_called()


def test_picker_catalogue_hides_uninstalled_ollama_entries(monkeypatch):
    from aria_code.apps.cli.commands import model_cmds
    monkeypatch.setattr(model_cmds, '_get_MODELS', lambda: {
        'missing': {'id':'qwen:7b'}, 'installed':{'id':'tiny:1b'},
        'cloud':{'id':'google/gemini-test'}})
    rows=model_cmds._installed_catalogue([{'name':'tiny:1b'}])
    assert set(rows)=={'installed','cloud'}


def test_health_provider_import_does_not_reenter_cli_diagnostic():
    import os, subprocess, sys
    code = '''
import asyncio, sys
async def check():
    sys.argv = ["aria", "health", "--model", "--json"]
    import aria_cli
asyncio.run(check())
print("health-import-ok")
'''
    child=subprocess.run([sys.executable,'-c',code],env=os.environ.copy(),capture_output=True,text=True,timeout=20)
    assert child.returncode == 0, child.stderr
    assert 'health-import-ok' in child.stdout
    assert 'was never awaited' not in child.stderr

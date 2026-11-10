"""Task-agent behavior through real native processes and the Aria host boundary."""
import json
import os
import subprocess
import unittest

from aria_code.agents.warehouse.task_agent import WarehouseTaskAgent, SCHEMA
from aria_code.runtime.tool_executor import ToolExecutor

SCENE = dict(width=5,height=4,start=[0,0],goal=[4,0],obstacles=[])


class WarehouseTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get('ARIA_PHYSICAL_BINARY'):
            raise unittest.SkipTest('Build and configure ARIA_PHYSICAL_BINARY')

    def setUp(self):
        self.agent = WarehouseTaskAgent()
        self.addCleanup(self.agent.close)

    def call(self, action, scope='chat-a', **args):
        return self.agent.handle(dict(action=action,_warehouse_scope=scope,**args))

    def create(self, scope='chat-a'):
        result=self.call('create',scope=scope,request_id='create-1',goal='Reach shelf B in this synthetic map',scenario=SCENE)
        self.assertTrue(result['success'],result)
        return result['result']['task_id']

    def test_goal_execution_and_completion_evidence(self):
        task=self.create()
        again=self.create()
        self.assertEqual(task,again)
        result=self.call('start',task_id=task,request_id='start-1')
        self.assertTrue(result['success'])
        for _ in range(3):
            result=self.call('wait',task_id=task,timeout_ms=1000)
            if result['result']['status']!='running':break
        self.assertEqual(result['result']['status'],'completed')
        self.assertTrue(result['result']['verified'])
        self.assertEqual(result['result']['world']['position'],[4,0])
        self.assertEqual(result['result']['world']['metrics']['accepted_steps'],4)
        retry=self.call('start',task_id=task,request_id='start-1')
        self.assertEqual(retry['result']['world']['metrics']['accepted_steps'],4)
        trace=self.call('trace',task_id=task)['result']['trace']
        self.assertTrue(trace['simulation_only'])
        self.assertFalse(trace['physics_engine'])

    def test_conversation_scope_cannot_be_selected_by_tool_args(self):
        task=self.create()
        tools={'warehouse_task':(self.agent.handle,'Task agent')}
        executor=ToolExecutor(tools,execution_context=lambda:{'_session_id':'chat-b'})
        result=executor.execute_local('warehouse_task',{'action':'status','task_id':task,'_warehouse_scope':'chat-a','_session_id':'chat-a'})
        self.assertFalse(result['success'])
        self.assertEqual(result['error'],'unknown_task')
        self.assertEqual(self.call('status',task_id=task)['result']['status'],'ready')

    def test_no_scope_and_read_only_host_are_enforced(self):
        result=self.agent.handle(dict(action='health'))
        self.assertFalse(result['success'])
        executor=ToolExecutor({'warehouse_task':(self.agent.handle,'Agent')},config={'permission_mode':'read-only'},execution_context=lambda:{'_session_id':'chat-a'})
        result=executor.execute_local('warehouse_task',dict(action='create',goal='demo',scenario=SCENE,request_id='a',_warehouse_read_only=False))
        self.assertFalse(result['success'])
        self.assertIn('read-only',result['error'])

    def test_unreachable_is_not_claimed_as_complete(self):
        task=self.create()
        self.assertTrue(self.call('obstacle',task_id=task,request_id='block',cell=[4,0])['success'])
        self.call('start',task_id=task,request_id='start')
        result=self.call('wait',task_id=task,timeout_ms=1000)['result']
        self.assertEqual(result['status'],'needs_input')
        self.assertFalse(result['verified'])
        self.assertEqual(result['reason'],'unreachable')

    def test_stop_and_constraints(self):
        task=self.create()
        self.call('stop',task_id=task)
        self.assertFalse(self.call('start',task_id=task,request_id='start')['success'])
        self.assertEqual(self.call('status',task_id=task)['result']['status'],'stopped')
        result=self.call('create',request_id='bad',goal='demo',scenario=SCENE,constraints={'max_steps':999})
        self.assertFalse(result['success'])

    def test_schema_and_prompt_reach_the_existing_agent(self):
        from aria_code.apps.cli.prompts.select import build_turn_system_prompt
        from aria_code.apps.cli.tool_scope import select_tool_schemas
        self.assertIn('create',SCHEMA['properties']['action']['enum'])
        schema={'type':'function','function':{'name':'warehouse_task'}}
        self.assertEqual(select_tool_schemas([schema],'Continue the previous task'),[schema])
        self.assertEqual(select_tool_schemas([schema],'warehouse navigation simulation'),[schema])
        self.assertEqual(select_tool_schemas([schema],'hello'),[])
        self.assertIn('Warehouse task agent workflow',build_turn_system_prompt('仓库导航仿真：运送到 B 货架'))
        self.assertNotIn('Warehouse task agent workflow',build_turn_system_prompt('what can you do?'))
        from aria_code import aria_cli
        self.assertIn('warehouse_task',aria_cli.LOCAL_TOOLS)
        names={s.get('function',{}).get('name') for s in aria_cli.LOCAL_TOOL_SCHEMAS}
        self.assertIn('warehouse_task',names)

    def test_existing_mcp_exposes_same_task_agent(self):
        from aria_code.packages.aria_mcp import server
        names={t['name'] for t in server.TOOLS}
        self.assertIn('aria.warehouse.task',names)
        self.assertIn('aria.warehouse.task',server._HANDLERS)


class NativeMCPTests(unittest.TestCase):
    def test_agent_public_api_stays_compatible_and_lazy(self):
        code = "import sys; import aria_code.agents; assert 'aria_code.agents.registry' not in sys.modules; assert 'BaseAgent' in dir(aria_code.agents); from aria_code.agents import BaseAgent, AgentRegistry, AgentTeam, get_registry; from aria_code.agents.base import BaseAgent as Direct; assert BaseAgent is Direct; assert callable(get_registry)"
        result=subprocess.run([os.sys.executable,'-c',code],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_stdio_handshake_discovery_and_task_call(self):
        binary=os.environ.get('ARIA_PHYSICAL_BINARY')
        if not binary:self.skipTest('Native binary not configured')
        requests=[
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'independent-test','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'aria.warehouse.task','arguments':{'action':'health'}}},
        ]
        proc=subprocess.run([binary,'mcp'],input='\n'.join(map(json.dumps,requests))+'\n',capture_output=True,text=True,timeout=10)
        self.assertEqual(proc.returncode,0,proc.stderr)
        results=[json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(results),3)
        self.assertEqual(results[1]['result']['tools'][0]['name'],'aria.warehouse.task')
        health=json.loads(results[2]['result']['content'][0]['text'])
        self.assertEqual(health['result']['protocol'],'warehouse-task-agent-v1')


if __name__=='__main__':unittest.main(verbosity=2)

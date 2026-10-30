"""Independent executable checks; the distance oracle does not import Rust code."""
import collections
import json
import os
from pathlib import Path
import queue
import random
import subprocess
import threading
import unittest


class Client:
    def __init__(self, binary, native=False):
        self.proc = subprocess.Popen([binary, *(['physical'] if native else []), 'jsonl'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding='utf-8')
        self.responses = queue.Queue()
        def read():
            for line in self.proc.stdout:
                self.responses.put(line)
            self.responses.put(None)
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()

    def call(self, op, **params):
        self.proc.stdin.write(json.dumps(dict(op=op, **params)) + '\n')
        self.proc.stdin.flush()
        line = self.responses.get(timeout=10)
        if line is None:
            raise AssertionError('Simulation process exited unexpectedly')
        return json.loads(line)

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.thread.join(timeout=2)
        self.proc.stdout.close()
        self.proc.stderr.close()


def distance(s):
    start, goal = tuple(s['start']), tuple(s['goal'])
    blocked = {tuple(c) for c in s['obstacles']}
    todo = collections.deque([(start, 0)])
    seen = {start}
    while todo:
        (x, y), d = todo.popleft()
        if (x, y) == goal:
            return d
        for cell in ((x-1,y), (x+1,y), (x,y-1), (x,y+1)):
            if 0 <= cell[0] < s['width'] and 0 <= cell[1] < s['height'] and cell not in blocked and cell not in seen:
                seen.add(cell)
                todo.append((cell, d+1))
    return None


def scenarios():
    rng = random.Random(20261029)
    for i in range(20):
        size = 4 + i % 5
        cells = [[x,y] for x in range(size) for y in range(size) if [x,y] not in ([0,0],[size-1,size-1])]
        yield dict(width=size, height=size, start=[0,0], goal=[size-1,size-1],
                   obstacles=rng.sample(cells, min(len(cells), i % 10)))


class NativePhysicalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = os.environ.get('ARIA_PHYSICAL_BINARY')
        if not cls.binary:
            raise unittest.SkipTest('Set ARIA_PHYSICAL_BINARY to a built Rust service')
        if not Path(cls.binary).is_file():
            raise AssertionError('Configured physical binary is missing')

    def setUp(self):
        self.client = Client(self.binary)
        self.addCleanup(self.client.close)

    def init(self):
        return self.client.call('init', scenario=dict(width=5,height=4,start=[0,0],goal=[4,0],obstacles=[]))['result']

    def plan(self):
        observed = self.client.call('observe')['result']
        return self.client.call('plan', observed_revision=observed['revision'])

    def step(self, plan_id):
        observed = self.client.call('observe')['result']
        return self.client.call('step', plan_id=plan_id, observed_revision=observed['revision'])

    def test_twenty_fixed_maps_against_independent_oracle(self):
        reports = []
        for i, s in enumerate(scenarios()):
            with self.subTest(scenario=i):
                self.assertTrue(self.client.call('init', scenario=s)['ok'])
                expected = distance(s)
                planned = self.plan()
                if expected is None:
                    self.assertEqual(planned['error'], 'unreachable')
                    reports.append(dict(case=i, expected_reachable=False, passed=True))
                    continue
                self.assertTrue(planned['ok'])
                state = planned['result']
                self.assertEqual(len(state['plan']['remaining']), expected)
                plan_id, previous = state['plan']['id'], s['start']
                for _ in range(expected):
                    result = self.step(plan_id)
                    self.assertTrue(result['ok'], result)
                    state = result['result']
                    position = state['world']['position']
                    self.assertEqual(sum(abs(a-b) for a,b in zip(previous,position)),1)
                    self.assertNotIn(position,s['obstacles'])
                    previous = position
                self.assertEqual(state['world']['position'],s['goal'])
                self.assertTrue(state['world']['reached'])
                self.assertEqual(state['world']['metrics']['accepted_steps'],expected)
                reports.append(dict(case=i, expected_reachable=True, steps=expected, passed=True))
        output = os.environ.get('ARIA_PHYSICAL_EVAL_REPORT')
        if output:
            Path(output).write_text(json.dumps({'kind':'deterministic grid evaluation; not an LLM benchmark', 'cases':reports},indent=2))

    def test_obstacle_rejects_old_route_and_replans(self):
        self.init()
        plan = self.plan()['result']
        self.client.call('obstacle',cell=[1,0])
        rejected = self.client.call('step',plan_id=plan['plan']['id'],observed_revision=plan['revision'])
        self.assertEqual(rejected['error'],'stale_observation')
        self.assertEqual(self.step(plan['plan']['id'])['error'],'stale_plan')
        fresh = self.plan()['result']
        self.assertEqual(len(fresh['plan']['remaining']),6)
        for _ in range(6):
            result = self.step(fresh['plan']['id'])
            self.assertTrue(result['ok'])
            self.assertNotEqual(result['result']['world']['position'],[1,0])
        state = result['result']
        self.assertTrue(state['world']['reached'])
        self.assertEqual(state['world']['metrics']['plans_created'],2)
        self.assertGreaterEqual(state['world']['metrics']['denied_actions'],2)

    def test_stop_is_latched_and_no_hardware_parameters(self):
        self.init()
        plan = self.plan()['result']['plan']['id']
        self.assertTrue(self.client.call('stop')['ok'])
        self.assertEqual(self.step(plan)['error'],'stopped')
        self.assertEqual(self.client.call('observe',mode='hardware')['error'],'invalid_request')
        self.assertEqual(self.client.call('actuator.command')['error'],'invalid_request')
        self.assertEqual(self.client.call('snapshot')['result']['world']['position'],[0,0])

    def test_protocol_errors_preserve_pose_and_process(self):
        state = self.init()
        for params in [dict(width=0,height=1,start=[0,0],goal=[0,0],obstacles=[]),
                       dict(width=3,height=3,start=[0,0],goal=[2,2],obstacles=[[0,0]])]:
            self.assertFalse(self.client.call('init',scenario=params)['ok'])
        after = self.client.call('snapshot')['result']
        self.assertEqual(after['revision'],state['revision'])
        self.assertEqual(after['world']['position'],state['world']['position'])

    def test_native_frontend_entrypoint(self):
        binary = os.environ.get('ARIA_NATIVE_BINARY')
        if not binary:
            self.skipTest('Native frontend not configured')
        client = Client(binary,native=True)
        self.addCleanup(client.close)
        self.assertTrue(client.call('init', scenario=dict(width=1,height=1,start=[0,0],goal=[0,0],obstacles=[]))['ok'])
        self.assertTrue(client.call('observe')['result']['world']['reached'])



class NativePhysicalHTTPTests(unittest.TestCase):
    def setUp(self):
        binary = os.environ.get('ARIA_PHYSICAL_BINARY')
        if not binary:
            self.skipTest('Rust service not configured')
        self.proc = subprocess.Popen([binary,'serve','--listen','127.0.0.1:0'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.close)
        q = queue.Queue()
        threading.Thread(target=lambda:q.put(self.proc.stderr.readline()),daemon=True).start()
        import re
        line = q.get(timeout=10)
        self.url = re.search(r'http://127\.0\.0\.1:[0-9]+',line).group(0)

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()

    def test_real_http_same_origin_and_size_boundaries(self):
        import urllib.request
        import urllib.error
        with urllib.request.urlopen(self.url+'/health',timeout=5) as response:
            self.assertTrue(json.load(response)['simulation_only'])
        with urllib.request.urlopen(self.url+'/',timeout=5) as response:
            self.assertIn(b'canvas',response.read())
        # Send the Host verbatim over a direct socket. System urllib proxies can
        # rewrite Host, which would not test the listener's rejection at all.
        import http.client
        authority = self.url.removeprefix('http://')
        for headers in [{'Host':'evil.example'}, {'Origin':'https://evil.example'}]:
            conn = http.client.HTTPConnection(authority,timeout=5)
            try:
                conn.request('GET','/health',headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status,403)
                response.read()
            finally:
                conn.close()
        request=urllib.request.Request(self.url+'/api',data=b'x'*65537,headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request,timeout=5)
        self.assertEqual(error.exception.code,422)
        self.assertEqual(json.load(error.exception)['error'],'message_too_large')

    def test_http_state_transition_and_stop(self):
        import urllib.request
        def api(body):
            request=urllib.request.Request(self.url+'/api',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':self.url})
            with urllib.request.urlopen(request,timeout=5) as response:
                return json.load(response)
        self.assertTrue(api(dict(op='init',scenario=dict(width=2,height=1,start=[0,0],goal=[1,0],obstacles=[])))['ok'])
        observed=api(dict(op='observe'))['result']
        plan=api(dict(op='plan',observed_revision=observed['revision']))['result']['plan']['id']
        result=api(dict(op='step',plan_id=plan,observed_revision=observed['revision']))
        self.assertTrue(result['result']['world']['reached'])
        self.assertTrue(api(dict(op='stop'))['result']['world']['stopped'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

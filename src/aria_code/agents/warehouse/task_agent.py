"""Stateful warehouse task agent capability; reasoning stays in Aria's model.

This is a thin process adapter, not another Python planner or control loop.
The native agent owns execution state and feedback-driven route decisions.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
from typing import Any

SCHEMA = json.loads(Path(__file__).with_name('navigation.schema.json').read_text())
DESCRIPTION = (
    "Delegate a bounded warehouse navigation simulation to the warehouse task agent. "
    "Use action=create with the user's goal, a known or explicitly requested demo scenario, "
    "constraints and a unique request_id. Then start once with task_id/request_id and "
    "use wait (timeout_ms <= 1000) or status until terminal. The native agent observes, "
    "plans, moves, replans after obstacles, and verifies the goal itself. "
    "Actions: health, create, start, status, wait, stop, obstacle, trace. "
    "Never report completion from a start acknowledgement: require completed and verified=true. "
    "Simulation only; no hardware, real warehouse sensing, or paid inference in the executor."
)
WORKFLOW_KNOWLEDGE = """Warehouse task agent workflow:
- Understand the user's goal, map, destination and constraints. Ask for missing real-world data;
  use synthetic scenes only when the user requested a demo/simulation. Never invent a real map.
- Delegate a mission through warehouse_task. Preserve the returned task_id and request IDs.
- The native agent selects and updates routes from current feedback; do not issue one model
  call per grid cell or tell the user to manually chain low-level APIs.
- Start is an acknowledgement, not success. Wait/status until completed, stopped, failed or
  needs_input. Only completed + verified=true proves the goal was reached.
- On needs_input, explain the actual obstacle/constraint and ask for the user's decision;
  do not silently widen the goal or restart an unsuccessful task with larger budgets.
- Honor stop requests. Review actual world/trace/metrics before reporting results.
- This capability is a logical simulation, not a connected robot or physics simulator.
"""


def is_navigation_task(message: str) -> bool:
    return bool(re.search(
        r'warehouse_task|warehouse.{0,40}(?:navigat|robot|shelf|simulation)|'
        r'\b(?:navigation|physical simulation)\b|仓库.{0,20}(?:导航|机器人|仿真)|'
        r'(?:送|运|移动|导航).{0,25}(?:货架|货位)|(?:导航|仓储)仿真',
        str(message or ''), re.I,
    ))


def _binary() -> tuple[str, list[str]]:
    configured = os.environ.get('ARIA_PHYSICAL_BINARY')
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RuntimeError('Configured ARIA_PHYSICAL_BINARY is missing')
        return str(path), ['physical'] if path.name.startswith('aria-native') else []
    from aria_code.runtime.native_index import native_binary
    candidate = native_binary()
    if candidate:
        return candidate, ['physical']
    root = Path(__file__).resolve().parents[4]
    suffix = '.exe' if os.name == 'nt' else ''
    candidate = root / 'rust' / 'target' / 'debug' / ('aria-physical' + suffix)
    if candidate.is_file():
        return str(candidate), []
    raise RuntimeError('Native warehouse task agent is not installed; build aria-physical and configure the host ARIA_PHYSICAL_BINARY')


class _NativeAgent:
    def __init__(self):
        binary, prefix = _binary()
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TMP', 'TEMP', 'TMPDIR'}}
        self.process = subprocess.Popen([binary, *prefix, 'agent-jsonl'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8', env=env)
        self.lock = threading.Lock()
        self.responses: queue.Queue = queue.Queue(maxsize=16)
        def read():
            while True:
                line = self.process.stdout.readline(1_048_577)
                if not line or len(line) > 1_048_576:
                    self.responses.put(None)
                    break
                self.responses.put(line)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        check = self.call({'action': 'health'})
        if not check.get('ok') or check.get('result', {}).get('protocol') != 'warehouse-task-agent-v1':
            self.close()
            raise RuntimeError('Installed native binary does not support the warehouse task agent')

    def call(self, params: dict) -> dict:
        raw = json.dumps(params, ensure_ascii=True, allow_nan=False)
        if len(raw.encode()) + 1 > 65536:
            raise ValueError('Task request exceeds 64 KiB')
        with self.lock:
            try:
                self.process.stdin.write(raw + '\n')
                self.process.stdin.flush()
                line = self.responses.get(timeout=5)
                if line is None:
                    raise RuntimeError('Native agent disconnected; task state is unknown')
                result = json.loads(line)
                if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
                    raise ValueError('Unexpected native agent response')
                return result
            except (OSError, queue.Empty, ValueError) as exc:
                # Never transparently restart/retry an uncertain mutation.
                self.close()
                raise RuntimeError('Native agent unavailable; task state is unknown, action not retried') from exc

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
        if self.process.stdout:
            self.process.stdout.close()


class WarehouseTaskAgent:
    """One native task-agent process per trusted Aria conversation context."""
    name = 'warehouse_navigation'
    capabilities = ('goal_contract', 'observe', 'plan', 'execute', 'replan', 'verify', 'stop')

    def __init__(self):
        self._agents: dict[str, _NativeAgent] = {}
        self._lock = threading.Lock()

    def handle(self, params: dict[str, Any]) -> dict:
        scope = params.get('_warehouse_scope')
        if not isinstance(scope, str) or not scope:
            return {'success': False, 'error': 'Trusted host conversation scope is required'}
        public = {k: v for k, v in params.items() if not k.startswith('_')}
        if params.get('_warehouse_read_only') and public.get('action') in {'create', 'start', 'obstacle'}:
            return {'success': False, 'error': 'Task mutations are blocked in read-only mode'}
        try:
            with self._lock:
                agent = self._agents.get(scope)
                if agent is None:
                    if len(self._agents) >= 16:
                        return {'success': False, 'error': 'Task agent conversation capacity reached'}
                    agent = _NativeAgent()
                    self._agents[scope] = agent
            result = agent.call(public)
            return {'success': result['ok'], **result}
        except (RuntimeError, ValueError, OSError) as exc:
            return {'success': False, 'error': str(exc), 'task_state': 'unknown'}

    def close(self):
        for agent in self._agents.values():
            agent.close()
        self._agents.clear()


WAREHOUSE_AGENT = WarehouseTaskAgent()
atexit.register(WAREHOUSE_AGENT.close)
WAREHOUSE_TOOLS = {'warehouse_task': (WAREHOUSE_AGENT.handle, DESCRIPTION)}
WAREHOUSE_SCHEMAS = [{'name': 'warehouse_task', 'description': DESCRIPTION, 'parameters': SCHEMA}]

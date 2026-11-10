# Research lab without a robot

This source example creates tangible research artifacts from a bounded simulation experiment. It uses the existing installed Rust task controller; the research inventory module is new in this PR and must be run from this source revision, not assumed present in installed 0.165.0.

From a checkout with the project's Python dependencies installed:

```sh
PYTHONPATH=src python examples/research-lab/run_navigation.py \
  --native /absolute/path/to/aria-code-bin/_internal/aria-native \
  --out /absolute/path/to/new-experiment

PYTHONPATH=src python examples/research-lab/run_navigation.py \
  --native /absolute/path/to/aria-code-bin/_internal/aria-native \
  --out /absolute/path/to/new-experiment --replay

PYTHONPATH=src python -m aria_code.runtime.research_bundle \
  /absolute/path/to/new-experiment manifest.json
```

The executable is explicitly selected by the host; no artifact chooses a command to execute. Use the official installed aria-native, whose `physical agent-jsonl` protocol is required. The output directory must be new or empty. The generator writes inputs, observed results, method, environment fingerprint, report and manifest. Replay requires matching input fixtures, simulator bytes and method bytes, then executes the simulator again and compares normalized results.

Three regression oracles: direct route (four steps), detour (six steps), blocked start (`needs_input`, zero steps). An unreachable task is an expected negative result; it must not be described as a successful navigation mission. These fixtures do not evaluate learned models, sensor noise, dynamics, statistical generalization or real equipment.

The inventory rejects missing/changed/unsafe artifacts and incompatible declared evidence scopes. It does not execute code, fetch URLs, authenticate provenance or judge scientific correctness. A malicious author can hash fabricated data; review and independently observed replay remain necessary. A claimed real_world conclusion is unsupported in this no-hardware contract.

To use the project workflow with existing Aria task roles, run the Python source CLI in this directory:

```sh
PYTHONPATH=src python -m aria_code.aria_cli -C examples/research-lab
```

Example request: “Help design a reproducible study of a navigation controller without hardware. Have a planner define a falsifiable hypothesis and fixed acceptance criteria. Use the project research protocol, run only authorized simulation commands, ask a reviewer to inspect limitations, and return a memo plus evidence inventory. Do not claim a real robot was validated.”

The local task-context manifest supplies WORKFLOW.md to eligible delegates under the existing scoped-context feature. No extra agent framework, global memory scan or second model account is introduced. Actual model adherence and literature quality still require separate evaluation.

For other domains, replace the experiment with numerical analysis, a small software prototype, a public-data study or a labeled synthetic dataset. Keep design, analytical, software and simulation claims separate from real-world effectiveness. If real data is absent, deliver an experiment protocol and data-collection plan rather than an empirical conclusion.

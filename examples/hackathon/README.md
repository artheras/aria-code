# Aria × Nebius × NVIDIA: reproducible coding demo

Aria turns an inventory request into Python code, executed tests, JSON output,
and a report. Inference uses **`nvidia/nemotron-3-super-120b-a12b` on Nebius Token
Factory**. This is synthetic inventory data, not a connected robot or warehouse.

## Run a fresh task

Use Python 3.10–3.13 on a machine where you permit the agent to edit a new task
directory and run Python commands. Use a disposable environment without private
files for evaluation. Dependency installation needs internet access.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
# Set NEBIUS_API_KEY privately in your shell or secret manager.
# Never commit it, paste it into the task, or include it in command arguments.
python examples/hackathon/run_demo.py --workspace /tmp/aria-demo-new --check
python examples/hackathon/run_demo.py --workspace /tmp/aria-demo-new
```

`--check` does not contact Nebius and is not proof that a key works. Each live run
can incur inference charges. Choose a new directory for every run; existing data
is never overwritten. No private Google Cloud account is needed. The key is used
only by a temporary localhost proxy; the child agent receives a placeholder key.
TLS certificate verification stays enabled. The agent retains the host's
workspace sandbox configuration. This is not a guarantee of complete isolation
on every operating system: use a disposable machine/container where appropriate.

The runner makes genuine inference requests and then separately verifies the
generated code with `verify.py`, outside the task folder. If that verifier fails,
it preserves the first trace and failure and allows one model repair (at most two
420-second agent attempts). The final result is checked again. It exits nonzero for
failed acceptance, failed completion or timeout. Inspect `evidence.json`,
`runtime.jsonl`, and `independent-acceptance.txt` in the workspace. Empty usage
counts are not interpreted as zero cost. Model output is nondeterministic: an
earlier successful run is not a promise that every run will pass.

## Recorded evidence and video

The review video is an **edited visualization of an actual runtime trace**, not
a screen recording of the interactive desktop product. The recorded source was
`671f559f36464c49b1e720d0be2f3b9c78bb860d`; later prompt/format fixes are separate.
The observed run took 39.36 seconds and made 9 inference requests. It generated
7 unittest cases and passed 8 independently defined acceptance tests. One stream
recorded `IncompleteRead`; do not describe transport as flawless. These results
describe one task, not a model benchmark. No GPU deployment, fine-tuning, robot
control, or cross-provider superiority is claimed.

For a captured workspace, rerun deterministic verification without any API key:

```sh
python examples/hackathon/verify.py /path/to/captured/workspace
```

Current review assets are prepared separately from the source repository. A
public demo/video URL and final Devpost submission still require entrant review
and publication. This directory is a runnable source example, not a hosted demo.

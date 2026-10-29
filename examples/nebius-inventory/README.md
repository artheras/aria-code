# Nebius inventory coding workflow

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
python examples/nebius-inventory/run_demo.py --workspace /tmp/aria-demo-new --check
python examples/nebius-inventory/run_demo.py --workspace /tmp/aria-demo-new
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

## Independent verification

Verify a generated workspace without making an inference call:

```sh
python examples/nebius-inventory/verify.py /path/to/generated/workspace
```

The verifier checks quantities, duplicate SKUs, over-reservation, CLI output,
and required deliverables. It is kept outside the model's task directory.

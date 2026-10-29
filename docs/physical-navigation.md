# Native navigation simulation

Aria provides a deterministic **2D logical grid simulation** implemented in Rust.
It is a software integration and verification fixture. It has no physics engine,
real camera, learned perception, robot dynamics, ROS transport or hardware driver.
The BFS planner does not call a model. Existing Nebius model workflows remain separate.

## Run

```sh
cd rust
cargo build --locked -p aria-native -p aria-physical
./target/debug/aria-native physical serve --listen 127.0.0.1:8765
# Standalone binary: ./target/debug/aria-physical serve
```

Open the printed numeric loopback URL. Plan the route, run the mission, and insert
an obstacle. The old route is rejected; the UI observes and replans before moving.
STOP is latched until a new mission. The current trace can be downloaded locally.
This is a single-user development service; all clients share its in-memory mission.
It is not an authenticated production endpoint. Restarting the process loses state.

The same Rust core is available as a persistent JSONL service, without Python:

```sh
./target/debug/aria-native physical jsonl
```

One JSON object per line, one response per line:

```json
{"op":"init","scenario":{"width":5,"height":4,"start":[0,0],"goal":[4,0],"obstacles":[]}}
{"op":"observe"}
{"op":"plan","observed_revision":1}
{"op":"step","plan_id":1,"observed_revision":1}
{"op":"observe"}
{"op":"obstacle","cell":[2,0]}
{"op":"snapshot"}
{"op":"stop"}
```

Read actual revisions and plan IDs from responses rather than assuming the example
numbers remain current. HTTP exposes the same objects at `POST /api`, plus `GET /`
and `GET /health`. Request bodies are bounded to 64 KiB. The HTTP listener accepts
only a numeric loopback address, validates Host and browser Origin, and exposes no
hardware mode or arbitrary command execution. Malformed/extra fields fail closed.

## State and action contract

- Worlds are at most 64 by 64 cells. Coordinates are nonnegative integer pairs.
- Observe issues a five-second monotonic lease for the current state revision.
  Snapshot reads do not renew the lease.
- Plan stores a path on the host; a step names its plan ID, not an arbitrary path.
- Every accepted step changes the revision and requires another observation.
- Adding an obstacle invalidates the old observation and route. A fresh observation
  alone cannot revive the route: a new plan is required.
- Every move must be an adjacent, in-bounds, unblocked cell. Missions have a
  512-accepted-step budget. STOP invalidates the route and prevents further movement.
- New missions reset mission metrics but do not reuse revisions or plan IDs.
- Ordered audit events retain initialization, obstacle updates, rejected requests,
  planning and actual accepted positions. The last 4096 events are retained;
  `dropped_events` explicitly reports truncation. This is bounded local diagnostics,
  not a durable, tamper-proof fleet event store.

## Independent validation

```sh
cargo test --workspace --locked
cd ..
ARIA_PHYSICAL_BINARY="$PWD/rust/target/debug/aria-physical" \
ARIA_NATIVE_BINARY="$PWD/rust/target/debug/aria-native" \
python3 tests/test_native_physical.py
```

The executable tests compare 20 fixed maps with an independent Python distance
oracle and check real per-step positions. They also exercise dynamic obstacles,
stale observations/routes, STOP, protocol errors, the native frontend entrypoint,
and real HTTP transitions, Host/Origin rejection and size limits. These are
**deterministic service tests**, not a 20-task LLM benchmark or a robotics accuracy
claim. Additional Rust tests cover lease expiry without wall-clock sleeps.

## Extension boundary

A future Isaac Sim/ROS adapter must preserve observation provenance, units, frame
IDs, timestamps, task IDs, constraints and measured execution feedback. Hardware
execution needs a separate driver, authorization boundary, watchdog and independent
stop mechanism. Do not add a `mode=hardware` switch to this simulator or treat the
CLI's existing command-name risk assessment as a real robot safety controller.
A model may propose a goal or bounded plan; validated local control owns execution.

Use a physics simulator to validate kinematics, sensing and collisions before any
sim-to-real claim. Cloud inference is not the control path for immediate stopping.

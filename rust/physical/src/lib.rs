//! Simulation-only, deterministic navigation. No hardware transport is provided.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{self, BufRead, Read, Write};
use std::net::SocketAddr;
use std::time::{Duration, Instant};

pub type Cell = [u16; 2];
const MAX_MESSAGE: usize = 65_536;
const MAX_EVENTS: usize = 4096;
const MAX_STEPS: u64 = 512;
const OBSERVATION_TTL: Duration = Duration::from_secs(5);

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Scenario {
    pub width: u16,
    pub height: u16,
    pub start: Cell,
    pub goal: Cell,
    pub obstacles: Vec<Cell>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum Request {
    Init {
        scenario: Scenario,
    },
    Observe,
    Plan {
        observed_revision: u64,
    },
    Step {
        plan_id: u64,
        observed_revision: u64,
    },
    Obstacle {
        cell: Cell,
    },
    Stop,
    Snapshot,
}

#[derive(Default, Clone, Debug, Serialize)]
pub struct Metrics {
    pub accepted_steps: u64,
    pub denied_actions: u64,
    pub plans_created: u64,
    pub obstacle_updates: u64,
}

#[derive(Debug)]
struct Plan {
    id: u64,
    revision: u64,
    remaining: VecDeque<Cell>,
}

#[derive(Debug)]
struct World {
    scenario: Scenario,
    position: Cell,
    blocked: BTreeSet<Cell>,
    stopped: bool,
    metrics: Metrics,
}

/// Only a revision issued by observe and still inside its lease may act.
/// A plan is stored in the host; a client cannot replace its path in step.
#[derive(Default)]
pub struct Engine {
    world: Option<World>,
    revision: u64,
    next_plan: u64,
    observation: Option<(u64, Instant)>,
    plan: Option<Plan>,
    sequence: u64,
    events: VecDeque<Value>,
    dropped_events: u64,
}

fn inside(s: &Scenario, c: Cell) -> bool {
    c[0] < s.width && c[1] < s.height
}

fn validate(s: &Scenario) -> Result<(), &'static str> {
    if s.width == 0 || s.height == 0 || s.width > 64 || s.height > 64 {
        return Err("grid_dimensions");
    }
    if s.obstacles.len() > 4096 || !inside(s, s.start) || !inside(s, s.goal) {
        return Err("invalid_cell");
    }
    let blocked: BTreeSet<_> = s.obstacles.iter().copied().collect();
    if blocked.len() != s.obstacles.len()
        || blocked.contains(&s.start)
        || blocked.contains(&s.goal)
        || blocked.iter().any(|c| !inside(s, *c))
    {
        return Err("invalid_obstacles");
    }
    Ok(())
}

fn neighbors(s: &Scenario, [x, y]: Cell) -> Vec<Cell> {
    [
        (i32::from(x) + 1, i32::from(y)),
        (i32::from(x), i32::from(y) + 1),
        (i32::from(x) - 1, i32::from(y)),
        (i32::from(x), i32::from(y) - 1),
    ]
    .into_iter()
    .filter(|(a, b)| *a >= 0 && *b >= 0 && *a < i32::from(s.width) && *b < i32::from(s.height))
    .map(|(a, b)| [a as u16, b as u16])
    .collect()
}

fn shortest_path(w: &World) -> Option<VecDeque<Cell>> {
    let mut queue = VecDeque::from([w.position]);
    let mut parent = BTreeMap::from([(w.position, w.position)]);
    while let Some(cell) = queue.pop_front() {
        if cell == w.scenario.goal {
            let mut path = VecDeque::new();
            let mut cursor = cell;
            while cursor != w.position {
                path.push_front(cursor);
                cursor = parent[&cursor];
            }
            return Some(path);
        }
        for next in neighbors(&w.scenario, cell) {
            if !w.blocked.contains(&next) && !parent.contains_key(&next) {
                parent.insert(next, cell);
                queue.push_back(next);
            }
        }
    }
    None
}

impl Engine {
    fn log(&mut self, op: &str, ok: bool, code: &str) {
        self.sequence += 1;
        let position = self.world.as_ref().map(|w| w.position);
        if self.events.len() == MAX_EVENTS {
            self.events.pop_front();
            self.dropped_events += 1;
        }
        self.events
            .push_back(json!({"seq": self.sequence, "op": op, "ok": ok,
            "code": code, "revision": self.revision, "position": position}));
    }

    fn fail(&mut self, op: &str, code: &str) -> Value {
        if let Some(w) = &mut self.world {
            w.metrics.denied_actions += 1;
        }
        self.log(op, false, code);
        json!({"ok": false, "error": code, "simulation_only": true, "revision": self.revision})
    }

    fn fresh(&self, revision: u64, now: Instant) -> Result<(), &'static str> {
        match self.observation {
            Some((r, when)) if r == revision && r == self.revision => {
                if now.saturating_duration_since(when) <= OBSERVATION_TTL {
                    Ok(())
                } else {
                    Err("observation_expired")
                }
            }
            _ => Err("stale_observation"),
        }
    }

    pub fn snapshot(&self) -> Value {
        let world = self.world.as_ref().map(|w| {
            json!({"width": w.scenario.width,
            "height": w.scenario.height, "position": w.position, "goal": w.scenario.goal,
            "obstacles": w.blocked, "stopped": w.stopped, "reached": w.position == w.scenario.goal,
            "metrics": w.metrics})
        });
        let plan = self
            .plan
            .as_ref()
            .map(|p| json!({"id": p.id, "remaining": p.remaining}));
        json!({"backend": "rust-grid-v1", "simulation_only": true, "physics_engine": false,
            "revision": self.revision, "world": world, "plan": plan,
            "events": self.events, "dropped_events": self.dropped_events})
    }

    pub fn handle(&mut self, request: Request) -> Value {
        self.handle_at(request, Instant::now())
    }

    fn handle_at(&mut self, request: Request, now: Instant) -> Value {
        let op = match &request {
            Request::Init { .. } => "init",
            Request::Observe => "observe",
            Request::Plan { .. } => "plan",
            Request::Step { .. } => "step",
            Request::Obstacle { .. } => "obstacle",
            Request::Stop => "stop",
            Request::Snapshot => "snapshot",
        };
        if let Request::Init { scenario } = request {
            if let Err(code) = validate(&scenario) {
                return self.fail(op, code);
            }
            self.revision += 1;
            self.observation = None;
            self.plan = None;
            self.world = Some(World {
                position: scenario.start,
                blocked: scenario.obstacles.iter().copied().collect(),
                scenario,
                stopped: false,
                metrics: Metrics::default(),
            });
            // Keep the audit sequence across resets; never reuse plan IDs.
            self.log(op, true, "initialized");
            self.events.back_mut().unwrap()["scenario"] =
                serde_json::to_value(&self.world.as_ref().unwrap().scenario).unwrap();
            return json!({"ok": true, "result": self.snapshot()});
        }
        if self.world.is_none() {
            return self.fail(op, "not_initialized");
        }
        match request {
            Request::Observe => {
                self.observation = Some((self.revision, now));
                self.log(op, true, "observed");
            }
            Request::Snapshot => {} // Snapshot is not an observation lease.
            Request::Stop => {
                self.world.as_mut().unwrap().stopped = true;
                self.plan = None;
                self.observation = None;
                self.revision += 1;
                self.log(op, true, "stopped");
            }
            Request::Obstacle { cell } => {
                let w = self.world.as_mut().unwrap();
                if !inside(&w.scenario, cell) || cell == w.position {
                    return self.fail(op, "invalid_obstacle");
                }
                if w.blocked.contains(&cell) {
                    return self.fail(op, "duplicate_obstacle");
                }
                w.blocked.insert(cell);
                w.metrics.obstacle_updates += 1;
                self.revision += 1;
                self.observation = None;
                // Keep the old plan for audit/rejection; it may never execute.
                self.log(op, true, "environment_changed");
                self.events.back_mut().unwrap()["cell"] = json!(cell);
            }
            Request::Plan { observed_revision } => {
                if self.world.as_ref().unwrap().stopped {
                    return self.fail(op, "stopped");
                }
                if let Err(code) = self.fresh(observed_revision, now) {
                    return self.fail(op, code);
                }
                self.plan = None; // A failed replan must not revive an older route.
                let Some(path) = shortest_path(self.world.as_ref().unwrap()) else {
                    return self.fail(op, "unreachable");
                };
                self.next_plan += 1;
                self.plan = Some(Plan {
                    id: self.next_plan,
                    revision: self.revision,
                    remaining: path,
                });
                self.world.as_mut().unwrap().metrics.plans_created += 1;
                self.log(op, true, "planned");
            }
            Request::Step {
                plan_id,
                observed_revision,
            } => {
                let w = self.world.as_ref().unwrap();
                if w.stopped {
                    return self.fail(op, "stopped");
                }
                if w.metrics.accepted_steps >= MAX_STEPS {
                    return self.fail(op, "step_budget");
                }
                if let Err(code) = self.fresh(observed_revision, now) {
                    return self.fail(op, code);
                }
                let Some(p) = self.plan.as_ref() else {
                    return self.fail(op, "no_plan");
                };
                if p.id != plan_id {
                    return self.fail(op, "unknown_plan");
                }
                if p.revision != self.revision {
                    return self.fail(op, "stale_plan");
                }
                let Some(&next) = p.remaining.front() else {
                    return self.fail(op, "plan_complete");
                };
                if !inside(&w.scenario, next)
                    || w.blocked.contains(&next)
                    || !neighbors(&w.scenario, w.position).contains(&next)
                {
                    return self.fail(op, "unsafe_step");
                }
                let w = self.world.as_mut().unwrap();
                w.position = next;
                w.metrics.accepted_steps += 1;
                self.revision += 1;
                self.observation = None;
                let p = self.plan.as_mut().unwrap();
                p.remaining.pop_front();
                p.revision = self.revision;
                self.log(op, true, "moved");
            }
            Request::Init { .. } => unreachable!(),
        }
        json!({"ok": true, "result": self.snapshot()})
    }

    pub fn dispatch(&mut self, raw: &[u8]) -> Value {
        if raw.len() > MAX_MESSAGE {
            return self.fail("protocol", "message_too_large");
        }
        // Serde unit variants otherwise ignore extra fields even with the enum's
        // deny_unknown_fields. Validate every operation's complete key set first.
        let parsed = serde_json::from_slice::<Value>(raw).ok();
        let allowed: &[&str] = match parsed
            .as_ref()
            .and_then(|v| v.get("op"))
            .and_then(Value::as_str)
        {
            Some("init") => &["op", "scenario"],
            Some("plan") => &["op", "observed_revision"],
            Some("step") => &["op", "plan_id", "observed_revision"],
            Some("obstacle") => &["op", "cell"],
            Some("observe" | "stop" | "snapshot") => &["op"],
            _ => return self.fail("protocol", "invalid_request"),
        };
        if !parsed
            .as_ref()
            .and_then(Value::as_object)
            .is_some_and(|obj| obj.keys().all(|key| allowed.contains(&key.as_str())))
        {
            return self.fail("protocol", "invalid_request");
        }
        match serde_json::from_slice::<Request>(raw) {
            Ok(request) => self.handle(request),
            Err(_) => self.fail("protocol", "invalid_request"),
        }
    }
}

pub fn jsonl(input: impl BufRead, mut output: impl Write) -> io::Result<()> {
    let mut input = input;
    let mut engine = Engine::default();
    loop {
        let mut raw = Vec::new();
        let n = input
            .by_ref()
            .take((MAX_MESSAGE + 2) as u64)
            .read_until(b'\n', &mut raw)?;
        if n == 0 {
            break;
        }
        let value = if raw.len() > MAX_MESSAGE {
            // Reject and terminate; never interpret a truncated tail as a new command.
            engine.fail("protocol", "message_too_large")
        } else {
            engine.dispatch(&raw)
        };
        serde_json::to_writer(&mut output, &value)?;
        writeln!(output)?;
        output.flush()?;
        if raw.len() > MAX_MESSAGE {
            break;
        }
    }
    Ok(())
}

fn serve(address: &str) -> Result<i32, String> {
    let address: SocketAddr = address
        .parse()
        .map_err(|_| "Use a numeric loopback IP and port")?;
    if !address.ip().is_loopback() {
        return Err("Simulation server binds only to loopback".into());
    }
    let server = tiny_http::Server::http(address).map_err(|e| e.to_string())?;
    let actual = server.server_addr().to_ip().ok_or("No TCP listener")?;
    let origin = format!("http://{actual}");
    eprintln!("Aria logical simulation: {origin} (no hardware connection)");
    let mut engine = Engine::default();
    for mut req in server.incoming_requests() {
        let trusted_host = req
            .headers()
            .iter()
            .any(|h| h.field.equiv("Host") && h.value.as_str() == actual.to_string());
        let trusted_origin = req
            .headers()
            .iter()
            .filter(|h| h.field.equiv("Origin"))
            .all(|h| h.value.as_str() == origin);
        let (body, status, content_type) = if !trusted_host || !trusted_origin {
            ("Forbidden".to_string(), 403, "text/plain")
        } else if req.method() == &tiny_http::Method::Get && req.url() == "/" {
            (
                include_str!("../ui/index.html").to_string(),
                200,
                "text/html; charset=utf-8",
            )
        } else if req.method() == &tiny_http::Method::Get && req.url() == "/health" {
            (
                json!({"ok": true, "backend": "rust-grid-v1", "simulation_only": true}).to_string(),
                200,
                "application/json",
            )
        } else if req.method() == &tiny_http::Method::Post && req.url() == "/api" {
            let mut raw = Vec::new();
            if req
                .as_reader()
                .take((MAX_MESSAGE + 1) as u64)
                .read_to_end(&mut raw)
                .is_err()
            {
                ("Bad request".into(), 400, "text/plain")
            } else {
                let result = engine.dispatch(&raw);
                let status = if result["ok"] == true { 200 } else { 422 };
                (result.to_string(), status, "application/json")
            }
        } else {
            ("Not found".into(), 404, "text/plain")
        };
        let response = tiny_http::Response::from_string(body)
            .with_status_code(status)
            .with_header(tiny_http::Header::from_bytes("Content-Type", content_type).unwrap())
            .with_header(tiny_http::Header::from_bytes("Cache-Control", "no-store").unwrap())
            .with_header(
                tiny_http::Header::from_bytes("X-Content-Type-Options", "nosniff").unwrap(),
            );
        let _ = req.respond(response);
    }
    Ok(0)
}

pub fn entry(args: &[String]) -> Result<i32, String> {
    match args {
        [] => {
            jsonl(io::stdin().lock(), io::stdout().lock()).map_err(|e| e.to_string())?;
            Ok(0)
        }
        [arg] if arg == "jsonl" => {
            jsonl(io::stdin().lock(), io::stdout().lock()).map_err(|e| e.to_string())?;
            Ok(0)
        }
        [arg] if arg == "serve" => serve("127.0.0.1:8765"),
        [arg, flag, address] if arg == "serve" && flag == "--listen" => serve(address),
        _ => Err("Usage: physical [jsonl|serve --listen 127.0.0.1:8765]".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn scenario() -> Scenario {
        Scenario {
            width: 5,
            height: 4,
            start: [0, 0],
            goal: [4, 0],
            obstacles: vec![],
        }
    }
    fn initialized() -> Engine {
        let mut e = Engine::default();
        assert_eq!(
            e.handle(Request::Init {
                scenario: scenario()
            })["ok"],
            true
        );
        e
    }
    fn plan(e: &mut Engine) -> u64 {
        let r = e.handle(Request::Observe)["result"]["revision"]
            .as_u64()
            .unwrap();
        e.handle(Request::Plan {
            observed_revision: r,
        })["result"]["plan"]["id"]
            .as_u64()
            .unwrap()
    }
    fn step(e: &mut Engine, id: u64) -> Value {
        let r = e.handle(Request::Observe)["result"]["revision"]
            .as_u64()
            .unwrap();
        e.handle(Request::Step {
            plan_id: id,
            observed_revision: r,
        })
    }
    #[test]
    fn obstacle_requires_fresh_observation_and_new_plan() {
        let mut e = initialized();
        let id = plan(&mut e);
        let old = e.revision;
        e.handle(Request::Obstacle { cell: [1, 0] });
        assert_eq!(
            e.handle(Request::Step {
                plan_id: id,
                observed_revision: old
            })["error"],
            "stale_observation"
        );
        assert_eq!(step(&mut e, id)["error"], "stale_plan");
        assert_eq!(e.snapshot()["world"]["position"], json!([0, 0]));
        let id = plan(&mut e);
        for _ in 0..6 {
            assert_eq!(step(&mut e, id)["ok"], true);
        }
        assert_eq!(e.snapshot()["world"]["reached"], true);
        assert_eq!(e.snapshot()["world"]["metrics"]["accepted_steps"], 6);
    }
    #[test]
    fn stopped_even_with_new_observation() {
        let mut e = initialized();
        let id = plan(&mut e);
        e.handle(Request::Stop);
        assert_eq!(step(&mut e, id)["error"], "stopped");
        let r = e.revision;
        assert_eq!(
            e.handle(Request::Plan {
                observed_revision: r
            })["error"],
            "stopped"
        );
        assert_eq!(e.snapshot()["world"]["position"], json!([0, 0]));
    }
    #[test]
    fn no_motion_without_lease_and_no_replayed_step() {
        let mut e = initialized();
        assert_eq!(
            e.handle(Request::Plan {
                observed_revision: e.revision
            })["error"],
            "stale_observation"
        );
        let id = plan(&mut e);
        let r = e.revision;
        assert_eq!(
            e.handle(Request::Step {
                plan_id: id,
                observed_revision: r
            })["ok"],
            true
        );
        assert_eq!(
            e.handle(Request::Step {
                plan_id: id,
                observed_revision: r
            })["error"],
            "stale_observation"
        );
        assert_eq!(e.snapshot()["world"]["position"], json!([1, 0]));
    }
    #[test]
    fn lease_expires_without_wall_clock_sleep() {
        let mut e = initialized();
        let now = Instant::now();
        e.handle_at(Request::Observe, now);
        let r = e.revision;
        assert_eq!(
            e.handle_at(
                Request::Plan {
                    observed_revision: r
                },
                now + OBSERVATION_TTL + Duration::from_millis(1)
            )["error"],
            "observation_expired"
        );
    }
    #[test]
    fn reset_does_not_reuse_plan_or_revision() {
        let mut e = initialized();
        let old_id = plan(&mut e);
        let old_r = e.revision;
        e.handle(Request::Init {
            scenario: scenario(),
        });
        let new_id = plan(&mut e);
        assert!(new_id > old_id && e.revision > old_r);
        assert_eq!(step(&mut e, old_id)["error"], "unknown_plan");
    }
    #[test]
    fn unreachable_clears_old_route() {
        let mut e = initialized();
        plan(&mut e);
        e.handle(Request::Obstacle { cell: [4, 0] });
        e.handle(Request::Observe);
        let r = e.revision;
        assert_eq!(
            e.handle(Request::Plan {
                observed_revision: r
            })["error"],
            "unreachable"
        );
        assert!(e.plan.is_none());
    }
    #[test]
    fn zero_length_mission_is_reached_without_moving() {
        let mut s = scenario();
        s.goal = s.start;
        let mut e = Engine::default();
        e.handle(Request::Init { scenario: s });
        let id = plan(&mut e);
        assert_eq!(e.snapshot()["world"]["reached"], true);
        assert_eq!(step(&mut e, id)["error"], "plan_complete");
    }
    #[test]
    fn invalid_reset_preserves_existing_world() {
        let mut e = initialized();
        let mut s = scenario();
        s.width = 65;
        let r = e.revision;
        assert_eq!(
            e.handle(Request::Init { scenario: s })["error"],
            "grid_dimensions"
        );
        assert_eq!(e.revision, r);
        let mut s = scenario();
        s.obstacles = vec![[0, 0]];
        assert_eq!(
            e.handle(Request::Init { scenario: s })["error"],
            "invalid_obstacles"
        );
    }
    #[test]
    fn strict_protocol_has_no_hardware_escape() {
        let mut e = initialized();
        for raw in [
            r#"{"op":"step","plan_id":1,"observed_revision":1,"velocity":3}"#,
            r#"{"op":"observe","mode":"hardware"}"#,
            r#"{"op":"actuator.command"}"#,
            r#"{"op":"plan","observed_revision":1.5}"#,
        ] {
            assert_eq!(e.dispatch(raw.as_bytes())["error"], "invalid_request");
        }
    }
    #[test]
    fn bad_messages_and_uninitialized_actions() {
        let mut e = Engine::default();
        assert_eq!(e.handle(Request::Observe)["error"], "not_initialized");
        assert_eq!(
            e.dispatch(&vec![b'x'; MAX_MESSAGE + 1])["error"],
            "message_too_large"
        );
        assert_eq!(e.dispatch(b"{}{}")["error"], "invalid_request");
    }
    #[test]
    fn events_retain_environment_changes_and_report_truncation() {
        let mut e = initialized();
        e.handle(Request::Obstacle { cell: [1, 0] });
        assert_eq!(e.events[0]["scenario"]["width"], 5);
        assert_eq!(e.events[1]["cell"], json!([1, 0]));
        for _ in 0..MAX_EVENTS {
            e.log("observe", true, "observed");
        }
        assert_eq!(e.events.len(), MAX_EVENTS);
        assert_eq!(e.dropped_events, 2);
    }
    #[test]
    fn no_hardware_server_bind_and_budget_enforced() {
        assert!(entry(&["serve".into(), "--listen".into(), "0.0.0.0:0".into()]).is_err());
        let mut e = initialized();
        let id = plan(&mut e);
        e.world.as_mut().unwrap().metrics.accepted_steps = MAX_STEPS;
        assert_eq!(step(&mut e, id)["error"], "step_budget");
    }
    #[test]
    fn jsonl_oversize_is_not_reinterpreted() {
        let mut data = vec![b' '; MAX_MESSAGE + 1];
        data.extend_from_slice(b"\n{\"op\":\"observe\"}\n");
        let mut output = Vec::new();
        jsonl(io::Cursor::new(data), &mut output).unwrap();
        let lines = String::from_utf8(output).unwrap();
        assert_eq!(lines.lines().count(), 1);
        assert!(lines.contains("message_too_large"));
    }
}

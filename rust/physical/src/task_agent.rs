//! A goal-directed simulation task: inspect, decide, act, verify, or stop.
//! Natural-language understanding remains with the calling Aria/Claude/GPT agent;
//! this bounded executor owns current observations and local navigation decisions.
use crate::{Engine, Request, Scenario, MAX_MESSAGE};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::{self, BufRead, Read, Write};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const MAX_TASKS: usize = 32;
const TICK: Duration = Duration::from_millis(100);
pub const SCHEMA: &str =
    include_str!("../../../src/aria_code/agents/warehouse/navigation.schema.json");

#[derive(Clone, Debug, Deserialize, serde::Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Constraints {
    max_steps: u16,
    deadline_ms: u64,
}
impl Default for Constraints {
    fn default() -> Self {
        Self {
            max_steps: 128,
            deadline_ms: 30_000,
        }
    }
}
#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Health,
    Create {
        request_id: String,
        goal: String,
        scenario: Scenario,
        #[serde(default)]
        constraints: Constraints,
    },
    Start {
        task_id: String,
        request_id: String,
    },
    Status {
        task_id: String,
    },
    Wait {
        task_id: String,
        timeout_ms: u64,
    },
    Stop {
        task_id: String,
    },
    Obstacle {
        task_id: String,
        request_id: String,
        cell: crate::Cell,
    },
    Trace {
        task_id: String,
    },
}
struct Task {
    goal: String,
    engine: Engine,
    constraints: Constraints,
    state: &'static str,
    reason: String,
    verified: bool,
    start_key: Option<String>,
    obstacle_keys: BTreeMap<String, crate::Cell>,
    decisions: Vec<Value>,
}
impl Task {
    fn decision(&mut self, action: &str, reason: &str) {
        if self.decisions.len() == 128 {
            self.decisions.remove(0);
        }
        self.decisions
            .push(json!({"action":action,"reason":reason,"revision":self.engine.revision}));
    }
    fn report(&self, task_id: &str, trace: bool) -> Value {
        let snapshot = self.engine.snapshot();
        json!({"agent":"warehouse_navigation", "task_id":task_id,"goal":self.goal,
            "status":self.state,"reason":self.reason,"verified":self.verified,
            "simulation_only":true,"model_inference_in_executor":false,"constraints":self.constraints,
            "world":snapshot["world"],"revision":snapshot["revision"],
            "decisions":self.decisions,"trace":if trace { snapshot } else { Value::Null }})
    }
    fn finish(&mut self, state: &'static str, reason: &str) {
        self.state = state;
        self.reason = reason.to_owned();
        self.decision(state, reason);
    }
    fn tick(&mut self, elapsed: Duration) {
        if self.state != "running" {
            return;
        }
        if elapsed.as_millis() >= u128::from(self.constraints.deadline_ms) {
            self.engine.handle(Request::Stop);
            self.finish("failed", "deadline_exceeded");
            return;
        }
        let snapshot = self.engine.snapshot();
        if snapshot["world"]["stopped"] == true {
            self.finish("stopped", "stop_latched");
            return;
        }
        if snapshot["world"]["reached"] == true {
            self.verified = true;
            self.finish("completed", "observed_position_matches_goal");
            return;
        }
        let steps = snapshot["world"]["metrics"]["accepted_steps"]
            .as_u64()
            .unwrap_or(0);
        if steps >= u64::from(self.constraints.max_steps) {
            self.engine.handle(Request::Stop);
            self.finish("failed", "step_budget_exceeded");
            return;
        }
        // No model round-trip sits between a fresh observation and local execution.
        let observed = self.engine.handle(Request::Observe);
        let revision = observed["result"]["revision"].as_u64().unwrap();
        let needs_plan = self
            .engine
            .plan
            .as_ref()
            .is_none_or(|p| p.revision != revision);
        if needs_plan {
            self.decision("plan", "route_missing_or_environment_changed");
            let planned = self.engine.handle(Request::Plan {
                observed_revision: revision,
            });
            if planned["ok"] != true {
                self.finish(
                    "needs_input",
                    planned["error"].as_str().unwrap_or("planning_failed"),
                );
                return;
            }
        }
        let id = self.engine.plan.as_ref().unwrap().id;
        let result = self.engine.handle(Request::Step {
            plan_id: id,
            observed_revision: revision,
        });
        if result["ok"] != true {
            self.finish(
                "failed",
                result["error"].as_str().unwrap_or("movement_rejected"),
            );
            return;
        }
        if result["result"]["world"]["reached"] == true {
            self.verified = true;
            self.finish("completed", "observed_position_matches_goal");
        }
    }
}
#[derive(Default)]
pub struct TaskAgent {
    tasks: BTreeMap<String, Arc<Mutex<Task>>>,
    create_keys: BTreeMap<String, (String, String)>,
    threads: Vec<JoinHandle<()>>,
    next_id: u64,
}
fn fail(reason: &str) -> Value {
    json!({"ok":false,"error":reason,"simulation_only":true})
}
fn success(result: Value) -> Value {
    json!({"ok":true,"result":result})
}
fn valid_key(value: &str) -> bool {
    !value.trim().is_empty() && value.len() <= 128
}
impl TaskAgent {
    pub fn dispatch(&mut self, raw: &[u8]) -> Value {
        if raw.len() > MAX_MESSAGE {
            return fail("message_too_large");
        }
        let value: Value = match serde_json::from_slice(raw) {
            Ok(v) => v,
            Err(_) => return fail("invalid_request"),
        };
        // Unit variants need an explicit field check (serde otherwise ignores extras).
        if value["action"] == "health" && value.as_object().is_none_or(|v| v.len() != 1) {
            return fail("invalid_request");
        }
        match serde_json::from_slice::<Command>(raw) {
            Ok(c) => self.handle(c),
            Err(_) => json!({"ok":false,"error":"invalid_request","simulation_only":true,
                "hint":"Use only fields for the selected action: create(request_id,goal,scenario,constraints?); start(task_id,request_id); status/stop/trace(task_id); wait(task_id,timeout_ms); obstacle(task_id,request_id,cell); health(). Omit request_id from read actions."}),
        }
    }
    pub fn handle(&mut self, command: Command) -> Value {
        if matches!(&command, Command::Health) {
            return success(
                json!({"agent":"warehouse_navigation","protocol":"warehouse-task-agent-v1",
                "simulation_only":true,"max_tasks":MAX_TASKS,"state_storage":"process_memory"}),
            );
        }
        if let Command::Create {
            request_id,
            goal,
            scenario,
            constraints,
        } = command
        {
            if !valid_key(&request_id) || goal.trim().is_empty() || goal.len() > 2048 {
                return fail("invalid_task_contract");
            }
            if !(1..=512).contains(&constraints.max_steps)
                || !(100..=120000).contains(&constraints.deadline_ms)
            {
                return fail("invalid_constraints");
            }
            let contract =
                json!({"goal":goal,"scenario":scenario,"constraints":constraints}).to_string();
            if let Some((prior, id)) = self.create_keys.get(&request_id) {
                return if prior == &contract {
                    success(self.tasks[id].lock().unwrap().report(id, false))
                } else {
                    fail("request_id_reused_with_different_contract")
                };
            }
            if self.tasks.len() >= MAX_TASKS {
                return fail("task_capacity");
            }
            let mut engine = Engine::default();
            let initialized = engine.handle(Request::Init { scenario });
            if initialized["ok"] != true {
                return initialized;
            }
            self.next_id += 1;
            let id = format!(
                "mission-{}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_nanos(),
                self.next_id
            );
            let mut task = Task {
                goal,
                engine,
                constraints,
                state: "ready",
                reason: "awaiting_start".into(),
                verified: false,
                start_key: None,
                obstacle_keys: BTreeMap::new(),
                decisions: Vec::new(),
            };
            task.decision("accept_goal", "bounded_simulation_contract_created");
            let result = task.report(&id, false);
            self.tasks.insert(id.clone(), Arc::new(Mutex::new(task)));
            self.create_keys.insert(request_id, (contract, id));
            return success(result);
        }
        let (id, trace) = match &command {
            Command::Start { task_id, .. }
            | Command::Status { task_id }
            | Command::Wait { task_id, .. }
            | Command::Stop { task_id }
            | Command::Obstacle { task_id, .. } => (task_id.clone(), false),
            Command::Trace { task_id } => (task_id.clone(), true),
            _ => return fail("invalid_request"),
        };
        let Some(shared) = self.tasks.get(&id).cloned() else {
            return fail("unknown_task");
        };
        match command {
            Command::Start { request_id, .. } => {
                if !valid_key(&request_id) {
                    return fail("invalid_request_id");
                }
                let mut task = shared.lock().unwrap();
                if let Some(prior) = &task.start_key {
                    return if prior == &request_id {
                        success(task.report(&id, false))
                    } else {
                        fail("task_already_started")
                    };
                }
                if task.state != "ready" {
                    return fail("task_not_ready");
                }
                task.start_key = Some(request_id);
                task.state = "running";
                task.decision("start", "execute_until_goal_or_constraint");
                drop(task);
                let worker = shared.clone();
                self.threads.retain(|handle| !handle.is_finished());
                self.threads.push(thread::spawn(move || {
                    let started = Instant::now();
                    loop {
                        {
                            let mut task = worker.lock().unwrap();
                            if task.state != "running" {
                                break;
                            }
                            task.tick(started.elapsed());
                            if task.state != "running" {
                                break;
                            }
                        }
                        thread::sleep(TICK);
                    }
                }));
            }
            Command::Wait { timeout_ms, .. } => {
                if timeout_ms > 1000 {
                    return fail("wait_too_long");
                }
                let until = Instant::now() + Duration::from_millis(timeout_ms);
                while shared.lock().unwrap().state == "running" && Instant::now() < until {
                    thread::sleep(Duration::from_millis(10));
                }
            }
            Command::Stop { .. } => {
                let mut task = shared.lock().unwrap();
                task.engine.handle(Request::Stop);
                if !matches!(
                    task.state,
                    "completed" | "failed" | "needs_input" | "stopped"
                ) {
                    task.finish("stopped", "user_requested_stop");
                }
            }
            Command::Obstacle {
                request_id, cell, ..
            } => {
                if !valid_key(&request_id) {
                    return fail("invalid_request_id");
                }
                let mut task = shared.lock().unwrap();
                if let Some(prior) = task.obstacle_keys.get(&request_id) {
                    return if prior == &cell {
                        success(task.report(&id, false))
                    } else {
                        fail("request_id_reused_with_different_contract")
                    };
                }
                if !matches!(task.state, "ready" | "running") {
                    return fail("task_not_active");
                }
                let result = task.engine.handle(Request::Obstacle { cell });
                if result["ok"] != true {
                    return result;
                }
                task.obstacle_keys.insert(request_id, cell);
                task.decision("environment_changed", "old_route_invalidated");
            }
            _ => {}
        }
        let result = shared.lock().unwrap().report(&id, trace);
        success(result)
    }
}
impl Drop for TaskAgent {
    fn drop(&mut self) {
        for shared in self.tasks.values() {
            if let Ok(mut task) = shared.lock() {
                if task.state == "running" {
                    task.engine.handle(Request::Stop);
                    task.finish("stopped", "host_disconnected");
                }
            }
        }
        for handle in self.threads.drain(..) {
            let _ = handle.join();
        }
    }
}
pub fn stdio(input: impl BufRead, mut output: impl Write, mcp: bool) -> io::Result<()> {
    let mut input = input;
    let mut agent = TaskAgent::default();
    let mut initialized = false;
    loop {
        let mut raw = Vec::new();
        if input
            .by_ref()
            .take((MAX_MESSAGE + 2) as u64)
            .read_until(b'\n', &mut raw)?
            == 0
        {
            break;
        }
        let oversized = raw.len() > MAX_MESSAGE;
        let result = if mcp {
            mcp_dispatch(&mut agent, &raw, &mut initialized)
        } else {
            Some(agent.dispatch(&raw))
        };
        if let Some(result) = result {
            serde_json::to_writer(&mut output, &result)?;
            writeln!(output)?;
            output.flush()?;
        }
        if oversized {
            break;
        }
    }
    Ok(())
}
fn mcp_dispatch(agent: &mut TaskAgent, raw: &[u8], initialized: &mut bool) -> Option<Value> {
    let req: Value = match serde_json::from_slice(raw) {
        Ok(v) if raw.len() <= MAX_MESSAGE => v,
        _ => {
            return Some(
                json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Invalid or oversized JSON"}}),
            )
        }
    };
    let error = |code, message| json!({"jsonrpc":"2.0","id":req["id"],"error":{"code":code,"message":message}});
    if !req.is_object() || req["jsonrpc"] != "2.0" || !req["method"].is_string() {
        return Some(error(-32600, "Invalid request"));
    }
    // Notifications never execute tools, even when their payload looks valid.
    req.get("id")?;
    if !(req["id"].is_null() || req["id"].is_string() || req["id"].is_i64() || req["id"].is_u64()) {
        return Some(error(-32600, "Invalid ID"));
    }
    let result = match req["method"].as_str().unwrap() {
        "initialize" => {
            *initialized = true;
            json!({"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"aria-warehouse-agent","version":env!("CARGO_PKG_VERSION")}})
        }
        "ping" => json!({}),
        _ if !*initialized => return Some(error(-32002, "Initialize first")),
        "tools/list" => {
            json!({"tools":[{"name":"aria.warehouse.task","description":"Delegate a bounded warehouse navigation simulation task. Create from a user goal and known/demo map, start once, wait/status until terminal, and verify the actual result. The Rust task agent observes, plans, moves, replans after obstacles and stops on limits. No hardware or model inference in this executor.","inputSchema":serde_json::from_str::<Value>(SCHEMA).unwrap(),"annotations":{"readOnlyHint":false,"destructiveHint":false,"openWorldHint":false}}]})
        }
        "tools/call" => {
            if req["params"]["name"] != "aria.warehouse.task" {
                return Some(error(-32601, "Unknown tool"));
            }
            let result = agent.dispatch(req["params"]["arguments"].to_string().as_bytes());
            json!({"content":[{"type":"text","text":result.to_string()}],"isError":result["ok"]!=true})
        }
        _ => return Some(error(-32601, "Method not found")),
    };
    Some(json!({"jsonrpc":"2.0","id":req["id"],"result":result}))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn scene() -> Scenario {
        Scenario {
            width: 5,
            height: 4,
            start: [0, 0],
            goal: [4, 0],
            obstacles: vec![],
        }
    }
    fn create(agent: &mut TaskAgent, key: &str) -> String {
        agent.handle(Command::Create {
            request_id: key.into(),
            goal: "Reach shelf B in the simulation".into(),
            scenario: scene(),
            constraints: Constraints::default(),
        })["result"]["task_id"]
            .as_str()
            .unwrap()
            .into()
    }
    fn ready_task() -> Task {
        let mut engine = Engine::default();
        engine.handle(Request::Init { scenario: scene() });
        Task {
            goal: "reach B".into(),
            engine,
            constraints: Constraints::default(),
            state: "running",
            reason: String::new(),
            verified: false,
            start_key: None,
            obstacle_keys: BTreeMap::new(),
            decisions: Vec::new(),
        }
    }
    #[test]
    fn task_decides_to_replan_after_environment_feedback() {
        let mut task = ready_task();
        task.tick(Duration::ZERO);
        assert_eq!(task.engine.snapshot()["world"]["position"], json!([1, 0]));
        task.engine.handle(Request::Obstacle { cell: [2, 0] });
        for _ in 0..8 {
            task.tick(Duration::from_millis(200));
        }
        assert_eq!(task.state, "completed");
        assert!(task.verified);
        assert_eq!(task.engine.snapshot()["world"]["position"], json!([4, 0]));
        assert_eq!(
            task.decisions
                .iter()
                .filter(|d| d["action"] == "plan")
                .count(),
            2
        );
    }
    #[test]
    fn unreachable_is_a_question_not_false_success() {
        let mut task = ready_task();
        task.engine.handle(Request::Obstacle { cell: [4, 0] });
        task.tick(Duration::ZERO);
        assert_eq!(task.state, "needs_input");
        assert_eq!(task.reason, "unreachable");
        assert!(!task.verified);
    }
    #[test]
    fn task_constraints_are_not_silently_widened() {
        let mut task = ready_task();
        task.constraints.max_steps = 1;
        task.tick(Duration::ZERO);
        task.tick(Duration::from_millis(100));
        assert_eq!(task.state, "failed");
        assert_eq!(task.reason, "step_budget_exceeded");
        assert!(!task.verified);
        let mut task = ready_task();
        task.tick(Duration::from_secs(31));
        assert_eq!(task.reason, "deadline_exceeded");
    }
    #[test]
    fn create_retry_preserves_mission_and_contract() {
        let mut agent = TaskAgent::default();
        let first = create(&mut agent, "create-1");
        assert_eq!(first, create(&mut agent, "create-1"));
        assert_eq!(agent.tasks.len(), 1);
        let result = agent.handle(Command::Create {
            request_id: "create-1".into(),
            goal: "different".into(),
            scenario: scene(),
            constraints: Constraints::default(),
        });
        assert_eq!(result["error"], "request_id_reused_with_different_contract");
    }
    #[test]
    fn independent_missions_and_latched_stop() {
        let mut agent = TaskAgent::default();
        let a = create(&mut agent, "a");
        let b = create(&mut agent, "b");
        assert_eq!(
            agent.handle(Command::Start {
                task_id: a.clone(),
                request_id: "start".into()
            })["ok"],
            true
        );
        agent.handle(Command::Stop { task_id: a.clone() });
        let stopped = agent.handle(Command::Status { task_id: a.clone() });
        assert_eq!(stopped["result"]["status"], "stopped");
        assert_eq!(
            agent.handle(Command::Status { task_id: b })["result"]["status"],
            "ready"
        );
        assert_eq!(
            agent.handle(Command::Start {
                task_id: a,
                request_id: "other-start".into()
            })["error"],
            "task_already_started"
        );
    }
    #[test]
    fn repeated_start_is_not_a_second_worker() {
        let mut agent = TaskAgent::default();
        let id = create(&mut agent, "a");
        for _ in 0..3 {
            assert_eq!(
                agent.handle(Command::Start {
                    task_id: id.clone(),
                    request_id: "start".into()
                })["ok"],
                true
            );
        }
        assert_eq!(agent.threads.len(), 1);
        let result = agent.handle(Command::Wait {
            task_id: id,
            timeout_ms: 1000,
        });
        assert_eq!(result["result"]["status"], "completed");
        assert_eq!(result["result"]["verified"], true);
    }
    #[test]
    fn strict_request_and_wait_bounds() {
        let mut agent = TaskAgent::default();
        let invalid =
            agent.dispatch(br#"{"action":"status","task_id":"demo","request_id":"extra"}"#);
        assert_eq!(invalid["error"], "invalid_request");
        assert!(invalid["hint"]
            .as_str()
            .unwrap()
            .contains("Omit request_id"));
        let id = create(&mut agent, "a");
        assert_eq!(
            agent.handle(Command::Wait {
                task_id: id,
                timeout_ms: 1001
            })["error"],
            "wait_too_long"
        );
        assert_eq!(
            agent.dispatch(br#"{"action":"health","mode":"hardware"}"#)["error"],
            "invalid_request"
        );
        assert_eq!(
            agent.handle(Command::Status {
                task_id: "other-conversation".into()
            })["error"],
            "unknown_task"
        );
    }
    #[test]
    fn mcp_discovers_agent_and_notifications_never_act() {
        let mut agent = TaskAgent::default();
        let mut initialized = false;
        assert_eq!(
            mcp_dispatch(
                &mut agent,
                br#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
                &mut initialized
            )
            .unwrap()["error"]["code"],
            -32002
        );
        mcp_dispatch(&mut agent,br#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}"#,&mut initialized);
        let tools = mcp_dispatch(
            &mut agent,
            br#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#,
            &mut initialized,
        )
        .unwrap();
        assert_eq!(tools["result"]["tools"][0]["name"], "aria.warehouse.task");
        assert!(mcp_dispatch(&mut agent,br#"{"jsonrpc":"2.0","method":"tools/call","params":{"name":"aria.warehouse.task","arguments":{"action":"health"}}}"#,&mut initialized).is_none());
        assert!(agent.tasks.is_empty());
    }
}

//! Where a coding turn is: inspecting, editing, running, verifying, reviewing.
//!
//! Tool calls are the model's view of the work; this is the person's. "read_file,
//! grep, read_file" is *inspecting*; "edit_file" is *editing*; `pytest` is
//! *verifying*. The phase is read off the tool events the frontend already
//! receives, so it works for every runtime (Python or Go) without a protocol
//! change, and a runtime that knows better can say so with a `turn.phase` event.

use serde_json::Value;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Inspect,
    Plan,
    Edit,
    Run,
    Verify,
    Review,
}

impl Phase {
    /// The word for the progress strip.
    pub fn name(self, zh: bool) -> &'static str {
        match (self, zh) {
            (Phase::Inspect, false) => "Inspect",
            (Phase::Inspect, true) => "检查",
            (Phase::Plan, false) => "Plan",
            (Phase::Plan, true) => "计划",
            (Phase::Edit, false) => "Edit",
            (Phase::Edit, true) => "修改",
            (Phase::Run, false) => "Run",
            (Phase::Run, true) => "执行",
            (Phase::Verify, false) => "Verify",
            (Phase::Verify, true) => "验证",
            (Phase::Review, false) => "Review",
            (Phase::Review, true) => "审查",
        }
    }

    /// What is happening, as a status: "Editing src/a.py".
    pub fn doing(self, zh: bool) -> &'static str {
        match (self, zh) {
            (Phase::Inspect, false) => "Inspecting",
            (Phase::Inspect, true) => "正在检查",
            (Phase::Plan, false) => "Planning",
            (Phase::Plan, true) => "正在制定计划",
            (Phase::Edit, false) => "Editing",
            (Phase::Edit, true) => "正在修改",
            (Phase::Run, false) => "Running",
            (Phase::Run, true) => "正在执行",
            (Phase::Verify, false) => "Verifying",
            (Phase::Verify, true) => "正在验证",
            (Phase::Review, false) => "Reviewing",
            (Phase::Review, true) => "正在审查",
        }
    }

    pub fn from_name(name: &str) -> Option<Phase> {
        match name.to_ascii_lowercase().as_str() {
            "inspect" | "understand" | "scan" => Some(Phase::Inspect),
            "plan" => Some(Phase::Plan),
            "edit" | "patch" | "execute" => Some(Phase::Edit),
            "run" => Some(Phase::Run),
            "verify" | "test" => Some(Phase::Verify),
            "review" => Some(Phase::Review),
            _ => None,
        }
    }
}

/// Commands whose job is to check the work rather than change anything.
const CHECKS: [&str; 22] = [
    "pytest",
    "unittest",
    "jest",
    "vitest",
    "mocha",
    "cargo test",
    "cargo check",
    "cargo clippy",
    "go test",
    "go vet",
    "ruff",
    "eslint",
    "flake8",
    "mypy",
    "pyright",
    "tsc",
    "typecheck",
    "npm test",
    "npm run test",
    "npm run lint",
    "make test",
    "ctest",
];

const READ_ONLY_GIT: [&str; 6] = ["status", "diff", "log", "show", "branch", "blame"];

/// Which phase a tool call belongs to.
pub fn classify(tool: &str, params: &Value) -> Phase {
    let tool = tool.to_ascii_lowercase();
    if [
        "edit",
        "write",
        "patch",
        "create",
        "delete",
        "move",
        "rename",
        "notebook_edit",
    ]
    .iter()
    .any(|k| tool.contains(k))
    {
        return Phase::Edit;
    }
    if ["plan", "todo", "task"].iter().any(|k| tool.contains(k)) {
        return Phase::Plan;
    }
    if tool.contains("review") {
        return Phase::Review;
    }
    if tool == "run_command" || tool.contains("shell") || tool.contains("bash") || tool == "exec" {
        let command = params["command"]
            .as_str()
            .unwrap_or("")
            .to_ascii_lowercase();
        if CHECKS.iter().any(|c| command.contains(c)) || command.contains("verify") {
            return Phase::Verify;
        }
        let mut words = command.split_whitespace();
        if words.next() == Some("git") {
            // Skip git's own options, including the value of -C <dir> / -c <k=v>.
            let mut sub = "";
            while let Some(w) = words.next() {
                if w == "-C" || w == "-c" {
                    words.next();
                } else if !w.starts_with('-') {
                    sub = w;
                    break;
                }
            }
            if READ_ONLY_GIT.contains(&sub) {
                return Phase::Inspect;
            }
        }
        if [
            "ls", "cat", "rg", "grep", "find", "head", "tail", "tree", "wc",
        ]
        .iter()
        .any(|c| command.split_whitespace().next() == Some(*c))
        {
            return Phase::Inspect;
        }
        return Phase::Run;
    }
    // read_file, list_files, glob, search_code, impact_analysis, web_fetch, …
    Phase::Inspect
}

/// The phases a turn has been through, and the one it is in.
#[derive(Clone, Debug, Default)]
pub struct PhaseTrack {
    seen: Vec<Phase>,
    current: Option<Phase>,
    target: String,
}

impl PhaseTrack {
    pub fn clear(&mut self) {
        *self = PhaseTrack::default();
    }

    pub fn is_empty(&self) -> bool {
        self.seen.is_empty()
    }

    /// The turn is now in `phase`, working on `target` (a path or command).
    pub fn enter(&mut self, phase: Phase, target: &str) {
        if !self.seen.contains(&phase) {
            self.seen.push(phase);
        }
        self.current = Some(phase);
        self.target = target.chars().take(80).collect();
    }

    /// The turn ended: nothing is in progress any more.
    pub fn finish(&mut self) {
        self.current = None;
        self.target.clear();
    }

    /// The strip: each phase seen so far, ✓ once left behind and ● while in it.
    /// After an edit with no check yet, `Verify ○` shows what is still expected.
    pub fn strip(&self, zh: bool) -> String {
        let mut parts: Vec<String> = self
            .seen
            .iter()
            .map(|p| {
                let mark = if Some(*p) == self.current {
                    "●"
                } else {
                    "✓"
                };
                format!("{} {mark}", p.name(zh))
            })
            .collect();
        if self.seen.contains(&Phase::Edit) && !self.seen.contains(&Phase::Verify) {
            parts.push(format!("{} ○", Phase::Verify.name(zh)));
        }
        parts.join(" · ")
    }

    /// "Editing src/a.py", or nothing when idle.
    pub fn doing(&self, zh: bool) -> String {
        match self.current {
            Some(p) if self.target.is_empty() => p.doing(zh).to_string(),
            Some(p) => format!("{} {}", p.doing(zh), self.target),
            None => String::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn tools_map_to_what_a_person_would_call_the_work() {
        assert_eq!(classify("read_file", &json!({"path":"a"})), Phase::Inspect);
        assert_eq!(classify("search_code", &json!({})), Phase::Inspect);
        assert_eq!(classify("impact_analysis", &json!({})), Phase::Inspect);
        assert_eq!(classify("edit_file", &json!({})), Phase::Edit);
        assert_eq!(classify("write_file", &json!({})), Phase::Edit);
        assert_eq!(classify("multi_edit", &json!({})), Phase::Edit);
        assert_eq!(classify("update_todos", &json!({})), Phase::Plan);
    }

    #[test]
    fn a_shell_command_is_judged_by_what_it_does() {
        let run = |c: &str| classify("run_command", &json!({"command": c}));
        assert_eq!(run("python3 -m pytest -q"), Phase::Verify);
        assert_eq!(run("cargo clippy --all-targets"), Phase::Verify);
        assert_eq!(run("npm run lint"), Phase::Verify);
        assert_eq!(run("git status"), Phase::Inspect);
        assert_eq!(run("git -C . diff --stat"), Phase::Inspect);
        assert_eq!(run("rg refresh_token src"), Phase::Inspect);
        assert_eq!(run("npm install jose"), Phase::Run);
        assert_eq!(run("git commit -m x"), Phase::Run);
    }

    #[test]
    fn the_strip_shows_progress_and_what_is_still_expected() {
        let mut t = PhaseTrack::default();
        assert!(t.is_empty() && t.strip(false).is_empty());
        t.enter(Phase::Inspect, "src/a.py");
        assert_eq!(t.strip(false), "Inspect ●");
        t.enter(Phase::Edit, "src/a.py");
        assert_eq!(t.strip(false), "Inspect ✓ · Edit ● · Verify ○");
        assert_eq!(t.doing(false), "Editing src/a.py");
        t.enter(Phase::Verify, "pytest -q");
        assert_eq!(t.strip(false), "Inspect ✓ · Edit ✓ · Verify ●");
        t.finish();
        assert_eq!(t.strip(false), "Inspect ✓ · Edit ✓ · Verify ✓");
        assert!(t.doing(false).is_empty());
        t.clear();
        assert!(t.is_empty());
    }

    #[test]
    fn going_back_to_a_phase_does_not_repeat_it() {
        let mut t = PhaseTrack::default();
        for p in [Phase::Inspect, Phase::Edit, Phase::Inspect, Phase::Edit] {
            t.enter(p, "");
        }
        assert_eq!(t.strip(true), "检查 ✓ · 修改 ● · 验证 ○");
    }

    #[test]
    fn a_runtime_can_name_the_phase_itself() {
        assert_eq!(Phase::from_name("Verify"), Some(Phase::Verify));
        assert_eq!(Phase::from_name("review"), Some(Phase::Review));
        assert_eq!(Phase::from_name("nonsense"), None);
    }
}

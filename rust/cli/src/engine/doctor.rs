//! Native Rust `/doctor` installation, provider, workspace, and MCP ecosystem diagnostics.
//!
//! Runs directly inside `aria-native` with zero Python interpreter startup.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use serde_json::{json, Value};

use crate::engine::mcp::discover_ecosystem_mcp_servers;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CheckStatus {
    Ok,
    Warn,
    Err,
}

impl CheckStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Warn => "warn",
            Self::Err => "err",
        }
    }

    fn icon(self) -> &'static str {
        match self {
            Self::Ok => "✓",
            Self::Warn => "!",
            Self::Err => "✗",
        }
    }
}

#[derive(Debug, Clone)]
pub struct DoctorCheck {
    pub name: String,
    pub status: CheckStatus,
    pub detail: String,
    pub suggestion: String,
}

#[derive(Debug, Clone)]
pub struct DoctorReport {
    pub checks: Vec<DoctorCheck>,
}

impl DoctorReport {
    pub fn passed(&self) -> usize {
        self.checks
            .iter()
            .filter(|c| c.status == CheckStatus::Ok)
            .count()
    }

    pub fn warnings(&self) -> usize {
        self.checks
            .iter()
            .filter(|c| c.status == CheckStatus::Warn)
            .count()
    }

    pub fn errors(&self) -> usize {
        self.checks
            .iter()
            .filter(|c| c.status == CheckStatus::Err)
            .count()
    }

    pub fn overall_status(&self) -> &'static str {
        if self.errors() > 0 {
            "err"
        } else if self.warnings() > 0 {
            "warn"
        } else {
            "ok"
        }
    }

    pub fn to_json(&self) -> Value {
        let items: Vec<Value> = self
            .checks
            .iter()
            .map(|c| {
                json!({
                    "name": c.name,
                    "status": c.status.as_str(),
                    "detail": c.detail,
                    "suggestion": c.suggestion,
                })
            })
            .collect();
        json!({
            "status": self.overall_status(),
            "passed": self.passed(),
            "warnings": self.warnings(),
            "errors": self.errors(),
            "checks": items,
        })
    }
}

fn which_version(bin: &str, flag: &str) -> Option<String> {
    let out = Command::new(bin).arg(flag).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let first = text.lines().next().unwrap_or("").trim();
    if first.is_empty() {
        let err = String::from_utf8_lossy(&out.stderr);
        Some(err.lines().next().unwrap_or("installed").trim().to_owned())
    } else {
        Some(first.to_owned())
    }
}

fn dir_writable(dir: &Path) -> bool {
    if !dir.is_dir() {
        return false;
    }
    let probe = dir.join(format!(".aria-doctor-probe-{}", std::process::id()));
    match fs::write(&probe, b"ok") {
        Ok(()) => {
            let _ = fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

fn aria_home_dir() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("ARIA_HOME") {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed));
        }
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    Some(PathBuf::from(home).join(".arthera"))
}

fn check_providers() -> DoctorCheck {
    let keys = [
        ("Anthropic", "ANTHROPIC_API_KEY"),
        ("OpenAI", "OPENAI_API_KEY"),
        ("Gemini", "GEMINI_API_KEY"),
        ("Google", "GOOGLE_API_KEY"),
        ("DeepSeek", "DEEPSEEK_API_KEY"),
        ("OpenRouter", "OPENROUTER_API_KEY"),
    ];
    let mut configured: Vec<&str> = keys
        .iter()
        .filter_map(|(label, env_key)| {
            std::env::var(env_key)
                .ok()
                .filter(|v| !v.trim().is_empty())
                .map(|_| *label)
        })
        .collect();
    if std::env::var("OLLAMA_HOST")
        .ok()
        .is_some_and(|v| !v.trim().is_empty())
    {
        configured.push("Ollama");
    }
    if configured.is_empty() {
        DoctorCheck {
            name: "providers".into(),
            status: CheckStatus::Warn,
            detail: "no LLM provider API key detected in environment".into(),
            suggestion:
                "Set GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY or configure Ollama".into(),
        }
    } else {
        DoctorCheck {
            name: "providers".into(),
            status: CheckStatus::Ok,
            detail: format!("configured via env: {}", configured.join(", ")),
            suggestion: String::new(),
        }
    }
}

fn check_mcp_config(home: Option<&Path>) -> DoctorCheck {
    let Some(home_dir) = home else {
        return DoctorCheck {
            name: "mcp_config".into(),
            status: CheckStatus::Warn,
            detail: "HOME directory not resolved".into(),
            suggestion: "Set HOME or ARIA_HOME".into(),
        };
    };
    let path = home_dir.join("mcp_servers.json");
    if !path.is_file() {
        return DoctorCheck {
            name: "mcp_config".into(),
            status: CheckStatus::Ok,
            detail: "no custom mcp_servers.json (using auto-discovery)".into(),
            suggestion: String::new(),
        };
    }
    match fs::read_to_string(&path).and_then(|s| {
        serde_json::from_str::<Value>(&s)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
    }) {
        Ok(val) => {
            let count = val
                .get("servers")
                .and_then(|v| v.as_object())
                .map(|m| m.len())
                .or_else(|| val.as_object().map(|m| m.len()))
                .unwrap_or(0);
            DoctorCheck {
                name: "mcp_config".into(),
                status: CheckStatus::Ok,
                detail: format!("{} valid ({count} entries)", path.display()),
                suggestion: String::new(),
            }
        }
        Err(err) => DoctorCheck {
            name: "mcp_config".into(),
            status: CheckStatus::Err,
            detail: format!("invalid JSON in {}: {err}", path.display()),
            suggestion: "Fix JSON syntax in ~/.arthera/mcp_servers.json".into(),
        },
    }
}

fn probe_local_ollama() -> DoctorCheck {
    let host = std::env::var("OLLAMA_HOST")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| "http://127.0.0.1:11434".to_owned());
    let url = format!("{}/api/tags", host.trim_end_matches('/'));
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(2)))
        .build()
        .new_agent();
    match agent.get(&url).call() {
        Ok(resp) if resp.status() == 200 => DoctorCheck {
            name: "ollama_network".into(),
            status: CheckStatus::Ok,
            detail: format!("reachable at {host}"),
            suggestion: String::new(),
        },
        Ok(resp) => DoctorCheck {
            name: "ollama_network".into(),
            status: CheckStatus::Warn,
            detail: format!("{host} returned HTTP {}", resp.status()),
            suggestion: "Ensure `ollama serve` is running".into(),
        },
        Err(_) => DoctorCheck {
            name: "ollama_network".into(),
            status: CheckStatus::Warn,
            detail: format!("not reachable at {host}"),
            suggestion: "Start Ollama (`ollama serve`) if using local models".into(),
        },
    }
}

pub fn run_doctor(workspace: &Path, check_network: bool) -> DoctorReport {
    let mut checks = Vec::new();

    // 1. Native Rust CLI version
    checks.push(DoctorCheck {
        name: "rust_cli".into(),
        status: CheckStatus::Ok,
        detail: format!("aria-native v{}", env!("CARGO_PKG_VERSION")),
        suggestion: String::new(),
    });

    // 2. Workspace writable
    checks.push(if dir_writable(workspace) {
        DoctorCheck {
            name: "workspace".into(),
            status: CheckStatus::Ok,
            detail: format!("{} (writable)", workspace.display()),
            suggestion: String::new(),
        }
    } else {
        DoctorCheck {
            name: "workspace".into(),
            status: CheckStatus::Err,
            detail: format!("{} is not writable", workspace.display()),
            suggestion: "Check directory permissions on the active workspace".into(),
        }
    });

    // 3. Toolchains: git, node (for TS Computer Use MCP)
    checks.push(match which_version("git", "--version") {
        Some(ver) => DoctorCheck {
            name: "git".into(),
            status: CheckStatus::Ok,
            detail: ver,
            suggestion: String::new(),
        },
        None => DoctorCheck {
            name: "git".into(),
            status: CheckStatus::Warn,
            detail: "git not found on PATH".into(),
            suggestion: "Install git for diff, worktree, and checkpoint features".into(),
        },
    });

    checks.push(match which_version("node", "--version") {
        Some(ver) => DoctorCheck {
            name: "node_runtime".into(),
            status: CheckStatus::Ok,
            detail: format!("Node.js {ver} (for aria-computer-use MCP)"),
            suggestion: String::new(),
        },
        None => DoctorCheck {
            name: "node_runtime".into(),
            status: CheckStatus::Warn,
            detail: "node not found on PATH".into(),
            suggestion: "Install Node.js >= 20 to run services/aria-computer-use".into(),
        },
    });

    // 4. Providers & MCP config
    checks.push(check_providers());
    let home = if cfg!(test) { None } else { aria_home_dir() };
    checks.push(check_mcp_config(home.as_deref()));

    // 5. Auto-discovered ecosystem MCP services
    let discovered = discover_ecosystem_mcp_servers(workspace);
    let names: Vec<String> = discovered.iter().map(|s| s.name.clone()).collect();
    checks.push(DoctorCheck {
        name: "ecosystem_mcp".into(),
        status: CheckStatus::Ok,
        detail: if names.is_empty() {
            "0 local ecosystem MCP services discovered".into()
        } else {
            format!("{} discovered: {}", names.len(), names.join(", "))
        },
        suggestion: String::new(),
    });

    // 5b. Tier-2 Document Studio visual QA binaries (soffice + pdftoppm)
    let soffice = which_version("soffice", "--version");
    let pdftoppm = which_version("pdftoppm", "-v");
    checks.push(match (soffice, pdftoppm) {
        (Some(so), Some(_)) => DoctorCheck {
            name: "document_studio_qa".into(),
            status: CheckStatus::Ok,
            detail: format!("LibreOffice ({so}) and pdftoppm ready for Tier-2 visual QA"),
            suggestion: String::new(),
        },
        (so, pdf) => {
            let mut missing = Vec::new();
            if so.is_none() {
                missing.push("soffice");
            }
            if pdf.is_none() {
                missing.push("pdftoppm");
            }
            DoctorCheck {
                name: "document_studio_qa".into(),
                status: CheckStatus::Warn,
                detail: format!("missing {} (Tier-1 Go OOXML still works)", missing.join(", ")),
                suggestion: "Install LibreOffice + poppler-utils or run services/arthera-doc-studio via Docker".into(),
            }
        }
    });

    // 6. Optional live network probe
    if check_network {
        checks.push(probe_local_ollama());
    }

    DoctorReport { checks }
}

pub fn format_doctor_report(report: &DoctorReport, zh: bool) -> String {
    let mut lines = Vec::new();
    lines.push(if zh {
        format!(
            "### Aria Code 原生诊断 (`/doctor`) — 状态: **{}**（通过 {} · 警告 {} · 错误 {}）",
            report.overall_status(),
            report.passed(),
            report.warnings(),
            report.errors()
        )
    } else {
        format!(
            "### Aria Code Native Doctor (`/doctor`) — status: **{}** ({} ok · {} warn · {} err)",
            report.overall_status(),
            report.passed(),
            report.warnings(),
            report.errors()
        )
    });
    for c in &report.checks {
        lines.push(format!(
            "- `{}` **{}**: {}",
            c.status.icon(),
            c.name,
            c.detail
        ));
        if !c.suggestion.is_empty() {
            lines.push(format!("  - ↳ {}", c.suggestion));
        }
    }
    lines.join("\n")
}

pub fn handle_doctor_command(workspace: &Path, arg: &str, zh: bool) -> (String, &'static str) {
    let tokens: Vec<&str> = arg.split_whitespace().collect();
    let want_json = tokens.contains(&"--json");
    let check_network = tokens.contains(&"--network");

    let report = run_doctor(workspace, check_network);
    let text = if want_json {
        serde_json::to_string_pretty(&report.to_json()).unwrap_or_else(|_| "{}".to_owned())
    } else {
        format_doctor_report(&report, zh)
    };
    let status = if report.errors() > 0 {
        "failed"
    } else {
        "completed"
    };
    (text, status)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_doctor_reports_workspace_toolchain_and_json() {
        let dir = tempfile::tempdir().expect("tempdir");
        let report = run_doctor(dir.path(), false);
        assert!(report.passed() >= 2);
        assert_eq!(report.errors(), 0);
        assert!(report.checks.iter().any(|c| c.name == "rust_cli"));
        assert!(report.checks.iter().any(|c| c.name == "workspace"));
        assert!(report.checks.iter().any(|c| c.name == "ecosystem_mcp"));

        let (json_out, status) = handle_doctor_command(dir.path(), "--json", false);
        assert_eq!(status, "completed");
        let parsed: Value = serde_json::from_str(&json_out).expect("valid json");
        assert!(parsed["checks"].as_array().is_some());
    }
}

//! Native Rust Ecosystem MCP orchestration (`/ecosystem` + system prompt routing).
//!
//! Coordinates decoupled vertical services (`arthera-finance`, `arthera-trading`,
//! `erp-service`, `aria-healthcare`, `aria-creative-media`) without embedding
//! vertical domain logic inside `aria-code` core.

use crate::engine::mcp::{
    discover_ecosystem_mcp_servers, load_mcp_server_configs, McpServerConfig, McpStdioClient,
};
use std::collections::BTreeMap;
use std::path::Path;

fn describe_service(name: &str) -> (&'static str, &'static str) {
    match name {
        "arthera-finance" => (
            "Finance & Quant",
            "Read-only market data, statements, OHLCV quality audit, portfolio backtest, pre-trade risk checks",
        ),
        "arthera-trading" => (
            "Trading / Brokers",
            "Paper & live broker order preview/execution (host-gated by ARTHERA_TRADING_ENABLED)",
        ),
        "erp-service" => (
            "Logistics & ERP",
            "3PL warehouse, inventory policy, carrier scoring, freight audit, owner-scoped shipper digest",
        ),
        "aria-healthcare" => (
            "Biomedical & Healthcare",
            "PubMed literature search, verifiable evidence bundles, screening tasks, symbolic navigation simulation",
        ),
        "aria-creative-media" => (
            "Creative & Media",
            "Figma inspection, Canva autofill/assets, OpenAI image generation/edit, Kling & Runway video jobs",
        ),
        "arthera-quant" => ("Quant Engine", "Arthera QuantEngine analytics bridge"),
        "arthera-commerce" => ("Commerce", "Catalogue order drafts and checkout workflow"),
        _ => ("External MCP", "Configured external MCP server"),
    }
}

fn collect_ecosystem_servers(workspace: &Path) -> Vec<McpServerConfig> {
    let mut merged: BTreeMap<String, McpServerConfig> = BTreeMap::new();
    for cfg in discover_ecosystem_mcp_servers(workspace) {
        merged.insert(cfg.name.clone(), cfg);
    }
    for cfg in load_mcp_server_configs(workspace) {
        merged.insert(cfg.name.clone(), cfg);
    }
    merged.into_values().collect()
}

/// Build a concise system-prompt routing section teaching the native Rust agent
/// how to orchestrate connected vertical MCP services.
pub fn format_ecosystem_prompt_section(workspace: &Path) -> String {
    let servers = collect_ecosystem_servers(workspace);
    if servers.is_empty() {
        return String::new();
    }
    let mut lines = vec![
        "## Connected Vertical Ecosystem MCP Services".to_owned(),
        "Delegate domain-specific tasks to the dedicated external MCP services below instead of writing ad-hoc scripts:".to_owned(),
    ];
    for srv in &servers {
        let (domain, desc) = describe_service(&srv.name);
        lines.push(format!("- **`{}`** ({domain}): {desc}", srv.name));
    }
    lines.join("\n")
}

pub fn handle_ecosystem_command(workspace: &Path, arg: &str, zh: bool) -> (String, &'static str) {
    let probe = arg
        .split_whitespace()
        .any(|t| matches!(t.to_ascii_lowercase().as_str(), "probe" | "check" | "test"));
    let servers = collect_ecosystem_servers(workspace);

    if servers.is_empty() {
        let msg = if zh {
            "未发现外部垂直 MCP 服务。可将 `financial-service`、`erp-service`、`healthcare-service`、`aria-plugins` 放在同级目录，或运行 `/mcp connect --uvx` 写入配置。"
        } else {
            "No external vertical MCP services discovered. Clone `financial-service`, `erp-service`, `healthcare-service`, or `aria-plugins` as sibling directories, or run `/mcp connect --uvx`."
        };
        return (msg.to_owned(), "ok");
    }

    let mut lines = vec![if zh {
        "### Aria 垂直生态 MCP 服务".to_owned()
    } else {
        "### Aria Vertical Ecosystem MCP Services".to_owned()
    }];
    lines.push(String::new());
    if probe {
        lines.push("| Service | Domain | Status | Tools (RO) |".to_owned());
        lines.push("| :--- | :--- | :--- | :--- |".to_owned());
        for srv in &servers {
            let (domain, _) = describe_service(&srv.name);
            match McpStdioClient::spawn_with_env(&srv.command, &srv.args, &srv.env) {
                Ok(mut client) => match client.list_tools() {
                    Ok(tools) => {
                        let ro = tools
                            .iter()
                            .filter(|t| {
                                t.pointer("/annotations/readOnlyHint")
                                    .and_then(|v| v.as_bool())
                                    .unwrap_or(false)
                            })
                            .count();
                        lines.push(format!(
                            "| `{}` | {domain} | ● connected | {} ({ro}) |",
                            srv.name,
                            tools.len()
                        ));
                    }
                    Err(e) => {
                        lines.push(format!(
                            "| `{}` | {domain} | ✗ list failed ({e}) | - |",
                            srv.name
                        ));
                    }
                },
                Err(e) => {
                    lines.push(format!(
                        "| `{}` | {domain} | ○ offline ({e}) | - |",
                        srv.name
                    ));
                }
            }
        }
    } else {
        lines.push("| Service | Domain | Capabilities |".to_owned());
        lines.push("| :--- | :--- | :--- |".to_owned());
        for srv in &servers {
            let (domain, desc) = describe_service(&srv.name);
            lines.push(format!("| `{}` | {domain} | {desc} |", srv.name));
        }
        lines.push(String::new());
        lines.push(if zh {
            "运行 `/ecosystem probe` 实时探测各服务的 MCP 联通性与工具数量。".to_owned()
        } else {
            "Run `/ecosystem probe` to live-check MCP connectivity and tool counts across all services.".to_owned()
        });
    }

    (lines.join("\n"), "ok")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn ecosystem_prompt_and_status_render_discovered_services() {
        let dir = tempfile::tempdir().unwrap();
        let ws = dir.path().join("aria-code");
        fs::create_dir_all(&ws).unwrap();

        for rel in [
            "financial-service/src/arthera_finance/server.py",
            "erp-service/src/erp_service/mcp_server.py",
            "healthcare-service/src/aria_healthcare/server.py",
            "aria-plugins/plugins/creative-media/python/aria_creative/mcp_server.py",
        ] {
            let p = dir.path().join(rel);
            fs::create_dir_all(p.parent().unwrap()).unwrap();
            fs::write(&p, "# stub\n").unwrap();
        }

        let prompt = format_ecosystem_prompt_section(&ws);
        assert!(prompt.contains("arthera-finance"));
        assert!(prompt.contains("erp-service"));
        assert!(prompt.contains("aria-healthcare"));
        assert!(prompt.contains("aria-creative-media"));

        let (status, st) = handle_ecosystem_command(&ws, "", false);
        assert_eq!(st, "ok");
        assert!(status.contains("Finance & Quant"));
        assert!(status.contains("Logistics & ERP"));
        assert!(status.contains("Biomedical & Healthcare"));
        assert!(status.contains("Creative & Media"));
    }
}

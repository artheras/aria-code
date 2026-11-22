//! Enterprise AI ROI & Token Efficiency Scorecard.
//!
//! Tracks token efficiency (avoiding monolithic "token-maxxing"), deterministic MCP tool offload
//! savings, security/compliance readiness, and platform consolidation metrics across Coding,
//! Finance (`arthera-finance`), 3PL ERP (`erp-service`), and Healthcare (`aria-healthcare`).

use std::path::Path;

use serde_json::{json, Value};

/// Baseline per-turn prompt bloat (in tokens) of a monolithic vertical agent that injects all
/// 50+ financial, ERP, healthcare, and broker tool schemas + domain prompts on every turn.
pub const MONOLITH_BASELINE_OVERHEAD_PER_TURN: usize = 12_500;

/// Auto-compaction context ceiling in tokens.
pub const AUTO_COMPACT_TOKEN_CEILING: usize = 24_000;

#[derive(Debug, Clone, PartialEq)]
pub struct RoiMetrics {
    pub turns: usize,
    pub active_tokens: usize,
    pub tokens_per_turn: usize,
    pub avoided_overhead_tokens: usize,
    pub efficiency_gain_pct: f64,
    pub est_cost_usd: f64,
    pub est_saved_usd: f64,
    pub configured_mcp_count: usize,
    pub discovered_mcp_count: usize,
    pub skill_count: usize,
    pub hook_count: usize,
    pub sandbox_mode: String,
    pub permission_mode: String,
    pub write_enabled: bool,
}

pub fn compute_roi_metrics(
    workspace: &Path,
    provider: &str,
    allow_write: bool,
    history: &[(String, String)],
) -> RoiMetrics {
    let msgs: Vec<Value> = history
        .iter()
        .map(|(role, content)| json!({ "role": role, "content": content }))
        .collect();
    let active_tokens = crate::engine::compact::estimate_messages_tokens(&msgs);
    let user_turns = history.iter().filter(|(r, _)| r == "user").count().max(1);
    let tokens_per_turn = active_tokens / user_turns;
    let avoided_overhead_tokens = user_turns * MONOLITH_BASELINE_OVERHEAD_PER_TURN;
    let total_hypothetical = active_tokens + avoided_overhead_tokens;
    let efficiency_gain_pct = if total_hypothetical > 0 {
        (avoided_overhead_tokens as f64 / total_hypothetical as f64) * 100.0
    } else {
        0.0
    };

    let is_local = provider.is_empty() || provider.eq_ignore_ascii_case("ollama");
    // Blended cloud rate (~$0.25 / 1M tokens) when using cloud providers; $0.00 on local Ollama.
    let est_cost_usd = if is_local {
        0.0
    } else {
        (active_tokens as f64) * 0.25 / 1_000_000.0
    };
    let est_saved_usd = (avoided_overhead_tokens as f64) * 0.25 / 1_000_000.0;

    let configured_mcp_count = crate::engine::mcp::load_mcp_server_configs(workspace).len();
    let discovered_mcp_count = crate::engine::mcp::discover_ecosystem_mcp_servers(workspace).len();
    let skill_count = crate::engine::skills::discover_skills(workspace).len();
    let hook_count = crate::engine::hooks::HookRegistry::load(workspace)
        .rules()
        .len();
    let sandbox_mode = match crate::engine::sandbox::SandboxMode::from_env() {
        crate::engine::sandbox::SandboxMode::Off => "off",
        crate::engine::sandbox::SandboxMode::ReadOnly => "read-only",
        crate::engine::sandbox::SandboxMode::WorkspaceWrite => "workspace-write",
    }
    .to_owned();
    let permission_mode = crate::engine::permissions::PermissionPolicy::load(workspace)
        .mode
        .as_str()
        .to_owned();

    RoiMetrics {
        turns: user_turns,
        active_tokens,
        tokens_per_turn,
        avoided_overhead_tokens,
        efficiency_gain_pct,
        est_cost_usd,
        est_saved_usd,
        configured_mcp_count,
        discovered_mcp_count,
        skill_count,
        hook_count,
        sandbox_mode,
        permission_mode,
        write_enabled: allow_write,
    }
}

pub fn build_roi_scorecard(
    workspace: &Path,
    provider: &str,
    model: &str,
    allow_write: bool,
    history: &[(String, String)],
    zh: bool,
) -> String {
    let m = compute_roi_metrics(workspace, provider, allow_write, history);
    let prov = if provider.is_empty() {
        "ollama"
    } else {
        provider
    };
    let mdl = if model.is_empty() { "default" } else { model };
    let budget_pct =
        ((m.active_tokens as f64 / AUTO_COMPACT_TOKEN_CEILING as f64) * 100.0).min(100.0);

    if zh {
        format!(
            "**Aria Code · AI ROI 与 Token 效率计分卡**\n\
             - **1. Token 效率（拒绝 Token-maxxing）**：当前上下文 `~{}` tokens（`{}` 轮，平均 `~{}/轮`，占自动压缩阈值 `{:.1}%`）\n\
             - **2. 按需技能与确定性 MCP 卸载收益**：已规避单体全量工具注入开销 `~{}` tokens（Token 效率提升 **{:.1}%**）\n\
             - **3. 直接财务成本（Direct Financial Outcome）**：当前会话预估 `${:.4}` USD（`{}/{}`）· 架构卸载累计节省 `${:.4}` USD\n\
             - **4. 安全、合规与治理就绪（#1 规模化前提）**：权限策略 `{}` · 写入开关 `{}` · OS 沙箱 `{}` · 生命周期 Hook `{}` 条 · MCP 进程 `-I` 隔离与只读契约\n\
             - **5. 平台整合与业务流嵌入**：已启用 `{}` 个 MCP 服务（探测到 `{}` 个同级垂直服务：`arthera-finance` / `erp-service` / `aria-healthcare`）· 按需编排技能 `{}` 个",
            m.active_tokens,
            m.turns,
            m.tokens_per_turn,
            budget_pct,
            m.avoided_overhead_tokens,
            m.efficiency_gain_pct,
            m.est_cost_usd,
            prov,
            mdl,
            m.est_saved_usd,
            m.permission_mode,
            if m.write_enabled { "enabled" } else { "read-only" },
            m.sandbox_mode,
            m.hook_count,
            m.configured_mcp_count,
            m.discovered_mcp_count,
            m.skill_count,
        )
    } else {
        format!(
            "**Aria Code · Enterprise AI ROI & Token Efficiency Scorecard**\n\
             - **1. Token Efficiency (vs. Token-Maxxing)**: active context `~{}` tokens across `{}` turn(s) (`~{}` tokens/turn, `{:.1}%` of 24k auto-compact budget)\n\
             - **2. On-Demand Skill & MCP Offload**: avoided `~{}` monolithic schema tokens (**{:.1}%** token efficiency gain)\n\
             - **3. Direct Financial Outcome**: estimated session cost `${:.4}` USD (`{}/{}`) · architecture offload savings `${:.4}` USD\n\
             - **4. Security, Compliance & Governance Readiness (#1 Scaling Enabler)**: policy `{}` · write gate `{}` · OS sandbox `{}` · lifecycle hooks `{}` · isolated `-I` read-only MCP contracts\n\
             - **5. Platform Consolidation & Workflow Integration**: `{}` active MCP server(s) (`{}` sibling ecosystem service(s) detected) · `{}` on-demand skill(s) ready",
            m.active_tokens,
            m.turns,
            m.tokens_per_turn,
            budget_pct,
            m.avoided_overhead_tokens,
            m.efficiency_gain_pct,
            m.est_cost_usd,
            prov,
            mdl,
            m.est_saved_usd,
            m.permission_mode,
            if m.write_enabled { "enabled" } else { "read-only" },
            m.sandbox_mode,
            m.hook_count,
            m.configured_mcp_count,
            m.discovered_mcp_count,
            m.skill_count,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn computes_token_efficiency_and_formats_scorecard() {
        let dir = tempfile::tempdir().expect("tempdir");
        let history = vec![
            (
                "user".to_owned(),
                "Analyze NVDA and check warehouse inventory".to_owned(),
            ),
            (
                "assistant".to_owned(),
                "Loaded financial-orchestration and erp-orchestration skills.".to_owned(),
            ),
        ];
        let metrics = compute_roi_metrics(dir.path(), "ollama", false, &history);
        assert_eq!(metrics.turns, 1);
        assert!(metrics.active_tokens > 0);
        assert_eq!(
            metrics.avoided_overhead_tokens,
            MONOLITH_BASELINE_OVERHEAD_PER_TURN
        );
        assert!(metrics.efficiency_gain_pct > 90.0);
        assert_eq!(metrics.est_cost_usd, 0.0);

        let en = build_roi_scorecard(
            dir.path(),
            "ollama",
            "qwen2.5-coder:7b",
            false,
            &history,
            false,
        );
        assert!(en.contains("Token Efficiency"));
        assert!(en.contains("Security, Compliance & Governance Readiness"));

        let zh = build_roi_scorecard(
            dir.path(),
            "ollama",
            "qwen2.5-coder:7b",
            false,
            &history,
            true,
        );
        assert!(zh.contains("AI ROI 与 Token 效率计分卡"));
    }
}

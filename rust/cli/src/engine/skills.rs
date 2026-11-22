//! Native project instruction (`ARIA.md` / `AGENTS.md`) and skill (`SKILL.md`) discovery.

use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkillInfo {
    pub name: String,
    pub description: String,
    pub path: PathBuf,
    pub scope: &'static str,
}

fn parse_skill_frontmatter(raw: &str, fallback_name: &str) -> (String, String) {
    let mut name = fallback_name.to_owned();
    let mut desc = String::new();
    let trimmed = raw.trim_start();
    if let Some(rest) = trimmed.strip_prefix("---") {
        if let Some(end) = rest.find("\n---") {
            for line in rest[..end].lines() {
                let line = line.trim();
                if let Some(v) = line.strip_prefix("name:") {
                    let v = v.trim().trim_matches('"').trim_matches('\'');
                    if !v.is_empty() {
                        name = v.to_owned();
                    }
                } else if let Some(v) = line.strip_prefix("description:") {
                    let v = v.trim().trim_matches('"').trim_matches('\'');
                    if !v.is_empty() {
                        desc = v.to_owned();
                    }
                }
            }
        }
    }
    if desc.is_empty() {
        for line in trimmed.lines() {
            let l = line.trim();
            if !l.is_empty() && l != "---" && !l.starts_with('#') {
                desc = l.chars().take(120).collect();
                break;
            }
        }
    }
    (name, desc)
}

fn collect_skills_from_dir(dir: &Path, scope: &'static str, out: &mut BTreeMap<String, SkillInfo>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let skill_md = path.join("SKILL.md");
        if !skill_md.is_file() {
            continue;
        }
        let Ok(raw) = fs::read_to_string(&skill_md) else {
            continue;
        };
        let fallback = path.file_name().and_then(|n| n.to_str()).unwrap_or("skill");
        let (name, description) = parse_skill_frontmatter(&raw, fallback);
        out.insert(
            name.clone(),
            SkillInfo {
                name,
                description,
                path: skill_md,
                scope,
            },
        );
    }
}

/// Discover skills from global (`~/.claude/skills`, `~/.agents/skills`, `~/.aria/skills`)
/// and project (`.claude/skills`, `.agents/skills`, `.aria/skills`) directories.
/// Project skills override global skills, and `.aria/skills` takes highest precedence.
pub fn discover_skills(workspace: &Path) -> Vec<SkillInfo> {
    let mut map = BTreeMap::new();
    if !cfg!(test) {
        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            let h = Path::new(&home);
            for folder in [".claude", ".agents", ".aria"] {
                collect_skills_from_dir(&h.join(folder).join("skills"), "global", &mut map);
            }
        }
    }
    for folder in [".claude", ".agents", ".aria"] {
        collect_skills_from_dir(&workspace.join(folder).join("skills"), "project", &mut map);
    }
    map.into_values().collect()
}

/// Built-in ecosystem MCP orchestration skills (`financial-orchestration`, `erp-orchestration`,
/// `healthcare-orchestration`) compiled into the binary for zero-config multi-tool coordination.
pub fn builtin_ecosystem_skills() -> Vec<(SkillInfo, &'static str)> {
    vec![
        (
            SkillInfo {
                name: "financial-orchestration".into(),
                description: "Coordinate arthera-finance MCP tools for symbol resolution, equity/macro research, correlation matrices, pairs stat-arb, and portfolio optimization".into(),
                path: PathBuf::from("builtin://financial-orchestration/SKILL.md"),
                scope: "builtin",
            },
            "---\nname: financial-orchestration\ndescription: Coordinate arthera-finance MCP tools for multi-step financial and quantitative analysis\n---\n\
             # Financial Service Orchestration Playbook\n\n\
             1. **Symbol Resolution First**: When the user provides a Chinese/English company name, brand, or bare 6-digit A-share code (e.g. `贵州茅台`, `宁德时代`, `路易威登`, `600036`), call `mcp__arthera_finance__financial_resolve_symbol` first to obtain the canonical ticker and market.\n\
             2. **Single-Asset Deep Research**: Chain `financial_quote` + `financial_fundamentals` + `financial_market_history` (or `financial_ashare_history` for CN) -> pass the close series to `financial_technical_factors` and statement fields to `financial_quality_scores` (Piotroski F / Altman Z'').\n\
             3. **Multi-Asset / Pairs / Stat-Arb**: Fetch aligned daily closes for each asset, then call:\n\
                - `financial_peer_comparison` for valuation multiples vs peer median\n\
                - `financial_correlation_matrix` for pairwise log-return correlations and diversification score\n\
                - `financial_stat_arb` for OLS log-price hedge ratio, rolling spread Z-score, and Ornstein-Uhlenbeck half-life\n\
                - `financial_optimize_weights` (`max_sharpe`, `min_var`, `equal_weight`) for portfolio allocation\n\
             4. **Macro & Derivatives Context**: Combine `financial_us_macro` / `financial_cn_macro` / `financial_central_bank_rates` / `financial_treasury_yields` or `financial_options_chain` / `financial_funding_rates` when macro or derivatives drivers are relevant.\n\
             5. **If MCP Tools Are Missing**: Guide the user to run `/mcp connect` (local sibling repo) or `/mcp connect --uvx`.\n",
        ),
        (
            SkillInfo {
                name: "erp-orchestration".into(),
                description: "Coordinate erp-service MCP tools across warehouse, inventory, procurement, logistics, and finance reconciliation".into(),
                path: PathBuf::from("builtin://erp-orchestration/SKILL.md"),
                scope: "builtin",
            },
            "---\nname: erp-orchestration\ndescription: Coordinate erp-service MCP tools in causal supply-chain order\n---\n\
             # ERP Service Orchestration Playbook\n\n\
             Execute supply-chain and enterprise operations in causal order:\n\
             1. **Inventory & Warehouse Diagnostics**: Inspect stock levels, safety stock gaps, and warehouse utilization via `mcp__erp_service__*` read-only tools.\n\
             2. **Procurement & Logistics Planning**: Feed stock-out risks and reorder quantities into procurement and carrier/route evaluation tools.\n\
             3. **Financial Reconciliation & Executive Decision**: Reconcile cost/margin impact and synthesize an actionable decision summary with explicit bottlenecks and next actions.\n",
        ),
        (
            SkillInfo {
                name: "healthcare-orchestration".into(),
                description: "Coordinate aria-healthcare MCP tools for PubMed literature search, evidence review, and clinical workflow navigation".into(),
                path: PathBuf::from("builtin://healthcare-orchestration/SKILL.md"),
                scope: "builtin",
            },
            "---\nname: healthcare-orchestration\ndescription: Coordinate aria-healthcare MCP tools for evidence-based biomedical research\n---\n\
             # Healthcare Service Orchestration Playbook\n\n\
             1. **Literature Retrieval**: Query PubMed / biomedical literature via `mcp__aria_healthcare__*` search tools with structured MeSH / keyword filters.\n\
             2. **Evidence Grading**: Pass retrieved studies into the evidence review / synthesis tool to separate RCTs, meta-analyses, and observational data.\n\
             3. **Clinical & Research Workbench**: Structure findings with clear citations, limitations, and non-diagnostic safety boundaries.\n",
        ),
    ]
}

/// Load a discovered or built-in skill's full `SKILL.md` content by exact name (case-insensitive fallback).
pub fn load_skill_content(workspace: &Path, name: &str) -> Result<(SkillInfo, String), String> {
    let target = name.trim();
    if target.is_empty() {
        return Err("Skill name is required".into());
    }
    let skills = discover_skills(workspace);
    if let Some(found) = skills
        .iter()
        .find(|s| s.name == target)
        .or_else(|| skills.iter().find(|s| s.name.eq_ignore_ascii_case(target)))
        .cloned()
    {
        let raw = fs::read_to_string(&found.path)
            .map_err(|e| format!("Failed to read `{}`: {e}", found.path.display()))?;
        let clipped: String = raw.chars().take(16_000).collect();
        return Ok((found, clipped));
    }
    for (info, body) in builtin_ecosystem_skills() {
        if info.name.eq_ignore_ascii_case(target) {
            return Ok((info, body.to_owned()));
        }
    }
    Err(format!("Skill `{target}` not found"))
}

/// Load `<workspace>/ARIA.md`, `AGENTS.md`, or `CLAUDE.md` if present.
#[allow(dead_code)]
pub fn load_project_instructions(workspace: &Path) -> Option<(PathBuf, String)> {
    for fname in ["ARIA.md", "AGENTS.md", "CLAUDE.md"] {
        let p = workspace.join(fname);
        if p.is_file() {
            if let Ok(raw) = fs::read_to_string(&p) {
                let text: String = raw.chars().take(8000).collect();
                if !text.trim().is_empty() {
                    return Some((p, text));
                }
            }
        }
    }
    None
}

/// Generate a concise `ARIA.md` for `workspace` unless it exists and `force` is false.
/// Returns `(path, written)`.
pub fn generate_aria_md(workspace: &Path, force: bool) -> Result<(PathBuf, bool), String> {
    let target = workspace.join("ARIA.md");
    if target.exists() && !force {
        return Ok((target, false));
    }
    let name = workspace
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("project");
    let mut stacks = Vec::new();
    let mut checks = Vec::new();
    if workspace.join("Cargo.toml").is_file() {
        stacks.push("Rust (Cargo)");
        checks.push("`cargo test --workspace`");
    } else if workspace
        .join("rust")
        .join("cli")
        .join("Cargo.toml")
        .is_file()
    {
        stacks.push("Rust (rust/cli)");
        checks.push("`cargo test --manifest-path rust/cli/Cargo.toml`");
    }
    if workspace.join("pyproject.toml").is_file() {
        stacks.push("Python");
        checks.push("`pytest -q`");
    }
    if workspace.join("package.json").is_file() {
        stacks.push("Node.js / TypeScript");
        checks.push("`npm test`");
    }
    if workspace.join("go.mod").is_file() {
        stacks.push("Go");
        checks.push("`go test ./...`");
    }
    let mut dirs = Vec::new();
    if let Ok(entries) = fs::read_dir(workspace) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                if let Some(d) = p.file_name().and_then(|n| n.to_str()) {
                    if !d.starts_with('.') && d != "target" && d != "node_modules" {
                        dirs.push(d.to_owned());
                    }
                }
            }
        }
    }
    dirs.sort();
    dirs.truncate(12);

    let stack_line = if stacks.is_empty() {
        "General workspace".to_owned()
    } else {
        stacks.join(", ")
    };
    let check_line = if checks.is_empty() {
        "Run project tests before finishing edits.".to_owned()
    } else {
        checks.join(" · ")
    };
    let dir_line = if dirs.is_empty() {
        "(root)".to_owned()
    } else {
        dirs.join(", ")
    };

    let content = format!(
        "# {name}\n\n\
         ## Overview\n\
         - Stack: {stack_line}\n\
         - Top-level directories: {dir_line}\n\n\
         ## Verification\n\
         - {check_line}\n\n\
         ## Working Rules\n\
         - Inspect relevant files before editing.\n\
         - Keep changes minimal and verify after edits.\n"
    );
    fs::write(&target, content).map_err(|e| format!("Failed to write ARIA.md: {e}"))?;
    Ok((target, true))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discovers_project_skills_from_yaml_frontmatter() {
        let tmp = tempfile::tempdir().unwrap();
        let skill_dir = tmp.path().join(".aria").join("skills").join("code-review");
        fs::create_dir_all(&skill_dir).unwrap();
        fs::write(
            skill_dir.join("SKILL.md"),
            "---\nname: code-review\ndescription: Review diffs for correctness\n---\nBody\n",
        )
        .unwrap();

        let skills = discover_skills(tmp.path());
        assert_eq!(skills.len(), 1);
        assert_eq!(skills[0].name, "code-review");
        assert_eq!(skills[0].description, "Review diffs for correctness");
        assert_eq!(skills[0].scope, "project");
    }

    #[test]
    fn generates_and_loads_aria_md_respecting_force_flag() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("Cargo.toml"), "[package]\nname=\"demo\"\n").unwrap();
        fs::create_dir_all(tmp.path().join("src")).unwrap();

        let (path, created) = generate_aria_md(tmp.path(), false).unwrap();
        assert!(created);
        assert!(path.is_file());
        let (_, loaded) = load_project_instructions(tmp.path()).unwrap();
        assert!(loaded.contains("Rust (Cargo)"));
        assert!(loaded.contains("cargo test --workspace"));

        fs::write(&path, "# Custom\n").unwrap();
        let (_, overwritten) = generate_aria_md(tmp.path(), false).unwrap();
        assert!(!overwritten);
        assert_eq!(fs::read_to_string(&path).unwrap(), "# Custom\n");

        let (_, forced) = generate_aria_md(tmp.path(), true).unwrap();
        assert!(forced);
        assert!(fs::read_to_string(&path).unwrap().contains("Rust (Cargo)"));
    }

    #[test]
    fn supports_claude_and_agents_skill_dirs_with_aria_precedence_and_claude_md() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("CLAUDE.md"), "Follow strict types.\n").unwrap();
        let (instr_path, instr) = load_project_instructions(tmp.path()).unwrap();
        assert!(instr_path.ends_with("CLAUDE.md"));
        assert!(instr.contains("Follow strict types."));

        let claude_skill = tmp.path().join(".claude").join("skills").join("audit");
        let aria_skill = tmp.path().join(".aria").join("skills").join("audit");
        let agents_skill = tmp.path().join(".agents").join("skills").join("deploy");
        fs::create_dir_all(&claude_skill).unwrap();
        fs::create_dir_all(&aria_skill).unwrap();
        fs::create_dir_all(&agents_skill).unwrap();
        fs::write(
            claude_skill.join("SKILL.md"),
            "---\nname: audit\ndescription: Claude version\n---\nOld\n",
        )
        .unwrap();
        fs::write(
            aria_skill.join("SKILL.md"),
            "---\nname: audit\ndescription: Aria override\n---\nNew body\n",
        )
        .unwrap();
        fs::write(
            agents_skill.join("SKILL.md"),
            "---\nname: deploy\ndescription: Deploy checklist\n---\nSteps\n",
        )
        .unwrap();

        let skills = discover_skills(tmp.path());
        assert_eq!(skills.len(), 2);
        let (info, body) = load_skill_content(tmp.path(), "audit").unwrap();
        assert_eq!(info.description, "Aria override");
        assert!(body.contains("New body"));
        let (deploy_info, _) = load_skill_content(tmp.path(), "DEPLOY").unwrap();
        assert_eq!(deploy_info.name, "deploy");

        let (fin_info, fin_body) =
            load_skill_content(tmp.path(), "financial-orchestration").unwrap();
        assert_eq!(fin_info.scope, "builtin");
        assert!(fin_body.contains("financial_resolve_symbol"));
        assert!(fin_body.contains("financial_stat_arb"));
    }
}

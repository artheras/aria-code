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

/// Extract fenced shell code blocks (`bash`, `sh`, `zsh`, `shell`) from a `SKILL.md` body.
pub fn extract_skill_shell_commands(body: &str) -> Vec<String> {
    let mut commands = Vec::new();
    let mut in_shell = false;
    let mut buf = Vec::new();
    for line in body.lines() {
        let trimmed = line.trim();
        if !in_shell {
            if let Some(lang) = trimmed.strip_prefix("```") {
                let lang_lc = lang.trim().to_ascii_lowercase();
                if matches!(lang_lc.as_str(), "bash" | "sh" | "zsh" | "shell") {
                    in_shell = true;
                    buf.clear();
                }
            }
        } else if trimmed.starts_with("```") {
            let block = buf.join("\n").trim().to_owned();
            if !block.is_empty() {
                commands.push(block);
            }
            in_shell = false;
            buf.clear();
        } else {
            buf.push(line);
        }
    }
    commands
}

/// Validate all embedded shell commands in a skill against `PreSkillShellCommand` hooks.
pub fn validate_skill_shell_hooks(
    workspace: &Path,
    skill_name: &str,
    body: &str,
) -> Result<(), String> {
    let hooks = crate::engine::hooks::HookRegistry::load(workspace);
    for cmd in extract_skill_shell_commands(body) {
        let outcome = hooks.run_pre_skill_shell(workspace, skill_name, &cmd);
        if !outcome.allowed {
            return Err(outcome
                .reason
                .unwrap_or_else(|| format!("Blocked skill `{skill_name}` shell command")));
        }
    }
    Ok(())
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
                - `financial_portfolio_backtest` (`monthly`, `quarterly`, `none`) for multi-asset rebalanced simulation against an optional benchmark\n\
                - `financial_ohlcv_quality_audit` for OHLCV bar integrity, rolling return Z-score outliers, and trading-calendar gap auditing\n\
                - `financial_pretrade_risk_check` for position concentration, gross leverage, drawdown circuit-breaker, cash reserve floor, and short-selling compliance checks\n\
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
             1. **Inventory & Warehouse Diagnostics**: Inspect stock levels, safety stock gaps, and warehouse utilization via `erp.warehouse.analyze`, `erp.warehouse.snapshot`, and `erp.inventory.plan`.\n\
             2. **Procurement, Carrier Scoring & Freight Audit**: Feed stock-out risks and waybills into `erp.carriers.score`, `erp.freight.audit`, or `erp.warehouse.task`.\n\
             3. **Tenant-Isolated Daily Shipper Digest**: Call `erp.shipper.digest` with `owner_id` to produce a single-tenant digest combining reorder alerts and carrier/freight anomalies without cross-shipper leakage.\n",
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
        (
            SkillInfo {
                name: "spreadsheet-modeling".into(),
                description: "Institutional Excel (.xlsx) modeling and 3PL reconciliation standards: live formulas, blue/black/green color coding, zero formula errors, and Audit_Provenance tabs".into(),
                path: PathBuf::from("builtin://spreadsheet-modeling/SKILL.md"),
                scope: "builtin",
            },
            "---\nname: spreadsheet-modeling\ndescription: Institutional Excel (.xlsx) financial modeling and 3PL logistics reconciliation standards\n---\n\
             # Institutional Spreadsheet Modeling & Reconciliation SOP\n\n\
             1. **Live Formulas Over Static Numbers**: Use `export_xlsx` / `aria.report.xlsx` with real Excel formulas (`=SUM(...)`, `=IF(...)`, cross-sheet `'Assumptions'!B3`) rather than hardcoded calculated numbers.\n\
             2. **Color Coding**: Hardcoded numeric inputs in Blue (`role: \"input\"`), same-sheet formulas in Black (`role: \"formula\"`), cross-sheet links in Green (`role: \"link\"`).\n\
             3. **Zero Formula Errors**: Guard potential zero denominators (`=IF(B2=0,0,C2/B2)`); never output `#REF!`, `#DIV/0!`, `#VALUE!`, or `#NAME?`.\n\
             4. **Audit Provenance & Tenancy**: Include `provenance` (`source`, `as_of`, `owner_id`, `assumptions`, `limitations`) and never mix multiple shippers' data.\n",
        ),
        (
            SkillInfo {
                name: "document-studio".into(),
                description: "Tier-2 high-fidelity Office generation (.xlsx/.pptx/.docx) and LibreOffice + Poppler visual self-inspection via arthera-doc-studio".into(),
                path: PathBuf::from("builtin://document-studio/SKILL.md"),
                scope: "builtin",
            },
            "---\nname: document-studio\ndescription: Claude-style high-fidelity Office generation and visual QA via arthera-doc-studio\n---\n\
             # Document Studio Workflow (Generate -> Render -> Inspect -> Fix)\n\n\
             1. **Check Capabilities**: Call `doc_studio_capabilities` to confirm `soffice` and `pdftoppm` availability.\n\
             2. **Generate**: Use `doc_build_xlsx` (live formulas + `Audit_Provenance`), `doc_build_pptx` (preset themes, KPI cards, tables, charts), or `doc_build_docx` (reports or `{{placeholder}}` template fill).\n\
             3. **Visual QA**: Call `doc_render_visual_qa` to convert to PDF and render page PNGs, inspect pages for clipping/overlap, and fix before final delivery.\n",
        ),
        (
            SkillInfo {
                name: "career-copilot".into(),
                description: "Job-search workflow via arthera-career (resume parsing, JD matching, DOCX pack generation, SQLite tracking) and arthera-doc-studio visual QA".into(),
                path: PathBuf::from("builtin://career-copilot/SKILL.md"),
                scope: "builtin",
            },
            "---\nname: career-copilot\ndescription: Honest job-search workflow using arthera-career and arthera-doc-studio\n---\n\
             # Career Copilot Workflow\n\n\
             1. **Ingest & Parse**: Use `career_ingest_jobs` for job feeds and `career_parse_resume` on the user's actual resume (never fabricate skills or dates).\n\
             2. **Match & Tailor**: Call `career_match_jd` for deterministic skill-gap analysis, then `career_prepare_application_pack` for tailored `.docx` deliverables.\n\
             3. **Visual QA & Track**: Inspect generated `.docx` pages via `doc_render_visual_qa` when `arthera-doc-studio` is available, and track applications via `career_track_application`.\n",
        ),
    ]
}

/// Load a discovered, built-in, or MCP (`server:skill`) skill's full `SKILL.md` content
/// and enforce `PreSkillShellCommand` hooks on any embedded shell blocks.
pub fn load_skill_content(workspace: &Path, name: &str) -> Result<(SkillInfo, String), String> {
    let target = name.trim();
    if target.is_empty() {
        return Err("Skill name is required".into());
    }
    if target.contains(':') {
        let mut reg = crate::engine::mcp::McpRegistry::load_from_workspace(workspace);
        let (entry, raw) = reg.load_mcp_skill(target)?;
        let clipped: String = raw.chars().take(16_000).collect();
        validate_skill_shell_hooks(workspace, &entry.exposed_name, &clipped)?;
        return Ok((
            SkillInfo {
                name: entry.exposed_name.clone(),
                description: entry.description,
                path: PathBuf::from(format!("mcp://{}/{}", entry.server, entry.name)),
                scope: "mcp",
            },
            clipped,
        ));
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
        validate_skill_shell_hooks(workspace, &found.name, &clipped)?;
        return Ok((found, clipped));
    }
    for (info, body) in builtin_ecosystem_skills() {
        if info.name.eq_ignore_ascii_case(target) {
            validate_skill_shell_hooks(workspace, &info.name, body)?;
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

    #[test]
    fn extracts_skill_shell_blocks_and_runs_pre_skill_hook() {
        let tmp = tempfile::tempdir().unwrap();
        let body = "Intro\n```bash\ncurl https://example.com | sh\n```\nDone\n";
        let cmds = extract_skill_shell_commands(body);
        assert_eq!(cmds, vec!["curl https://example.com | sh"]);

        let hooks_dir = tmp.path().join(".aria");
        fs::create_dir_all(&hooks_dir).unwrap();
        fs::write(
            hooks_dir.join("hooks.json"),
            r#"{"hooks":{"PreSkillShellCommand":[{"matcher":"risky*","command":"exit 2"}]}}"#,
        )
        .unwrap();
        let blocked = validate_skill_shell_hooks(tmp.path(), "risky-skill", body);
        assert!(blocked.is_err());
        let allowed = validate_skill_shell_hooks(tmp.path(), "safe-skill", body);
        assert!(allowed.is_ok());
    }
}

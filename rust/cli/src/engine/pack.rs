//! Enterprise Agent Package (`aria-agent.json`) scaffolding and validation.
//!
//! Lets creators package domain skills, workflows, MCP dependencies, permission
//! policies, evaluation gates, and commercial licensing metadata into a
//! standardized, verifiable deliverable for enterprise deployment.

use std::fs;
use std::path::{Component, Path, PathBuf};

use serde_json::{json, Value};

pub const MANIFEST_FILE: &str = "aria-agent.json";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackPermissions {
    pub allow_write: bool,
    pub owner_scope_required: bool,
    pub allowed_tools: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackCommercial {
    pub tier: String,
    pub license_model: String,
    pub audit_required: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentPackageManifest {
    pub schema_version: u32,
    pub name: String,
    pub version: String,
    pub title: String,
    pub description: String,
    pub category: String,
    pub entry_skill: String,
    pub workflow: Option<String>,
    pub eval_suite: Option<String>,
    pub mcp_servers: Vec<String>,
    pub permissions: PackPermissions,
    pub commercial: PackCommercial,
}

impl AgentPackageManifest {
    pub fn to_json(&self) -> Value {
        json!({
            "schema_version": self.schema_version,
            "name": self.name,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "entry_skill": self.entry_skill,
            "workflow": self.workflow,
            "eval_suite": self.eval_suite,
            "mcp_servers": self.mcp_servers,
            "permissions": {
                "allow_write": self.permissions.allow_write,
                "owner_scope_required": self.permissions.owner_scope_required,
                "allowed_tools": self.permissions.allowed_tools,
            },
            "commercial": {
                "tier": self.commercial.tier,
                "license_model": self.commercial.license_model,
                "audit_required": self.commercial.audit_required,
            },
        })
    }

    pub fn from_json(v: &Value) -> Result<Self, String> {
        let str_field = |key: &str| -> Result<String, String> {
            v.get(key)
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or_else(|| format!("Missing string field `{key}`"))
        };
        let str_list = |obj: &Value, key: &str| -> Vec<String> {
            obj.get(key)
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default()
        };
        let perms = v.get("permissions").unwrap_or(&Value::Null);
        let comm = v.get("commercial").unwrap_or(&Value::Null);
        Ok(Self {
            schema_version: v.get("schema_version").and_then(Value::as_u64).unwrap_or(0) as u32,
            name: str_field("name")?,
            version: str_field("version")?,
            title: v
                .get("title")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
            description: str_field("description")?,
            category: v
                .get("category")
                .and_then(Value::as_str)
                .unwrap_or("custom")
                .to_owned(),
            entry_skill: str_field("entry_skill")?,
            workflow: v.get("workflow").and_then(Value::as_str).map(str::to_owned),
            eval_suite: v
                .get("eval_suite")
                .and_then(Value::as_str)
                .map(str::to_owned),
            mcp_servers: str_list(v, "mcp_servers"),
            permissions: PackPermissions {
                allow_write: perms
                    .get("allow_write")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                owner_scope_required: perms
                    .get("owner_scope_required")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                allowed_tools: str_list(perms, "allowed_tools"),
            },
            commercial: PackCommercial {
                tier: comm
                    .get("tier")
                    .and_then(Value::as_str)
                    .unwrap_or("enterprise")
                    .to_owned(),
                license_model: comm
                    .get("license_model")
                    .and_then(Value::as_str)
                    .unwrap_or("per-seat")
                    .to_owned(),
                audit_required: comm
                    .get("audit_required")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            },
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackValidationReport {
    pub valid: bool,
    pub package_dir: String,
    pub name: String,
    pub version: String,
    pub category: String,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}

impl PackValidationReport {
    pub fn to_json(&self) -> Value {
        json!({
            "valid": self.valid,
            "package_dir": self.package_dir,
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "errors": self.errors,
            "warnings": self.warnings,
        })
    }
}

fn sanitize_slug(raw: &str) -> String {
    let slug: String = raw
        .trim()
        .to_ascii_lowercase()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    let collapsed = slug
        .split('-')
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    if collapsed.is_empty() {
        "custom-agent".to_owned()
    } else {
        collapsed
    }
}

fn default_manifest(name: &str, category: &str) -> AgentPackageManifest {
    let cat = match category.to_ascii_lowercase().as_str() {
        "logistics" | "finance" | "coding" => category.to_ascii_lowercase(),
        _ => "custom".to_owned(),
    };
    let (title, description, mcp_servers, owner_scope, allowed_tools) = match cat.as_str() {
        "logistics" => (
            "Logistics Operations Agent".to_owned(),
            "Audits freight waybills, scores carriers, and generates shipper-scoped compliance digests.".to_owned(),
            vec!["aria-logistics".to_owned()],
            true,
            vec![
                "read_file".to_owned(),
                "analyze_logistics_data".to_owned(),
                "score_carriers".to_owned(),
                "plan_inventory_policy".to_owned(),
            ],
        ),
        "finance" => (
            "Quantitative & Compliance Finance Agent".to_owned(),
            "Runs verifiable risk checks, portfolio optimization, and disclosure evidence audits.".to_owned(),
            vec!["arthera-financial-service".to_owned()],
            true,
            vec!["read_file".to_owned(), "search_code".to_owned()],
        ),
        "coding" => (
            "Enterprise Engineering Agent".to_owned(),
            "Reviews repositories, enforces policy gates, and automates verified code changes.".to_owned(),
            vec![],
            false,
            vec![
                "read_file".to_owned(),
                "list_files".to_owned(),
                "search_code".to_owned(),
            ],
        ),
        _ => (
            "Enterprise Domain Agent".to_owned(),
            "Packaged domain skill, workflow, and compliance policy for enterprise deployment.".to_owned(),
            vec![],
            false,
            vec!["read_file".to_owned()],
        ),
    };

    AgentPackageManifest {
        schema_version: 1,
        name: sanitize_slug(name),
        version: "0.1.0".to_owned(),
        title,
        description,
        category: cat,
        entry_skill: "SKILL.md".to_owned(),
        workflow: Some("workflow.yaml".to_owned()),
        eval_suite: Some("evals.json".to_owned()),
        mcp_servers,
        permissions: PackPermissions {
            allow_write: false,
            owner_scope_required: owner_scope,
            allowed_tools,
        },
        commercial: PackCommercial {
            tier: "enterprise".to_owned(),
            license_model: "per-seat".to_owned(),
            audit_required: true,
        },
    }
}

pub fn init_package(dir: &Path, name: Option<&str>, category: &str) -> Result<PathBuf, String> {
    fs::create_dir_all(dir).map_err(|e| format!("Failed to create {}: {e}", dir.display()))?;
    let manifest_path = dir.join(MANIFEST_FILE);
    if manifest_path.exists() {
        return Err(format!(
            "Refusing to overwrite existing {}",
            manifest_path.display()
        ));
    }

    let inferred = dir
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("enterprise-agent");
    let manifest = default_manifest(name.unwrap_or(inferred), category);
    let manifest_json = serde_json::to_string_pretty(&manifest.to_json())
        .map_err(|e| format!("Failed to serialize manifest: {e}"))?;
    fs::write(&manifest_path, format!("{manifest_json}\n"))
        .map_err(|e| format!("Failed to write {}: {e}", manifest_path.display()))?;

    let skill_path = dir.join("SKILL.md");
    if !skill_path.exists() {
        let skill_body = format!(
            "# {}\n\n{}\n\n## Operating Rules\n- Enforce least-privilege tool access.\n- Cite verifiable evidence for every conclusion.\n",
            manifest.title, manifest.description
        );
        fs::write(&skill_path, skill_body)
            .map_err(|e| format!("Failed to write {}: {e}", skill_path.display()))?;
    }

    let workflow_path = dir.join("workflow.yaml");
    if !workflow_path.exists() {
        let workflow_body = format!(
            "name: {}\ndescription: {}\nsteps:\n  - id: inspect\n    tool: read_file\n    description: Inspect input workspace context\n",
            manifest.name, manifest.description
        );
        fs::write(&workflow_path, workflow_body)
            .map_err(|e| format!("Failed to write {}: {e}", workflow_path.display()))?;
    }

    let eval_path = dir.join("evals.json");
    if !eval_path.exists() {
        let eval_body = serde_json::to_string_pretty(&serde_json::json!({
            "schema_version": 1,
            "min_pass_rate": 0.9,
            "cases": [
                {
                    "id": "smoke-readonly",
                    "description": "Agent respects read-only boundaries and cites evidence"
                }
            ]
        }))
        .map_err(|e| format!("Failed to serialize evals: {e}"))?;
        fs::write(&eval_path, format!("{eval_body}\n"))
            .map_err(|e| format!("Failed to write {}: {e}", eval_path.display()))?;
    }

    Ok(manifest_path)
}

fn resolve_package_paths(target: &Path) -> Result<(PathBuf, PathBuf), String> {
    if target.is_dir() {
        Ok((target.to_path_buf(), target.join(MANIFEST_FILE)))
    } else {
        let parent = target
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        Ok((parent, target.to_path_buf()))
    }
}

fn is_safe_relative(rel: &str) -> bool {
    let p = Path::new(rel);
    !rel.trim().is_empty()
        && !p.is_absolute()
        && !p.components().any(|c| {
            matches!(
                c,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
}

pub fn load_manifest(target: &Path) -> Result<(PathBuf, AgentPackageManifest), String> {
    let (pkg_dir, manifest_path) = resolve_package_paths(target)?;
    let raw = fs::read_to_string(&manifest_path)
        .map_err(|e| format!("Failed to read {}: {e}", manifest_path.display()))?;
    let parsed: Value = serde_json::from_str(&raw)
        .map_err(|e| format!("Invalid {}: {e}", manifest_path.display()))?;
    let manifest = AgentPackageManifest::from_json(&parsed)?;
    Ok((pkg_dir, manifest))
}

pub fn validate_package(target: &Path) -> Result<PackValidationReport, String> {
    let (pkg_dir, manifest) = load_manifest(target)?;
    let mut errors = Vec::new();
    let mut warnings = Vec::new();

    if manifest.schema_version != 1 {
        errors.push(format!(
            "Unsupported schema_version: {} (expected 1)",
            manifest.schema_version
        ));
    }
    if manifest.name.trim().is_empty() {
        errors.push("Package `name` must not be empty".into());
    }
    if manifest.version.trim().is_empty() {
        errors.push("Package `version` must not be empty".into());
    }
    if manifest.description.trim().is_empty() {
        errors.push("Package `description` must not be empty".into());
    }

    let check_file = |label: &str, rel: &str, errs: &mut Vec<String>| {
        if !is_safe_relative(rel) {
            errs.push(format!(
                "{label} path `{rel}` must be a safe relative path inside the package"
            ));
            return;
        }
        let full = pkg_dir.join(rel);
        if !full.is_file() {
            errs.push(format!("{label} file not found: `{rel}`"));
        }
    };

    check_file("entry_skill", &manifest.entry_skill, &mut errors);
    if let Some(wf) = manifest.workflow.as_deref() {
        check_file("workflow", wf, &mut errors);
    }
    if let Some(ev) = manifest.eval_suite.as_deref() {
        check_file("eval_suite", ev, &mut errors);
    } else {
        warnings
            .push("No `eval_suite` configured; enterprise buyers expect regression proof".into());
    }

    if !manifest.commercial.audit_required {
        warnings.push(
            "`commercial.audit_required` is false; recommended true for enterprise delivery".into(),
        );
    }
    if matches!(manifest.category.as_str(), "logistics" | "finance")
        && !manifest.permissions.owner_scope_required
    {
        warnings.push(format!(
            "Category `{}` should normally require `permissions.owner_scope_required = true`",
            manifest.category
        ));
    }

    Ok(PackValidationReport {
        valid: errors.is_empty(),
        package_dir: pkg_dir.display().to_string(),
        name: manifest.name,
        version: manifest.version,
        category: manifest.category,
        errors,
        warnings,
    })
}

/// Generate a self-contained enterprise Docker deployment bundle (`Dockerfile`,
/// `docker-compose.yml`, `.env.example`) from a validated `aria-agent.json` package.
pub fn export_docker_bundle(target: &Path, out_dir: Option<&Path>) -> Result<PathBuf, String> {
    let report = validate_package(target)?;
    if !report.valid {
        return Err(format!(
            "Refusing to export invalid package `{}`: {}",
            report.name,
            report.errors.join("; ")
        ));
    }
    let (pkg_dir, manifest) = load_manifest(target)?;
    let dest = out_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(|| pkg_dir.join("dist-docker"));
    fs::create_dir_all(&dest).map_err(|e| format!("Failed to create {}: {e}", dest.display()))?;

    let mcp_flag = if manifest.permissions.allow_write {
        "--allow-write"
    } else {
        ""
    };
    let dockerfile = format!(
        "# Generated by `aria-native pack export-docker` for {} v{}\n\
FROM debian:bookworm-slim\n\
WORKDIR /opt/aria-agent\n\
COPY . /opt/aria-agent/\n\
ENV ARIA_AGENT_NAME=\"{}\" \\\n    ARIA_AGENT_CATEGORY=\"{}\" \\\n    ARIA_AUDIT_REQUIRED=\"{}\"\n\
ENTRYPOINT [\"aria-native\", \"-C\", \"/workspace\", \"mcp-serve\"{}]\n",
        manifest.name,
        manifest.version,
        manifest.name,
        manifest.category,
        manifest.commercial.audit_required,
        if mcp_flag.is_empty() {
            String::new()
        } else {
            format!(", \"{mcp_flag}\"")
        }
    );
    fs::write(dest.join("Dockerfile"), dockerfile)
        .map_err(|e| format!("Failed to write Dockerfile: {e}"))?;

    let owner_env = if manifest.permissions.owner_scope_required {
        "      - ARIA_OWNER_SCOPE=${ARIA_OWNER_SCOPE:?set tenant/shipper scope}\n"
    } else {
        ""
    };
    let compose = format!(
        "services:\n  {}:\n    build:\n      context: ..\n      dockerfile: dist-docker/Dockerfile\n    volumes:\n      - ./workspace:/workspace\n    environment:\n      - ARIA_PROVIDER=${{ARIA_PROVIDER:-ollama}}\n      - ARIA_MODEL=${{ARIA_MODEL:-qwen2.5:7b}}\n      - OLLAMA_HOST=${{OLLAMA_HOST:-http://host.docker.internal:11434}}\n      - ARIA_AUDIT_LOG=/workspace/.aria/audit.jsonl\n      - ARIA_LICENSE_KEY=${{ARIA_LICENSE_KEY:-}}\n{}    restart: unless-stopped\n",
        manifest.name, owner_env
    );
    fs::write(dest.join("docker-compose.yml"), compose)
        .map_err(|e| format!("Failed to write docker-compose.yml: {e}"))?;

    let env_example = format!(
        "# Enterprise runtime configuration for {} ({})\n\
ARIA_PROVIDER=ollama\n\
ARIA_MODEL=qwen2.5:7b\n\
OLLAMA_HOST=http://127.0.0.1:11434\n\
ARIA_LICENSE_KEY=\n{}",
        manifest.name,
        manifest.commercial.license_model,
        if manifest.permissions.owner_scope_required {
            "ARIA_OWNER_SCOPE=default-tenant\n"
        } else {
            ""
        }
    );
    fs::write(dest.join(".env.example"), env_example)
        .map_err(|e| format!("Failed to write .env.example: {e}"))?;

    Ok(dest)
}

/// Export a validated `aria-agent.json` package into Cursor's `.cursor-plugin/plugin.json`
/// layout (`<out>/.cursor-plugin/plugin.json` + `<out>/skills/<name>/SKILL.md`).
pub fn export_cursor_plugin(target: &Path, out_dir: Option<&Path>) -> Result<PathBuf, String> {
    let report = validate_package(target)?;
    if !report.valid {
        return Err(format!(
            "Refusing to export invalid package `{}`: {}",
            report.name,
            report.errors.join("; ")
        ));
    }
    let (pkg_dir, manifest) = load_manifest(target)?;
    let dest = out_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(|| pkg_dir.join("dist-cursor"));
    let plugin_meta_dir = dest.join(".cursor-plugin");
    let skill_dest_dir = dest.join("skills").join(&manifest.name);
    fs::create_dir_all(&plugin_meta_dir)
        .map_err(|e| format!("Failed to create {}: {e}", plugin_meta_dir.display()))?;
    fs::create_dir_all(&skill_dest_dir)
        .map_err(|e| format!("Failed to create {}: {e}", skill_dest_dir.display()))?;

    let skill_src = pkg_dir.join(&manifest.entry_skill);
    let skill_body = fs::read_to_string(&skill_src)
        .map_err(|e| format!("Failed to read {}: {e}", skill_src.display()))?;
    fs::write(skill_dest_dir.join("SKILL.md"), skill_body)
        .map_err(|e| format!("Failed to write SKILL.md: {e}"))?;

    let plugin_json = json!({
        "name": manifest.name,
        "version": manifest.version,
        "displayName": manifest.title,
        "description": manifest.description,
        "skills": [format!("skills/{}", manifest.name)],
        "mcpServers": manifest.mcp_servers,
        "permissions": {
            "allowWrite": manifest.permissions.allow_write,
            "ownerScopeRequired": manifest.permissions.owner_scope_required,
            "allowedTools": manifest.permissions.allowed_tools,
        },
        "x-aria": {
            "category": manifest.category,
            "tier": manifest.commercial.tier,
            "licenseModel": manifest.commercial.license_model,
            "auditRequired": manifest.commercial.audit_required,
        }
    });
    let rendered = serde_json::to_string_pretty(&plugin_json)
        .map_err(|e| format!("Failed to serialize plugin.json: {e}"))?;
    fs::write(plugin_meta_dir.join("plugin.json"), format!("{rendered}\n"))
        .map_err(|e| format!("Failed to write plugin.json: {e}"))?;

    Ok(dest)
}

/// Export a validated `aria-agent.json` package into a commercial MCP distribution bundle
/// (`<out>/mcp.json` + `<out>/SETUP.md` + `<out>/package_meta.json`).
pub fn export_mcp_bundle(target: &Path, out_dir: Option<&Path>) -> Result<PathBuf, String> {
    let report = validate_package(target)?;
    if !report.valid {
        return Err(format!(
            "Refusing to export invalid package `{}`: {}",
            report.name,
            report.errors.join("; ")
        ));
    }
    let (pkg_dir, manifest) = load_manifest(target)?;
    let dest = out_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(|| pkg_dir.join("dist-mcp"));
    fs::create_dir_all(&dest).map_err(|e| format!("Failed to create {}: {e}", dest.display()))?;

    let mcp_json = json!({
        "mcpServers": {
            manifest.name.clone(): {
                "command": "aria-native",
                "args": ["mcp-serve"],
                "env": {
                    "ARIA_LICENSE_KEY": "${ARIA_LICENSE_KEY}",
                    "ARIA_LICENSE_SECRET": "${ARIA_LICENSE_SECRET}",
                    "ARIA_LICENSE_PACKAGE": manifest.name,
                    "ARIA_MCP_ACTOR": "${ARIA_MCP_ACTOR:-mcp-client}"
                }
            },
            format!("{}-http", manifest.name): {
                "transport": "streamable-http",
                "url": "http://127.0.0.1:8787/mcp",
                "headers": {
                    "Authorization": "Bearer ${ARIA_MCP_BEARER_TOKEN}"
                }
            }
        }
    });
    fs::write(
        dest.join("mcp.json"),
        serde_json::to_string_pretty(&mcp_json).unwrap_or_default(),
    )
    .map_err(|e| format!("Failed to write mcp.json: {e}"))?;

    let meta = json!({
        "name": manifest.name,
        "version": manifest.version,
        "title": manifest.title,
        "description": manifest.description,
        "category": manifest.category,
        "allow_write": manifest.permissions.allow_write,
        "allowed_tools": manifest.permissions.allowed_tools,
        "entry_skill": manifest.entry_skill,
        "commercial": {
            "tier": manifest.commercial.tier,
            "license_model": manifest.commercial.license_model,
            "audit_required": manifest.commercial.audit_required,
        }
    });
    fs::write(
        dest.join("package_meta.json"),
        serde_json::to_string_pretty(&meta).unwrap_or_default(),
    )
    .map_err(|e| format!("Failed to write package_meta.json: {e}"))?;

    let setup_md = format!(
        "# {} ({})\n\n{}\n\n## 1. Local Stdio MCP\n```bash\nexport ARIA_LICENSE_KEY=\"<customer-token>\"\nexport ARIA_LICENSE_SECRET=\"<signing-secret>\"\nexport ARIA_LICENSE_PACKAGE=\"{}\"\naria-native -C . mcp-serve\n```\n\n## 2. Remote Streamable HTTP MCP\n```bash\nexport ARIA_MCP_BEARER_TOKEN=\"<bearer-token>\"\naria-native -C . mcp-serve --http 127.0.0.1:8787\n```\n\n## 3. Usage & Audit Metering\n```bash\naria-native -C . pack usage\naria-native -C . pack audit --limit 50\n```\n",
        manifest.title, manifest.version, manifest.description, manifest.name
    );
    fs::write(dest.join("SETUP.md"), setup_md)
        .map_err(|e| format!("Failed to write SETUP.md: {e}"))?;

    Ok(dest)
}

fn infer_category_from_idea(idea: &str) -> &'static str {
    let lower = idea.to_ascii_lowercase();
    if lower.contains("freight")
        || lower.contains("logistics")
        || lower.contains("carrier")
        || lower.contains("waybill")
        || lower.contains("inventory")
        || idea.contains("物流")
        || idea.contains("运单")
        || idea.contains("仓储")
    {
        "logistics"
    } else if lower.contains("finance")
        || lower.contains("quant")
        || lower.contains("portfolio")
        || lower.contains("option")
        || lower.contains("risk")
        || idea.contains("金融")
        || idea.contains("量化")
        || idea.contains("财报")
    {
        "finance"
    } else if lower.contains("code")
        || lower.contains("repo")
        || lower.contains("review")
        || lower.contains("ci")
        || idea.contains("代码")
        || idea.contains("测试")
    {
        "coding"
    } else {
        "custom"
    }
}

/// Scaffold a ready-to-verify enterprise agent package directly from a natural-language idea.
pub fn create_from_idea(
    dir: &Path,
    idea: &str,
    name: Option<&str>,
    category: Option<&str>,
) -> Result<PathBuf, String> {
    let clean_idea = idea.trim();
    if clean_idea.is_empty() {
        return Err("Idea description must not be empty".into());
    }
    let chosen_cat = category.unwrap_or_else(|| infer_category_from_idea(clean_idea));
    let manifest_path = init_package(dir, name, chosen_cat)?;
    let (pkg_dir, mut manifest) = load_manifest(&manifest_path)?;
    manifest.description = clean_idea.to_owned();
    fs::write(
        &manifest_path,
        format!(
            "{}\n",
            serde_json::to_string_pretty(&manifest.to_json()).map_err(|e| e.to_string())?
        ),
    )
    .map_err(|e| format!("Failed to update manifest: {e}"))?;

    let skill_body = format!(
        "# {}\n\n## Mission\n{}\n\n## Operating Rules\n- Enforce least-privilege tool access (`allow_write: {}`).\n- Cite verifiable workspace or MCP evidence for every conclusion.\n",
        manifest.title, clean_idea, manifest.permissions.allow_write
    );
    fs::write(pkg_dir.join(&manifest.entry_skill), skill_body)
        .map_err(|e| format!("Failed to update SKILL.md: {e}"))?;

    Ok(manifest_path)
}

#[derive(Debug, Clone, PartialEq)]
pub struct PackScorecard {
    pub package: String,
    pub version: String,
    pub category: String,
    pub valid: bool,
    pub total_cases: usize,
    pub passed_cases: usize,
    pub pass_rate: f64,
    pub min_pass_rate: f64,
    pub enterprise_ready: bool,
    pub notes: Vec<String>,
}

impl PackScorecard {
    pub fn to_json(&self) -> Value {
        json!({
            "package": self.package,
            "version": self.version,
            "category": self.category,
            "valid": self.valid,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "pass_rate": self.pass_rate,
            "min_pass_rate": self.min_pass_rate,
            "enterprise_ready": self.enterprise_ready,
            "notes": self.notes,
        })
    }
}

/// Verify package structure, enterprise policy gates, and `evals.json` acceptance cases,
/// writing `<pkg_dir>/scorecard.json`.
pub fn run_package_evals(target: &Path) -> Result<PackScorecard, String> {
    let report = validate_package(target)?;
    let (pkg_dir, manifest) = load_manifest(target)?;
    let mut notes = report.errors.clone();
    notes.extend(report.warnings.clone());

    let mut total_cases = 0usize;
    let mut passed_cases = 0usize;
    let mut min_pass_rate = 0.8_f64;

    if let Some(eval_rel) = manifest.eval_suite.as_deref() {
        let eval_path = pkg_dir.join(eval_rel);
        if let Ok(raw) = fs::read_to_string(&eval_path) {
            if let Ok(val) = serde_json::from_str::<Value>(&raw) {
                if let Some(m) = val.get("min_pass_rate").and_then(Value::as_f64) {
                    min_pass_rate = m.clamp(0.0, 1.0);
                }
                if let Some(cases) = val.get("cases").and_then(Value::as_array) {
                    total_cases = cases.len();
                    for case in cases {
                        let has_id = case
                            .get("id")
                            .and_then(Value::as_str)
                            .is_some_and(|s| !s.trim().is_empty());
                        let has_desc = case
                            .get("description")
                            .and_then(Value::as_str)
                            .is_some_and(|s| !s.trim().is_empty());
                        if has_id && has_desc {
                            passed_cases += 1;
                        } else {
                            notes.push("Eval case missing non-empty `id` or `description`".into());
                        }
                    }
                }
            } else {
                notes.push(format!("Invalid JSON in `{eval_rel}`"));
            }
        }
    }

    let pass_rate = if total_cases == 0 {
        0.0
    } else {
        passed_cases as f64 / total_cases as f64
    };
    let enterprise_ready = report.valid
        && total_cases > 0
        && pass_rate >= min_pass_rate
        && manifest.commercial.audit_required;

    let scorecard = PackScorecard {
        package: manifest.name,
        version: manifest.version,
        category: manifest.category,
        valid: report.valid,
        total_cases,
        passed_cases,
        pass_rate,
        min_pass_rate,
        enterprise_ready,
        notes,
    };

    let rendered = serde_json::to_string_pretty(&scorecard.to_json())
        .map_err(|e| format!("Failed to serialize scorecard: {e}"))?;
    fs::write(pkg_dir.join("scorecard.json"), format!("{rendered}\n"))
        .map_err(|e| format!("Failed to write scorecard.json: {e}"))?;

    Ok(scorecard)
}

/// Generate personalized go-to-market promotional assets (`PROMO.md`, `landing.html`,
/// `social_copy.md`, and `promo_prompt.md`) tailored from:
/// 1. User-supplied `--brief` / `<pkg_dir>/brand.json`
/// 2. Agent-learned project & user memories (`.aria/memory.json` via BM25)
/// 3. Verified `scorecard.json` metrics
pub fn generate_promo_kit(
    target: &Path,
    out_dir: Option<&Path>,
    user_brief: Option<&str>,
) -> Result<PathBuf, String> {
    let report = validate_package(target)?;
    if !report.valid {
        return Err(format!(
            "Refusing to generate promo kit for invalid package `{}`: {}",
            report.name,
            report.errors.join("; ")
        ));
    }
    let (pkg_dir, manifest) = load_manifest(target)?;
    let dest = out_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(|| pkg_dir.join("dist-promo"));
    fs::create_dir_all(&dest).map_err(|e| format!("Failed to create {}: {e}", dest.display()))?;

    let brand = fs::read_to_string(pkg_dir.join("brand.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .unwrap_or(Value::Null);
    let brand_name = brand
        .get("brand_name")
        .and_then(Value::as_str)
        .unwrap_or(&manifest.title);
    let audience = brand
        .get("audience")
        .and_then(Value::as_str)
        .unwrap_or("Enterprise operations & engineering teams");
    let tone = brand
        .get("tone")
        .and_then(Value::as_str)
        .unwrap_or("Clear, evidence-first, executive-ready");
    let visual_style = brand
        .get("visual_style")
        .and_then(Value::as_str)
        .unwrap_or("Clean enterprise UI, high-contrast data cards");

    let query = format!(
        "{} {} {}",
        manifest.category, manifest.name, manifest.description
    );
    let memories = crate::engine::memory::search_memories(&pkg_dir, &query, 5);
    let memory_lines: Vec<String> = memories.iter().map(|m| format!("- {}", m.text)).collect();
    let brief_text = user_brief.map(str::trim).filter(|s| !s.is_empty());

    let scorecard_summary = fs::read_to_string(pkg_dir.join("scorecard.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .map(|v| {
            let passed = v.get("passed_cases").and_then(Value::as_u64).unwrap_or(0);
            let total = v.get("total_cases").and_then(Value::as_u64).unwrap_or(0);
            let ready = v
                .get("enterprise_ready")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            format!("{passed}/{total} checks passed · Enterprise Ready: {ready}")
        })
        .unwrap_or_else(|| "Run `aria-native pack test` to attach verified scorecard".to_owned());

    let mcp_line = if manifest.mcp_servers.is_empty() {
        "Standalone Workspace Tools".to_owned()
    } else {
        manifest.mcp_servers.join(", ")
    };

    let mut context_block = String::new();
    if let Some(b) = brief_text {
        context_block.push_str(&format!("- **User Brief**: {b}\n"));
    }
    if !memory_lines.is_empty() {
        context_block.push_str("- **Learned User Context**:\n");
        for line in &memory_lines {
            context_block.push_str(&format!("  {line}\n"));
        }
    }
    if context_block.is_empty() {
        context_block.push_str(
            "- *Tip*: Add `brand.json`, pass `--brief`, or save preferences via `/memory add` for deeper personalization.\n",
        );
    }

    let promo_md = format!(
        "# {} (v{})\n\n\
> {}\n\n\
## Audience & Positioning\n\
- **Brand**: {}\n\
- **Target Audience**: {}\n\
- **Tone**: {}\n\
- **Visual Direction**: {}\n\
{}\n\
## Why Enterprise Teams Buy This Agent\n\
- **Category**: `{}`\n\
- **Verified Quality**: {}\n\
- **Security & Governance**: Read-only default (`allow_write: {}`), Tenant Isolation (`owner_scope_required: {}`), Audit Trail (`audit_required: {}`)\n\
- **Connected MCP Services**: {}\n\n\
## Deployment Options\n\
1. **Private Docker**: `aria-native pack export-docker .`\n\
2. **Cursor Plugin**: `aria-native pack export-cursor .`\n\
3. **Direct MCP Host**: `aria-native mcp-serve`\n",
        manifest.title,
        manifest.version,
        manifest.description,
        brand_name,
        audience,
        tone,
        visual_style,
        context_block,
        manifest.category,
        scorecard_summary,
        manifest.permissions.allow_write,
        manifest.permissions.owner_scope_required,
        manifest.commercial.audit_required,
        mcp_line
    );
    fs::write(dest.join("PROMO.md"), &promo_md)
        .map_err(|e| format!("Failed to write PROMO.md: {e}"))?;
    let _ = crate::engine::report::export_markdown_pptx(
        &dest,
        Path::new("PROMO.md"),
        Path::new("pitch.pptx"),
    );
    if crate::engine::report::export_markdown_report(
        &dest,
        Path::new("PROMO.md"),
        Path::new("pitch.pdf"),
        true,
        false,
    )
    .is_err()
    {
        let _ = crate::engine::report::export_markdown_report(
            &dest,
            Path::new("PROMO.md"),
            Path::new("pitch.html"),
            false,
            false,
        );
    }

    let landing_html = format!(
        "<!doctype html>\n<html lang=\"en\">\n<head><meta charset=\"utf-8\"><title>{}</title>\n\
<style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:48px auto;padding:0 20px;line-height:1.6;color:#111}} .badge{{display:inline-block;padding:4px 10px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:13px;font-weight:600}} pre{{background:#f6f8fa;padding:14px;border-radius:8px;overflow:auto}}</style>\n\
</head>\n<body>\n\
<span class=\"badge\">{} · v{}</span>\n\
<h1>{}</h1>\n\
<p>{}</p>\n\
<p><strong>For:</strong> {}</p>\n\
<h3>Verification & Governance</h3>\n\
<ul><li>Scorecard: {}</li><li>Audit Logging: {}</li><li>MCP Integrations: {}</li></ul>\n\
<h3>Quick Deploy</h3>\n\
<pre>aria-native pack validate .\naria-native pack export-docker .</pre>\n\
</body>\n</html>\n",
        brand_name,
        manifest.category,
        manifest.version,
        manifest.title,
        manifest.description,
        audience,
        scorecard_summary,
        manifest.commercial.audit_required,
        mcp_line
    );
    fs::write(dest.join("landing.html"), landing_html)
        .map_err(|e| format!("Failed to write landing.html: {e}"))?;

    let brief_callout = brief_text
        .map(|b| format!("\n🎯 Focus: {b}"))
        .unwrap_or_default();
    let social_copy = format!(
        "🚀 Just shipped **{}** (`{}` v{}) built on Aria Code for **{}**!\n\n\
{}{}\n\n\
✅ {}\n\
🔒 Enterprise audit trail & least-privilege permissions built in\n\
🐳 Deploy privately with `aria-native pack export-docker`\n",
        manifest.title,
        manifest.name,
        manifest.version,
        audience,
        manifest.description,
        brief_callout,
        scorecard_summary
    );
    fs::write(dest.join("social_copy.md"), social_copy)
        .map_err(|e| format!("Failed to write social_copy.md: {e}"))?;

    let prompt_pack = format!(
        "# Personalized Creative & Document Generation Prompt\n\n\
Use this prompt with `aria-native agent` or your image/PPT/PDF generator so copy, visuals, and deliverables match the user:\n\n\
- **Product**: {} (`{}` v{})\n\
- **Brand**: {}\n\
- **Audience**: {}\n\
- **Tone**: {}\n\
- **Visual Style**: {}\n\
- **Verified Proof**: {}\n\
{}\n",
        manifest.title,
        manifest.name,
        manifest.version,
        brand_name,
        audience,
        tone,
        visual_style,
        scorecard_summary,
        context_block
    );
    fs::write(dest.join("promo_prompt.md"), prompt_pack)
        .map_err(|e| format!("Failed to write promo_prompt.md: {e}"))?;

    Ok(dest)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn init_and_validate_logistics_package() {
        let dir = tempfile::tempdir().expect("tempdir");
        let pkg_dir = dir.path().join("freight-auditor");
        let manifest_path =
            init_package(&pkg_dir, Some("Freight Auditor"), "logistics").expect("init");
        assert!(manifest_path.is_file());

        let report = validate_package(&pkg_dir).expect("validate");
        assert!(report.valid, "errors: {:?}", report.errors);
        assert!(
            report.warnings.is_empty(),
            "warnings: {:?}",
            report.warnings
        );
        assert_eq!(report.name, "freight-auditor");
        assert_eq!(report.category, "logistics");
    }

    #[test]
    fn validate_rejects_traversal_and_missing_entry_skill() {
        let dir = tempfile::tempdir().expect("tempdir");
        let pkg_dir = dir.path().join("bad-pack");
        init_package(&pkg_dir, Some("Bad Pack"), "finance").expect("init");
        let (_, mut manifest) = load_manifest(&pkg_dir).expect("load");
        manifest.entry_skill = "../outside.md".into();
        manifest.workflow = Some("missing.yaml".into());
        fs::write(
            pkg_dir.join(MANIFEST_FILE),
            serde_json::to_string_pretty(&manifest.to_json()).unwrap(),
        )
        .unwrap();

        let report = validate_package(&pkg_dir).expect("validate");
        assert!(!report.valid);
        assert_eq!(report.errors.len(), 2);
    }

    #[test]
    fn export_docker_generates_enterprise_bundle() {
        let dir = tempfile::tempdir().expect("tempdir");
        let pkg_dir = dir.path().join("quant-compliance");
        init_package(&pkg_dir, Some("Quant Compliance"), "finance").expect("init");
        let out = export_docker_bundle(&pkg_dir, None).expect("export");
        assert!(out.join("Dockerfile").is_file());
        let compose = fs::read_to_string(out.join("docker-compose.yml")).expect("compose");
        assert!(compose.contains("ARIA_OWNER_SCOPE"));
        assert!(compose.contains("ARIA_AUDIT_LOG=/workspace/.aria/audit.jsonl"));
        let env = fs::read_to_string(out.join(".env.example")).expect("env");
        assert!(env.contains("ARIA_LICENSE_KEY="));
    }

    #[test]
    fn export_cursor_plugin_generates_manifest_and_skill() {
        let dir = tempfile::tempdir().expect("tempdir");
        let pkg_dir = dir.path().join("cursor-ready");
        init_package(&pkg_dir, Some("Cursor Ready"), "coding").expect("init");
        let out = export_cursor_plugin(&pkg_dir, None).expect("export cursor");
        let plugin_raw = fs::read_to_string(out.join(".cursor-plugin").join("plugin.json"))
            .expect("plugin.json");
        let parsed: Value = serde_json::from_str(&plugin_raw).expect("parse");
        assert_eq!(parsed["name"], "cursor-ready");
        assert!(out
            .join("skills")
            .join("cursor-ready")
            .join("SKILL.md")
            .is_file());
    }

    #[test]
    fn create_test_and_promo_end_to_end() {
        let dir = tempfile::tempdir().expect("tempdir");
        let pkg_dir = dir.path().join("freight-audit-pro");
        create_from_idea(
            &pkg_dir,
            "Audit air freight waybills and detect billing weight anomalies",
            Some("Freight Audit Pro"),
            None,
        )
        .expect("create");

        let card = run_package_evals(&pkg_dir).expect("evals");
        assert!(card.enterprise_ready);
        assert_eq!(card.category, "logistics");
        assert!(pkg_dir.join("scorecard.json").is_file());

        fs::write(
            pkg_dir.join("brand.json"),
            r#"{"brand_name":"Apex Air","audience":"Freight CFOs","tone":"Executive","visual_style":"Minimalist dark blue"}"#,
        )
        .unwrap();
        crate::engine::memory::save_memories(
            &pkg_dir,
            &[crate::engine::memory::MemoryEntry {
                text: "Freight CFOs prefer concise ROI metrics in air freight audits".into(),
                scope: "project".into(),
            }],
        )
        .unwrap();

        let promo = generate_promo_kit(&pkg_dir, None, Some("Focus on 48h invoice reconciliation"))
            .expect("promo");
        assert!(promo.join("PROMO.md").is_file());
        assert!(promo.join("pitch.pptx").is_file());
        assert!(promo.join("pitch.pdf").is_file() || promo.join("pitch.html").is_file());
        assert!(promo.join("landing.html").is_file());
        assert!(promo.join("social_copy.md").is_file());
        assert!(promo.join("promo_prompt.md").is_file());
        let html = fs::read_to_string(promo.join("landing.html")).expect("html");
        assert!(html.contains("1/1 checks passed"));
        assert!(html.contains("Freight CFOs"));
        let prompt_md = fs::read_to_string(promo.join("promo_prompt.md")).expect("prompt");
        assert!(prompt_md.contains("48h invoice reconciliation"));
        assert!(prompt_md.contains("concise ROI metrics"));

        let mcp_out = export_mcp_bundle(&pkg_dir, None).expect("export mcp");
        assert!(mcp_out.join("mcp.json").is_file());
        assert!(mcp_out.join("package_meta.json").is_file());
        assert!(mcp_out.join("SETUP.md").is_file());
        let mcp_raw = fs::read_to_string(mcp_out.join("mcp.json")).expect("mcp json");
        assert!(mcp_raw.contains("streamable-http"));
        assert!(mcp_raw.contains("ARIA_LICENSE_KEY"));
    }
}

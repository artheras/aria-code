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
}

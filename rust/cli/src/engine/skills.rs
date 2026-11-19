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

/// Discover skills from `~/.aria/skills` and `<workspace>/.aria/skills` (workspace overrides global).
pub fn discover_skills(workspace: &Path) -> Vec<SkillInfo> {
    let mut map = BTreeMap::new();
    if !cfg!(test) {
        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            collect_skills_from_dir(
                &Path::new(&home).join(".aria").join("skills"),
                "global",
                &mut map,
            );
        }
    }
    collect_skills_from_dir(&workspace.join(".aria").join("skills"), "project", &mut map);
    map.into_values().collect()
}

/// Load `<workspace>/ARIA.md` or `<workspace>/AGENTS.md` if present.
#[allow(dead_code)]
pub fn load_project_instructions(workspace: &Path) -> Option<(PathBuf, String)> {
    for fname in ["ARIA.md", "AGENTS.md"] {
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
}

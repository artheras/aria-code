//! Native Rust `/artifacts` management (list, stats, path, prune).
//!
//! Scans workspace `.aria/exports`, `.aria/artifacts`, optional `ARIA_ARTIFACT_ROOT`,
//! and `~/.arthera/artifacts` (skipped during unit tests for isolation).

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone)]
pub struct ArtifactItem {
    pub path: PathBuf,
    pub root_label: String,
    pub kind: String,
    pub size_bytes: u64,
    pub modified_secs: u64,
}

fn artifact_roots(workspace: &Path) -> Vec<(String, PathBuf)> {
    let mut roots = vec![
        (
            "workspace/.aria/exports".to_owned(),
            workspace.join(".aria").join("exports"),
        ),
        (
            "workspace/.aria/artifacts".to_owned(),
            workspace.join(".aria").join("artifacts"),
        ),
    ];
    if let Ok(env_root) = std::env::var("ARIA_ARTIFACT_ROOT") {
        let trimmed = env_root.trim();
        if !trimmed.is_empty() {
            roots.push(("ARIA_ARTIFACT_ROOT".to_owned(), PathBuf::from(trimmed)));
        }
    }
    if !cfg!(test) {
        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            roots.push((
                "~/.arthera/artifacts".to_owned(),
                PathBuf::from(home).join(".arthera").join("artifacts"),
            ));
        }
    }
    roots
}

fn collect_files(root_label: &str, dir: &Path, depth: usize, out: &mut Vec<ArtifactItem>) {
    if depth > 4 || !dir.is_dir() {
        return;
    }
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name();
        if name.to_string_lossy().starts_with('.') {
            continue;
        }
        if path.is_dir() {
            collect_files(root_label, &path, depth + 1, out);
        } else if path.is_file() {
            let Ok(meta) = entry.metadata() else {
                continue;
            };
            let modified_secs = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let kind = path
                .extension()
                .and_then(|e| e.to_str())
                .map(str::to_ascii_lowercase)
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "file".to_owned());
            out.push(ArtifactItem {
                path,
                root_label: root_label.to_owned(),
                kind,
                size_bytes: meta.len(),
                modified_secs,
            });
        }
    }
}

pub fn list_artifacts(workspace: &Path) -> Vec<ArtifactItem> {
    let mut items = Vec::new();
    for (label, root) in artifact_roots(workspace) {
        collect_files(&label, &root, 0, &mut items);
    }
    items.sort_by(|a, b| {
        b.modified_secs
            .cmp(&a.modified_secs)
            .then_with(|| a.path.cmp(&b.path))
    });
    items
}

fn format_bytes(bytes: u64) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else if bytes < 1024 * 1024 {
        format!("{:.1} KB", bytes as f64 / 1024.0)
    } else {
        format!("{:.1} MB", bytes as f64 / (1024.0 * 1024.0))
    }
}

fn format_age(modified_secs: u64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let diff = now.saturating_sub(modified_secs);
    if diff < 60 {
        format!("{diff}s ago")
    } else if diff < 3600 {
        format!("{}m ago", diff / 60)
    } else if diff < 86400 {
        format!("{}h ago", diff / 3600)
    } else {
        format!("{}d ago", diff / 86400)
    }
}

pub fn handle_artifacts_command(
    workspace: &Path,
    arg: &str,
    allow_write: bool,
    zh: bool,
) -> (String, &'static str) {
    let tokens: Vec<&str> = arg.split_whitespace().collect();
    let head = tokens
        .first()
        .copied()
        .unwrap_or("list")
        .to_ascii_lowercase();
    let items = list_artifacts(workspace);

    match head.as_str() {
        "stats" | "summary" => {
            let total_bytes: u64 = items.iter().map(|i| i.size_bytes).sum();
            let mut by_kind: BTreeMap<String, usize> = BTreeMap::new();
            for item in &items {
                *by_kind.entry(item.kind.clone()).or_insert(0) += 1;
            }
            let mut lines = vec![if zh {
                "### 产物统计".to_owned()
            } else {
                "### Artifact Inventory".to_owned()
            }];
            lines.push(format!(
                "- Total: **{}** files ({})",
                items.len(),
                format_bytes(total_bytes)
            ));
            for (kind, count) in by_kind {
                lines.push(format!("- `.{kind}`: {count}"));
            }
            (lines.join("\n"), "ok")
        }
        "path" | "show" => {
            let selector = tokens.get(1).copied().unwrap_or("latest");
            let selected = if selector.eq_ignore_ascii_case("latest")
                || selector.eq_ignore_ascii_case("last")
            {
                items.first()
            } else if let Ok(n) = selector.parse::<usize>() {
                n.checked_sub(1).and_then(|idx| items.get(idx))
            } else {
                let needle = selector.to_ascii_lowercase();
                items.iter().find(|i| {
                    i.path
                        .file_name()
                        .and_then(|n| n.to_str())
                        .is_some_and(|n| n.to_ascii_lowercase().contains(&needle))
                })
            };
            match selected {
                Some(item) => (format!("`{}`", item.path.display()), "ok"),
                None => (
                    format!("Artifact `{selector}` not found. Run `/artifacts` to list items."),
                    "failed",
                ),
            }
        }
        "prune" | "cleanup" | "gc" => {
            let dry_run = tokens.iter().any(|t| *t == "--dry-run" || *t == "-n") || !allow_write;
            let keep = tokens
                .iter()
                .skip(1)
                .find_map(|t| t.parse::<usize>().ok())
                .unwrap_or(20);
            let to_remove: Vec<&ArtifactItem> = items.iter().skip(keep).collect();
            if to_remove.is_empty() {
                return (
                    format!(
                        "Nothing to prune ({} artifacts <= keep {keep}).",
                        items.len()
                    ),
                    "ok",
                );
            }
            let mut lines = vec![if dry_run {
                format!("### Artifact Prune Dry-Run (keep={keep})")
            } else {
                format!("### Artifact Prune (keep={keep})")
            }];
            if !allow_write && !tokens.iter().any(|t| *t == "--dry-run" || *t == "-n") {
                lines.push(if zh {
                    "当前处于只读保护模式；已切换为 dry-run 预览。实际删除请先运行 `/write-mode on`。"
                        .to_owned()
                } else {
                    "Write mode is OFF; showing dry-run preview. Run `/write-mode on` to delete files."
                        .to_owned()
                });
            }
            let mut removed = 0usize;
            for item in to_remove {
                let name = item
                    .path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("artifact");
                if dry_run {
                    lines.push(format!("- would remove `{name}` ({})", item.root_label));
                } else if fs::remove_file(&item.path).is_ok() {
                    removed += 1;
                    lines.push(format!("- removed `{name}` ({})", item.root_label));
                }
            }
            if !dry_run {
                lines.push(format!("\nRemoved **{removed}** artifact(s)."));
            }
            (lines.join("\n"), "ok")
        }
        _ => {
            let limit = head
                .parse::<usize>()
                .ok()
                .or_else(|| tokens.get(1).and_then(|t| t.parse::<usize>().ok()))
                .unwrap_or(15)
                .clamp(1, 50);
            if items.is_empty() {
                let msg = if zh {
                    "暂无生成产物。运行 `/export` 或生成报告后可通过 `/artifacts` 查看。"
                } else {
                    "No artifacts found. Export transcripts with `/export` or generate deliverables to see them here."
                };
                return (msg.to_owned(), "ok");
            }
            let mut lines = vec![if zh {
                "### 生成的产物".to_owned()
            } else {
                "### Generated Artifacts".to_owned()
            }];
            for (idx, item) in items.iter().take(limit).enumerate() {
                let name = item
                    .path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("artifact");
                lines.push(format!(
                    "{}. `{name}` — {} · {} · _{}_",
                    idx + 1,
                    format_bytes(item.size_bytes),
                    format_age(item.modified_secs),
                    item.root_label
                ));
            }
            lines.push(String::new());
            lines.push(if zh {
                "操作：`/artifacts path 1` · `/artifacts stats` · `/artifacts prune 20 [--dry-run]`"
                    .to_owned()
            } else {
                "Usage: `/artifacts path 1` · `/artifacts stats` · `/artifacts prune 20 [--dry-run]`"
                    .to_owned()
            });
            (lines.join("\n"), "ok")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn artifacts_list_stats_path_and_prune_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let exp = dir.path().join(".aria").join("exports");
        fs::create_dir_all(&exp).unwrap();
        fs::write(exp.join("a.md"), "# report a").unwrap();
        fs::write(exp.join("b.json"), "{\"ok\":true}").unwrap();
        fs::write(exp.join("c.md"), "# report c").unwrap();

        let (listed, st_list) = handle_artifacts_command(dir.path(), "", false, false);
        assert_eq!(st_list, "ok");
        assert!(listed.contains("a.md"));
        assert!(listed.contains("b.json"));

        let (stats, st_stats) = handle_artifacts_command(dir.path(), "stats", false, false);
        assert_eq!(st_stats, "ok");
        assert!(stats.contains("**3** files"));
        assert!(stats.contains("`.md`: 2"));

        let (path_out, st_path) = handle_artifacts_command(dir.path(), "path b.json", false, false);
        assert_eq!(st_path, "ok");
        assert!(path_out.contains("b.json"));

        // Read-only mode forces dry-run and preserves all 3 files.
        let (dry, st_dry) = handle_artifacts_command(dir.path(), "prune 1", false, false);
        assert_eq!(st_dry, "ok");
        assert!(dry.contains("Dry-Run"));
        assert_eq!(list_artifacts(dir.path()).len(), 3);

        // Write mode prunes down to 1 file.
        let (pruned, st_prune) = handle_artifacts_command(dir.path(), "prune 1", true, false);
        assert_eq!(st_prune, "ok");
        assert!(pruned.contains("Removed **2**"));
        assert_eq!(list_artifacts(dir.path()).len(), 1);
    }
}

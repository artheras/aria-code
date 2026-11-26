//! Built-in read-only SQLite connector with strict SQL validation and workspace confinement.
//!
//! Enables logistics, finance, and engineering agents to inspect local SQLite databases
//! safely without risking mutations, multi-statement injection, or host path escapes.

use std::path::{Component, Path, PathBuf};
use std::process::Command;

use serde_json::{json, Value};

const MAX_ROWS_CAP: usize = 200;

const FORBIDDEN_KEYWORDS: &[&str] = &[
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE", "ATTACH", "DETACH",
    "VACUUM", "REINDEX", "TRUNCATE", "GRANT", "REVOKE", "COMMIT", "ROLLBACK",
];

/// Validate that `sql` is a single read-only query (`SELECT`, `WITH`, `EXPLAIN`,
/// or read-only `PRAGMA table_info(...)` / `PRAGMA table_list`).
pub fn validate_readonly_sql(sql: &str) -> Result<String, String> {
    let trimmed = sql.trim().trim_end_matches(';').trim();
    if trimmed.is_empty() {
        return Err("SQL query must not be empty".into());
    }
    if trimmed.contains(';') {
        return Err("Multi-statement SQL queries are not allowed".into());
    }
    let upper = trimmed.to_ascii_uppercase();
    let first = upper.split_whitespace().next().unwrap_or("");
    let allowed_prefix = matches!(first, "SELECT" | "WITH" | "EXPLAIN")
        || (first == "PRAGMA"
            && !upper.contains('=')
            && (upper.contains("TABLE_INFO") || upper.contains("TABLE_LIST")));
    if !allowed_prefix {
        return Err(format!(
            "Only read-only SELECT/WITH/EXPLAIN/PRAGMA table_info queries are allowed (got `{first}`)"
        ));
    }
    for kw in FORBIDDEN_KEYWORDS {
        if upper
            .split(|c: char| !c.is_ascii_alphanumeric() && c != '_')
            .any(|tok| tok == *kw)
        {
            return Err(format!("Forbidden SQL keyword `{kw}` in read-only mode"));
        }
    }
    Ok(trimmed.to_owned())
}

fn resolve_workspace_db(workspace: &Path, db_rel: &str) -> Result<PathBuf, String> {
    let rel = Path::new(db_rel.trim());
    if db_rel.trim().is_empty()
        || rel.is_absolute()
        || rel.components().any(|c| {
            matches!(
                c,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err("Database path must be a safe relative path inside workspace".into());
    }
    let root = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let full = root.join(rel);
    let canon = full
        .canonicalize()
        .map_err(|e| format!("Database not found: {e}"))?;
    if canon != root && !canon.starts_with(&root) {
        return Err("Database path escapes workspace".into());
    }
    Ok(canon)
}

/// Execute a validated read-only SQL query against a workspace-local SQLite file.
#[allow(dead_code)]
pub fn query_sqlite_readonly(
    workspace: &Path,
    db_rel: &str,
    sql: &str,
    max_rows: usize,
) -> Result<Value, String> {
    let clean_sql = validate_readonly_sql(sql)?;
    let db_path = resolve_workspace_db(workspace, db_rel)?;
    let cap = max_rows.clamp(1, MAX_ROWS_CAP);

    let py_script = r#"
import json, sqlite3, sys
db_path, query, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
try:
    cur = conn.execute(query)
    cols = [d[0] for d in (cur.description or [])]
    rows = cur.fetchmany(cap + 1)
    truncated = len(rows) > cap
    if truncated:
        rows = rows[:cap]
    print(json.dumps({"columns": cols, "rows": [list(r) for r in rows], "row_count": len(rows), "truncated": truncated}, default=str))
finally:
    conn.close()
"#;

    let py = if cfg!(windows) { "python" } else { "python3" };
    let out = Command::new(py)
        .args([
            "-c",
            py_script,
            &db_path.to_string_lossy(),
            &clean_sql,
            &cap.to_string(),
        ])
        .output()
        .map_err(|e| format!("Failed to invoke SQLite reader: {e}"))?;

    if !out.status.success() {
        return Err(format!(
            "SQLite query failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    serde_json::from_slice::<Value>(&out.stdout)
        .map_err(|e| format!("Invalid SQLite JSON output: {e}"))
        .map(|data| json!({ "ok": true, "data": data }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_readonly_sql_and_blocks_mutations() {
        assert!(validate_readonly_sql("SELECT id, name FROM orders LIMIT 10;").is_ok());
        assert!(validate_readonly_sql("WITH cte AS (SELECT 1) SELECT * FROM cte").is_ok());
        assert!(validate_readonly_sql("PRAGMA table_info(orders)").is_ok());
        assert!(validate_readonly_sql("DROP TABLE orders").is_err());
        assert!(validate_readonly_sql("SELECT 1; DELETE FROM orders").is_err());
        assert!(validate_readonly_sql("INSERT INTO orders VALUES (1)").is_err());
    }

    #[test]
    fn query_sqlite_readonly_reads_rows_and_blocks_traversal() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("demo.sqlite");
        let py = if cfg!(windows) { "python" } else { "python3" };
        let setup = Command::new(py)
            .args([
                "-c",
                "import sqlite3, sys; c=sqlite3.connect(sys.argv[1]); c.execute('CREATE TABLE items (id INT, sku TEXT)'); c.execute(\"INSERT INTO items VALUES (1, 'SKU-A'), (2, 'SKU-B')\"); c.commit(); c.close()",
                &db.to_string_lossy(),
            ])
            .status()
            .unwrap();
        assert!(setup.success());

        let res = query_sqlite_readonly(dir.path(), "demo.sqlite", "SELECT id, sku FROM items", 1)
            .expect("query");
        assert_eq!(res["data"]["row_count"], 1);
        assert_eq!(res["data"]["truncated"], true);
        assert!(query_sqlite_readonly(dir.path(), "../outside.sqlite", "SELECT 1", 10).is_err());
    }
}

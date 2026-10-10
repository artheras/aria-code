//! Native, read-only access to the same stored user state as Python Aria.
use serde_json::{json, Map, Value};
use std::{
    env,
    fs::File,
    io::Read,
    path::{Path, PathBuf},
};

pub const MAX_STATE: u64 = 8 * 1024 * 1024;

fn home() -> Result<PathBuf, String> {
    #[cfg(windows)]
    let root = env::var_os("USERPROFILE");
    #[cfg(not(windows))]
    let root = env::var_os("HOME");
    root.filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .ok_or("Cannot resolve user home directory".into())
}

fn expand(path: PathBuf) -> Result<PathBuf, String> {
    if path == Path::new("~") {
        return home();
    }
    if let Ok(relative) = path.strip_prefix("~") {
        return Ok(home()?.join(relative));
    }
    if path.to_string_lossy().starts_with('~') {
        return Err("Named-user ~ paths are unsupported; use an absolute path".into());
    }
    Ok(path)
}

pub fn root() -> Result<PathBuf, String> {
    if let Some(value) = env::var_os("ARIA_HOME").filter(|v| !v.is_empty()) {
        return expand(PathBuf::from(value));
    }
    let user = home()?;
    let legacy = user.join(".arthera");
    Ok(if legacy.exists() {
        legacy
    } else {
        user.join(".aria-code")
    })
}

pub fn paths() -> Result<Value, String> {
    let root = root()?;
    let output = match env::var_os("ARIA_USER_OUTPUT_ROOT").filter(|v| !v.is_empty()) {
        Some(path) => expand(path.into())?,
        None => home()?.join("Documents").join("Aria Code"),
    };
    Ok(
        json!({"config_dir":root,"config_file":root.join("config.json"),
        "history_file":root.join("history"),"sessions_dir":root.join("sessions"),
        "providers_file":root.join("providers.json"),"hooks_file":root.join("hooks.json"),
        "user_output_root":output,"user_generated_dir":output.join("generated"),
        "user_projects_dir":output.join("projects")}),
    )
}

pub fn read_text(path: &Path, max: u64) -> Result<String, String> {
    let metadata = path
        .symlink_metadata()
        .map_err(|e| format!("{}: {e}", path.display()))?;
    if !metadata.is_file() || metadata.len() > max {
        return Err(format!(
            "{} must be a regular file of at most {max} bytes",
            path.display()
        ));
    }
    let file = File::open(path).map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    file.take(max + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > max {
        return Err("State file exceeds size limit".into());
    }
    String::from_utf8(bytes).map_err(|_| "State file must contain UTF-8".into())
}

pub fn read_json(path: &Path, max: u64) -> Result<Value, String> {
    serde_json::from_str(&read_text(path, max)?)
        .map_err(|e| format!("Invalid JSON in {}: {e}", path.display()))
}

pub fn config() -> Result<Value, String> {
    let path = root()?.join("config.json");
    let exists = path.exists();
    let data = if exists {
        read_json(&path, 1024 * 1024)?
    } else {
        json!({})
    };
    let object = data
        .as_object()
        .ok_or("Stored configuration must be a JSON object")?;
    // A positive list: never print credentials, nested provider data, hooks,
    // arbitrary URLs, or newly introduced secret fields by accident.
    let mut settings = Map::new();
    for key in [
        "model",
        "local_provider",
        "local_mode",
        "ui_lang",
        "banner_mode",
        "thinking_mode",
        "auto_update_check",
        "max_agent_iterations",
    ] {
        if let Some(value) = object.get(key).filter(|v| !v.is_object() && !v.is_array()) {
            settings.insert(key.into(), value.clone());
        }
    }
    Ok(
        json!({"path":path,"exists":exists,"scope":"stored_user_config",
        "effective":false,"settings":settings}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_oversized_and_non_utf8_state() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state");
        std::fs::write(&path, b"12345").unwrap();
        assert!(read_text(&path, 4).is_err());
        std::fs::write(&path, [0xff]).unwrap();
        assert!(read_text(&path, 4).is_err());
        assert!(read_text(dir.path(), 4).is_err());
    }
}

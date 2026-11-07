//! Native update metadata and managed-release rollback operations.
use crate::state;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use flate2::read::GzDecoder;
use serde_json::{json, Value};
use sha2::{Digest, Sha256, Sha512};
use std::{
    env, fs,
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    process::Command,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use ureq::tls::{RootCerts, TlsConfig};

pub const RELEASE_URL: &str = "https://api.github.com/repos/artheras/aria-code/releases/latest";
pub const NPM_URL: &str = "https://registry.npmjs.org/@artheras%2Faria-code/latest";
const CACHE_TTL: f64 = 86_400.0;
const MAX_METADATA: u64 = 2 * 1024 * 1024;
const MAX_ARCHIVE: u64 = 768 * 1024 * 1024;
const MAX_EXPANDED: u64 = 2 * 1024 * 1024 * 1024;
const PRODUCT_PREFIX: &str = "aria-code ";
const LEGACY: [[u64; 3]; 8] = [
    [4, 1, 3],
    [4, 1, 4],
    [4, 1, 7],
    [4, 2, 0],
    [4, 3, 0],
    [4, 4, 0],
    [4, 4, 1],
    [4, 4, 2],
];

#[derive(Debug)]
pub struct Check {
    pub current: String,
    pub channel: String,
    pub offline: bool,
    pub refresh: bool,
    pub timeout: Duration,
}

pub fn parse_version(value: &str) -> Option<[u64; 3]> {
    let text = value.trim().strip_prefix('v').unwrap_or(value.trim());
    let fields: Vec<_> = text.split('.').collect();
    if fields.len() != 3 {
        return None;
    }
    let mut version = [0; 3];
    for (i, field) in fields.into_iter().enumerate() {
        if field.is_empty() || !field.bytes().all(|v| v.is_ascii_digit()) {
            return None;
        }
        version[i] = field.parse().ok()?;
    }
    Some(version)
}

pub fn newer(latest: &str, current: &str) -> bool {
    let (Some(latest), Some(current)) = (parse_version(latest), parse_version(current)) else {
        return false;
    };
    if LEGACY.contains(&current) && latest[0] == 0 {
        return true;
    }
    if LEGACY.contains(&latest) && current[0] == 0 {
        return false;
    }
    latest > current
}

fn source(channel: &str) -> Result<&'static str, String> {
    match channel {
        "npm" => Ok(NPM_URL),
        "native" | "pip" | "source" => Ok(RELEASE_URL),
        _ => Err("Channel must be native, npm, pip or source".into()),
    }
}

fn agent(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .https_only(true)
        .max_redirects(0)
        .timeout_global(Some(timeout))
        .tls_config(
            TlsConfig::builder()
                .root_certs(RootCerts::PlatformVerifier)
                .build(),
        )
        .build()
        .new_agent()
}

fn fetch(url: &str, timeout: Duration) -> Result<Value, String> {
    fetch_using(&agent(timeout), url)
}

fn fetch_using(agent: &ureq::Agent, url: &str) -> Result<Value, String> {
    let mut response = agent
        .get(url)
        .header("Accept", "application/json")
        .header("User-Agent", "aria-native-update-check")
        .call()
        .map_err(|e| match e {
            ureq::Error::StatusCode(code) => {
                format!("Official update metadata returned HTTP {code}")
            }
            _ => "Official update metadata request failed (network, TLS or timeout)".into(),
        })?;
    if !response.status().is_success() {
        return Err("Update metadata redirects are not accepted".into());
    }
    let mut body = Vec::new();
    response
        .body_mut()
        .as_reader()
        .take(MAX_METADATA + 1)
        .read_to_end(&mut body)
        .map_err(|_| "Update metadata body read failed or timed out")?;
    if body.len() as u64 > MAX_METADATA {
        return Err("Update metadata exceeds 2 MiB".into());
    }
    serde_json::from_slice(&body).map_err(|_| "Update source returned invalid JSON".into())
}

fn published_version(data: &Value, channel: &str) -> Result<String, String> {
    if channel == "npm" {
        if data["name"] != "@artheras/aria-code" {
            return Err("Metadata does not belong to @artheras/aria-code".into());
        }
    } else if data["draft"] != false || data["prerelease"] != false {
        return Err("Metadata is not a stable published GitHub release".into());
    }
    let field = if channel == "npm" {
        "version"
    } else {
        "tag_name"
    };
    let raw = data[field].as_str().ok_or("Missing release version")?;
    if parse_version(raw).is_none() {
        return Err("Update source did not return a stable version".into());
    }
    if channel != "npm"
        && data["html_url"] != format!("https://github.com/artheras/aria-code/releases/tag/{raw}")
    {
        return Err("Metadata does not belong to the Aria Code release".into());
    }
    Ok(raw.trim().trim_start_matches('v').into())
}

fn cache_value(data: &Value, expected_source: &str, now: f64) -> Option<(String, bool)> {
    if data["schema"] != 1 || data["source"] != expected_source {
        return None;
    }
    let latest = data["latest"].as_str()?;
    parse_version(latest)?;
    let checked = data["checked_at"].as_f64()?;
    if checked < 0.0 || checked > now {
        return None;
    }
    Some((
        latest.trim().trim_start_matches('v').into(),
        now - checked < CACHE_TTL,
    ))
}

fn save_cache(root: &Path, channel: &str, data: &Value) -> Result<(), String> {
    std::fs::create_dir_all(root).map_err(|e| e.to_string())?;
    // NamedTempFile is owner-only on Unix; persist replaces the cache atomically
    // on Windows too. A failed write never discards the previous cached result.
    let mut file = tempfile::NamedTempFile::new_in(root).map_err(|e| e.to_string())?;
    file.write_all(data.to_string().as_bytes())
        .map_err(|e| e.to_string())?;
    file.as_file().sync_all().map_err(|e| e.to_string())?;
    file.persist(root.join(format!("native_update_check-{channel}.json")))
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn output(check: &Check, latest: Option<&str>, status: &str, error: Option<String>) -> Value {
    let command = match check.channel.as_str() {
        "npm" => Some("npm install -g @artheras/aria-code@latest".into()),
        "pip" => latest.map(|v| format!("python3 -m pip install --upgrade \"aria-code=={v}\"")),
        "native" => Some("aria update".into()),
        _ => Some("git pull && python3 -m pip install -e .".into()),
    };
    json!({"product":"aria-code","current":check.current,"channel":check.channel,
        "latest":latest,"update_available":latest.map(|v|newer(v,&check.current)),
        "status":status,"error":error,"update_command":command,
        "scope":"stable_python_product_metadata","prototype_version":env!("CARGO_PKG_VERSION")})
}

pub fn check(root: &Path, options: &Check) -> Result<Value, String> {
    check_using(root, options, fetch)
}

fn app_root() -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("ARIA_CODE_HOME") {
        return expand_user(PathBuf::from(path));
    }
    Ok(home_dir()?.join(".local/share/aria-code"))
}

fn install_dir() -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("ARIA_CODE_INSTALL_DIR") {
        return expand_user(PathBuf::from(path));
    }
    Ok(home_dir()?.join(".local/bin"))
}

fn home_dir() -> Result<PathBuf, String> {
    env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| "Could not locate the user home directory".into())
}

fn expand_user(path: PathBuf) -> Result<PathBuf, String> {
    if path == Path::new("~") {
        return home_dir();
    }
    if let Ok(relative) = path.strip_prefix("~") {
        return Ok(home_dir()?.join(relative));
    }
    Ok(path)
}

fn executable(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::metadata(path).is_ok_and(|metadata| metadata.permissions().mode() & 0o111 != 0)
    }
    #[cfg(not(unix))]
    {
        path.extension().is_some_and(|extension| extension == "exe")
    }
}

fn recorded_release(root: &Path, record: &Value) -> Result<PathBuf, String> {
    let releases = root.join("releases").canonicalize().map_err(|_| {
        "No previous managed release is recorded; use aria update --to VERSION instead"
    })?;
    let relative = record["previous"]
        .as_str()
        .ok_or("No previous managed release is recorded; use aria update --to VERSION instead")?;
    let binary = releases.join(relative).canonicalize().map_err(|_| {
        "Previous release is missing or not executable; the installed version was kept"
    })?;
    binary
        .strip_prefix(&releases)
        .map_err(|_| "Previous release is outside the managed releases directory")?;
    if !executable(&binary) {
        return Err(
            "Previous release is missing or not executable; the installed version was kept".into(),
        );
    }
    Ok(binary)
}

fn write_previous(root: &Path, relative: &Path) -> Result<(), String> {
    let mut file = tempfile::NamedTempFile::new_in(root).map_err(|e| e.to_string())?;
    let value = json!({"previous": relative});
    file.write_all(value.to_string().as_bytes())
        .map_err(|e| e.to_string())?;
    file.as_file().sync_all().map_err(|e| e.to_string())?;
    file.persist(root.join("previous.json"))
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(unix)]
struct UpdateLock {
    _file: fs::File,
}

#[cfg(unix)]
fn acquire_update_lock(root: &Path) -> Result<UpdateLock, String> {
    acquire_named_lock(root, "update.lock")
}

#[cfg(unix)]
fn acquire_named_lock(root: &Path, name: &str) -> Result<UpdateLock, String> {
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::OpenOptionsExt;
    let lock = fs::OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(root.join(name))
        .map_err(|e| e.to_string())?;
    // Keep the inode permanently: unlinking a flock file can let a second
    // process lock a new inode while the first still owns the old one.
    let result = unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if result != 0 {
        return Err("Another Aria update is already in progress".into());
    }
    Ok(UpdateLock { _file: lock })
}

#[cfg(not(unix))]
struct UpdateLock;

#[cfg(not(unix))]
fn acquire_update_lock(_root: &Path) -> Result<UpdateLock, String> {
    Err("Native managed updates are supported on macOS and Linux".into())
}

#[cfg(not(unix))]
fn acquire_named_lock(_root: &Path, _name: &str) -> Result<UpdateLock, String> {
    Err("Native automatic updates are supported on macOS and Linux".into())
}

/// Restore the previous managed release without network access. The target is
/// checked before the command symlink is atomically replaced.
pub fn rollback() -> Result<PathBuf, String> {
    #[cfg(not(unix))]
    return Err("Native rollback is currently supported on macOS and Linux installs".into());

    #[cfg(unix)]
    {
        let root = app_root()?.canonicalize().map_err(|_| {
            "No previous managed release is recorded; use aria update --to VERSION instead"
        })?;
        let _lock = acquire_update_lock(&root)?;
        let record_path = root.join("previous.json");
        let record: Value = serde_json::from_slice(&fs::read(&record_path).map_err(|_| {
            "No previous managed release is recorded; use aria update --to VERSION instead"
        })?)
        .map_err(|_| "Previous release record is invalid; the installed version was kept")?;
        let binary = recorded_release(&root, &record)?;
        let output = Command::new(&binary)
            .arg("--version")
            .output()
            .map_err(|_| {
                "Previous release failed its version check; the installed version was kept"
            })?;
        let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let raw_version = version
            .strip_prefix(PRODUCT_PREFIX)
            .ok_or("Previous release failed its version check; the installed version was kept")?;
        if !output.status.success() || parse_version(raw_version).is_none() {
            return Err(
                "Previous release failed its version check; the installed version was kept".into(),
            );
        }

        let command = install_dir()?.join("aria-code");
        let current = command.canonicalize().ok().and_then(|path| {
            path.strip_prefix(root.join("releases").canonicalize().ok()?)
                .ok()
                .map(Path::to_path_buf)
        });
        let temp_link =
            command.with_file_name(format!(".aria-code-rollback-{}", std::process::id()));
        let _ = fs::remove_file(&temp_link);
        std::os::unix::fs::symlink(&binary, &temp_link).map_err(|e| e.to_string())?;
        if let Some(current) = current {
            if let Err(error) = write_previous(&root, &current) {
                let _ = fs::remove_file(&temp_link);
                return Err(error);
            }
        }
        if let Err(error) = fs::rename(&temp_link, &command) {
            let _ = fs::remove_file(&temp_link);
            return Err(format!(
                "Could not atomically switch the CLI; installed version was kept: {error}"
            ));
        }
        Ok(command)
    }
}

fn asset_agent(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .https_only(true)
        .max_redirects(5)
        .timeout_global(Some(timeout))
        .tls_config(
            TlsConfig::builder()
                .root_certs(RootCerts::PlatformVerifier)
                .build(),
        )
        .build()
        .new_agent()
}

fn download_to(url: &str, target: &Path, max_bytes: u64) -> Result<(), String> {
    let agent = asset_agent(Duration::from_secs(300));
    let part = target.with_extension("part");
    let state = target.with_extension("part.json");
    let mut last_error = "Release download failed".to_string();
    let mut metadata: Value = fs::read(&state)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .filter(|value: &Value| value["url"] == url)
        .unwrap_or_else(|| {
            let _ = fs::remove_file(&part);
            json!({"url":url})
        });
    for attempt in 0..3 {
        let result = (|| {
            let offset = fs::metadata(&part).map(|meta| meta.len()).unwrap_or(0);
            if offset > max_bytes {
                let _ = fs::remove_file(&part);
                let _ = fs::remove_file(&state);
                return Err("Partial release download exceeds its size limit".to_string());
            }
            let mut request = agent
                .get(url)
                .header("Accept", "*/*")
                .header("Accept-Encoding", "identity")
                .header("User-Agent", "aria-code-updater");
            if offset > 0 {
                request = request.header("Range", &format!("bytes={offset}-"));
                if let Some(validator) = metadata["validator"].as_str() {
                    request = request.header("If-Range", validator);
                }
            }
            let mut response = match request.call() {
                Ok(response) => response,
                Err(ureq::Error::StatusCode(416)) if offset > 0 => {
                    let _ = fs::remove_file(&part);
                    let _ = fs::remove_file(&state);
                    metadata = json!({"url":url});
                    return Err("Saved partial rejected; restarting release download".into());
                }
                Err(_) => {
                    return Err(
                        "Release download failed (network, TLS, redirect or timeout)".into(),
                    )
                }
            };
            if !response.status().is_success() {
                return Err(format!(
                    "Release server returned HTTP {}",
                    response.status()
                ));
            }
            let headers = response.headers();
            let content_length = headers
                .get("content-length")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<u64>().ok());
            let header = |name: &str| {
                headers
                    .get(name)
                    .and_then(|value| value.to_str().ok())
                    .map(str::to_string)
            };
            let validator = header("etag")
                .filter(|value| !value.starts_with("W/"))
                .or_else(|| header("last-modified"));
            let mut total = content_length;
            let mut received = 0_u64;
            let append = response.status() == 206;
            if append {
                let range = header("content-range")
                    .ok_or("Release server returned an invalid resume range")?;
                let span = range
                    .strip_prefix("bytes ")
                    .ok_or("Release server returned an invalid resume range")?;
                let (positions, declared_total) = span
                    .split_once('/')
                    .ok_or("Release server returned an invalid resume range")?;
                let (start, end) = positions
                    .split_once('-')
                    .ok_or("Release server returned an invalid resume range")?;
                let start = start.parse::<u64>().map_err(|_| "Invalid resume range")?;
                let end = end.parse::<u64>().map_err(|_| "Invalid resume range")?;
                let declared_total = declared_total
                    .parse::<u64>()
                    .map_err(|_| "Invalid resume size")?;
                if offset == 0
                    || start != offset
                    || end < start
                    || end >= declared_total
                    || declared_total > max_bytes
                    || metadata["total"]
                        .as_u64()
                        .is_some_and(|saved| saved != declared_total)
                    || metadata["validator"]
                        .as_str()
                        .zip(validator.as_deref())
                        .is_some_and(|(saved, current)| saved != current)
                {
                    let _ = fs::remove_file(&part);
                    let _ = fs::remove_file(&state);
                    return Err("Release changed or returned an invalid resume range".into());
                }
                received = offset;
                total = Some(declared_total);
            } else if content_length.is_some_and(|size| size > max_bytes) {
                return Err("Release asset exceeds its download size limit".into());
            }
            if total.is_some_and(|size| size > max_bytes) {
                return Err("Release asset exceeds its download size limit".into());
            }
            metadata = json!({"url":url,"validator":validator,"total":total});
            let mut state_file =
                tempfile::NamedTempFile::new_in(target.parent().ok_or("Invalid download path")?)
                    .map_err(|e| e.to_string())?;
            state_file
                .write_all(metadata.to_string().as_bytes())
                .map_err(|e| e.to_string())?;
            state_file.as_file().sync_all().map_err(|e| e.to_string())?;
            state_file
                .persist(&state)
                .map_err(|e| e.error.to_string())?;
            let mut output = fs::OpenOptions::new()
                .create(true)
                .write(true)
                .append(append)
                .truncate(!append)
                .open(&part)
                .map_err(|e| e.to_string())?;
            let remaining = max_bytes.saturating_sub(received).saturating_add(1);
            let size = std::io::copy(
                &mut response.body_mut().as_reader().take(remaining),
                &mut output,
            )
            .map_err(|e| e.to_string())?;
            received = received.saturating_add(size);
            if received > max_bytes {
                let _ = fs::remove_file(&part);
                let _ = fs::remove_file(&state);
                return Err("Release asset exceeds its download size limit".into());
            }
            output.sync_all().map_err(|e| e.to_string())?;
            if total.is_some_and(|expected| expected != received) {
                return Err("Incomplete release transfer; saved bytes retained for resume".into());
            }
            fs::rename(&part, target).map_err(|e| e.to_string())?;
            let _ = fs::remove_file(&state);
            Ok(())
        })();
        match result {
            Ok(()) => return Ok(()),
            Err(error) => {
                last_error = error;
                if last_error.contains("Saved partial rejected") {
                    continue;
                }
                if attempt < 2 {
                    thread::sleep(Duration::from_secs(1 << attempt));
                }
            }
        }
    }
    Err(last_error)
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut digest = Sha256::new();
    let mut block = [0_u8; 1024 * 1024];
    loop {
        let size = file.read(&mut block).map_err(|e| e.to_string())?;
        if size == 0 {
            break;
        }
        digest.update(&block[..size]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn sha512_file(path: &Path) -> Result<Vec<u8>, String> {
    let mut file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut digest = Sha512::new();
    let mut block = [0_u8; 1024 * 1024];
    loop {
        let size = file.read(&mut block).map_err(|e| e.to_string())?;
        if size == 0 {
            break;
        }
        digest.update(&block[..size]);
    }
    Ok(digest.finalize().to_vec())
}

fn expected_sha256(sums: &str, name: &str) -> Result<String, String> {
    for line in sums.lines() {
        let mut parts = line.split_whitespace();
        let Some(hash) = parts.next() else { continue };
        let Some(file) = parts.next() else { continue };
        if file.trim_start_matches('*') == name
            && hash.len() == 64
            && hash.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Ok(hash.to_ascii_lowercase());
        }
    }
    Err(format!("Release checksums do not include {name}"))
}

fn safe_relative(path: &Path) -> bool {
    !path.as_os_str().is_empty()
        && path
            .components()
            .all(|part| matches!(part, Component::Normal(_) | Component::CurDir))
}

fn safe_link_target(path: &Path, target: &Path, hard_link: bool) -> bool {
    if target.is_absolute()
        || target
            .components()
            .any(|part| matches!(part, Component::RootDir | Component::Prefix(_)))
    {
        return false;
    }
    let base = if hard_link {
        PathBuf::new()
    } else {
        path.parent().unwrap_or(Path::new(".")).to_path_buf()
    };
    let mut depth = base
        .components()
        .filter(|p| matches!(p, Component::Normal(_)))
        .count() as isize;
    for part in target.components() {
        match part {
            Component::Normal(_) => depth += 1,
            Component::CurDir => {}
            Component::ParentDir => {
                depth -= 1;
                if depth < 0 {
                    return false;
                }
            }
            Component::RootDir | Component::Prefix(_) => return false,
        }
    }
    true
}

fn unpack_gzip_tar(archive_path: &Path, destination: &Path) -> Result<(), String> {
    fs::create_dir_all(destination).map_err(|e| e.to_string())?;
    let input = fs::File::open(archive_path).map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(GzDecoder::new(input));
    let entries = archive
        .entries()
        .map_err(|_| "Release archive is not a valid gzip TAR".to_string())?;
    let mut expanded = 0_u64;
    let mut count = 0_u64;
    for entry in entries {
        let mut entry =
            entry.map_err(|_| "Release archive contains an invalid entry".to_string())?;
        count += 1;
        if count > 100_000 {
            return Err("Release archive contains too many entries".into());
        }
        let path = entry
            .path()
            .map_err(|_| "Release archive contains an invalid path".to_string())?
            .into_owned();
        if !safe_relative(&path) {
            return Err("Release archive contains an unsafe path".into());
        }
        let kind = entry.header().entry_type();
        if !(kind.is_file() || kind.is_dir() || kind.is_symlink() || kind.is_hard_link()) {
            return Err("Release archive contains an unexpected file type".into());
        }
        if kind.is_symlink() || kind.is_hard_link() {
            let target = entry
                .link_name()
                .map_err(|_| "Release archive contains an invalid link".to_string())?
                .ok_or("Release archive contains an empty link target")?
                .into_owned();
            if !safe_link_target(&path, &target, kind.is_hard_link()) {
                return Err("Release archive contains a link outside its install directory".into());
            }
        }
        expanded = expanded.saturating_add(
            entry
                .header()
                .size()
                .map_err(|_| "Release archive contains an invalid size".to_string())?,
        );
        if expanded > MAX_EXPANDED {
            return Err("Expanded release exceeds its size limit".into());
        }
        entry
            .unpack_in(destination)
            .map_err(|_| "Could not safely extract release archive".to_string())?;
    }
    Ok(())
}

fn platform() -> Result<(&'static str, &'static str), String> {
    let system = match env::consts::OS {
        "macos" => "macos",
        "linux" => "linux",
        _ => {
            return Err(
                "Native self-update supports macOS and Linux; use the platform installer".into(),
            )
        }
    };
    let arch = match env::consts::ARCH {
        "aarch64" => "arm64",
        "x86_64" => "x64",
        _ => return Err("No native release is published for this CPU architecture".into()),
    };
    Ok((system, arch))
}

fn release_asset<'a>(release: &'a Value, name: &str) -> Option<&'a Value> {
    release["assets"]
        .as_array()?
        .iter()
        .find(|asset| asset["name"] == name)
}

fn github_asset_url(release: &Value, name: &str, version: &str) -> Result<String, String> {
    let asset = release_asset(release, name)
        .ok_or_else(|| format!("Release is not ready for {name}; installed version is kept"))?;
    let url = asset["browser_download_url"]
        .as_str()
        .ok_or("Release asset URL is missing")?;
    let prefix = format!("https://github.com/artheras/aria-code/releases/download/v{version}/");
    if !url.starts_with(&prefix) || url[prefix.len()..].contains('/') {
        return Err("Release asset URL does not belong to the requested Aria Code version".into());
    }
    Ok(url.into())
}

fn verify_binary(binary: &Path, version: &str) -> Result<(), String> {
    let output = Command::new(binary)
        .arg("--version")
        .output()
        .map_err(|_| "Downloaded binary could not be started; installed version was kept")?;
    if !output.status.success()
        || String::from_utf8_lossy(&output.stdout).trim() != format!("{PRODUCT_PREFIX}{version}")
    {
        return Err("Downloaded binary version does not match release metadata".into());
    }
    let help = Command::new(binary)
        .arg("--help")
        .output()
        .map_err(|_| "Downloaded CLI failed its startup check; installed version was kept")?;
    if !help.status.success() || help.stdout.is_empty() {
        return Err("Downloaded CLI failed its startup check; installed version was kept".into());
    }
    Ok(())
}

fn activate(root: &Path, command_dir: &Path, binary: &Path) -> Result<PathBuf, String> {
    fs::create_dir_all(command_dir).map_err(|e| e.to_string())?;
    let command_dir = command_dir.canonicalize().map_err(|e| e.to_string())?;
    let alias = command_dir.join("aria");
    if !alias.exists() {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&alias)
            .map_err(|e| format!("Could not prepare the aria command alias: {e}"))?;
        file.write_all(b"#!/bin/sh\nif [ \"${1-}\" = code ]; then shift; fi\nexec \"$(dirname \"$0\")/aria-code\" \"$@\"\n")
            .map_err(|e| format!("Could not write the aria command alias: {e}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&alias, fs::Permissions::from_mode(0o755))
                .map_err(|e| format!("Could not set aria command alias permissions: {e}"))?;
        }
    }
    let command = command_dir.join("aria-code");
    let releases = root
        .join("releases")
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let current = if command.exists() || command.symlink_metadata().is_ok() {
        let resolved = command.canonicalize().map_err(|_| {
            "Existing aria-code command cannot be resolved; installed version was kept"
        })?;
        match resolved.strip_prefix(&releases) {
            Ok(relative) => Some(relative.to_path_buf()),
            Err(_) => {
                // Older installers placed a regular executable directly in
                // the command directory. Verify and preserve it before
                // switching so the first native update remains reversible.
                let output = Command::new(&resolved)
                    .arg("--version")
                    .output()
                    .map_err(|_| {
                        "Existing aria-code command failed verification; installed version was kept"
                    })?;
                let version = String::from_utf8_lossy(&output.stdout)
                    .trim()
                    .strip_prefix(PRODUCT_PREFIX)
                    .map(str::to_string)
                    .ok_or(
                        "Existing command is not an Aria Code release; installed version was kept",
                    )?;
                if !output.status.success() || parse_version(&version).is_none() {
                    return Err("Existing aria-code command failed verification; installed version was kept".into());
                }
                verify_binary(&resolved, &version)?;
                let staging = tempfile::Builder::new()
                    .prefix(".legacy-")
                    .tempdir_in(&releases)
                    .map_err(|e| e.to_string())?;
                let preserved = staging.path().join("aria-code-bin");
                fs::copy(&resolved, &preserved).map_err(|e| e.to_string())?;
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    fs::set_permissions(&preserved, fs::Permissions::from_mode(0o755))
                        .map_err(|e| e.to_string())?;
                }
                verify_binary(&preserved, &version)?;
                let saved = staging.keep();
                let destination = releases.join(format!(
                    "legacy-v{version}-{}-{}",
                    std::process::id(),
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_nanos()
                ));
                fs::rename(&saved, &destination).map_err(|e| e.to_string())?;
                Some(
                    destination
                        .join("aria-code-bin")
                        .strip_prefix(&releases)
                        .map_err(|e| e.to_string())?
                        .to_path_buf(),
                )
            }
        }
    } else {
        None
    };
    let link = command.with_file_name(format!(".aria-code-update-{}", std::process::id()));
    let _ = fs::remove_file(&link);
    #[cfg(unix)]
    std::os::unix::fs::symlink(binary, &link).map_err(|e| e.to_string())?;
    #[cfg(not(unix))]
    return Err("Native symlink activation is supported on macOS and Linux".into());
    let previous_record = root.join("previous.json");
    let previous_snapshot = fs::read(&previous_record).ok();
    if let Some(previous) = current {
        if let Err(error) = write_previous(root, &previous) {
            let _ = fs::remove_file(&link);
            return Err(error);
        }
    }
    if let Err(error) = fs::rename(&link, &command) {
        let _ = fs::remove_file(&link);
        if let Some(snapshot) = previous_snapshot {
            if let Ok(mut record) = tempfile::NamedTempFile::new_in(root) {
                if record.write_all(&snapshot).is_ok() {
                    let _ = record.persist(&previous_record);
                }
            }
        } else {
            let _ = fs::remove_file(&previous_record);
        }
        return Err(format!(
            "Could not atomically switch the CLI; installed version was kept: {error}"
        ));
    }
    Ok(command)
}

fn npm_fallback(
    version: &str,
    system: &str,
    arch: &str,
    cache: &Path,
    stage: &Path,
) -> Result<PathBuf, String> {
    let npm_system = if system == "macos" { "darwin" } else { system };
    let package = format!("aria-code-{npm_system}-{arch}");
    let metadata_url = format!("https://registry.npmjs.org/@artheras%2F{package}/{version}");
    let metadata = fetch(&metadata_url, Duration::from_secs(15))?;
    if metadata["name"] != format!("@artheras/{package}") || metadata["version"] != version {
        return Err("npm fallback does not match this Aria Code platform release".into());
    }
    let expected_url =
        format!("https://registry.npmjs.org/@artheras/{package}/-/{package}-{version}.tgz");
    if metadata.pointer("/dist/tarball").and_then(Value::as_str) != Some(&expected_url) {
        return Err("npm fallback URL does not belong to this Aria Code release".into());
    }
    let integrity = metadata
        .pointer("/dist/integrity")
        .and_then(Value::as_str)
        .ok_or("npm fallback has no SHA-512 integrity")?;
    let encoded = integrity
        .strip_prefix("sha512-")
        .ok_or("npm fallback has no SHA-512 integrity")?;
    let expected = STANDARD
        .decode(encoded)
        .map_err(|_| "Invalid npm fallback integrity")?;
    if expected.len() != 64 {
        return Err("Invalid npm fallback integrity".into());
    }
    let archive = cache.join(format!("{package}-{version}.tgz"));
    download_to(&expected_url, &archive, MAX_ARCHIVE)?;
    if sha512_file(&archive)? != expected {
        let _ = fs::remove_file(&archive);
        return Err("npm fallback checksum verification failed".into());
    }
    let unpacked = stage.join("npm");
    unpack_gzip_tar(&archive, &unpacked)?;
    let binary = unpacked.join("package/bin/aria-code-bin");
    if !executable(&binary) {
        return Err("npm fallback package does not contain a runnable Aria Code binary".into());
    }
    Ok(binary)
}

/// Download, verify, stage and activate one stable native release. Existing
/// binaries stay in place until the verified release passes both startup probes.
pub fn install(version: &str) -> Result<PathBuf, String> {
    let version = version.trim_start_matches('v');
    if parse_version(version).is_none() {
        return Err("Version must look like 0.110.0 or v0.110.0".into());
    }
    let (system, arch) = platform()?;
    let metadata_url =
        format!("https://api.github.com/repos/artheras/aria-code/releases/tags/v{version}");
    let release = fetch(&metadata_url, Duration::from_secs(20))?;
    if published_version(&release, "native")? != version {
        return Err("Release metadata does not match the requested stable version".into());
    }
    let archive_name = format!("aria-code-{system}-{arch}.tar.gz");
    let archive_url = github_asset_url(&release, &archive_name, version)?;
    let sums_url = github_asset_url(&release, "SHA256SUMS", version)?;
    let root = app_root()?;
    fs::create_dir_all(root.join("releases")).map_err(|e| e.to_string())?;
    fs::create_dir_all(root.join("downloads").join(format!("v{version}")))
        .map_err(|e| e.to_string())?;
    let root = root.canonicalize().map_err(|e| e.to_string())?;
    let _lock = acquire_update_lock(&root)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&root, fs::Permissions::from_mode(0o700));
        let _ = fs::set_permissions(root.join("downloads"), fs::Permissions::from_mode(0o700));
    }
    let cache = root.join("downloads").join(format!("v{version}"));
    let archive = cache.join(&archive_name);
    let sums_path = cache.join("SHA256SUMS");
    download_to(&sums_url, &sums_path, 1024 * 1024)?;
    let sums = fs::read_to_string(&sums_path).map_err(|e| e.to_string())?;
    let expected = expected_sha256(&sums, &archive_name)?;
    let cached_ok = archive.is_file() && sha256_file(&archive).is_ok_and(|hash| hash == expected);
    if archive.exists() && !cached_ok {
        fs::remove_file(&archive).map_err(|e| e.to_string())?;
    }
    let git_error = if cached_ok {
        None
    } else {
        match download_to(&archive_url, &archive, MAX_ARCHIVE) {
            Ok(()) => {
                if sha256_file(&archive)? != expected {
                    let _ = fs::remove_file(&archive);
                    return Err(format!(
                        "Release checksum verification failed for {archive_name}"
                    ));
                }
                None
            }
            Err(error) => Some(error),
        }
    };
    let releases = root.join("releases");
    let staging = tempfile::Builder::new()
        .prefix(".update-")
        .tempdir_in(&releases)
        .map_err(|e| e.to_string())?;
    let unpacked = staging.path().join("unpacked");
    let binary = if let Some(error) = git_error {
        eprintln!("GitHub transfer failed ({error}); trying the verified official npm artifact.");
        let binary = npm_fallback(version, system, arch, &cache, staging.path())?;
        let target = unpacked.join("aria-code-bin");
        fs::create_dir_all(&unpacked).map_err(|e| e.to_string())?;
        fs::copy(&binary, &target).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&target, fs::Permissions::from_mode(0o755))
                .map_err(|e| e.to_string())?;
        }
        target
    } else {
        unpack_gzip_tar(&archive, &unpacked)?;
        unpacked.join("aria-code-bin/aria-code-bin")
    };
    if !executable(&binary) {
        return Err("Release archive does not contain a runnable Aria Code binary".into());
    }
    verify_binary(&binary, version)?;
    let active = releases.join(format!(
        "v{version}-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    fs::rename(&unpacked, &active).map_err(|e| e.to_string())?;
    let active_binary = if active.join("aria-code-bin/aria-code-bin").is_file() {
        active.join("aria-code-bin/aria-code-bin")
    } else {
        active.join("aria-code-bin")
    };
    if let Err(error) = verify_binary(&active_binary, version) {
        let _ = fs::remove_dir_all(&active);
        return Err(error);
    }
    let command_dir = install_dir()?;
    match activate(&root, &command_dir, &active_binary) {
        Ok(command) => Ok(command),
        Err(error) => {
            let _ = fs::remove_dir_all(&active);
            Err(error)
        }
    }
}

fn remember_auto_failure(root: &Path, version: &str, now: u64) -> Result<(), String> {
    let mut file = tempfile::NamedTempFile::new_in(root).map_err(|e| e.to_string())?;
    file.write_all(json!({"failed": version, "at": now}).to_string().as_bytes())
        .map_err(|e| e.to_string())?;
    file.as_file().sync_all().map_err(|e| e.to_string())?;
    file.persist(root.join("auto-update.json"))
        .map_err(|e| e.to_string())?;
    Ok(())
}

/// Check and install one newer stable native release for a startup worker.
/// The caller keeps UI scheduling and the user's opt-out preference.
pub fn auto_install(current: &str) -> Result<Value, String> {
    if parse_version(current).is_none() {
        return Err("--current must be the installed Aria product version".into());
    }
    platform()?;
    let root = app_root()?;
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let root = root.canonicalize().map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&root, fs::Permissions::from_mode(0o700));
    }
    let _auto_lock = match acquire_named_lock(&root, "auto-update-native.lock") {
        Ok(lock) => lock,
        Err(error) if error.contains("already in progress") => {
            return Ok(json!({"status":"skipped","reason":"another update is active"}));
        }
        Err(error) => return Err(error),
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_secs();
    let options = Check {
        current: current.to_string(),
        channel: "native".into(),
        offline: false,
        refresh: false,
        timeout: Duration::from_secs(4),
    };
    let metadata = check(&state::root()?, &options)?;
    let Some(latest) = metadata["latest"]
        .as_str()
        .filter(|v| parse_version(v).is_some())
    else {
        return Ok(json!({"status":"skipped","reason":"update metadata unavailable"}));
    };
    if !newer(latest, current) {
        return Ok(json!({"status":"skipped","reason":"already current"}));
    }
    if let Ok(bytes) = fs::read(root.join("auto-update.json")) {
        if let Ok(saved) = serde_json::from_slice::<Value>(&bytes) {
            let failed = saved["failed"].as_str() == Some(latest);
            let at = saved["at"].as_u64().unwrap_or_default();
            if failed && now.saturating_sub(at) < 86_400 {
                return Ok(json!({"status":"skipped","reason":"recent install failure"}));
            }
        }
    }
    match install(latest) {
        Ok(command) => {
            let _ = fs::remove_file(root.join("auto-update.json"));
            Ok(json!({"status":"installed","version":latest,"command":command}))
        }
        Err(error) if error.contains("Another Aria update is already in progress") => {
            Ok(json!({"status":"skipped","reason":"another update is active"}))
        }
        Err(error) => {
            let _ = remember_auto_failure(&root, latest, now);
            Ok(json!({"status":"failed","version":latest,"error":error}))
        }
    }
}

fn check_using(
    root: &Path,
    options: &Check,
    fetcher: impl Fn(&str, Duration) -> Result<Value, String>,
) -> Result<Value, String> {
    if parse_version(&options.current).is_none() {
        return Err("--current must be the installed Aria product version (e.g. 0.126.0), not the Rust crate version".into());
    }
    let source = source(&options.channel)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_secs_f64();
    let cache = state::read_json(
        &root.join(format!("native_update_check-{}.json", options.channel)),
        MAX_METADATA,
    )
    .ok()
    .and_then(|v| cache_value(&v, source, now));
    if options.offline || (!options.refresh && cache.as_ref().is_some_and(|(_, fresh)| *fresh)) {
        return Ok(match cache {
            Some((latest, fresh)) => output(
                options,
                Some(&latest),
                if fresh { "cached" } else { "stale" },
                None,
            ),
            None => output(options, None, "unknown", None),
        });
    }
    let started = Instant::now();
    let result = (|| {
        let data = fetcher(source, options.timeout)?;
        let latest = published_version(&data, &options.channel)?;
        if options.channel == "pip" {
            let remaining = options
                .timeout
                .checked_sub(started.elapsed())
                .ok_or("Update check timed out")?;
            let package = fetcher(
                &format!("https://pypi.org/pypi/aria-code/{latest}/json"),
                remaining,
            )?;
            if package.pointer("/info/name").and_then(Value::as_str) != Some("aria-code")
                || package.pointer("/info/version").and_then(Value::as_str) != Some(&latest)
            {
                return Err(
                    "The GitHub release is not ready on PyPI; installed version is kept".into(),
                );
            }
        }
        Ok::<_, String>(latest)
    })();
    match result {
        Ok(latest) => {
            let warning = save_cache(
                root,
                &options.channel,
                &json!({"schema":1,"source":source,"latest":latest,"checked_at":now}),
            )
            .err()
            .map(|_| "Update metadata was checked but its cache could not be saved".into());
            Ok(output(options, Some(&latest), "checked", warning))
        }
        Err(error) => Ok(output(
            options,
            cache.as_ref().map(|(latest, _)| latest.as_str()),
            if cache.is_some() {
                "stale"
            } else {
                "unavailable"
            },
            Some(error),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn options(channel: &str) -> Check {
        Check {
            current: "0.126.0".into(),
            channel: channel.into(),
            offline: false,
            refresh: false,
            timeout: Duration::from_secs(4),
        }
    }
    fn release(version: &str) -> Value {
        json!({"tag_name":version,"draft":false,"prerelease":false,"html_url":format!("https://github.com/artheras/aria-code/releases/tag/{version}")})
    }
    #[test]
    fn stable_and_legacy_versions() {
        assert!(newer("v0.126.0", "4.4.2"));
        assert!(!newer("4.4.2", "0.126.0"));
        assert!(newer("4.5.0", "0.126.0"));
        for v in ["1.2.3-beta", "other 1.2.3", "1.2", "1.2.3+build", "-1.2.3"] {
            assert!(parse_version(v).is_none());
        }
    }
    #[test]
    fn official_metadata_cache_and_failure() {
        let root = tempfile::tempdir().unwrap();
        let options = options("native");
        let result = check_using(root.path(), &options, |url, _| {
            assert_eq!(url, RELEASE_URL);
            Ok(release("v0.127.0"))
        })
        .unwrap();
        assert_eq!(result["update_available"], true);
        let result = check_using(root.path(), &options, |_, _| {
            panic!("fresh cache must not fetch")
        })
        .unwrap();
        assert_eq!(result["status"], "cached");
        let mut options = options;
        options.refresh = true;
        let result = check_using(root.path(), &options, |_, _| Err("offline".into())).unwrap();
        assert_eq!(result["status"], "stale");
        assert_eq!(result["latest"], "0.127.0");
    }
    #[test]
    fn rejects_drafts_foreign_packages_and_unready_pypi() {
        let root = tempfile::tempdir().unwrap();
        for data in [
            json!({"tag_name":"v0.127.0","draft":true}),
            json!({"tag_name":"v0.127.0","draft":false,"prerelease":true}),
            json!({"tag_name":"v0.127.0","draft":false,"prerelease":false,"html_url":"https://example.org"}),
        ] {
            let result =
                check_using(root.path(), &options("native"), |_, _| Ok(data.clone())).unwrap();
            assert_eq!(result["status"], "unavailable");
        }
        assert!(
            published_version(&json!({"name":"aria-code","version":"0.127.0"}), "npm").is_err()
        );
        let result = check_using(root.path(), &options("pip"), |url, _| {
            if url == RELEASE_URL {
                Ok(release("v0.127.0"))
            } else {
                Ok(json!({"info":{"name":"aria-code","version":"4.4.2"}}))
            }
        })
        .unwrap();
        assert_eq!(result["status"], "unavailable");
        assert!(!root.path().join("native_update_check-pip.json").exists());
    }
    #[test]
    fn actual_http_body_limits_and_timeout() {
        use std::net::TcpListener;
        use std::thread;
        for (payload, delay, timeout) in [
            (
                vec![b'x'; MAX_METADATA as usize + 1],
                false,
                Duration::from_secs(3),
            ),
            (b"{}".to_vec(), true, Duration::from_millis(100)),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let _ = stream.read(&mut [0; 4096]);
                if delay {
                    thread::sleep(Duration::from_millis(200));
                }
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    payload.len()
                );
                let _ = stream.write_all(&payload);
            });
            let agent = ureq::Agent::config_builder()
                .proxy(None)
                .timeout_global(Some(timeout))
                .build()
                .new_agent();
            assert!(fetch_using(&agent, &url).is_err());
            server.join().unwrap();
        }
    }
}

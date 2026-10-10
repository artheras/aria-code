//! Explicit native metadata checks. Never installs, downloads or runs a release.
use crate::state;
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::Path,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use ureq::tls::{RootCerts, TlsConfig};

pub const RELEASE_URL: &str = "https://api.github.com/repos/artheras/aria-code/releases/latest";
pub const NPM_URL: &str = "https://registry.npmjs.org/@artheras%2Faria-code/latest";
const CACHE_TTL: f64 = 86_400.0;
const MAX_METADATA: u64 = 2 * 1024 * 1024;
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

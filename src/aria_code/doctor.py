"""Reusable health checks for Aria Code installations."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from aria_code.packages.aria_core.paths import aria_home


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str = ""
    suggestion: str = ""


@dataclass(frozen=True)
class DoctorReport:
    checks: List[DoctorCheck] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for check in self.checks if check.status == "ok")

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.status == "warn")

    @property
    def errors(self) -> int:
        return sum(1 for check in self.checks if check.status == "err")

    @property
    def status(self) -> str:
        if self.errors:
            return "err"
        if self.warnings:
            return "warn"
        return "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "warnings": self.warnings,
            "errors": self.errors,
            "checks": [check.__dict__ for check in self.checks],
        }


# `npm run repair` used to be here as the npm-install branch of this hint. The
# npm package no longer has that script, or any script but `test`: it was
# rewritten from a postinstall that built a venv into a dispatcher that runs a
# prebuilt binary. An npm install therefore has no venv to rebuild, and this
# hint only applies to a repo checkout.
VENV_REBUILD_HINT = (
    "Rebuild the venv with your current Python: bash install.sh --rebuild "
    "(repo checkout), or reinstall with pip install -U \"aria-code<4\"."
)

# What the npm package is today: a zero-dependency launcher that execs a
# prebuilt binary delivered as an optionalDependency, one package per platform.
# Nothing to repair in place — the fix for every broken state is a reinstall.
NPM_REINSTALL_HINT = "Reinstall with: npm install -g @artheras/aria-code@latest"


def analyze_python_drift(
    recorded_version: str,
    running_version: str,
    home_exists: bool,
) -> DoctorCheck:
    """Pure drift analysis for a venv's pyvenv.cfg vs the running interpreter.

    Two real-world failure signals:
      • the venv's base interpreter (``home =``) was removed — e.g. Homebrew
        upgraded python@3.13 → 3.14 and deleted the old keg; the venv breaks
        on the next symlink resolution even though it still "exists";
      • the recorded creation version differs from the interpreter actually
        running — stale metadata after a relink, or the entrypoint resolved a
        different Python than the one the venv was built with.
    """
    if not home_exists:
        return _check(
            "python_venv",
            "err",
            f"venv base interpreter is gone (built with {recorded_version or 'unknown'})",
            VENV_REBUILD_HINT,
        )
    rec = tuple((recorded_version or "").split(".")[:2])
    run = tuple((running_version or "").split(".")[:2])
    if rec and run and rec != run:
        return _check(
            "python_venv",
            "warn",
            f"venv was created with Python {recorded_version} but {running_version} is running",
            VENV_REBUILD_HINT,
        )
    return _check("python_venv", "ok", f"venv matches running Python {running_version}")


def _check_python_drift() -> Optional[DoctorCheck]:
    """Read the active venv's pyvenv.cfg; None when not running inside a venv."""
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return None
    cfg = Path(sys.prefix) / "pyvenv.cfg"
    if not cfg.exists():
        return None
    recorded, home = "", ""
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if key == "version":
                recorded = value.strip()
            elif key == "home":
                home = value.strip()
    except Exception:
        return None
    running = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return analyze_python_drift(recorded, running, home_exists=bool(home) and Path(home).exists())


def _check(name: str, status: str, detail: str = "", suggestion: str = "") -> DoctorCheck:
    return DoctorCheck(name=name, status=status, detail=detail, suggestion=suggestion)


def _format_age(seconds: float | int | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h{rem:02d}m"


def _has_module(name: str) -> bool:
    # find_spec on a dotted name imports the parent package first, and raises
    # rather than returning None when that parent is absent ("google.genai"
    # with no "google" installed). Absent is the answer either way.
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _is_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".aria-doctor-", dir=path, delete=True) as handle:
            handle.write(b"ok")
        return True, str(path)
    except Exception as exc:
        return False, str(exc)


def _is_path_ready(path: Path, *, expect_file: bool = False) -> tuple[str, str]:
    """Read-only path readiness check for runtime/config/cache diagnostics."""
    try:
        path = path.expanduser()
        if path.exists():
            if expect_file and not path.is_file():
                return "err", f"{path} exists but is not a file"
            if not expect_file and not path.is_dir():
                return "err", f"{path} exists but is not a directory"
            writable_target = path.parent if expect_file else path
            if os.access(writable_target, os.W_OK):
                return "ok", str(path)
            return "warn", f"{path} exists but may not be writable"
        parent = path.parent
        if parent.exists() and os.access(parent, os.W_OK):
            return "warn", f"{path} not created yet; parent writable"
        return "warn", f"{path} not found"
    except Exception as exc:
        return "err", str(exc)


def _capture_cmd(cmd: list[str], timeout: float = 2.0) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return result.returncode, (result.stdout or result.stderr or "").strip()
    except Exception as exc:
        return 1, str(exc)


def _expand_home(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value == "~":
        return str(Path.home())
    if value.startswith("~/") or value.startswith("~\\"):
        return str(Path.home() / value[2:])
    return value


def _npm_config_home() -> tuple[str, str]:
    for key in ("npm_config_aria_code_home", "npm_config_aria_home", "npm_config_ariacode_home"):
        value = _expand_home(os.getenv(key))
        if value:
            return str(Path(value).expanduser().resolve()), f"env:{key}"
    npm = shutil.which("npm")
    if not npm:
        return "", ""
    code, out = _capture_cmd([npm, "config", "get", "aria-code:home"], timeout=1.5)
    value = _expand_home(out)
    if code == 0 and value and value.lower() not in ("undefined", "null"):
        return str(Path(value).expanduser().resolve()), "npm-config:aria-code:home"
    return "", ""


def _platform_data_dir() -> Path:
    system = platform.system().lower()
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "Aria Code"
    if system == "windows":
        return Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "AriaCode"
    return Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "aria-code"


def _platform_config_dir() -> Path:
    system = platform.system().lower()
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "Aria Code"
    if system == "windows":
        return Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "AriaCode"
    return Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "aria-code"


def _platform_cache_dir() -> Path:
    system = platform.system().lower()
    if system == "darwin":
        return Path.home() / "Library" / "Caches" / "Aria Code"
    if system == "windows":
        return Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "AriaCode" / "Cache"
    return Path(os.getenv("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "aria-code"


def _resolve_runtime_paths() -> dict[str, Any]:
    legacy = Path.home() / ".aria-code"
    source = ""
    install_raw = os.getenv("ARIA_HOME") or os.getenv("ARIA_CODE_HOME") or ""
    if install_raw:
        install_dir = Path(_expand_home(install_raw)).expanduser().resolve()
        source = "env:ARIA_HOME" if os.getenv("ARIA_HOME") else "env:ARIA_CODE_HOME"
    else:
        npm_home, npm_source = _npm_config_home()
        if npm_home:
            install_dir = Path(npm_home)
            source = npm_source
        elif legacy.exists():
            install_dir = legacy
            source = "legacy-existing"
        else:
            install_dir = _platform_data_dir()
            source = "platform-default"

    config_dir = Path(_expand_home(os.getenv("ARIA_CONFIG_DIR"))).expanduser().resolve() if os.getenv("ARIA_CONFIG_DIR") else _platform_config_dir()
    cache_dir = Path(_expand_home(os.getenv("ARIA_CACHE_DIR"))).expanduser().resolve() if os.getenv("ARIA_CACHE_DIR") else _platform_cache_dir()
    info_file = install_dir / ".npm-install-info.json"
    config_info_file = config_dir / "install.json"
    legacy_info_file = legacy / ".npm-install-info.json"
    info_candidates = []
    for candidate in (info_file, config_info_file, legacy_info_file):
        if candidate not in info_candidates:
            info_candidates.append(candidate)
    return {
        "install_dir": install_dir,
        "install_dir_source": source,
        "legacy_install_dir": legacy,
        "venv_dir": install_dir / ".venv",
        "venv_py": install_dir / ".venv" / ("Scripts/python.exe" if platform.system().lower() == "windows" else "bin/python"),
        "aria_cli": install_dir / "aria_cli.py",
        "config_dir": config_dir,
        "cache_dir": cache_dir,
        "info_file": info_file,
        "config_info_file": config_info_file,
        "legacy_info_file": legacy_info_file,
        "info_candidates": info_candidates,
    }


def _provider_key_present(module_name: str, key_fn: str) -> bool:
    """Call a client module's own key-lookup function (e.g.
    openai_image_client._api_key) rather than re-implementing env-var/
    providers.json lookup here — avoids doctor.py silently drifting out of
    sync with each module's actual lookup logic."""
    try:
        import importlib
        mod = importlib.import_module(module_name)
        result = getattr(mod, key_fn)()
        if isinstance(result, tuple):
            return all(bool(v) for v in result)
        return bool(result)
    except Exception:
        return False


def integration_checks() -> List[DoctorCheck]:
    """Health checks for the report/media generation integrations added
    across the MCP-exposure work: ffmpeg (video editing), faster-whisper +
    opencv (video analysis), self-hosted image generation, and the
    API-key-gated cloud providers (OpenAI images, Kling/Runway video, Canva,
    Figma). None of these are required for core aria-code usage — missing
    ones are "warn", not "err", and each points at how to fix it."""
    checks: List[DoctorCheck] = []

    if shutil.which("ffmpeg"):
        checks.append(_check("integration:ffmpeg", "ok", "found on PATH — local video editing (aria.video.*) available"))
    else:
        checks.append(_check(
            "integration:ffmpeg", "warn", "not found on PATH",
            "Install ffmpeg (e.g. `brew install ffmpeg`) to use aria.video.trim/concat/overlay_*/convert/change_speed.",
        ))

    if _has_module("faster_whisper"):
        checks.append(_check("integration:faster_whisper", "ok", "available — aria.video.transcribe ready"))
    else:
        checks.append(_check(
            "integration:faster_whisper", "warn", "optional 'video_analysis' extra not installed",
            "pip install 'aria-code[video_analysis]' to use aria.video.transcribe.",
        ))

    if _has_module("cv2"):
        checks.append(_check("integration:opencv", "ok", "available — aria.video.detect_scenes ready"))
    else:
        checks.append(_check(
            "integration:opencv", "warn", "optional 'video' extra not installed",
            "pip install 'aria-code[video]' to use aria.video.detect_scenes.",
        ))

    if _has_module("diffusers") and _has_module("torch"):
        checks.append(_check("integration:local_image_gen", "ok", "available — aria.report.generate_image_local/edit_image_local ready (no API key needed)"))
    else:
        checks.append(_check(
            "integration:local_image_gen", "warn", "optional 'image_gen' extra not installed",
            "pip install 'aria-code[image_gen]' for free local image generation, or use aria.report.generate_image (OpenAI, needs an API key).",
        ))

    key_checks = [
        ("integration:openai_images", "openai_image_client", "_api_key", "/apikey set openai sk-...", "aria.report.generate_image/edit_image"),
        ("integration:kling", "kling_video_client", "_keys", "/apikey set kling <access_key>:<secret_key>", "aria.video.generate_submit (provider=kling)"),
        ("integration:runway", "runway_video_client", "_api_key", "/apikey set runway <key>", "aria.video.generate_submit (provider=runway)"),
        ("integration:figma", "figma_client", "_token", "/apikey set figma <personal_access_token>", "aria.figma.read_file/comments"),
    ]
    for name, module_name, key_fn, setup_hint, gates in key_checks:
        if _provider_key_present(module_name, key_fn):
            checks.append(_check(name, "ok", f"key configured — {gates} usable"))
        else:
            checks.append(_check(name, "warn", "no API key configured", f"{setup_hint} to use {gates}."))

    try:
        # Read stored config directly rather than calling _access_token(),
        # which refreshes an expired token over the network — this function
        # is a local-first, no-mutation health check, not a live probe.
        from canva_client import _load_canva_config

        connected = bool(_load_canva_config().get("access_token"))
        checks.append(_check(
            "integration:canva", "ok" if connected else "warn",
            "connected — aria.report.canva_design/canva_upload_asset usable" if connected else "not connected",
            "" if connected else "Run /canva connect <client_id> <client_secret> (needs a Canva Connect app + brand template).",
        ))
    except Exception as exc:
        checks.append(_check("integration:canva", "warn", f"could not read config: {exc}", "Run /canva connect <client_id> <client_secret>."))

    return checks


def npm_platform_key(system: str = "", machine: str = "") -> str:
    """This machine's platform key, matching npm/lib/platform.js PLATFORM_KEYS.

    Returns "" for a platform with no prebuilt binary, which is a real answer:
    those users install from pip.
    """
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_part = {"darwin": "darwin", "linux": "linux", "windows": "win32"}.get(system, "")
    arch_part = {
        "x86_64": "x64", "amd64": "x64",
        "arm64": "arm64", "aarch64": "arm64",
    }.get(machine, "")
    if not os_part or not arch_part:
        return ""
    key = f"{os_part}-{arch_part}"
    # win32-arm64 and linux-arm64-on-win are not built; see PLATFORM_KEYS.
    return key if key in (
        "darwin-arm64", "darwin-x64", "linux-x64", "linux-arm64", "win32-x64"
    ) else ""


def _platform_package_check(npm: Optional[str]) -> Optional[DoctorCheck]:
    """Did this machine's prebuilt-binary package actually get installed?

    The launcher lists one package per platform as an optionalDependency, and
    npm skips an optionalDependency it cannot resolve *silently* — by design,
    that is what "optional" means. So the failure mode this catches is a
    launcher with no binary behind it, which reports only when the user tries
    to run something.

    It is not hypothetical: @artheras/aria-code-darwin-arm64 and its four
    siblings have never been published, so every npm install to date has been
    in exactly this state.
    """
    key = npm_platform_key()
    if not key:
        return _check(
            "npm_runtime:binary",
            "skip",
            f"no prebuilt binary for {platform.system()}/{platform.machine()}",
            "Install with pip instead: pip install -U \"aria-code<4\"",
        )
    pkg = f"@artheras/aria-code-{key}"
    if not npm:
        return None
    code, root = _capture_cmd([npm, "root", "-g"], timeout=1.5)
    if code != 0 or not root:
        return None
    installed = Path(root) / "@artheras" / f"aria-code-{key}"
    if installed.is_dir():
        return _check("npm_runtime:binary", "ok", f"{pkg} at {installed}")
    return _check(
        "npm_runtime:binary",
        "err",
        f"{pkg} is not installed — the launcher has no binary to run",
        "npm skips an optional dependency it cannot resolve without saying so. "
        + NPM_REINSTALL_HINT,
    )


def _npm_install_detected(paths: dict) -> bool:
    """Is there an npm-launcher installation for these checks to be about?

    Any one of these is proof: the launcher's own aria_cli.py, the install
    metadata it writes, or an install dir that came from npm config rather
    than a platform default.
    """
    if paths["aria_cli"].is_file():
        return True
    if any(candidate.is_file() for candidate in paths["info_candidates"]):
        return True
    return str(paths.get("install_dir_source", "")).startswith("npm")


def npm_runtime_checks(*, cwd: Optional[Path] = None) -> List[DoctorCheck]:
    """Return npm launcher/runtime path diagnostics."""
    checks: List[DoctorCheck] = []
    paths = _resolve_runtime_paths()
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    source_cli = cwd / "aria_cli.py"
    source_venv_py = cwd / ".venv" / ("Scripts/python.exe" if platform.system().lower() == "windows" else "bin/python")
    using_source_checkout = source_cli.is_file() and not paths["aria_cli"].is_file()

    # A pip install has no npm launcher, so every check below is about
    # something that was never supposed to exist. Reporting them made a clean
    # `pip install aria-code` end in "1 errors", and the repair suggestion —
    # `node $(npm root -g)/aria-code/scripts/postinstall.js` — is not just
    # wrong for that user, it is unrunnable on a machine with no node.
    if not using_source_checkout and not _npm_install_detected(paths):
        checks.append(_check(
            "npm_runtime",
            "skip",
            "not an npm install — nothing to check",
            "These checks apply to `npm install -g @artheras/aria-code`.",
        ))
        return checks

    node = shutil.which("node")
    if node:
        code, version = _capture_cmd([node, "--version"], timeout=1.5)
        checks.append(_check("npm_runtime:node", "ok" if code == 0 else "warn", f"{node} {version}".strip()))
    else:
        checks.append(_check("npm_runtime:node", "warn", "node not found", "Install Node.js if you use the npm launcher."))

    npm = shutil.which("npm")
    if npm:
        _code, version = _capture_cmd([npm, "--version"], timeout=1.5)
        prefix_code, prefix = _capture_cmd([npm, "config", "get", "prefix"], timeout=1.5)
        root_code, root = _capture_cmd([npm, "root", "-g"], timeout=1.5)
        detail = f"{npm} v{version or '?'}"
        if prefix_code == 0 and prefix:
            detail += f"; prefix={prefix}"
        if root_code == 0 and root:
            detail += f"; root={root}"
        checks.append(_check("npm_runtime:npm", "ok", detail))
    else:
        checks.append(_check("npm_runtime:npm", "warn", "npm not found", "Install Node.js/npm if you use npm install -g aria-code."))

    binary_check = _platform_package_check(npm)
    if binary_check is not None:
        checks.append(binary_check)

    install_status, install_detail = _is_path_ready(paths["install_dir"])
    install_suggestion = ""
    if using_source_checkout:
        install_suggestion = f"Set ARIA_HOME={cwd} when launching through npm, or run npm install -g aria-code."
    elif not paths["aria_cli"].exists():
        install_suggestion = "Run npm install -g aria-code or set ARIA_HOME to the cloned repo."
    checks.append(_check(
        "npm_runtime:install_dir",
        "warn" if using_source_checkout else ("ok" if paths["aria_cli"].exists() else install_status),
        (
            f"{install_detail}; source={paths['install_dir_source']}"
            + (f"; source_checkout={cwd}" if using_source_checkout else "")
        ),
        install_suggestion,
    ))

    actual_cli = paths["aria_cli"] if paths["aria_cli"].is_file() else (source_cli if source_cli.is_file() else paths["aria_cli"])
    checks.append(_check(
        "npm_runtime:aria_cli",
        "ok" if actual_cli.is_file() else "err",
        str(actual_cli),
        # Was "node $(npm root -g)/aria-code/scripts/postinstall.js". That file
        # was deleted with the launcher rewrite, so the suggestion sent anyone
        # who hit it to a path that does not exist.
        NPM_REINSTALL_HINT if not actual_cli.is_file() else "",
    ))

    actual_venv_py = paths["venv_py"] if paths["venv_py"].is_file() else (source_venv_py if source_venv_py.is_file() else paths["venv_py"])
    checks.append(_check(
        "npm_runtime:venv",
        "ok" if actual_venv_py.is_file() else "warn",
        str(actual_venv_py),
        # No `repair` or `update-engine` script exists any more. A missing venv
        # is also no longer a fault on an npm install, which ships a binary and
        # builds no venv at all — so this is only meaningful for the legacy
        # layout or a source checkout.
        (NPM_REINSTALL_HINT if not actual_venv_py.is_file() else ""),
    ))

    info_hit = next((p for p in paths["info_candidates"] if p.is_file()), None)
    checks.append(_check(
        "npm_runtime:install_info",
        "ok" if info_hit else "warn",
        str(info_hit) if info_hit else "none found; checked " + ", ".join(str(p) for p in paths["info_candidates"]),
        NPM_REINSTALL_HINT if not info_hit else "",
    ))

    config_status, config_detail = _is_path_ready(paths["config_dir"])
    cache_status, cache_detail = _is_path_ready(paths["cache_dir"])
    checks.append(_check("npm_runtime:config_dir", config_status, config_detail))
    checks.append(_check("npm_runtime:cache_dir", cache_status, cache_detail))
    return checks


def _check_ollama(url: str, timeout: float = 1.5, *, required: bool = True) -> DoctorCheck:
    """Report on the local Ollama server.

    ``required`` says whether this install actually depends on it. Ollama is one
    of several ways to get a model, and reporting its absence as a warning meant
    a perfectly healthy cloud-only install could never show green — so the
    signal people were meant to read stopped meaning anything.
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{url.rstrip('/')}/api/tags", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = [str(model.get("name", "")) for model in data.get("models", []) if model.get("name")]
        if models:
            return _check("ollama", "ok", f"{len(models)} models: {', '.join(models[:4])}")
        if required:
            return _check("ollama", "warn", "running but no models installed", "ollama pull qwen2.5-coder:7b")
        return _check("ollama", "skip", "running, no models installed (not this install's provider)")
    except Exception as exc:
        if required:
            return _check("ollama", "warn", f"not reachable at {url}: {exc}",
                          "Start Ollama, or switch provider with /model.")
        return _check("ollama", "skip", "not running (offline mode unused — this install uses another provider)")


def provider_health_checks(snapshot: Optional[List[Dict[str, Any]]] = None) -> List[DoctorCheck]:
    """Convert data provider health state into doctor checks."""
    if snapshot is None:
        try:
            from packages.aria_services.provider_health import GLOBAL_PROVIDER_HEALTH

            snapshot = GLOBAL_PROVIDER_HEALTH.snapshot()
        except Exception:
            snapshot = []

    if not snapshot:
        return [
            _check(
                "data_provider_health",
                "warn",
                "no provider calls recorded in this session",
                "Run /quote, /ta, /analyze, or /report to populate provider health.",
            )
        ]

    checks: List[DoctorCheck] = []
    for row in snapshot:
        provider = str(row.get("provider") or "provider")
        status = str(row.get("status") or "unknown")
        failures = int(row.get("failures") or 0)
        cooldown = bool(row.get("cooldown_active"))
        remaining = int(row.get("cooldown_remaining_seconds") or 0)
        detail = status
        if failures:
            detail += f", failures={failures}"
        if cooldown:
            detail += f", cooldown={remaining}s"
        if row.get("last_error"):
            detail += f", last={row.get('last_error')}"
        last_success = _format_age((time.time() - float(row.get("last_success_at"))) if row.get("last_success_at") else None)
        if last_success:
            detail += f", last_ok={last_success}"
        check_status = "ok" if status == "ok" else "warn" if row.get("last_error_category") != "auth" else "err"
        suggestion = "Wait for cooldown or switch provider/API key." if cooldown else ""
        if row.get("last_error_category") == "auth":
            suggestion = "Check provider API key with /apikey list or /apikey set."
        checks.append(_check(f"data_provider:{provider}", check_status, detail, suggestion))
    return checks


def provider_health_summary(snapshot: Optional[List[Dict[str, Any]]] = None) -> DoctorCheck:
    """Summarise provider health into one dashboard-style check."""
    if snapshot is None:
        try:
            from packages.aria_services.provider_health import GLOBAL_PROVIDER_HEALTH

            snapshot = GLOBAL_PROVIDER_HEALTH.snapshot()
        except Exception:
            snapshot = []
    try:
        from packages.aria_services.provider_health import summarize_provider_health

        summary = summarize_provider_health(snapshot)
        return _check("provider_health_summary", summary.status, summary.detail, summary.suggestion)
    except Exception:
        if not snapshot:
            return _check(
                "provider_health_summary",
                "warn",
                "no provider calls recorded in this session",
                "Run /quote, /ta, /analyze, or /report to populate provider health.",
            )
        total = len(snapshot)
        return _check(
            "provider_health_summary",
            "warn",
            f"{total} providers",
            "Run /doctor --network or inspect /apikey.",
        )


def _iter_required_modules() -> Iterable[tuple[str, str]]:
    """Core dependencies: a plain `pip install aria-code` must provide these.

    Kept in step with [project].dependencies in pyproject.toml;
    tests/test_first_run_works.py fails if one is listed here but not there.
    """
    yield "aiohttp", "async HTTP"
    yield "rich", "terminal UI"
    yield "prompt_toolkit", "interactive input"
    yield "requests", "HTTP client"
    yield "pandas", "dataframes"
    yield "numpy", "numeric processing"
    yield "yfinance", "US/HK/global market data"
    # The default model is google/gemini-2.5-pro, so without this a fresh
    # install cannot answer a single prompt.
    yield "google.genai", "Gemini / Vertex AI (the default model)"


def _iter_optional_modules() -> Iterable[tuple[str, str, str]]:
    """Optional extras: (module, purpose, extra that installs it).

    These used to sit in the required list, so a clean install of aria-code
    always ended doctor with "1 errors" — for akshare, which pyproject has
    always declared as the optional `cn` extra. Missing an optional extra is a
    choice the user made, not a fault.
    """
    yield "akshare", "China A-share market data", "cn"


def run_doctor(
    config: Optional[Dict[str, Any]] = None,
    *,
    cwd: Optional[Path] = None,
    check_network: bool = False,
    context_stats: Optional[Dict[str, Any]] = None,
) -> DoctorReport:
    """Run local-first diagnostics without mutating user configuration."""

    config = config or {}
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    checks: List[DoctorCheck] = []

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        checks.append(_check("python", "ok", f"{pyver} on {platform.system()}"))
    else:
        checks.append(_check("python", "err", f"{pyver} is unsupported", "Install Python 3.10 or newer."))

    drift = _check_python_drift()
    if drift is not None:
        checks.append(drift)

    if context_stats:
        _fill = float(context_stats.get("fill_ratio") or 0.0)
        _thr = float(context_stats.get("threshold") or 0.78)
        _ctx_status = "ok" if _fill < _thr else ("warn" if _fill < 0.95 else "err")
        checks.append(_check(
            "context",
            _ctx_status,
            f"{context_stats.get('estimated_tokens', 0)}/{context_stats.get('max_tokens', 0)} tokens "
            f"({context_stats.get('fill_pct', 0)}%) across {context_stats.get('message_count', 0)} messages",
            "" if _ctx_status == "ok" else "/compact to shrink the conversation before the window overflows.",
        ))

    for module, purpose in _iter_required_modules():
        if _has_module(module):
            checks.append(_check(f"package:{module}", "ok", purpose))
        else:
            checks.append(_check(f"package:{module}", "err", f"{purpose} missing",
                                 "Reinstall: pip install -U \"aria-code<4\""))

    for module, purpose, extra in _iter_optional_modules():
        if _has_module(module):
            checks.append(_check(f"package:{module}", "ok", purpose))
        else:
            checks.append(_check(f"package:{module}", "skip", f"{purpose} not installed (optional)",
                                 f"pip install 'aria-code[{extra}]'"))

    for module in ("data_service", "artifacts", "report_generator", "backtest_report"):
        checks.append(
            _check(f"module:{module}", "ok" if _has_module(module) else "err", "importable" if _has_module(module) else "missing from install")
        )

    checks.extend(npm_runtime_checks(cwd=cwd))
    checks.extend(integration_checks())

    try:
        from artifacts import artifact_root, artifact_summary

        root = artifact_root()
        writable, detail = _is_writable(root)
        checks.append(_check("artifact_root", "ok" if writable else "err", detail, "Set ARIA_ARTIFACT_ROOT to a writable folder."))
        summary = artifact_summary(root)
        total = int(summary.get("total") or 0)
        total_size = int(summary.get("total_size_bytes") or 0)
        by_kind = summary.get("by_kind") or {}
        detail_bits = [f"{total} artifacts", f"{total_size} bytes"]
        if by_kind:
            detail_bits.extend(f"{kind}={count}" for kind, count in list(by_kind.items())[:4])
        checks.append(
            _check(
                "artifact_inventory",
                "ok" if total else "warn",
                ", ".join(detail_bits),
                "Run /artifacts stats or generate a report to populate local outputs.",
            )
        )
    except Exception as exc:
        checks.append(_check("artifact_root", "err", str(exc)))

    config_dir = aria_home()
    config_file = config_dir / "config.json"
    if config_file.exists():
        checks.append(_check("config", "ok", str(config_file)))
    else:
        checks.append(_check("config", "warn", f"{config_file} not found", "Run aria-code once or use /config set key=value."))

    data_sharing = bool(config.get("data_sharing", False))
    feedback_upload = bool(config.get("feedback_upload", False))
    privacy_detail = f"data_sharing={data_sharing}, feedback_upload={feedback_upload}"
    checks.append(_check("privacy", "ok", privacy_detail))

    try:
        from datasources.router import DataRouter

        sources = DataRouter().list_sources()
        configured = [src["name"] for src in sources if src.get("configured")]
        missing = [src["name"] for src in sources if src.get("needs_key") and not src.get("configured")]
        status = "ok" if configured else "warn"
        detail = f"configured: {', '.join(configured) or 'none'}"
        suggestion = f"optional keys missing: {', '.join(missing)}" if missing else ""
        checks.append(_check("datasources", status, detail, suggestion))
    except Exception as exc:
        checks.append(_check("datasources", "warn", str(exc)))

    checks.append(provider_health_summary())
    checks.extend(provider_health_checks())

    # Ollama only matters when this install routes through it. A user on the
    # gateway or a cloud key never wants it, and flagging that as a warning is
    # how a green report becomes unreachable.
    _uses_ollama = (
        str(config.get("local_provider") or "").lower() == "ollama"
        or not str(config.get("model") or "").strip()
        or ":" in str(config.get("model") or "")  # bare Ollama-style tag, e.g. qwen2.5-coder:1.5b
    )
    if check_network:
        checks.append(_check_ollama(
            str(config.get("ollama_url") or "http://localhost:11434"),
            required=_uses_ollama,
        ))
    else:
        # A check that did not run is not a warning about the thing it checks.
        checks.append(_check("ollama", "skip", "network check not requested",
                             "Run /doctor --network to probe the local server."))

    if (cwd / ".ariarc").exists():
        checks.append(_check("project_config", "ok", str(cwd / ".ariarc")))
    else:
        checks.append(_check("project_config", "warn", ".ariarc not found", "Optional: add .ariarc for project-local settings."))

    return DoctorReport(checks=checks)


def format_doctor_plain(report: DoctorReport) -> str:
    marks = {"ok": "OK", "warn": "WARN", "err": "ERR", "skip": "--"}
    lines = ["Aria Code doctor"]
    for check in report.checks:
        suffix = f" — {check.detail}" if check.detail else ""
        if check.suggestion:
            suffix += f" ({check.suggestion})"
        lines.append(f"{marks.get(check.status, check.status.upper()):<4} {check.name}{suffix}")
    skipped = sum(1 for check in report.checks if check.status == "skip")
    tail = f" · {skipped} skipped" if skipped else ""
    lines.append(f"{report.passed} passed · {report.warnings} warnings · {report.errors} errors{tail}")
    return "\n".join(lines)

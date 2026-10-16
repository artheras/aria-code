"use strict";
/**
 * Which prebuilt binary this machine needs, and where npm put it.
 *
 * The previous install did none of this: postinstall.js ran 651 lines of
 * bootstrap on every machine — Xcode CLT, Homebrew, a Python toolchain, a git
 * clone of this repository, a venv, then pip. Every one of those is a way for
 * `npm install -g` to fail, and it made the install depend on GitHub, Homebrew
 * and PyPI all being reachable.
 *
 * Claude Code and Codex both solve it the same way, and measurably smaller:
 * a dispatcher package of 0.2 MB and 0.01 MB respectively, zero runtime
 * dependencies, and the per-platform binaries as optionalDependencies so npm
 * downloads exactly the one that matches. This module is the resolution half of
 * that, kept pure so it can be tested without a binary present.
 */

// All five are built by build-native-binaries.yml — macOS on macos-14 and
// macos-15-intel, Linux on ubuntu-latest and ubuntu-24.04-arm, Windows on
// windows-latest. Both references ship these same platforms (Codex six,
// Claude Code eight, the extra two being musl variants), and every runner here
// is GitHub-hosted, so each one is a native build.
//
// Not covered: musl (Alpine). A glibc binary does not run there, which is why
// Claude Code ships linux-x64-musl and linux-arm64-musl separately. Anyone on
// Alpine gets the unsupported message and `pip install aria-code`.
const PLATFORM_KEYS = Object.freeze([
  "darwin-arm64",
  "darwin-x64",
  "linux-x64",
  "linux-arm64",
  "win32-x64",
]);

const SCOPE = "@artheras";
const BASE = "aria-code";

/** The binary's filename inside a platform package. */
function binaryName(platform, name = "aria-code-bin") {
  return platform === "win32" ? `${name}.exe` : name;
}

/**
 * "darwin-arm64" etc. for this process, or null when unsupported.
 * @param {{platform: string, arch: string}} proc
 */
function platformKey(proc) {
  const platform = proc && proc.platform;
  const arch = proc && proc.arch;
  if (!platform || !arch) return null;
  const key = `${platform}-${arch}`;
  return PLATFORM_KEYS.includes(key) ? key : null;
}

/** The npm package that carries the requested binary for a platform key. */
function packageNameFor(key, name = "aria-code-bin") {
  const kind = name === "aria-code-mcp-bin" ? "mcp-" : "";
  return `${SCOPE}/${BASE}-${kind}${key}`;
}

/**
 * The require path of a binary inside its platform package.
 *
 * The package carries a PyInstaller --onedir build: the executable sits in a
 * directory beside its _internal/ libraries. A --onefile binary unpacked ~400
 * libraries to a fresh temp directory on every launch and macOS re-scanned
 * each one, so every command took 90 s; see go/buildtools/README.md.
 */
function binaryRequestFor(key, name = "aria-code-bin") {
  const platform = key.split("-")[0];
  return `${packageNameFor(key, name)}/bin/${name}/${binaryName(platform, name)}`;
}

/**
 * What to tell someone whose platform has no build. Never a silent fallback:
 * pip install is a real, supported path and saying so is more useful than
 * attempting a source build behind a spinner.
 */
function unsupportedMessage(proc) {
  const seen = `${(proc && proc.platform) || "?"}-${(proc && proc.arch) || "?"}`;
  return [
    `No prebuilt aria-code binary for ${seen}.`,
    "",
    "Supported by the npm install:",
    ...PLATFORM_KEYS.map((k) => `  ${k}`),
    "",
    "On any other platform install from PyPI instead, which builds for yours:",
    "  pip install aria-code",
  ].join("\n");
}

/** What to tell someone whose platform IS supported but whose package is absent. */
function missingPackageMessage(key) {
  return [
    `The aria-code binary for ${key} is not installed.`,
    "",
    `npm should have fetched ${packageNameFor(key)} as an optional dependency.`,
    "That usually means the install ran with --no-optional, or it was",
    "interrupted. Reinstalling fetches it:",
    "",
    "  npm install -g @artheras/aria-code",
    "",
    "Or install from PyPI instead:  pip install aria-code",
  ].join("\n");
}

/**
 * The first launch of a newly installed build on macOS takes about a minute
 * and a half: the system scans each of its ~400 native libraries once before
 * they may load. Every later launch takes ~2 s. Without a word, that first
 * launch looks like a hang, so the dispatcher says so — once per installed
 * build, keyed by the binary's path and modification time so an upgrade
 * (new files, new scan) is told again.
 */
const FIRST_LAUNCH_NOTICE =
  "aria-code: first launch of this version — macOS is checking its bundled " +
  "libraries, which takes about a minute once. Later launches take seconds.";

function firstLaunchMarker(homedir, binaryPath, mtimeMs) {
  const crypto = require("crypto");
  const path = require("path");
  const id = crypto.createHash("sha256").update(`${binaryPath}\0${mtimeMs}`).digest("hex").slice(0, 16);
  return path.join(homedir, ".aria-code", "launched", id);
}

module.exports = {
  FIRST_LAUNCH_NOTICE,
  firstLaunchMarker,
  PLATFORM_KEYS,
  SCOPE,
  BASE,
  binaryName,
  platformKey,
  packageNameFor,
  binaryRequestFor,
  unsupportedMessage,
  missingPackageMessage,
};

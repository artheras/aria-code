#!/usr/bin/env node
"use strict";
/**
 * Dispatcher. Finds the prebuilt binary for this platform and becomes it.
 *
 * This replaces a launcher that located a git clone, a venv and a Python
 * interpreter, and a 651-line postinstall that created all three. Nothing is
 * built or fetched at install time now: npm resolves one optionalDependency
 * matching the platform, and this file execs what is inside it.
 *
 * The exec is a replacement, not a wrapper: signals, exit codes, stdin and the
 * tty all belong to the binary. A wrapper would have to forward each of them
 * and would get Ctrl-C subtly wrong.
 */

const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const {
  FIRST_LAUNCH_NOTICE,
  firstLaunchMarker,
  platformKey,
  binaryRequestFor,
  unsupportedMessage,
  missingPackageMessage,
} = require("../lib/platform");

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function resolveBinary(name) {
  const key = platformKey(process);
  if (!key) fail(unsupportedMessage(process));
  try {
    return require.resolve(binaryRequestFor(key, name));
  } catch {
    fail(missingPackageMessage(key));
  }
}

// argv[2..] is the user's command line; argv[0..1] are node and this script.
const binary = resolveBinary("aria-code-bin");

// Best effort throughout: a notice that cannot be recorded is shown again next
// time, which is harmless; it must never stop the binary from running.
let marker = null;
if (process.platform === "darwin" && process.stderr.isTTY) {
  try {
    marker = firstLaunchMarker(os.homedir(), binary, fs.statSync(binary).mtimeMs);
    if (fs.existsSync(marker)) marker = null;
    else process.stderr.write(`${FIRST_LAUNCH_NOTICE}\n`);
  } catch {
    marker = null;
  }
}

const result = spawnSync(binary, process.argv.slice(2), {
  stdio: "inherit",
  env: { ...process.env, ARIA_CODE_INSTALL_CHANNEL: "npm" },
});

if (marker && !result.error) {
  try {
    fs.mkdirSync(path.dirname(marker), { recursive: true });
    fs.writeFileSync(marker, "");
  } catch {}
}

if (result.error) {
  // ENOENT here means the package resolved but the file is gone or not
  // executable — worth distinguishing from "package not installed", which the
  // resolve step above already reported.
  fail(`Could not run ${binary}\n${result.error.message}`);
}
// Signal deaths must not look like a clean exit: 128+signal is what a shell
// reports, and CI reads the exit code.
process.exit(result.signal ? 128 + (require("os").constants.signals[result.signal] || 0)
                           : (result.status === null ? 1 : result.status));

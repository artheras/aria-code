import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

export type ApprovalMode = "strict" | "tiered";

export function parseApprovalMode(raw?: string): ApprovalMode {
  return String(raw ?? "").trim().toLowerCase() === "tiered" ? "tiered" : "strict";
}

export function resolveSafeUploadPath(rawPath: string, allowedRoot: string): string {
  const root = fs.realpathSync(path.resolve(allowedRoot));
  const candidate = path.resolve(root, rawPath);
  if (!fs.existsSync(candidate) || !fs.statSync(candidate).isFile()) {
    throw new Error(`Upload file does not exist: ${rawPath}`);
  }
  const real = fs.realpathSync(candidate);
  const rel = path.relative(root, real);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new Error("Upload file must reside inside the authorized upload root");
  }
  return real;
}

const INJECTION_PATTERNS: RegExp[] = [
  /ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions/i,
  /disregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions/i,
  /(?:system\s+prompt|developer\s+message)\s*:/i,
  /<\|im_start\|>|<\|endoftext\|>/i,
  /curl\s+[^\n|]+\|\s*(?:ba)?sh/i,
];

const SENSITIVE_LABEL_RE =
  /\b(delete|remove|destroy|pay|purchase|checkout|transfer|withdraw|confirm|submit)\b|(删除|移除|清空|支付|付款|转账|提现|确认提交)/i;

export function normalizeOrigin(rawUrl: string): string {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error("Use an HTTP(S) URL without embedded credentials");
  }
  if (
    (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
    !parsed.hostname ||
    parsed.username !== "" ||
    parsed.password !== ""
  ) {
    throw new Error("Use an HTTP(S) URL without embedded credentials");
  }
  const defaultPort = parsed.protocol === "http:" ? "80" : "443";
  const portSuffix = parsed.port && parsed.port !== defaultPort ? `:${parsed.port}` : "";
  return `${parsed.protocol}//${parsed.hostname.toLowerCase()}${portSuffix}`;
}

export function detectPromptInjection(text: string): string[] {
  if (!text) return [];
  const warnings: string[] = [];
  for (const pat of INJECTION_PATTERNS) {
    const m = pat.exec(text);
    if (m) warnings.push(m[0].slice(0, 80));
  }
  return warnings;
}

export function isSensitiveLabel(text: string): boolean {
  return SENSITIVE_LABEL_RE.test(text);
}

function commandExists(cmd: string): boolean {
  try {
    execFileSync(process.platform === "win32" ? "where" : "which", [cmd], {
      stdio: "ignore",
      timeout: 3000,
    });
    return true;
  } catch {
    return false;
  }
}

export function approvalBackendLabel(
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
  hasCmd: (cmd: string) => boolean = commandExists,
): string {
  if (platform === "darwin") return "native macOS dialog (osascript)";
  if (platform === "win32") return "native Windows dialog (MessageBox)";
  if (platform === "linux" && (env.DISPLAY || env.WAYLAND_DISPLAY)) {
    if (hasCmd("zenity")) return "native Linux dialog (zenity)";
    if (hasCmd("kdialog")) return "native Linux dialog (kdialog)";
  }
  return "unavailable (fail-closed deny)";
}

export type ExecRunner = (
  file: string,
  args: string[],
  options: { timeout: number; encoding: "utf8"; env?: NodeJS.ProcessEnv },
) => string;

const defaultExec: ExecRunner = (file, args, options) => execFileSync(file, args, options);

/**
 * Human-only approval — never an MCP argument or model-callable tool.
 * Fail-closed on headless hosts, cancellation, or timeout.
 */
export function requestHumanApproval(
  summary: string,
  deps?: {
    platform?: NodeJS.Platform;
    env?: NodeJS.ProcessEnv;
    exec?: ExecRunner;
    hasCmd?: (cmd: string) => boolean;
  },
): boolean {
  const platform = deps?.platform ?? process.platform;
  const env = deps?.env ?? process.env;
  const exec = deps?.exec ?? defaultExec;
  const hasCmd = deps?.hasCmd ?? commandExists;
  const prompt = summary.slice(0, 2000);

  if (platform === "darwin") {
    const script = [
      "on run argv",
      "try",
      'display dialog (item 1 of argv) with title "Aria Computer Use" buttons {"Cancel", "Allow once"} default button "Cancel" cancel button "Cancel" giving up after 20',
      "return button returned of result",
      "on error",
      'return "Cancel"',
      "end try",
      "end run",
    ].join("\n");
    try {
      const out = exec("osascript", ["-e", script, prompt], {
        timeout: 25_000,
        encoding: "utf8",
      });
      return out.trim() === "Allow once";
    } catch {
      return false;
    }
  }

  if (platform === "win32") {
    const ps =
      "Add-Type -AssemblyName System.Windows.Forms; " +
      "$r = [System.Windows.Forms.MessageBox]::Show(" +
      "$env:ARIA_APPROVAL_PROMPT, 'Aria Computer Use', " +
      "[System.Windows.Forms.MessageBoxButtons]::YesNo, " +
      "[System.Windows.Forms.MessageBoxIcon]::Warning, " +
      "[System.Windows.Forms.MessageBoxDefaultButton]::Button2); " +
      "Write-Output $r";
    try {
      const out = exec("powershell", ["-NoProfile", "-NonInteractive", "-Command", ps], {
        timeout: 25_000,
        encoding: "utf8",
        env: { ...env, ARIA_APPROVAL_PROMPT: prompt },
      });
      return out.trim() === "Yes";
    } catch {
      return false;
    }
  }

  if (platform === "linux" && (env.DISPLAY || env.WAYLAND_DISPLAY)) {
    try {
      if (hasCmd("zenity")) {
        exec(
          "zenity",
          [
            "--question",
            "--title=Aria Computer Use",
            "--ok-label=Allow once",
            "--cancel-label=Cancel",
            "--timeout=20",
            "--text",
            prompt,
          ],
          { timeout: 25_000, encoding: "utf8" },
        );
        return true;
      }
      if (hasCmd("kdialog")) {
        exec("kdialog", ["--title", "Aria Computer Use", "--yesno", prompt], {
          timeout: 25_000,
          encoding: "utf8",
        });
        return true;
      }
    } catch {
      return false;
    }
  }

  return false;
}

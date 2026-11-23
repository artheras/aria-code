import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  approvalBackendLabel,
  type ApprovalMode,
  detectPromptInjection,
  isSensitiveLabel,
  normalizeOrigin,
  parseApprovalMode,
  requestHumanApproval,
  resolveSafeUploadPath,
} from "./security.js";
import {
  buildOverlayScript,
  CLEAR_OVERLAY_SCRIPT,
  EXTRACT_ELEMENTS_SCRIPT,
} from "./observer.js";
import { TOOLS } from "./tools.js";

export class PermissionDeniedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PermissionDeniedError";
  }
}

export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

export interface LocatorLike {
  count(): Promise<number>;
  isVisible(): Promise<boolean>;
  click(): Promise<void>;
  fill(value: string): Promise<void>;
  press(key: string): Promise<void>;
  hover(): Promise<void>;
  selectOption(opt: { value?: string; label?: string }): Promise<unknown>;
  setInputFiles?(files: string | string[]): Promise<void>;
  dragTo?(target: LocatorLike): Promise<void>;
  ariaSnapshot?(): Promise<string>;
  first?: LocatorLike;
  waitFor?(opts: { state: "visible"; timeout: number }): Promise<void>;
}

export interface FrameLike {
  url(): string;
  evaluate<T = unknown>(fn: string | ((arg?: unknown) => T), arg?: unknown): Promise<T>;
  locator(selector: string): LocatorLike;
}

export interface PageLike extends FrameLike {
  title(): Promise<string>;
  content(): Promise<string>;
  innerText(selector: string): Promise<string>;
  screenshot(opts?: { type?: "png"; fullPage?: boolean }): Promise<Buffer>;
  goto(url: string, opts?: { waitUntil?: "domcontentloaded"; timeout?: number }): Promise<unknown>;
  goBack(opts?: { waitUntil?: "domcontentloaded"; timeout?: number }): Promise<unknown>;
  goForward(opts?: { waitUntil?: "domcontentloaded"; timeout?: number }): Promise<unknown>;
  reload(opts?: { waitUntil?: "domcontentloaded"; timeout?: number }): Promise<unknown>;
  frames?(): FrameLike[];
  close?(): Promise<void>;
  isClosed?(): boolean;
}

export interface IndexedElement {
  element_id: number;
  selector: string;
  frame_index: number;
  in_shadow_dom: boolean;
  tag: string;
  role: string;
  label: string;
  sensitive: boolean;
  rect?: { x: number; y: number; w: number; h: number };
}

interface ElementRef {
  selector: string;
  frameIndex: number;
  label: string;
  sensitive: boolean;
}

interface ObservationState {
  id: string;
  atMs: number;
  fingerprint: string;
}

interface PreparedStep {
  toolName: string;
  target: LocatorLike;
  selector: string;
  frameIndex: number;
  value: unknown;
  detail: string;
  sensitive: boolean;
}

const SUPPORTED_KEYS = new Set([
  "Enter",
  "Tab",
  "Shift+Tab",
  "Escape",
  "Backspace",
  "Delete",
  "Home",
  "End",
  "PageUp",
  "PageDown",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Space",
  "Control+a",
  "Control+c",
  "Control+v",
  "Control+z",
  "Meta+a",
  "Meta+c",
  "Meta+v",
  "Meta+z",
]);

const MAX_RECORDING_FRAMES = 40;
const MAX_DOWNLOAD_RECORDS = 50;
const TIERED_BUDGET_LIMIT = 20;
const TIERED_WINDOW_MS = 10 * 60 * 1000;

export interface DownloadRecord {
  filename: string;
  path: string;
  url: string;
  size_bytes: number;
  captured_at: number;
}

export class BrowserSession {
  readonly origins: ReadonlySet<string>;
  readonly approvalMode: ApprovalMode;
  readonly uploadRoot: string;
  readonly downloadDir: string | null;
  readonly persistentProfileDir: string | null;
  readonly headless: boolean;
  private readonly approval: (summary: string) => boolean;
  private readonly clock: () => number;

  page: PageLike | null = null;
  private pages: PageLike[] = [];
  private context: { close(): Promise<void>; pages(): PageLike[] } | null = null;
  private profileDir: string | null = null;
  private ephemeralProfile = true;
  private observation: ObservationState | null = null;
  private elementMap = new Map<number, ElementRef>();
  private elementRects = new Map<string, { x: number; y: number; w: number; h: number }>();
  private lastThought = "";
  private cursorPos: [number, number] | null = null;
  private recordingFrames: Buffer[] = [];
  private actionTrail: Array<{ tool: string; selector: string; thought: string; at: number }> = [];
  private tieredGrants = new Map<string, { remaining: number; expiresAt: number }>();
  readonly consoleErrors: string[] = [];
  readonly failedRequests: string[] = [];
  readonly downloads: DownloadRecord[] = [];

  constructor(
    origins: string[],
    opts?: {
      approval?: (summary: string) => boolean;
      clock?: () => number;
      approvalMode?: ApprovalMode;
      uploadRoot?: string;
      downloadDir?: string | null;
      profileDir?: string | null;
      headless?: boolean;
    },
  ) {
    const normalized = new Set<string>();
    for (const raw of origins) {
      const trimmed = raw.trim();
      if (trimmed) normalized.add(normalizeOrigin(trimmed));
    }
    this.origins = normalized;
    this.approval = opts?.approval ?? ((summary) => requestHumanApproval(summary));
    this.clock = opts?.clock ?? (() => Date.now());
    this.approvalMode = opts?.approvalMode ?? parseApprovalMode(process.env.ARIA_COMPUTER_APPROVAL_MODE);
    this.uploadRoot = opts?.uploadRoot ?? process.env.ARIA_COMPUTER_UPLOAD_DIR ?? process.cwd();
    const rawDownloadDir = opts?.downloadDir !== undefined ? opts.downloadDir : process.env.ARIA_COMPUTER_DOWNLOAD_DIR;
    this.downloadDir = rawDownloadDir && rawDownloadDir.trim() ? path.resolve(rawDownloadDir.trim()) : null;
    const rawProfileDir = opts?.profileDir !== undefined ? opts.profileDir : process.env.ARIA_COMPUTER_PROFILE_DIR;
    this.persistentProfileDir = rawProfileDir && rawProfileDir.trim() ? path.resolve(rawProfileDir.trim()) : null;
    const envHeadless = (process.env.ARIA_COMPUTER_HEADLESS ?? "").trim().toLowerCase();
    this.headless = opts?.headless ?? (envHeadless === "1" || envHeadless === "true");
  }

  private assertAllowed(url: string): string {
    const org = normalizeOrigin(url);
    if (!this.origins.has(org)) {
      throw new ValidationError(
        `Origin ${org} is not in ARIA_COMPUTER_ORIGINS (${[...this.origins].sort().join(", ") || "empty"})`,
      );
    }
    return org;
  }

  private authorizeMutation(siteOrigin: string, prompt: string, forceExplicit: boolean): void {
    if (this.approvalMode === "tiered" && !forceExplicit) {
      const grant = this.tieredGrants.get(siteOrigin);
      if (grant && grant.remaining > 0 && this.clock() <= grant.expiresAt) {
        grant.remaining -= 1;
        return;
      }
    }
    if (!this.approval(prompt)) {
      throw new PermissionDeniedError("User approval was denied or unavailable");
    }
    if (this.approvalMode === "tiered" && !forceExplicit) {
      this.tieredGrants.set(siteOrigin, {
        remaining: TIERED_BUDGET_LIMIT - 1,
        expiresAt: this.clock() + TIERED_WINDOW_MS,
      });
    }
  }

  private getFrame(frameIndex: number): FrameLike {
    if (!this.page) throw new ValidationError("No page is open; call browser_open first");
    if (frameIndex === 0 || !this.page.frames) return this.page;
    const all = this.page.frames();
    const target = all[frameIndex];
    if (!target) {
      throw new ValidationError(`Frame index ${frameIndex} is no longer available; call browser_observe again`);
    }
    this.assertAllowed(target.url());
    return target;
  }

  private async fingerprint(): Promise<string> {
    if (!this.page) return "";
    const url = this.page.url();
    const html = await this.page.content();
    return crypto.createHash("sha256").update(url).update("\0").update(html).digest("hex");
  }

  private async assertFresh(token: unknown): Promise<void> {
    if (!this.page || !this.observation) {
      throw new ValidationError("Observation is stale; call browser_observe before acting");
    }
    if (
      typeof token !== "string" ||
      token !== this.observation.id ||
      this.clock() - this.observation.atMs > 60_000 ||
      (await this.fingerprint()) !== this.observation.fingerprint
    ) {
      throw new ValidationError("Observation is stale; call browser_observe before acting");
    }
    this.assertAllowed(this.page.url());
  }

  private async startBrowser(): Promise<void> {
    if (this.page) return;
    const { chromium } = await import("playwright");
    if (this.persistentProfileDir) {
      fs.mkdirSync(this.persistentProfileDir, { recursive: true });
      this.profileDir = this.persistentProfileDir;
      this.ephemeralProfile = false;
    } else {
      this.profileDir = fs.mkdtempSync(path.join(os.tmpdir(), "aria-cu-ts-"));
      this.ephemeralProfile = true;
    }
    if (this.downloadDir) {
      fs.mkdirSync(this.downloadDir, { recursive: true });
    }
    const ctx = await chromium.launchPersistentContext(this.profileDir, {
      headless: this.headless,
      acceptDownloads: Boolean(this.downloadDir),
      viewport: { width: 1280, height: 840 },
    });
    this.context = ctx as unknown as { close(): Promise<void>; pages(): PageLike[] };
    const existing = ctx.pages();
    const pg = existing[0] ?? (await ctx.newPage());

    const attachGuards = (p: (typeof existing)[number]) => {
      p.on("console", (msg) => {
        if (msg.type() === "error") {
          this.consoleErrors.push(msg.text().slice(0, 300));
          if (this.consoleErrors.length > 50) this.consoleErrors.shift();
        }
      });
      p.on("requestfailed", (req) => {
        this.failedRequests.push(`${req.method()} ${req.url().slice(0, 160)} (${req.failure()?.errorText ?? "failed"})`);
        if (this.failedRequests.length > 50) this.failedRequests.shift();
      });
      p.on("response", (resp) => {
        if (resp.status() >= 400) {
          this.failedRequests.push(`${resp.status()} ${resp.url().slice(0, 180)}`);
          if (this.failedRequests.length > 50) this.failedRequests.shift();
        }
      });
      if (this.downloadDir) {
        const dlDir = this.downloadDir;
        p.on("download", (dl) => {
          void (async () => {
            try {
              const rawName = path.basename(dl.suggestedFilename() || "download.bin").replace(/[^a-zA-Z0-9._-]/g, "_");
              let dest = path.join(dlDir, rawName);
              if (fs.existsSync(dest)) {
                dest = path.join(dlDir, `${Date.now()}-${rawName}`);
              }
              await dl.saveAs(dest);
              const size = fs.existsSync(dest) ? fs.statSync(dest).size : 0;
              this.downloads.push({
                filename: path.basename(dest),
                path: dest,
                url: dl.url(),
                size_bytes: size,
                captured_at: this.clock(),
              });
              if (this.downloads.length > MAX_DOWNLOAD_RECORDS) this.downloads.shift();
            } catch {
              // ignore cancelled/failed download saves
            }
          })();
        });
      }
    };

    attachGuards(pg);
    ctx.on("page", (newPage) => {
      attachGuards(newPage);
      const u = newPage.url();
      if (u && u !== "about:blank") {
        try {
          this.assertAllowed(u);
          this.pages.push(newPage as unknown as PageLike);
        } catch {
          void newPage.close().catch(() => {});
        }
      } else {
        this.pages.push(newPage as unknown as PageLike);
      }
    });

    await ctx.route("**/*", (route) => {
      const req = route.request();
      if (req.isNavigationRequest() && req.frame().parentFrame() === null) {
        try {
          this.assertAllowed(req.url());
        } catch {
          void route.abort("blockedbyclient");
          return;
        }
      }
      void route.continue();
    });

    this.page = pg as unknown as PageLike;
    this.pages = [this.page];
  }

  async close(): Promise<Record<string, unknown>> {
    this.observation = null;
    this.elementMap.clear();
    this.elementRects.clear();
    this.tieredGrants.clear();
    const ctx = this.context;
    this.context = null;
    this.page = null;
    this.pages = [];
    if (ctx) {
      try {
        await ctx.close();
      } catch {
        // ignore close errors
      }
    }
    if (this.profileDir && this.ephemeralProfile) {
      try {
        fs.rmSync(this.profileDir, { recursive: true, force: true });
      } catch {
        // ignore cleanup errors
      }
    }
    this.profileDir = null;
    return { closed: true };
  }

  private async renderOverlay(elements: IndexedElement[], markElements: boolean): Promise<void> {
    if (!this.page || (!markElements && !this.cursorPos && !this.lastThought)) return;
    const mainElements = elements.filter((e) => e.frame_index === 0);
    try {
      await this.page.evaluate(buildOverlayScript(mainElements, markElements, this.cursorPos));
    } catch {
      // non-fatal on mock/restricted pages
    }
  }

  private async clearOverlay(): Promise<void> {
    if (!this.page) return;
    try {
      await this.page.evaluate(CLEAR_OVERLAY_SCRIPT);
    } catch {
      // ignore
    }
  }

  async observe(
    markElements = false,
    includeAxTree = false,
    attempt = 0,
  ): Promise<Record<string, unknown>> {
    if (!this.page) {
      throw new ValidationError("No page is open; call browser_open first");
    }
    this.assertAllowed(this.page.url());
    const fpBefore = await this.fingerprint();

    this.elementMap.clear();
    this.elementRects.clear();
    const elements: IndexedElement[] = [];

    const frames: FrameLike[] = this.page.frames ? this.page.frames() : [this.page];
    const frameSummaries: Array<{ index: number; url: string }> = [];

    for (let fIdx = 0; fIdx < frames.length && elements.length < 100; fIdx++) {
      const frame = frames[fIdx];
      const fUrl = frame.url();
      if (fIdx > 0) {
        try {
          this.assertAllowed(fUrl);
        } catch {
          continue; // skip cross-origin third-party iframes
        }
      }
      frameSummaries.push({ index: fIdx, url: fUrl });
      let rawElements: Array<{
        selector: string;
        in_shadow_dom?: boolean;
        tag: string;
        role: string;
        label: string;
        rect?: { x: number; y: number; w: number; h: number };
      }> = [];
      try {
        const res = await frame.evaluate(EXTRACT_ELEMENTS_SCRIPT);
        if (Array.isArray(res)) rawElements = res;
      } catch {
        continue;
      }
      for (const item of rawElements) {
        if (elements.length >= 100) break;
        const eid = elements.length + 1;
        const sensitive = isSensitiveLabel(`${item.label} ${item.selector}`);
        this.elementMap.set(eid, {
          selector: item.selector,
          frameIndex: fIdx,
          label: item.label,
          sensitive,
        });
        if (item.rect && fIdx === 0) {
          this.elementRects.set(item.selector, item.rect);
        }
        elements.push({
          element_id: eid,
          selector: item.selector,
          frame_index: fIdx,
          in_shadow_dom: Boolean(item.in_shadow_dom),
          tag: item.tag,
          role: item.role,
          label: item.label,
          sensitive,
          rect: item.rect,
        });
      }
    }

    let axTree: string | undefined;
    if (includeAxTree) {
      try {
        const bodyLoc = this.page.locator("body");
        if (bodyLoc.ariaSnapshot) {
          axTree = (await bodyLoc.ariaSnapshot()).slice(0, 6000);
        }
      } catch {
        axTree = undefined;
      }
    }

    await this.renderOverlay(elements, markElements);
    let pngBytes: Buffer;
    try {
      pngBytes = await this.page.screenshot({ type: "png", fullPage: false });
    } finally {
      await this.clearOverlay();
    }

    if (pngBytes.length > 0) {
      this.recordingFrames.push(pngBytes);
      if (this.recordingFrames.length > MAX_RECORDING_FRAMES) {
        this.recordingFrames.shift();
      }
    }

    const obsId = crypto.randomBytes(12).toString("hex");
    this.observation = { id: obsId, atMs: this.clock(), fingerprint: fpBefore };
    const title = await this.page.title();
    let rawText = "";
    try {
      rawText = (await this.page.innerText("body")).slice(0, 12_000);
    } catch {
      rawText = "";
    }

    if ((await this.fingerprint()) !== fpBefore) {
      this.observation = null;
      if (attempt < 1) {
        await new Promise((resolve) => setTimeout(resolve, 150));
        return this.observe(markElements, includeAxTree, attempt + 1);
      }
      throw new ValidationError("Page changed while observing; call browser_observe again");
    }

    const result: Record<string, unknown> = {
      observation_id: obsId,
      url: this.page.url(),
      title,
      text: rawText,
      elements,
      frames: frameSummaries,
      thought: this.lastThought,
      cursor: this.cursorPos,
      recorded_frames: this.recordingFrames.length,
      action_trail: this.actionTrail.slice(-10),
      untrusted_content: true,
      _image: pngBytes.toString("base64"),
    };
    if (axTree) result.ax_tree = axTree;
    const warnings = detectPromptInjection(rawText);
    if (warnings.length > 0) result.injection_warnings = warnings;
    if (this.consoleErrors.length > 0) result.console_errors = this.consoleErrors.slice(-10);
    if (this.failedRequests.length > 0) result.failed_requests = this.failedRequests.slice(-10);
    return result;
  }

  private resolveTargetRef(
    args: Record<string, unknown>,
    selectorKey = "selector",
    elementIdKey = "element_id",
  ): ElementRef {
    const rawSel = typeof args[selectorKey] === "string" ? String(args[selectorKey]) : "";
    const explicitFrame =
      typeof args.frame_index === "number" && Number.isInteger(args.frame_index) ? args.frame_index : 0;

    if (!rawSel && args[elementIdKey] !== undefined) {
      const eid = Number(args[elementIdKey]);
      if (!Number.isInteger(eid)) throw new ValidationError(`${elementIdKey} must be an integer`);
      const ref = this.elementMap.get(eid);
      if (!ref) {
        throw new ValidationError(`Unknown ${elementIdKey} ${eid}; call browser_observe first`);
      }
      return ref;
    }
    if (!rawSel || rawSel.length > 500) {
      throw new ValidationError(`Provide ${selectorKey} of 1..500 characters`);
    }
    return {
      selector: rawSel,
      frameIndex: explicitFrame,
      label: rawSel,
      sensitive: isSensitiveLabel(rawSel),
    };
  }

  private async prepareStep(toolName: string, stepArgs: Record<string, unknown>): Promise<PreparedStep> {
    const ref = this.resolveTargetRef(stepArgs);
    const frame = this.getFrame(ref.frameIndex);
    const target = frame.locator(ref.selector);
    if ((await target.count()) !== 1 || !(await target.isVisible())) {
      throw new ValidationError("Selector must match exactly one visible element");
    }
    let value: unknown = null;
    let detail = "";
    let extraSensitive = ref.sensitive;
    if (toolName === "browser_fill") {
      if (typeof stepArgs.text !== "string" || stepArgs.text.length > 20_000) {
        throw new ValidationError("text must be a string of at most 20000 characters");
      }
      value = stepArgs.text;
      detail = `Fill ${stepArgs.text.length} characters (text hidden)`;
      if (/password|passwd|secret|token|card|cvv/i.test(`${ref.selector} ${ref.label}`)) {
        extraSensitive = true;
      }
    } else if (toolName === "browser_press") {
      if (typeof stepArgs.key !== "string" || !SUPPORTED_KEYS.has(stepArgs.key)) {
        throw new ValidationError("Unsupported key");
      }
      value = stepArgs.key;
      detail = `Press ${stepArgs.key}`;
      if (stepArgs.key === "Enter") extraSensitive = true;
    } else if (toolName === "browser_select") {
      const val = stepArgs.value;
      const lbl = stepArgs.label;
      const chosen = val ?? lbl;
      if (typeof chosen !== "string" || chosen.length < 1 || chosen.length > 500) {
        throw new ValidationError("Provide a value or label of 1..500 characters");
      }
      value = val !== undefined ? { value: String(val) } : { label: String(lbl) };
      detail = `Select option '${chosen.slice(0, 80)}'`;
    } else if (toolName === "browser_click") {
      detail = "Click";
    } else {
      throw new ValidationError("Unknown tool");
    }
    const riskTag = extraSensitive ? " [HIGH RISK]" : "";
    return {
      toolName,
      target,
      selector: ref.selector,
      frameIndex: ref.frameIndex,
      value,
      detail: `${detail}${riskTag}`,
      sensitive: extraSensitive,
    };
  }

  private recordAction(toolName: string, selector: string, stepArgs: Record<string, unknown>): void {
    const rect = this.elementRects.get(selector);
    if (rect) {
      this.cursorPos = [
        Math.round(rect.x + Math.max(rect.w, 8) / 2),
        Math.round(rect.y + Math.max(rect.h, 8) / 2),
      ];
    }
    if (typeof stepArgs.thought === "string" && stepArgs.thought.trim()) {
      this.lastThought = stepArgs.thought.trim().slice(0, 120);
    }
    this.actionTrail.push({
      tool: toolName,
      selector,
      thought: this.lastThought,
      at: this.clock(),
    });
    if (this.actionTrail.length > 50) this.actionTrail.shift();
  }

  private async executeStep(step: PreparedStep): Promise<void> {
    if (step.toolName === "browser_fill") {
      await step.target.fill(String(step.value));
    } else if (step.toolName === "browser_press") {
      await step.target.press(String(step.value));
    } else if (step.toolName === "browser_select") {
      await step.target.selectOption(step.value as { value?: string; label?: string });
    } else {
      await step.target.click();
    }
  }

  private async finalizeMutation(prevFp: string, prevUrl: string): Promise<Record<string, unknown>> {
    const out = await this.observe();
    const changed = (this.observation?.fingerprint ?? "") !== prevFp || (this.page?.url() ?? "") !== prevUrl;
    out.state_changed = changed;
    if (!changed) {
      out.dead_click_warning = "DOM and form state did not change after action";
    }
    return out;
  }

  async call(name: string, args: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    if (name === "browser_capabilities") {
      return {
        engine: "node-typescript-playwright",
        origins: [...this.origins].sort(),
        isolated_profile: !this.persistentProfileDir,
        persistent_profile: Boolean(this.persistentProfileDir),
        headless: this.headless,
        downloads_enabled: Boolean(this.downloadDir),
        approval: approvalBackendLabel(),
        approval_mode: this.approvalMode,
        desktop_input: false,
        transport: "stdio",
        shadow_dom: true,
        same_origin_iframes: true,
        ax_tree: true,
        virtual_cursor: true,
        screen_recording: true,
        tools_count: TOOLS.length,
      };
    }
    if (name === "browser_close") {
      return this.close();
    }
    if (name === "browser_export_recording") {
      const outDir =
        typeof args.output_path === "string" && args.output_path.trim()
          ? path.resolve(args.output_path.trim())
          : path.join(os.tmpdir(), `aria-cu-recording-${Date.now()}`);
      fs.mkdirSync(outDir, { recursive: true });
      this.recordingFrames.forEach((buf, i) => {
        fs.writeFileSync(path.join(outDir, `frame-${String(i + 1).padStart(3, "0")}.png`), buf);
      });
      const manifestPath = path.join(outDir, "trail.json");
      fs.writeFileSync(
        manifestPath,
        JSON.stringify({ frames: this.recordingFrames.length, action_trail: this.actionTrail }, null, 2),
        "utf8",
      );
      return {
        path: outDir,
        manifest: manifestPath,
        frames: this.recordingFrames.length,
        action_trail: this.actionTrail.slice(-20),
      };
    }
    if (name === "browser_observe") {
      if (typeof args.thought === "string" && args.thought.trim()) {
        this.lastThought = args.thought.trim().slice(0, 120);
      }
      return this.observe(Boolean(args.mark_elements), Boolean(args.include_ax_tree));
    }
    if (name === "browser_open") {
      const url = typeof args.url === "string" ? args.url : "";
      const siteOrigin = this.assertAllowed(url);
      this.authorizeMutation(siteOrigin, `Open a page in Aria's isolated browser?\n${url}`, true);
      this.observation = null;
      this.consoleErrors.length = 0;
      this.failedRequests.length = 0;
      await this.startBrowser();
      await this.page!.goto(url, { waitUntil: "domcontentloaded", timeout: 15_000 });
      return this.observe();
    }
    if (name === "browser_scroll") {
      if (!this.page) throw new ValidationError("No page is open; call browser_open first");
      this.assertAllowed(this.page.url());
      const direction = String(args.direction ?? "down").toLowerCase();
      if (!["down", "up", "top", "bottom"].includes(direction)) {
        throw new ValidationError("direction must be one of down, up, top, bottom");
      }
      const amount = Math.max(100, Math.min(2000, Number(args.amount ?? 600) || 600));
      if (direction === "top") {
        await this.page.evaluate("window.scrollTo(0, 0)");
      } else if (direction === "bottom") {
        await this.page.evaluate("window.scrollTo(0, document.body.scrollHeight)");
      } else {
        const delta = direction === "down" ? amount : -amount;
        await this.page.evaluate(`window.scrollBy(0, ${Math.trunc(delta)})`);
      }
      return this.observe(Boolean(args.mark_elements));
    }
    if (name === "browser_wait") {
      if (!this.page) throw new ValidationError("No page is open; call browser_open first");
      this.assertAllowed(this.page.url());
      const waitMs = Math.max(100, Math.min(5000, Number(args.wait_ms ?? 500) || 500));
      const selector = typeof args.selector === "string" ? args.selector.trim() : "";
      if (selector) {
        const loc = this.page.locator(selector);
        const first = loc.first ?? loc;
        if (first.waitFor) {
          await first.waitFor({ state: "visible", timeout: waitMs });
        }
      } else {
        await new Promise((resolve) => setTimeout(resolve, waitMs));
      }
      return this.observe(Boolean(args.mark_elements));
    }
    if (name === "browser_hover") {
      await this.assertFresh(args.observation_id);
      const ref = this.resolveTargetRef(args);
      const target = this.getFrame(ref.frameIndex).locator(ref.selector);
      if ((await target.count()) !== 1 || !(await target.isVisible())) {
        throw new ValidationError("Selector must match exactly one visible element");
      }
      this.recordAction("browser_hover", ref.selector, args);
      await target.hover();
      return this.observe(Boolean(args.mark_elements));
    }
    if (name === "browser_history") {
      if (!this.page) throw new ValidationError("No page is open; call browser_open first");
      this.assertAllowed(this.page.url());
      const action = String(args.action ?? "back").toLowerCase();
      if (action === "back") {
        await this.page.goBack({ waitUntil: "domcontentloaded", timeout: 15_000 });
      } else if (action === "forward") {
        await this.page.goForward({ waitUntil: "domcontentloaded", timeout: 15_000 });
      } else if (action === "reload") {
        await this.page.reload({ waitUntil: "domcontentloaded", timeout: 15_000 });
      } else {
        throw new ValidationError("action must be one of back, forward, reload");
      }
      this.assertAllowed(this.page.url());
      this.observation = null;
      return this.observe(Boolean(args.mark_elements));
    }
    if (name === "browser_tabs") {
      const action = String(args.action ?? "list").toLowerCase();
      const activeList = this.pages.filter((p) => !p.isClosed || !p.isClosed());
      this.pages = activeList;
      if (action === "list") {
        const tabs = await Promise.all(
          activeList.map(async (p, idx) => ({
            index: idx,
            url: p.url(),
            title: await p.title().catch(() => ""),
            active: p === this.page,
          })),
        );
        return { tabs };
      }
      const idx = Number(args.index);
      if (!Number.isInteger(idx) || idx < 0 || idx >= activeList.length) {
        throw new ValidationError(`Invalid tab index ${String(args.index)}`);
      }
      if (action === "switch") {
        const chosen = activeList[idx];
        this.assertAllowed(chosen.url());
        this.page = chosen;
        this.observation = null;
        return this.observe();
      }
      if (action === "close") {
        if (activeList.length <= 1) {
          throw new ValidationError("Cannot close the last remaining tab; use browser_close");
        }
        const target = activeList[idx];
        if (target.close) await target.close();
        this.pages = activeList.filter((p) => p !== target);
        if (this.page === target) {
          this.page = this.pages[0] ?? null;
          this.observation = null;
        }
        return { closed_index: idx, remaining_tabs: this.pages.length };
      }
      throw new ValidationError("action must be one of list, switch, close");
    }
    if (name === "browser_downloads") {
      const action = String(args.action ?? "list").toLowerCase();
      if (action === "list") {
        return {
          downloads_enabled: Boolean(this.downloadDir),
          download_dir: this.downloadDir,
          downloads: [...this.downloads],
        };
      }
      if (action === "clear") {
        const cleared = this.downloads.length;
        this.downloads.length = 0;
        return { cleared };
      }
      throw new ValidationError("action must be one of list, clear");
    }
    if (name === "browser_upload") {
      await this.assertFresh(args.observation_id);
      const rawPaths = args.file_paths;
      if (!Array.isArray(rawPaths) || rawPaths.length < 1 || rawPaths.length > 5) {
        throw new ValidationError("file_paths must be an array of 1..5 file paths");
      }
      const safePaths = rawPaths.map((p) => {
        if (typeof p !== "string" || !p.trim()) {
          throw new ValidationError("Each upload file path must be a non-empty string");
        }
        try {
          return resolveSafeUploadPath(p.trim(), this.uploadRoot);
        } catch (err) {
          throw new ValidationError(err instanceof Error ? err.message : "Invalid upload path");
        }
      });
      const ref = this.resolveTargetRef(args);
      const target = this.getFrame(ref.frameIndex).locator(ref.selector);
      if ((await target.count()) !== 1) {
        throw new ValidationError("Upload selector must match exactly one file input element");
      }
      const siteOrigin = normalizeOrigin(this.page!.url());
      const names = safePaths.map((p) => path.basename(p)).join(", ");
      this.authorizeMutation(
        siteOrigin,
        `Upload file(s): ${names}\nSite: ${siteOrigin}\nTarget: ${ref.selector}\nApprove only if you intend to share these files with this site.`,
        true, // File uploads ALWAYS require explicit human approval
      );
      await this.assertFresh(args.observation_id);
      const prevFp = this.observation!.fingerprint;
      const prevUrl = this.page!.url();
      this.observation = null;
      this.recordAction("browser_upload", ref.selector, args);
      if (!target.setInputFiles) {
        throw new ValidationError("Target element does not support file upload");
      }
      await target.setInputFiles(safePaths);
      return this.finalizeMutation(prevFp, prevUrl);
    }
    if (name === "browser_drag") {
      await this.assertFresh(args.observation_id);
      const srcRef = this.resolveTargetRef(args, "source_selector", "source_element_id");
      const dstRef = this.resolveTargetRef(args, "target_selector", "target_element_id");
      const src = this.getFrame(srcRef.frameIndex).locator(srcRef.selector);
      const dst = this.getFrame(dstRef.frameIndex).locator(dstRef.selector);
      if ((await src.count()) !== 1 || !(await src.isVisible()) || (await dst.count()) !== 1 || !(await dst.isVisible())) {
        throw new ValidationError("Source and target selectors must each match one visible element");
      }
      const siteOrigin = normalizeOrigin(this.page!.url());
      this.authorizeMutation(
        siteOrigin,
        `Drag ${srcRef.selector} -> ${dstRef.selector}\nSite: ${siteOrigin}`,
        srcRef.sensitive || dstRef.sensitive,
      );
      await this.assertFresh(args.observation_id);
      const prevFp = this.observation!.fingerprint;
      const prevUrl = this.page!.url();
      this.observation = null;
      this.recordAction("browser_drag", `${srcRef.selector}->${dstRef.selector}`, args);
      if (!src.dragTo) throw new ValidationError("Drag operation not supported on target");
      await src.dragTo(dst);
      return this.finalizeMutation(prevFp, prevUrl);
    }
    if (name === "browser_batch") {
      await this.assertFresh(args.observation_id);
      const rawSteps = args.steps;
      if (!Array.isArray(rawSteps) || rawSteps.length < 1 || rawSteps.length > 10) {
        throw new ValidationError("steps must be a list of 1..10 actions");
      }
      const prepared: Array<{ step: PreparedStep; raw: Record<string, unknown> }> = [];
      const lines: string[] = [];
      let anySensitive = false;
      for (let i = 0; i < rawSteps.length; i++) {
        const raw = rawSteps[i];
        if (!raw || typeof raw !== "object") {
          throw new ValidationError("Each batch step must be an object");
        }
        const obj = raw as Record<string, unknown>;
        const action = String(obj.action ?? "").toLowerCase();
        const mapped: Record<string, string> = {
          click: "browser_click",
          fill: "browser_fill",
          press: "browser_press",
          select: "browser_select",
        };
        const step = await this.prepareStep(mapped[action] ?? action, obj);
        if (step.sensitive) anySensitive = true;
        prepared.push({ step, raw: obj });
        lines.push(`${i + 1}. ${step.detail} -> ${step.selector}`);
      }
      const siteOrigin = normalizeOrigin(this.page!.url());
      const summary = `Batch (${prepared.length} steps)\nSite: ${siteOrigin}\n${lines.join("\n")}\nThis may submit or publish content. Approve only after reviewing the page.`;
      this.authorizeMutation(siteOrigin, summary, anySensitive);
      await this.assertFresh(args.observation_id);
      const prevFp = this.observation!.fingerprint;
      const prevUrl = this.page!.url();
      this.observation = null;
      for (const item of prepared) {
        this.assertAllowed(this.page!.url());
        this.recordAction(item.step.toolName, item.step.selector, item.raw);
        await this.executeStep(item.step);
      }
      return this.finalizeMutation(prevFp, prevUrl);
    }

    if (!["browser_click", "browser_fill", "browser_press", "browser_select"].includes(name)) {
      throw new ValidationError("Unknown tool");
    }
    await this.assertFresh(args.observation_id);
    const step = await this.prepareStep(name, args);
    const siteOrigin = normalizeOrigin(this.page!.url());
    const prompt = `${step.detail}\nSite: ${siteOrigin}\nTarget: ${step.selector}\nThis may submit or publish content. Approve only after reviewing the page.`;
    this.authorizeMutation(siteOrigin, prompt, step.sensitive);
    await this.assertFresh(args.observation_id);
    const prevFp = this.observation!.fingerprint;
    const prevUrl = this.page!.url();
    this.observation = null;
    this.recordAction(step.toolName, step.selector, args);
    await this.executeStep(step);
    return this.finalizeMutation(prevFp, prevUrl);
  }
}

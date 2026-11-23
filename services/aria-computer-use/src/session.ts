import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  approvalBackendLabel,
  detectPromptInjection,
  isSensitiveLabel,
  normalizeOrigin,
  requestHumanApproval,
} from "./security.js";
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
  first?: LocatorLike;
  waitFor?(opts: { state: "visible"; timeout: number }): Promise<void>;
}

export interface PageLike {
  url(): string;
  title(): Promise<string>;
  content(): Promise<string>;
  innerText(selector: string): Promise<string>;
  screenshot(opts?: { type?: "png"; fullPage?: boolean }): Promise<Buffer>;
  evaluate<T = unknown>(fn: string | ((arg?: unknown) => T), arg?: unknown): Promise<T>;
  locator(selector: string): LocatorLike;
  goto(url: string, opts?: { waitUntil?: "domcontentloaded"; timeout?: number }): Promise<unknown>;
  goBack(opts?: { waitUntil?: "domcontentloaded"; timeout?: number }): Promise<unknown>;
  goForward(opts?: { waitUntil?: "domcontentloaded"; timeout?: number }): Promise<unknown>;
  reload(opts?: { waitUntil?: "domcontentloaded"; timeout?: number }): Promise<unknown>;
  close?(): Promise<void>;
  isClosed?(): boolean;
}

export interface IndexedElement {
  element_id: number;
  selector: string;
  tag: string;
  role: string;
  label: string;
  sensitive: boolean;
  rect?: { x: number; y: number; w: number; h: number };
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
  value: unknown;
  detail: string;
}

const SUPPORTED_KEYS = new Set(["Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "Space"]);
const MAX_RECORDING_FRAMES = 40;

const EXTRACT_ELEMENTS_SCRIPT = `(() => {
  const nodes = Array.from(document.querySelectorAll(
    'a, button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [contenteditable="true"]'
  ));
  const out = [];
  for (const el of nodes) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (r.bottom < 0 || r.top > window.innerHeight || r.right < 0 || r.left > window.innerWidth) continue;
    let selector = '';
    if (el.id) {
      selector = '#' + CSS.escape(el.id);
    } else if (el.getAttribute('data-testid')) {
      selector = '[data-testid="' + el.getAttribute('data-testid').replace(/"/g, '\\"') + '"]';
    } else if (el.getAttribute('name')) {
      selector = el.tagName.toLowerCase() + '[name="' + el.getAttribute('name').replace(/"/g, '\\"') + '"]';
    } else {
      const siblings = Array.from(el.parentElement ? el.parentElement.children : []).filter(c => c.tagName === el.tagName);
      const idx = siblings.indexOf(el) + 1;
      selector = el.tagName.toLowerCase() + ':nth-of-type(' + idx + ')';
    }
    const label = (
      el.getAttribute('aria-label') ||
      el.innerText ||
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      el.value ||
      ''
    ).trim().replace(/\\s+/g, ' ').slice(0, 120);
    out.push({
      selector,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      label,
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    });
    if (out.length >= 80) break;
  }
  return out;
})()`;

export class BrowserSession {
  readonly origins: ReadonlySet<string>;
  private readonly approval: (summary: string) => boolean;
  private readonly clock: () => number;

  page: PageLike | null = null;
  private pages: PageLike[] = [];
  private context: { close(): Promise<void>; pages(): PageLike[] } | null = null;
  private profileDir: string | null = null;
  private observation: ObservationState | null = null;
  private elementMap = new Map<number, string>();
  private elementRects = new Map<string, { x: number; y: number; w: number; h: number }>();
  private lastThought = "";
  private cursorPos: [number, number] | null = null;
  private recordingFrames: Buffer[] = [];
  private actionTrail: Array<{ tool: string; selector: string; thought: string; at: number }> = [];
  readonly consoleErrors: string[] = [];
  readonly failedRequests: string[] = [];

  constructor(
    origins: string[],
    opts?: {
      approval?: (summary: string) => boolean;
      clock?: () => number;
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
    this.profileDir = fs.mkdtempSync(path.join(os.tmpdir(), "aria-cu-ts-"));
    const ctx = await chromium.launchPersistentContext(this.profileDir, {
      headless: false,
      acceptDownloads: false,
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
      if (req.isNavigationRequest() && req.frame() === pg.mainFrame()) {
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
    if (this.profileDir) {
      try {
        fs.rmSync(this.profileDir, { recursive: true, force: true });
      } catch {
        // ignore cleanup errors
      }
      this.profileDir = null;
    }
    return { closed: true };
  }

  private async renderOverlay(elements: IndexedElement[], markElements: boolean): Promise<void> {
    if (!this.page || (!markElements && !this.cursorPos && !this.lastThought)) return;
    try {
      await this.page.evaluate(
        `(cfg => {
          const prev = document.getElementById('__aria_cu_overlay__');
          if (prev) prev.remove();
          const root = document.createElement('div');
          root.id = '__aria_cu_overlay__';
          root.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;font-family:monospace;';
          if (cfg.markElements) {
            for (const el of cfg.elements) {
              if (!el.rect) continue;
              const color = el.sensitive ? '#dc2626' : '#2563eb';
              const box = document.createElement('div');
              box.style.cssText = 'position:fixed;border:2px solid ' + color + ';left:' + el.rect.x + 'px;top:' + el.rect.y + 'px;width:' + el.rect.w + 'px;height:' + el.rect.h + 'px;box-sizing:border-box;';
              const tag = document.createElement('div');
              tag.textContent = '#' + el.element_id;
              tag.style.cssText = 'position:absolute;top:-16px;left:0;background:' + color + ';color:#fff;font-size:11px;padding:1px 4px;border-radius:2px;line-height:14px;';
              box.appendChild(tag);
              root.appendChild(box);
            }
          }
          if (cfg.cursorPos) {
            const dot = document.createElement('div');
            dot.style.cssText = 'position:fixed;left:' + (cfg.cursorPos[0] - 8) + 'px;top:' + (cfg.cursorPos[1] - 8) + 'px;width:16px;height:16px;border-radius:50%;border:2px solid #10b981;background:rgba(16,185,129,0.25);';
            root.appendChild(dot);
          }
          document.documentElement.appendChild(root);
        })(${JSON.stringify({ elements, markElements, cursorPos: this.cursorPos })})`,
      );
    } catch {
      // non-fatal on mock/restricted pages
    }
  }

  private async clearOverlay(): Promise<void> {
    if (!this.page) return;
    try {
      await this.page.evaluate(
        "(() => { const el = document.getElementById('__aria_cu_overlay__'); if (el) el.remove(); })()",
      );
    } catch {
      // ignore
    }
  }

  async observe(markElements = false): Promise<Record<string, unknown>> {
    if (!this.page) {
      throw new ValidationError("No page is open; call browser_open first");
    }
    this.assertAllowed(this.page.url());
    const fpBefore = await this.fingerprint();
    const rawElements = (await this.page.evaluate(EXTRACT_ELEMENTS_SCRIPT)) as Array<{
      selector: string;
      tag: string;
      role: string;
      label: string;
      rect?: { x: number; y: number; w: number; h: number };
    }>;

    this.elementMap.clear();
    this.elementRects.clear();
    const elements: IndexedElement[] = [];
    if (Array.isArray(rawElements)) {
      rawElements.forEach((item, idx) => {
        const eid = idx + 1;
        this.elementMap.set(eid, item.selector);
        if (item.rect) this.elementRects.set(item.selector, item.rect);
        elements.push({
          element_id: eid,
          selector: item.selector,
          tag: item.tag,
          role: item.role,
          label: item.label,
          sensitive: isSensitiveLabel(`${item.label} ${item.selector}`),
          rect: item.rect,
        });
      });
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
      throw new ValidationError("Page changed while observing; call browser_observe again");
    }

    const result: Record<string, unknown> = {
      observation_id: obsId,
      url: this.page.url(),
      title,
      text: rawText,
      elements,
      thought: this.lastThought,
      cursor: this.cursorPos,
      recorded_frames: this.recordingFrames.length,
      action_trail: this.actionTrail.slice(-10),
      untrusted_content: true,
      _image: pngBytes.toString("base64"),
    };
    const warnings = detectPromptInjection(rawText);
    if (warnings.length > 0) result.injection_warnings = warnings;
    if (this.consoleErrors.length > 0) result.console_errors = this.consoleErrors.slice(-10);
    if (this.failedRequests.length > 0) result.failed_requests = this.failedRequests.slice(-10);
    return result;
  }

  private resolveSelector(args: Record<string, unknown>): string {
    let selector = typeof args.selector === "string" ? args.selector : "";
    if (!selector && args.element_id !== undefined) {
      const eid = Number(args.element_id);
      if (!Number.isInteger(eid)) throw new ValidationError("element_id must be an integer");
      selector = this.elementMap.get(eid) ?? "";
      if (!selector) {
        throw new ValidationError(`Unknown element_id ${eid}; call browser_observe first`);
      }
    }
    if (!selector || selector.length > 500) {
      throw new ValidationError("Provide a selector of 1..500 characters");
    }
    return selector;
  }

  private async prepareStep(toolName: string, stepArgs: Record<string, unknown>): Promise<PreparedStep> {
    if (!this.page) throw new ValidationError("No page is open; call browser_open first");
    const selector = this.resolveSelector(stepArgs);
    const target = this.page.locator(selector);
    if ((await target.count()) !== 1 || !(await target.isVisible())) {
      throw new ValidationError("Selector must match exactly one visible element");
    }
    let value: unknown = null;
    let detail = "";
    if (toolName === "browser_fill") {
      if (typeof stepArgs.text !== "string" || stepArgs.text.length > 20_000) {
        throw new ValidationError("text must be a string of at most 20000 characters");
      }
      value = stepArgs.text;
      detail = `Fill ${stepArgs.text.length} characters (text hidden)`;
    } else if (toolName === "browser_press") {
      if (typeof stepArgs.key !== "string" || !SUPPORTED_KEYS.has(stepArgs.key)) {
        throw new ValidationError("Unsupported key");
      }
      value = stepArgs.key;
      detail = `Press ${stepArgs.key}`;
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
    const riskTag = isSensitiveLabel(selector) ? " [HIGH RISK]" : "";
    return { toolName, target, selector, value, detail: `${detail}${riskTag}` };
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
        isolated_profile: true,
        approval: approvalBackendLabel(),
        desktop_input: false,
        transport: "stdio",
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
      return this.observe(Boolean(args.mark_elements));
    }
    if (name === "browser_open") {
      const url = typeof args.url === "string" ? args.url : "";
      this.assertAllowed(url);
      if (!this.approval(`Open a page in Aria's isolated browser?\n${url}`)) {
        throw new PermissionDeniedError("User approval was denied or unavailable");
      }
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
      const selector = this.resolveSelector(args);
      const target = this.page!.locator(selector);
      if ((await target.count()) !== 1 || !(await target.isVisible())) {
        throw new ValidationError("Selector must match exactly one visible element");
      }
      this.recordAction("browser_hover", selector, args);
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
    if (name === "browser_batch") {
      await this.assertFresh(args.observation_id);
      const rawSteps = args.steps;
      if (!Array.isArray(rawSteps) || rawSteps.length < 1 || rawSteps.length > 10) {
        throw new ValidationError("steps must be a list of 1..10 actions");
      }
      const prepared: Array<{ step: PreparedStep; raw: Record<string, unknown> }> = [];
      const lines: string[] = [];
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
        prepared.push({ step, raw: obj });
        lines.push(`${i + 1}. ${step.detail} -> ${step.selector}`);
      }
      const summary = `Batch (${prepared.length} steps)\nSite: ${normalizeOrigin(this.page!.url())}\n${lines.join("\n")}\nThis may submit or publish content. Approve only after reviewing the page.`;
      if (!this.approval(summary)) {
        throw new PermissionDeniedError("User approval was denied or unavailable");
      }
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
    const prompt = `${step.detail}\nSite: ${normalizeOrigin(this.page!.url())}\nTarget: ${step.selector}\nThis may submit or publish content. Approve only after reviewing the page.`;
    if (!this.approval(prompt)) {
      throw new PermissionDeniedError("User approval was denied or unavailable");
    }
    await this.assertFresh(args.observation_id);
    const prevFp = this.observation!.fingerprint;
    const prevUrl = this.page!.url();
    this.observation = null;
    this.recordAction(step.toolName, step.selector, args);
    await this.executeStep(step);
    return this.finalizeMutation(prevFp, prevUrl);
  }
}

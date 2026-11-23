import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { BrowserSession } from "./server.js";

function startFixtureServer(): Promise<{ server: http.Server; origin: string }> {
  const mainHtml = `<!doctype html>
<html>
  <head><title>Aria E2E Fixture</title></head>
  <body>
    <h1>Aria Computer Use E2E</h1>
    <div id="status">idle</div>
    <x-panel id="panel"></x-panel>
    <iframe id="child" src="/embed" width="320" height="80"></iframe>
    <div>
      <input id="file-in" type="file" />
      <span id="file-out">none</span>
    </div>
    <div>
      <a id="open-tab" href="/tab2" target="_blank">Open Tab 2</a>
      <a id="dl-link" href="/report.csv" download="report.csv">Download Report</a>
    </div>
    <script>
      console.error("fixture-console-error");
      const host = document.getElementById("panel");
      const root = host.attachShadow({ mode: "open" });
      root.innerHTML = '<button id="shadow-btn">Shadow Go</button>';
      root.getElementById("shadow-btn").addEventListener("click", () => {
        document.getElementById("status").textContent = "shadow-clicked";
      });
      document.getElementById("file-in").addEventListener("change", (e) => {
        const f = e.target.files && e.target.files[0];
        document.getElementById("file-out").textContent = f ? f.name : "none";
      });
    </script>
  </body>
</html>`;

  const embedHtml = `<!doctype html>
<html>
  <body>
    <button id="iframe-btn" onclick="document.getElementById('iframe-status').textContent='iframe-clicked'">Iframe Go</button>
    <span id="iframe-status">ready</span>
  </body>
</html>`;

  const tab2Html = `<!doctype html>
<html>
  <head><title>Second Tab</title></head>
  <body><h1>Second Tab Ready</h1></body>
</html>`;

  const server = http.createServer((req, res) => {
    if (req.url === "/embed") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(embedHtml);
      return;
    }
    if (req.url === "/tab2") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(tab2Html);
      return;
    }
    if (req.url === "/report.csv") {
      res.writeHead(200, {
        "content-type": "text/csv; charset=utf-8",
        "content-disposition": 'attachment; filename="report.csv"',
      });
      res.end("col1,col2\n1,ok\n");
      return;
    }
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(mainHtml);
  });

  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      if (!addr || typeof addr === "string") {
        throw new Error("Unexpected server address");
      }
      resolve({ server, origin: `http://127.0.0.1:${addr.port}` });
    });
  });
}

test("real headless Chromium E2E: Shadow DOM, iframe, upload, tabs, downloads, diagnostics", async (t) => {
  const { chromium } = await import("playwright");
  const exe = chromium.executablePath();
  if (!fs.existsSync(exe)) {
    if (process.env.ARIA_COMPUTER_E2E_REQUIRED === "1") {
      assert.fail(`Playwright Chromium binary missing at ${exe}`);
    }
    t.skip("Playwright Chromium binary not installed locally (set ARIA_COMPUTER_E2E_REQUIRED=1 in CI)");
    return;
  }

  const { server, origin } = await startFixtureServer();
  const uploadDir = fs.mkdtempSync(path.join(os.tmpdir(), "aria-e2e-upload-"));
  const downloadDir = fs.mkdtempSync(path.join(os.tmpdir(), "aria-e2e-download-"));
  const sampleFile = path.join(uploadDir, "sample.txt");
  fs.writeFileSync(sampleFile, "hello aria");

  const session = new BrowserSession([origin], {
    approval: () => true,
    approvalMode: "tiered",
    uploadRoot: uploadDir,
    downloadDir,
    headless: true,
  });

  try {
    const opened = await session.call("browser_open", { url: origin });
    assert.equal(opened.title, "Aria E2E Fixture");

    const obs = await session.call("browser_observe", {
      mark_elements: true,
      include_ax_tree: true,
    });
    assert.ok(typeof obs.ax_tree === "string" && obs.ax_tree.length > 0);
    const consoleErrors = obs.console_errors as string[];
    assert.ok(consoleErrors.some((e) => e.includes("fixture-console-error")));

    const elements = obs.elements as Array<{
      element_id: number;
      in_shadow_dom: boolean;
      frame_index: number;
      label: string;
    }>;
    const shadowBtn = elements.find((e) => e.in_shadow_dom && e.label.includes("Shadow Go"));
    const iframeBtn = elements.find((e) => e.frame_index === 1 && e.label.includes("Iframe Go"));
    assert.ok(shadowBtn, "Expected Shadow DOM button to be discovered");
    assert.ok(iframeBtn, "Expected same-origin iframe button to be discovered");

    // Click inside Shadow DOM via element_id
    const afterShadow = await session.call("browser_click", {
      observation_id: obs.observation_id,
      element_id: shadowBtn.element_id,
    });
    assert.ok(String(afterShadow.text).includes("shadow-clicked"));

    // Click inside same-origin iframe via element_id (re-resolved from fresh observation)
    const freshElements = afterShadow.elements as typeof elements;
    const freshIframeBtn = freshElements.find((e) => e.frame_index === 1 && e.label.includes("Iframe Go"));
    assert.ok(freshIframeBtn, "Expected iframe button in post-click observation");
    const afterIframe = await session.call("browser_click", {
      observation_id: afterShadow.observation_id,
      element_id: freshIframeBtn.element_id,
    });
    assert.ok(typeof afterIframe.observation_id === "string");

    // Upload file confined to uploadDir
    const afterUpload = await session.call("browser_upload", {
      observation_id: afterIframe.observation_id,
      selector: "#file-in",
      file_paths: [sampleFile],
    });
    assert.ok(String(afterUpload.text).includes("sample.txt"));

    // Trigger download and verify browser_downloads captures it
    const afterDl = await session.call("browser_click", {
      observation_id: afterUpload.observation_id,
      selector: "#dl-link",
    });
    await session.call("browser_wait", { wait_ms: 400 });
    const dlList = await session.call("browser_downloads", { action: "list" });
    assert.equal(dlList.downloads_enabled, true);
    const downloads = dlList.downloads as Array<{ filename: string; path: string; size_bytes: number }>;
    assert.ok(downloads.length >= 1, "Expected at least 1 captured download");
    assert.ok(downloads[0].filename.endsWith("report.csv"));
    assert.ok(fs.existsSync(downloads[0].path));
    assert.ok(downloads[0].size_bytes > 0);

    // Open second tab and switch/close via browser_tabs
    const freshObs = await session.call("browser_observe", {});
    await session.call("browser_click", {
      observation_id: freshObs.observation_id,
      selector: "#open-tab",
    });
    await session.call("browser_wait", { wait_ms: 400 });
    const tabList = await session.call("browser_tabs", { action: "list" });
    const tabs = tabList.tabs as Array<{ index: number; url: string }>;
    assert.equal(tabs.length, 2);
    const switched = await session.call("browser_tabs", { action: "switch", index: 1 });
    assert.equal(switched.title, "Second Tab");
    const closed = await session.call("browser_tabs", { action: "close", index: 1 });
    assert.equal(closed.remaining_tabs, 1);
    assert.ok(typeof afterDl.observation_id === "string");
  } finally {
    await session.close();
    server.close();
    fs.rmSync(uploadDir, { recursive: true, force: true });
    fs.rmSync(downloadDir, { recursive: true, force: true });
  }
});

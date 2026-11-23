#!/usr/bin/env node
import readline from "node:readline";
import { BrowserSession, dispatch } from "./server.js";

const MAX_LINE_BYTES = 100_000;

async function main(): Promise<void> {
  let session: BrowserSession;
  try {
    const rawOrigins = (process.env.ARIA_COMPUTER_ORIGINS ?? "").split(",");
    session = new BrowserSession(rawOrigins);
  } catch {
    process.stderr.write("Invalid ARIA_COMPUTER_ORIGINS\n");
    process.exit(2);
  }

  const cleanup = async () => {
    await session.close();
  };
  process.on("SIGINT", () => {
    void cleanup().finally(() => process.exit(0));
  });
  process.on("SIGTERM", () => {
    void cleanup().finally(() => process.exit(0));
  });

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of rl) {
    let reply: Record<string, unknown> | null;
    if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) {
      reply = {
        jsonrpc: "2.0",
        id: null,
        error: { code: -32600, message: "Request too large" },
      };
    } else {
      try {
        reply = await dispatch(session, JSON.parse(line));
      } catch {
        reply = {
          jsonrpc: "2.0",
          id: null,
          error: { code: -32700, message: "Parse error" },
        };
      }
    }
    if (reply !== null) {
      process.stdout.write(`${JSON.stringify(reply)}\n`);
    }
  }
  await cleanup();
}

void main();

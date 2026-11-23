import { BrowserSession, PermissionDeniedError, ValidationError } from "./session.js";
import { TOOLS } from "./tools.js";

export const PROTOCOL = "2024-11-05";

export interface JsonRpcRequest {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
}

export async function dispatch(
  session: BrowserSession,
  message: unknown,
): Promise<Record<string, unknown> | null> {
  if (!message || typeof message !== "object" || (message as JsonRpcRequest).jsonrpc !== "2.0") {
    return {
      jsonrpc: "2.0",
      id: null,
      error: { code: -32600, message: "Invalid Request" },
    };
  }
  const req = message as JsonRpcRequest;
  if (!("id" in req)) return null;

  const response: Record<string, unknown> = { jsonrpc: "2.0", id: req.id ?? null };
  const method = req.method;
  const params = req.params ?? {};

  try {
    if (method === "initialize") {
      response.result = {
        protocolVersion: PROTOCOL,
        capabilities: { tools: {} },
        serverInfo: { name: "aria-computer-use", version: "0.2.0" },
      };
    } else if (method === "ping") {
      response.result = {};
    } else if (method === "tools/list") {
      response.result = { tools: TOOLS };
    } else if (method === "tools/call") {
      if (
        !params ||
        typeof params !== "object" ||
        (params.arguments !== undefined && (typeof params.arguments !== "object" || params.arguments === null))
      ) {
        throw new ValidationError("Invalid tool arguments");
      }
      const name = String(params.name ?? "");
      const args = (params.arguments as Record<string, unknown>) ?? {};
      const result = await session.call(name, args);
      const image = typeof result._image === "string" ? result._image : null;
      delete result._image;
      const content: Array<Record<string, unknown>> = [
        { type: "text", text: JSON.stringify(result) },
      ];
      if (image) {
        content.push({ type: "image", mimeType: "image/png", data: image });
      }
      response.result = { content, isError: false };
    } else {
      response.error = { code: -32601, message: "Method not found" };
    }
  } catch (err) {
    const safe =
      err instanceof ValidationError || err instanceof PermissionDeniedError
        ? err.message
        : "Browser operation failed; observe again or check the local browser installation";
    response.result = {
      content: [{ type: "text", text: safe }],
      isError: true,
    };
  }
  return response;
}

export { BrowserSession, TOOLS };

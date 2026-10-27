#!/usr/bin/env node
// Always-on mode: serves the mail tools over HTTPS (via a tunnel) so claude.ai and the Claude
// mobile apps can use them as a custom connector. Protected by OAuth with a connector password.

import express from "express";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { mcpAuthRouter, getOAuthProtectedResourceMetadataUrl } from "@modelcontextprotocol/sdk/server/auth/router.js";
import { requireBearerAuth } from "@modelcontextprotocol/sdk/server/auth/middleware/bearerAuth.js";
import { loadConfig } from "./config.js";
import { MailOAuthProvider } from "./oauth.js";
import { createServer } from "./tools.js";

const config = loadConfig({ remote: true });
const { publicUrl, port, connectorPassword, dataDir } = config.remote;
const mcpUrl = new URL("/mcp", publicUrl);

const provider = new MailOAuthProvider({ password: connectorPassword, dataDir });
const app = express();
// The tunnel (Tailscale Funnel / Cloudflare) connects from this machine and sets X-Forwarded-For.
app.set("trust proxy", "loopback");

app.use(
  mcpAuthRouter({
    provider,
    issuerUrl: publicUrl,
    resourceServerUrl: mcpUrl,
    resourceName: "Mail",
  }),
);
app.post("/login", express.urlencoded({ extended: false, limit: "10kb" }), provider.handleLogin);

const auth = requireBearerAuth({ verifier: provider, resourceMetadataUrl: getOAuthProtectedResourceMetadataUrl(mcpUrl) });

app.post("/mcp", auth, express.json({ limit: "50mb" }), async (req, res) => {
  // Stateless: a fresh server and transport per request.
  const server = createServer(config);
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  res.on("close", () => {
    transport.close();
    server.close();
  });
  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error("[mcp]", err);
    if (!res.headersSent) res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "Internal error" }, id: null });
  }
});
app.all("/mcp", auth, (_req, res) => res.status(405).set("Allow", "POST").send("Method not allowed"));
app.get("/", (_req, res) => res.type("text").send("Mail connector is running."));

// Listen on this machine only; the tunnel provides the public HTTPS address.
app.listen(port, "127.0.0.1", () => {
  console.log(`Mail connector listening on http://127.0.0.1:${port}`);
  console.log(`Add this URL as a custom connector in Claude: ${mcpUrl.href}`);
});

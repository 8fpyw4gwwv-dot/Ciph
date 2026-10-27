// OAuth 2.1 authorization server for the always-on connector.
// Single user: whoever knows CONNECTOR_PASSWORD can link the connector to their Claude account.
// Clients, access tokens and refresh tokens are persisted (tokens only as SHA-256 hashes).

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {
  InvalidClientMetadataError,
  InvalidGrantError,
  InvalidTokenError,
} from "@modelcontextprotocol/sdk/server/auth/errors.js";

const ACCESS_TOKEN_TTL = 60 * 60; // seconds
const REFRESH_TOKEN_TTL = 90 * 24 * 60 * 60;
const CODE_TTL = 5 * 60 * 1000; // ms
const LOGIN_TTL = 10 * 60 * 1000;
const MAX_FAILURES = 5;
const LOCKOUT_WINDOW = 15 * 60 * 1000;
const MAX_CLIENTS = 50;

// Redirect targets a client may register: Claude's web/mobile callback, plus loopback for Claude Desktop/Code.
const ALLOWED_REDIRECTS = ["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"];
const isLoopback = (u) => u.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);

function redirectAllowed(uri) {
  if (ALLOWED_REDIRECTS.includes(uri)) return true;
  try {
    return isLoopback(new URL(uri));
  } catch {
    return false;
  }
}

const randomToken = () => crypto.randomBytes(32).toString("base64url");
const hash = (s) => crypto.createHash("sha256").update(s).digest("hex");
const nowSeconds = () => Math.floor(Date.now() / 1000);

const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export class MailOAuthProvider {
  constructor({ password, dataDir }) {
    this.passwordHash = Buffer.from(hash(password), "hex");
    this.file = path.join(dataDir, "auth.json");
    fs.mkdirSync(dataDir, { recursive: true });
    this.state = fs.existsSync(this.file)
      ? JSON.parse(fs.readFileSync(this.file, "utf8"))
      : { clients: {}, accessTokens: {}, refreshTokens: {} };
    this.pendingLogins = new Map();
    this.codes = new Map();
    this.failures = [];

    const provider = this;
    this.clientsStore = {
      getClient: (id) => provider.state.clients[id],
      registerClient(client) {
        const bad = (client.redirect_uris || []).filter((u) => !redirectAllowed(String(u)));
        if (!client.redirect_uris?.length || bad.length) {
          throw new InvalidClientMetadataError(`redirect_uri not allowed: ${bad.join(", ") || "(none)"}`);
        }
        const ids = Object.keys(provider.state.clients);
        if (ids.length >= MAX_CLIENTS) {
          const oldest = ids.sort((a, b) => provider.state.clients[a].client_id_issued_at - provider.state.clients[b].client_id_issued_at)[0];
          delete provider.state.clients[oldest];
        }
        provider.state.clients[client.client_id] = client;
        provider.save();
        return client;
      },
    };
  }

  save() {
    const now = nowSeconds();
    for (const kind of ["accessTokens", "refreshTokens"]) {
      for (const [k, v] of Object.entries(this.state[kind])) if (v.expiresAt < now) delete this.state[kind][k];
    }
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.state, null, 2), { mode: 0o600 });
    fs.renameSync(tmp, this.file);
  }

  // ---------- login page ----------

  async authorize(client, params, res) {
    const id = randomToken();
    this.pendingLogins.set(id, { clientId: client.client_id, params, expires: Date.now() + LOGIN_TTL });
    res.status(200).type("html").send(this.loginPage(id, client, params));
  }

  loginPage(id, client, params, error) {
    const target = new URL(params.redirectUri).host;
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mail connector sign-in</title>
<style>
  body { font-family: system-ui, sans-serif; background: #f4f4f5; margin: 0; display: grid; place-items: center; min-height: 100vh; }
  form { background: #fff; padding: 28px; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.08); width: min(360px, calc(100vw - 32px)); box-sizing: border-box; }
  h1 { font-size: 20px; margin: 0 0 8px; }
  p { color: #52525b; font-size: 14px; line-height: 1.45; }
  input, button { width: 100%; box-sizing: border-box; font-size: 16px; padding: 10px 12px; border-radius: 8px; }
  input { border: 1px solid #d4d4d8; margin: 8px 0 16px; }
  button { border: 0; background: #18181b; color: #fff; cursor: pointer; }
  .error { color: #b91c1c; font-weight: 600; }
</style></head>
<body><form method="post" action="login">
  <h1>Connect your mailbox</h1>
  <p><strong>${escapeHtml(client.client_name || "An app")}</strong> on <strong>${escapeHtml(target)}</strong> wants to read and send email for you.</p>
  ${error ? `<p class="error">${escapeHtml(error)}</p>` : ""}
  <input type="hidden" name="request" value="${escapeHtml(id)}">
  <label for="password">Connector password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
  <button type="submit">Allow access</button>
</form></body></html>`;
  }

  // POST /login
  handleLogin = (req, res) => {
    const id = String(req.body?.request || "");
    const pending = this.pendingLogins.get(id);
    if (!pending || pending.expires < Date.now()) {
      this.pendingLogins.delete(id);
      return res.status(400).type("text").send("This sign-in link has expired. Start connecting again from Claude.");
    }
    const client = this.state.clients[pending.clientId];

    this.failures = this.failures.filter((t) => t > Date.now() - LOCKOUT_WINDOW);
    if (this.failures.length >= MAX_FAILURES) {
      const minutes = Math.ceil((this.failures[0] + LOCKOUT_WINDOW - Date.now()) / 60000);
      return res.status(429).type("html").send(this.loginPage(id, client, pending.params, `Too many wrong passwords. Try again in ${minutes} minutes.`));
    }

    const given = crypto.createHash("sha256").update(String(req.body?.password || "")).digest();
    if (!crypto.timingSafeEqual(given, this.passwordHash)) {
      this.failures.push(Date.now());
      console.warn(`[auth] wrong connector password from ${req.ip} (${this.failures.length}/${MAX_FAILURES})`);
      return res.status(401).type("html").send(this.loginPage(id, client, pending.params, "Wrong password."));
    }

    this.pendingLogins.delete(id);
    this.failures = [];
    const code = randomToken();
    this.codes.set(code, { clientId: pending.clientId, params: pending.params, expires: Date.now() + CODE_TTL });
    console.log(`[auth] access granted to ${client.client_name || pending.clientId}`);

    const target = new URL(pending.params.redirectUri);
    target.searchParams.set("code", code);
    if (pending.params.state !== undefined) target.searchParams.set("state", pending.params.state);
    res.redirect(302, target.href);
  };

  // ---------- token endpoint ----------

  takeCode(client, code) {
    const entry = this.codes.get(code);
    if (!entry || entry.clientId !== client.client_id || entry.expires < Date.now()) {
      throw new InvalidGrantError("Invalid or expired authorization code");
    }
    return entry;
  }

  async challengeForAuthorizationCode(client, code) {
    return this.takeCode(client, code).params.codeChallenge;
  }

  async exchangeAuthorizationCode(client, code, _verifier, redirectUri) {
    const entry = this.takeCode(client, code);
    if (redirectUri && redirectUri !== entry.params.redirectUri) throw new InvalidGrantError("redirect_uri mismatch");
    this.codes.delete(code);
    return this.issueTokens(client.client_id);
  }

  async exchangeRefreshToken(client, refreshToken) {
    const key = hash(refreshToken);
    const entry = this.state.refreshTokens[key];
    if (!entry || entry.clientId !== client.client_id || entry.expiresAt < nowSeconds()) {
      throw new InvalidGrantError("Invalid or expired refresh token");
    }
    delete this.state.refreshTokens[key]; // rotate
    return this.issueTokens(client.client_id);
  }

  issueTokens(clientId) {
    const access = randomToken();
    const refresh = randomToken();
    this.state.accessTokens[hash(access)] = { clientId, expiresAt: nowSeconds() + ACCESS_TOKEN_TTL };
    this.state.refreshTokens[hash(refresh)] = { clientId, expiresAt: nowSeconds() + REFRESH_TOKEN_TTL };
    this.save();
    return { access_token: access, token_type: "bearer", expires_in: ACCESS_TOKEN_TTL, refresh_token: refresh };
  }

  async verifyAccessToken(token) {
    const entry = this.state.accessTokens[hash(token)];
    if (!entry || entry.expiresAt < nowSeconds()) throw new InvalidTokenError("Invalid or expired access token");
    return { token, clientId: entry.clientId, scopes: [], expiresAt: entry.expiresAt };
  }

  async revokeToken(client, { token }) {
    const key = hash(token);
    for (const kind of ["accessTokens", "refreshTokens"]) {
      if (this.state[kind][key]?.clientId === client.client_id) delete this.state[kind][key];
    }
    this.save();
  }
}

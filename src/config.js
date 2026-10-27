// Reads connection settings from environment variables, or from a .env file in the project folder.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const projectDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const envFile = path.join(projectDir, ".env");

const bool = (value, fallback) =>
  value === undefined || value === "" ? fallback : ["1", "true", "yes"].includes(value.toLowerCase());

function fail(message) {
  console.error(`${message} See README.md.`);
  process.exit(1);
}

// Mailbox 1 uses MAIL_USER, IMAP_HOST, ...; mailbox n (2-9) uses MAILn_USER, MAILn_IMAP_HOST, ...
function readAccount(env, n) {
  const mail = n === 1 ? "MAIL_" : `MAIL${n}_`;
  const server = n === 1 ? "" : `MAIL${n}_`;
  const get = (prefix, key) => env[prefix + key];
  if (n > 1 && !get(mail, "USER")) return null;

  const missing = [[mail, "USER"], [mail, "PASSWORD"], [server, "IMAP_HOST"], [server, "SMTP_HOST"]]
    .filter(([p, k]) => !get(p, k))
    .map(([p, k]) => p + k);
  if (missing.length) fail(`Missing required settings: ${missing.join(", ")}.`);

  const imapSecure = bool(get(server, "IMAP_SECURE"), true);
  const smtpPort = Number(get(server, "SMTP_PORT") || 465);
  const from = get(mail, "FROM") || get(mail, "USER");
  return {
    id: (from.match(/<([^>]+)>/)?.[1] || from).trim().toLowerCase(),
    user: get(mail, "USER"),
    password: get(mail, "PASSWORD"),
    from,
    imap: {
      host: get(server, "IMAP_HOST"),
      port: Number(get(server, "IMAP_PORT") || (imapSecure ? 993 : 143)),
      secure: imapSecure,
    },
    smtp: {
      host: get(server, "SMTP_HOST"),
      port: smtpPort,
      // Port 465 uses implicit TLS; 587 starts plain and upgrades with STARTTLS.
      secure: bool(get(server, "SMTP_SECURE"), smtpPort === 465),
    },
  };
}

export function loadConfig({ remote = false } = {}) {
  // Values already in the environment (e.g. from Claude Desktop's config) win over .env.
  if (fs.existsSync(envFile)) process.loadEnvFile(envFile);
  const env = process.env;

  const required = remote ? ["PUBLIC_URL", "CONNECTOR_PASSWORD"] : [];
  const missing = required.filter((k) => !env[k]);
  if (missing.length) fail(`Missing required settings: ${missing.join(", ")}.`);

  const accounts = [];
  for (let n = 1; n <= 9; n++) {
    const acc = readAccount(env, n);
    if (acc) accounts.push(acc);
  }
  if (!accounts.length) fail("No mailbox configured (MAIL_USER, MAIL_PASSWORD, IMAP_HOST, SMTP_HOST).");
  const ids = accounts.map((a) => a.id);
  if (new Set(ids).size !== ids.length) fail("Two mailboxes have the same email address (MAIL_FROM).");

  const config = {
    accounts,
    saveSent: bool(env.SAVE_SENT, true),
    attachmentDir: env.ATTACHMENT_DIR,
  };

  if (remote) {
    if (env.CONNECTOR_PASSWORD.length < 12) fail("CONNECTOR_PASSWORD must be at least 12 characters.");
    const publicUrl = new URL(env.PUBLIC_URL);
    if (publicUrl.protocol !== "https:") fail("PUBLIC_URL must start with https://.");
    config.remote = {
      publicUrl,
      port: Number(env.PORT || 8787),
      connectorPassword: env.CONNECTOR_PASSWORD,
      dataDir: path.resolve(projectDir, env.DATA_DIR || "data"),
    };
    // The only folder Claude may attach files from or save attachments to.
    config.fileRoot = path.resolve(env.FILE_ROOT || env.ATTACHMENT_DIR || path.join(os.homedir(), "Downloads"));
    const rel = path.relative(config.fileRoot, projectDir);
    if (!rel.startsWith("..") && !path.isAbsolute(rel)) {
      fail(`The connector folder is inside ${config.fileRoot}, which Claude can read. Move it, e.g. to Documents.`);
    }
  }

  return config;
}

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

export function loadConfig({ remote = false } = {}) {
  // Values already in the environment (e.g. from Claude Desktop's config) win over .env.
  if (fs.existsSync(envFile)) process.loadEnvFile(envFile);
  const env = process.env;

  const required = ["MAIL_USER", "MAIL_PASSWORD", "IMAP_HOST", "SMTP_HOST"];
  if (remote) required.push("PUBLIC_URL", "CONNECTOR_PASSWORD");
  const missing = required.filter((k) => !env[k]);
  if (missing.length) fail(`Missing required settings: ${missing.join(", ")}.`);

  const imapSecure = bool(env.IMAP_SECURE, true);
  const smtpPort = Number(env.SMTP_PORT || 465);

  const config = {
    user: env.MAIL_USER,
    password: env.MAIL_PASSWORD,
    from: env.MAIL_FROM || env.MAIL_USER,
    saveSent: bool(env.SAVE_SENT, true),
    attachmentDir: env.ATTACHMENT_DIR,
    imap: {
      host: env.IMAP_HOST,
      port: Number(env.IMAP_PORT || (imapSecure ? 993 : 143)),
      secure: imapSecure,
    },
    smtp: {
      host: env.SMTP_HOST,
      port: smtpPort,
      // Port 465 uses implicit TLS; 587 starts plain and upgrades with STARTTLS.
      secure: bool(env.SMTP_SECURE, smtpPort === 465),
    },
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

#!/usr/bin/env node
// One-step setup: reads mail server settings from Thunderbird and checks the login. Then either
// registers the connector in Claude Desktop's config (default), or with --remote prepares this
// computer to serve it around the clock: .env file, public HTTPS address, background service.

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { ImapFlow } from "imapflow";
import nodemailer from "nodemailer";

const srcDir = path.dirname(fileURLToPath(import.meta.url));
const projectDir = path.resolve(srcDir, "..");
const serverScript = path.join(srcDir, "index.js");
const remote = process.argv.includes("--remote");
const REMOTE_PORT = 8787;
const TASK_NAME = "Claude Mail Connector";
const home = os.homedir();
const appData = process.env.APPDATA || path.join(home, "AppData", "Roaming");

const thunderbirdRoots = {
  win32: [path.join(appData, "Thunderbird", "Profiles")],
  darwin: [path.join(home, "Library", "Thunderbird", "Profiles")],
  linux: [path.join(home, ".thunderbird"), path.join(home, "snap", "thunderbird", "common", ".thunderbird")],
}[process.platform] || [];

const claudeConfigPath = {
  win32: path.join(appData, "Claude", "claude_desktop_config.json"),
  darwin: path.join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json"),
}[process.platform] || path.join(home, ".config", "Claude", "claude_desktop_config.json");

// ---------- prompts ----------

const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
const ask = (q) => new Promise((resolve) => rl.question(q, (a) => resolve(a.trim())));

function askHidden(q) {
  return new Promise((resolve) => {
    const write = rl._writeToOutput.bind(rl);
    rl._writeToOutput = (s) => (s.includes(q) ? write(s) : write(""));
    rl.question(q, (a) => {
      rl._writeToOutput = write;
      process.stdout.write("\n");
      resolve(a);
    });
  });
}

async function choose(label, items, describe) {
  if (items.length === 1) return items[0];
  console.log(`\n${label}:`);
  items.forEach((item, i) => console.log(`  ${i + 1}. ${describe(item)}`));
  for (;;) {
    const n = Number(await ask(`Choose 1-${items.length}: `));
    if (n >= 1 && n <= items.length) return items[n - 1];
  }
}

// ---------- Thunderbird ----------

function findProfiles() {
  const profiles = [];
  for (const root of thunderbirdRoots) {
    if (!fs.existsSync(root)) continue;
    for (const name of fs.readdirSync(root)) {
      const prefs = path.join(root, name, "prefs.js");
      if (fs.existsSync(prefs)) profiles.push({ name, prefs, mtime: fs.statSync(prefs).mtimeMs });
    }
  }
  return profiles.sort((a, b) => b.mtime - a.mtime);
}

function readPrefs(file) {
  const prefs = {};
  const re = /user_pref\("([^"]+)",\s*(.+?)\);/g;
  for (const [, key, raw] of fs.readFileSync(file, "utf8").matchAll(re)) {
    try {
      prefs[key] = JSON.parse(raw);
    } catch {
      prefs[key] = raw;
    }
  }
  return prefs;
}

function imapAccounts(prefs) {
  const accounts = [];
  const accountIds = String(prefs["mail.accountmanager.accounts"] || "").split(",").filter(Boolean);
  for (const id of accountIds) {
    const server = prefs[`mail.account.${id}.server`];
    if (!server || prefs[`mail.server.${server}.type`] !== "imap") continue;

    const s = (k) => prefs[`mail.server.${server}.${k}`];
    const identity = String(prefs[`mail.account.${id}.identities`] || "").split(",")[0];
    const email = prefs[`mail.identity.${identity}.useremail`];
    const fullName = prefs[`mail.identity.${identity}.fullName`];
    const smtpId = prefs[`mail.identity.${identity}.smtpServer`] || defaultSmtp(prefs);
    const t = (k) => prefs[`mail.smtpserver.${smtpId}.${k}`];

    // Thunderbird socketType: 2 = STARTTLS, 3 = SSL/TLS. SMTP uses "try_ssl" with the same values.
    const imapSsl = s("socketType") !== 2 && s("socketType") !== 0;
    const smtpSsl = t("try_ssl") === 3;
    accounts.push({
      email,
      fullName,
      user: s("userName") || email,
      imapHost: s("realhostname") || s("hostname"),
      imapPort: s("port") || (imapSsl ? 993 : 143),
      imapSecure: imapSsl,
      smtpHost: t("hostname"),
      smtpPort: t("port") || (smtpSsl ? 465 : 587),
      smtpSecure: smtpSsl,
    });
  }
  return accounts;
}

function defaultSmtp(prefs) {
  return prefs["mail.smtp.defaultserver"] || String(prefs["mail.smtpservers"] || "").split(",")[0];
}

// ---------- connection check ----------

async function checkLogin(acc, password) {
  const imap = new ImapFlow({
    host: acc.imapHost,
    port: acc.imapPort,
    secure: acc.imapSecure,
    auth: { user: acc.user, pass: password },
    logger: false,
  });
  await imap.connect();
  const inbox = await imap.status("INBOX", { messages: true, unseen: true });
  await imap.logout();

  await nodemailer
    .createTransport({
      host: acc.smtpHost,
      port: acc.smtpPort,
      secure: acc.smtpSecure,
      auth: { user: acc.user, pass: password },
    })
    .verify();
  return inbox;
}

// ---------- Claude Desktop ----------

function writeClaudeConfig(name, acc, password) {
  let config = {};
  if (fs.existsSync(claudeConfigPath)) {
    config = JSON.parse(fs.readFileSync(claudeConfigPath, "utf8") || "{}");
    fs.copyFileSync(claudeConfigPath, `${claudeConfigPath}.bak`);
  } else {
    fs.mkdirSync(path.dirname(claudeConfigPath), { recursive: true });
  }
  config.mcpServers ||= {};
  config.mcpServers[name] = {
    command: process.execPath,
    args: [serverScript],
    env: {
      MAIL_USER: acc.user,
      MAIL_PASSWORD: password,
      MAIL_FROM: acc.fullName ? `${acc.fullName} <${acc.email}>` : acc.email,
      IMAP_HOST: acc.imapHost,
      IMAP_PORT: String(acc.imapPort),
      IMAP_SECURE: String(acc.imapSecure),
      SMTP_HOST: acc.smtpHost,
      SMTP_PORT: String(acc.smtpPort),
      SMTP_SECURE: String(acc.smtpSecure),
    },
  };
  fs.writeFileSync(claudeConfigPath, JSON.stringify(config, null, 2));
}

// ---------- always-on (remote) mode ----------

function findTailscale() {
  const candidates = {
    win32: ["C:\\Program Files\\Tailscale\\tailscale.exe", "tailscale"],
    darwin: ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "tailscale"],
  }[process.platform] || ["tailscale"];
  for (const bin of candidates) {
    try {
      execFileSync(bin, ["version"], { stdio: "ignore" });
      return bin;
    } catch {}
  }
  return null;
}

async function publicAddress() {
  const ts = findTailscale();
  if (ts) {
    try {
      const status = JSON.parse(execFileSync(ts, ["status", "--json"], { encoding: "utf8" }));
      const dns = status.Self?.DNSName?.replace(/\.$/, "");
      if (dns) {
        console.log(`\nTailscale is set up on this computer (${dns}).`);
        console.log("Turning on Tailscale Funnel. If it shows a link, open it, click Enable, and come back here.");
        execFileSync(ts, ["funnel", "--bg", String(REMOTE_PORT)], { stdio: "inherit", timeout: 10 * 60 * 1000 });
        return `https://${dns}`;
      }
    } catch (err) {
      console.log(`Couldn't turn on Tailscale Funnel automatically (${err.message.split("\n")[0]}).`);
    }
  } else {
    console.log("\nTailscale is not installed or not signed in. See README.md, section \"Always-on setup\".");
  }
  console.log("You can also enter a public HTTPS address yourself (e.g. from a Cloudflare Tunnel).");
  for (;;) {
    const url = (await ask("Public address (https://...): ")).replace(/\/+$/, "");
    if (/^https:\/\/[^\s/]+$/.test(url)) return url;
    console.log("It must look like https://name.example.com");
  }
}

function readEnvFile() {
  const file = path.join(projectDir, ".env");
  if (!fs.existsSync(file)) return {};
  const values = {};
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const m = line.match(/^([A-Z_]+)=(['"`])(.*)\2$/);
    if (m) values[m[1]] = m[3];
  }
  return values;
}

async function askConnectorPassword() {
  const existing = readEnvFile().CONNECTOR_PASSWORD;
  if (existing && (await ask("\nKeep your existing connector password? [Y/n] ")).toLowerCase() !== "n") return existing;

  console.log("\nChoose a connector password. You type it once on claude.ai when you add the connector,");
  console.log("and it keeps anyone else from reading your mail. At least 12 characters. Press Enter to generate one.");
  for (;;) {
    const pw = await askHidden("Connector password: ");
    if (!pw) {
      const generated = crypto.randomBytes(12).toString("base64url");
      console.log(`\nYour connector password: ${generated}`);
      console.log("Save it in your password manager or write it down now.");
      return generated;
    }
    if (pw.length < 12) {
      console.log("Too short. Use at least 12 characters.");
      continue;
    }
    if ((await askHidden("Repeat it: ")) !== pw) {
      console.log("They didn't match. Try again.");
      continue;
    }
    return pw;
  }
}

function envLine(key, value) {
  value = String(value);
  // Single quotes keep the value literal; fall back to other quotes if the value contains one.
  const quote = ["'", '"', "`"].find((q) => !value.includes(q));
  if (!quote || /[\r\n]/.test(value)) throw new Error(`${key} contains characters that can't be stored.`);
  return `${key}=${quote}${value}${quote}`;
}

function writeEnv(acc, password, publicUrl, connectorPassword) {
  const values = {
    MAIL_USER: acc.user,
    MAIL_PASSWORD: password,
    MAIL_FROM: acc.fullName ? `${acc.fullName} <${acc.email}>` : acc.email,
    IMAP_HOST: acc.imapHost,
    IMAP_PORT: acc.imapPort,
    IMAP_SECURE: acc.imapSecure,
    SMTP_HOST: acc.smtpHost,
    SMTP_PORT: acc.smtpPort,
    SMTP_SECURE: acc.smtpSecure,
    PUBLIC_URL: publicUrl,
    PORT: REMOTE_PORT,
    CONNECTOR_PASSWORD: connectorPassword,
    // The service runs as a system account, so point saved attachments at this user's Downloads.
    ATTACHMENT_DIR: path.join(home, "Downloads"),
  };
  const lines = ["# Settings for the always-on mail connector. Keep this file private.", ...Object.entries(values).map(([k, v]) => envLine(k, v))];
  fs.writeFileSync(path.join(projectDir, ".env"), lines.join("\n") + "\n", { mode: 0o600 });
}

const psQuote = (s) => `'${String(s).replace(/'/g, "''")}'`;

function installWindowsService() {
  const script = path.join(srcDir, "service.js");
  const ps = [
    `$action = New-ScheduledTaskAction -Execute ${psQuote(process.execPath)} -Argument ${psQuote(`"${script}"`)} -WorkingDirectory ${psQuote(projectDir)}`,
    "$trigger = New-ScheduledTaskTrigger -AtStartup",
    "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)",
    "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest",
    `Stop-ScheduledTask -TaskName ${psQuote(TASK_NAME)} -ErrorAction SilentlyContinue`,
    `Register-ScheduledTask -TaskName ${psQuote(TASK_NAME)} -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null`,
    `Start-ScheduledTask -TaskName ${psQuote(TASK_NAME)}`,
  ].join("; ");
  execFileSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], { stdio: "inherit" });
}

async function waitFor(url, check, seconds) {
  for (let i = 0; i < seconds; i++) {
    try {
      const res = await fetch(url);
      if (res.ok && check(await res.text())) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

async function setupRemote(acc, password) {
  const publicUrl = await publicAddress();
  const connectorPassword = await askConnectorPassword();
  writeEnv(acc, password, publicUrl, connectorPassword);
  console.log(`\nSaved settings to ${path.join(projectDir, ".env")}`);

  if (process.platform === "win32") {
    console.log(`Installing background service "${TASK_NAME}" (starts automatically with Windows)...`);
    installWindowsService();
  } else {
    console.log("\nStart the connector with: npm run service");
    console.log("To start it automatically at boot, see README.md, section \"Always-on setup\".");
    return;
  }

  process.stdout.write("Waiting for the connector to start... ");
  if (!(await waitFor(`http://127.0.0.1:${REMOTE_PORT}/`, (t) => t.includes("running"), 30))) {
    console.log(`it didn't start. Check ${path.join(projectDir, "data", "connector.log")}`);
    return;
  }
  console.log("running.");
  process.stdout.write("Checking it's reachable from the internet... ");
  const reachable = await waitFor(`${publicUrl}/.well-known/oauth-authorization-server`, (t) => t.includes("authorization_endpoint"), 30);
  console.log(reachable ? "yes." : "not yet. Check that Tailscale Funnel is on (see README.md).");

  console.log("\nLast step, in Claude (claude.ai in a browser):");
  console.log("  Settings → Connectors → Add custom connector");
  console.log(`  Name: Email     URL: ${publicUrl}/mcp`);
  console.log("  Click Connect and enter your connector password.");
  console.log("It then works in the Claude app on your iPhone too.");
}

// ---------- main ----------

async function manualAccount() {
  console.log("\nEnter your mail server settings (Thunderbird: Account Settings → Server Settings).");
  const email = await ask("Email address: ");
  const imapHost = await ask("Incoming (IMAP) server: ");
  const smtpHost = await ask("Outgoing (SMTP) server: ");
  const smtpPort = Number((await ask("Outgoing port [465]: ")) || 465);
  return { email, user: email, imapHost, imapPort: 993, imapSecure: true, smtpHost, smtpPort, smtpSecure: smtpPort === 465 };
}

async function main() {
  console.log(remote ? "Always-on mail connector setup\n" : "Mail connector setup for Claude Desktop\n");

  let acc;
  const profiles = findProfiles();
  const accounts = profiles.flatMap((p) => imapAccounts(readPrefs(p.prefs)));
  if (accounts.length) {
    acc = await choose("Found these Thunderbird accounts", accounts, (a) => `${a.email} (${a.imapHost})`);
    console.log(`\nUsing ${acc.email}`);
    console.log(`  Incoming: ${acc.imapHost}:${acc.imapPort}  Outgoing: ${acc.smtpHost}:${acc.smtpPort}`);
  } else {
    console.log("No Thunderbird IMAP account found.");
    acc = await manualAccount();
  }
  if (!acc.smtpHost) acc.smtpHost = await ask("Outgoing (SMTP) server: ");

  for (;;) {
    const password = await askHidden(`\nPassword for ${acc.user}: `);
    process.stdout.write("Checking login... ");
    try {
      const inbox = await checkLogin(acc, password);
      console.log(`OK. Inbox has ${inbox.messages} messages (${inbox.unseen} unread).`);
      if (remote) {
        await setupRemote(acc, password);
      } else {
        const name = "email";
        writeClaudeConfig(name, acc, password);
        console.log(`\nAdded "${name}" connector to ${claudeConfigPath}`);
        console.log("Quit Claude Desktop completely and reopen it. Then ask Claude: \"Show my unread emails\".");
      }
      break;
    } catch (err) {
      console.log(`failed: ${err.responseText || err.response || err.message}`);
      if ((await ask("Try again? [Y/n] ")).toLowerCase() === "n") break;
    }
  }
  rl.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

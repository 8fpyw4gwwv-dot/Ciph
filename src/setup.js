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

function listDirs(dir) {
  try {
    return fs.readdirSync(dir, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => path.join(dir, d.name));
  } catch {
    return [];
  }
}

// Folders that may hold Thunderbird's profiles.ini. On Windows this covers every user on the
// computer (running as administrator can switch to another account) and the Microsoft Store version.
function thunderbirdBaseDirs() {
  if (process.platform === "win32") {
    const homes = new Set([home, ...listDirs(path.join((process.env.SystemDrive || "C:") + path.sep, "Users"))]);
    const dirs = [path.join(appData, "Thunderbird")];
    for (const h of homes) {
      dirs.push(path.join(h, "AppData", "Roaming", "Thunderbird"));
      for (const pkg of listDirs(path.join(h, "AppData", "Local", "Packages"))) {
        if (/thunderbird/i.test(path.basename(pkg))) dirs.push(path.join(pkg, "LocalCache", "Roaming", "Thunderbird"));
      }
    }
    return dirs;
  }
  if (process.platform === "darwin") return [path.join(home, "Library", "Thunderbird")];
  return [
    path.join(home, ".thunderbird"),
    path.join(home, "snap", "thunderbird", "common", ".thunderbird"),
    path.join(home, ".var", "app", "org.mozilla.Thunderbird", ".thunderbird"),
  ];
}

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

function profileDirs(base) {
  const dirs = [...listDirs(path.join(base, "Profiles")), ...listDirs(base)];
  // profiles.ini can point to profiles stored anywhere (e.g. another drive).
  const ini = path.join(base, "profiles.ini");
  if (fs.existsSync(ini)) {
    let relative = true;
    for (const line of fs.readFileSync(ini, "utf8").split(/\r?\n/)) {
      if (line.startsWith("[")) relative = true;
      const m = line.match(/^(IsRelative|Path)=(.*)$/);
      if (m?.[1] === "IsRelative") relative = m[2].trim() !== "0";
      if (m?.[1] === "Path") dirs.push(relative ? path.join(base, m[2].trim()) : m[2].trim());
    }
  }
  return dirs;
}

function findProfiles() {
  const seen = new Set();
  const profiles = [];
  for (const base of thunderbirdBaseDirs()) {
    for (const dir of profileDirs(base)) {
      const prefs = path.join(dir, "prefs.js");
      if (!fs.existsSync(prefs)) continue;
      const key = fs.realpathSync(prefs).toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      profiles.push({ dir, prefs, mtime: fs.statSync(prefs).mtimeMs });
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

// Thunderbird accounts that can be reached over IMAP. POP3 accounts are included with a guessed
// IMAP server (pop.example.com -> imap.example.com); the login check confirms the guess.
function mailAccounts(prefs) {
  const accounts = [];
  const accountIds = String(prefs["mail.accountmanager.accounts"] || "").split(",").filter(Boolean);
  for (const id of accountIds) {
    const server = prefs[`mail.account.${id}.server`];
    const type = prefs[`mail.server.${server}.type`];
    if (!server || !["imap", "pop3"].includes(type)) continue;

    const s = (k) => prefs[`mail.server.${server}.${k}`];
    const identity = String(prefs[`mail.account.${id}.identities`] || "").split(",")[0];
    const email = prefs[`mail.identity.${identity}.useremail`];
    const fullName = prefs[`mail.identity.${identity}.fullName`];
    const smtpId = prefs[`mail.identity.${identity}.smtpServer`] || defaultSmtp(prefs);
    const t = (k) => prefs[`mail.smtpserver.${smtpId}.${k}`];
    const host = s("realhostname") || s("hostname");

    // Thunderbird socketType: 0 = none, 2 = STARTTLS, 3 = SSL/TLS. SMTP uses "try_ssl" with the same values.
    const smtpSsl = t("try_ssl") === 3;
    const account = {
      email,
      fullName,
      type,
      user: s("userName") || email,
      smtpHost: t("hostname"),
      smtpPort: t("port") || (smtpSsl ? 465 : 587),
      smtpSecure: smtpSsl,
    };
    if (type === "imap") {
      const imapSsl = s("socketType") !== 2 && s("socketType") !== 0;
      Object.assign(account, { imapHost: host, imapPort: s("port") || (imapSsl ? 993 : 143), imapSecure: imapSsl });
    } else {
      Object.assign(account, {
        imapHost: /^pop3?\./i.test(host) ? host.replace(/^pop3?\./i, "imap.") : host,
        imapPort: 993,
        imapSecure: true,
        popHost: host,
        // POP3 without "leave on server" deletes mail from the server after downloading it.
        leaveOnServer: s("leave_on_server") === true,
      });
    }
    accounts.push(account);
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

// Environment variables for the verified mailboxes: MAIL_USER, IMAP_HOST, ... for the first,
// MAIL2_USER, MAIL2_IMAP_HOST, ... for the second, and so on.
function accountEnv(mailboxes) {
  const env = {};
  mailboxes.forEach(({ acc, password }, i) => {
    const mail = i === 0 ? "MAIL_" : `MAIL${i + 1}_`;
    const server = i === 0 ? "" : `MAIL${i + 1}_`;
    Object.assign(env, {
      [`${mail}USER`]: acc.user,
      [`${mail}PASSWORD`]: password,
      [`${mail}FROM`]: acc.fullName ? `${acc.fullName} <${acc.email}>` : acc.email,
      [`${server}IMAP_HOST`]: acc.imapHost,
      [`${server}IMAP_PORT`]: String(acc.imapPort),
      [`${server}IMAP_SECURE`]: String(acc.imapSecure),
      [`${server}SMTP_HOST`]: acc.smtpHost,
      [`${server}SMTP_PORT`]: String(acc.smtpPort),
      [`${server}SMTP_SECURE`]: String(acc.smtpSecure),
    });
  });
  return env;
}

function writeClaudeConfig(name, mailboxes) {
  let config = {};
  if (fs.existsSync(claudeConfigPath)) {
    config = JSON.parse(fs.readFileSync(claudeConfigPath, "utf8") || "{}");
    fs.copyFileSync(claudeConfigPath, `${claudeConfigPath}.bak`);
  } else {
    fs.mkdirSync(path.dirname(claudeConfigPath), { recursive: true });
  }
  config.mcpServers ||= {};
  config.mcpServers[name] = { command: process.execPath, args: [serverScript], env: accountEnv(mailboxes) };
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
    const m = line.match(/^([A-Z0-9_]+)=(['"`])(.*)\2$/);
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

function writeEnv(mailboxes, publicUrl, connectorPassword) {
  const values = {
    ...accountEnv(mailboxes),
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

async function setupRemote(mailboxes) {
  const publicUrl = await publicAddress();
  const connectorPassword = await askConnectorPassword();
  writeEnv(mailboxes, publicUrl, connectorPassword);
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

// Known providers, so only the email address, login name and password need typing.
const PRESETS = {
  "easyapotheken.de": {
    imap: "mail.easyapotheken.de",
    smtp: "mail.easyapotheken.de",
    userHint: 'Login name = "Benutzername" on your easyWebmail access sheet, e.g. info@nedderfeld.easyapotheken.de',
  },
  "gmx.de": { imap: "imap.gmx.net", smtp: "mail.gmx.net" },
  "gmx.net": { imap: "imap.gmx.net", smtp: "mail.gmx.net" },
  "web.de": { imap: "imap.web.de", smtp: "smtp.web.de", smtpPort: 587 },
  "t-online.de": { imap: "secureimap.t-online.de", smtp: "securesmtp.t-online.de" },
  "yahoo.com": { imap: "imap.mail.yahoo.com", smtp: "smtp.mail.yahoo.com" },
  "icloud.com": { imap: "imap.mail.me.com", smtp: "smtp.mail.me.com", smtpPort: 587 },
};

// "host" or "host:port" -> [host, port]
function parseHost(input, defaultPort) {
  const [, host, port] = input.trim().match(/^([^:\s]+)(?::(\d+))?$/) || [];
  return [host, port ? Number(port) : defaultPort];
}

async function manualAccount() {
  console.log("\nEnter your mailbox settings.");
  let email;
  while (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test((email = await ask("Email address: ")))) console.log("That doesn't look like an email address.");
  const domain = email.split("@")[1].toLowerCase();
  const preset = PRESETS[domain];
  let imapHost, imapPort = 993, smtpHost, smtpPort;
  if (preset) {
    ({ imap: imapHost, smtp: smtpHost, smtpPort = 465 } = preset);
    console.log(`Server settings for ${domain}: ${imapHost} (IMAP 993), ${smtpHost} (SMTP ${smtpPort}), encrypted.`);
  } else {
    console.log("Enter each server as name or name:port. Default ports: IMAP 993, SMTP 465.");
    [imapHost, imapPort] = parseHost((await ask(`Incoming (IMAP) server [mail.${domain}]: `)) || `mail.${domain}`, 993);
    [smtpHost, smtpPort] = parseHost((await ask(`Outgoing (SMTP) server [${imapHost}]: `)) || imapHost, 465);
  }
  if (preset?.userHint) console.log(preset.userHint);
  const user = (await ask(`Login name [${email}]: `)) || email;
  return { email, user, type: "imap", imapHost, imapPort, imapSecure: imapPort === 993, smtpHost, smtpPort, smtpSecure: smtpPort === 465 };
}

async function pickAccount(accounts) {
  if (!accounts.length) return manualAccount();
  const describe = (a) =>
    a.manual ? "Another mailbox, enter settings myself" : `${a.email} (${a.type === "pop3" ? `POP3 ${a.popHost}` : a.imapHost})`;
  const acc = await choose("Found these Thunderbird accounts", [...accounts, { manual: true }], describe);
  return acc.manual ? manualAccount() : { ...acc };
}

// Mailboxes Thunderbird knows about on this computer (may be none).
async function thunderbirdAccounts() {
  const profiles = findProfiles();
  let accounts = profiles.flatMap((p) => mailAccounts(readPrefs(p.prefs)));
  if (accounts.length) return accounts;
  if (profiles.length) {
    console.log(`Found Thunderbird (${profiles.map((p) => p.dir).join(", ")}) but no mail account in it.`);
    return [];
  }
  console.log("Thunderbird isn't installed here. That's fine: you can type the settings yourself.");
  const dir = (await ask("Press Enter to continue, or paste a Thunderbird profile folder if it's installed somewhere unusual: ")).replace(/^"|"$/g, "");
  if (!dir) return [];
  accounts = [dir, ...listDirs(dir)]
    .map((d) => path.join(d, "prefs.js"))
    .filter((p) => fs.existsSync(p))
    .flatMap((p) => mailAccounts(readPrefs(p)));
  if (!accounts.length) console.log("No mail account found in that folder.");
  return accounts;
}

// Asks for one mailbox and checks its login. Returns { acc, password }, or null if the user gives up.
async function verifiedMailbox(found) {
  const acc = await pickAccount(found);
  if (acc.type === "pop3") {
    console.log(`\nThunderbird gets this account over POP3. The connector needs IMAP, so it will try ${acc.imapHost}.`);
    if (!acc.leaveOnServer) {
      console.log("Warning: Thunderbird is set to delete emails from the server after downloading them.");
      console.log("Claude can only see emails still on the server. To keep them there, in Thunderbird open");
      console.log('Account Settings → Server Settings and tick "Leave messages on server".');
    }
  }
  if (!acc.smtpHost) acc.smtpHost = await ask("Outgoing (SMTP) server: ");
  console.log(`\nUsing ${acc.email} (login: ${acc.user})`);
  console.log(`  Incoming (IMAP): ${acc.imapHost}:${acc.imapPort}  Outgoing (SMTP): ${acc.smtpHost}:${acc.smtpPort}`);

  for (;;) {
    const password = await askHidden(`\nPassword for ${acc.user}: `);
    process.stdout.write("Checking login... ");
    try {
      const inbox = await checkLogin(acc, password);
      console.log(`OK. Inbox has ${inbox.messages} messages (${inbox.unseen} unread).`);
      return { acc, password };
    } catch (err) {
      const reason = err.responseText || err.response || err.message;
      console.log(`failed: ${reason}`);
      if (/ENOTFOUND|ECONNREFUSED|ETIMEDOUT|EHOSTUNREACH/.test(err.code || err.message)) {
        const host = await ask(`Couldn't reach ${acc.imapHost}. Enter the correct IMAP server, optionally as server:port (or press Enter to keep it): `);
        if (host) {
          const [h, port] = parseHost(host, acc.imapPort);
          if (h) Object.assign(acc, { imapHost: h, imapPort: port, imapSecure: port === 993 });
        }
      } else if (err.authenticationFailed || /auth|login|credential|password/i.test(reason)) {
        console.log("The login name or password is wrong. The login name can differ from the email address.");
        acc.user = (await ask(`Login name [${acc.user}]: `)) || acc.user;
      }
      if ((await ask("Try again? [Y/n] ")).toLowerCase() === "n") return null;
    }
  }
}

// Mailboxes saved by an earlier run (in .env, or in Claude Desktop's config), so a new run adds to them.
function existingMailboxes() {
  let env = {};
  if (remote) {
    env = readEnvFile();
  } else if (fs.existsSync(claudeConfigPath)) {
    try {
      env = JSON.parse(fs.readFileSync(claudeConfigPath, "utf8")).mcpServers?.email?.env || {};
    } catch {}
  }
  const list = [];
  for (let n = 1; n <= 9; n++) {
    const mail = n === 1 ? "MAIL_" : `MAIL${n}_`;
    const server = n === 1 ? "" : `MAIL${n}_`;
    const get = (p, k) => env[p + k];
    if (!get(mail, "USER") || !get(mail, "PASSWORD") || !get(server, "IMAP_HOST")) continue;
    const from = get(mail, "FROM") || get(mail, "USER");
    const [, name, address] = from.match(/^\s*(.*?)\s*<([^>]+)>\s*$/) || [null, "", from.trim()];
    list.push({
      password: get(mail, "PASSWORD"),
      acc: {
        email: address,
        fullName: name || undefined,
        user: get(mail, "USER"),
        imapHost: get(server, "IMAP_HOST"),
        imapPort: Number(get(server, "IMAP_PORT") || 993),
        imapSecure: get(server, "IMAP_SECURE") !== "false",
        smtpHost: get(server, "SMTP_HOST"),
        smtpPort: Number(get(server, "SMTP_PORT") || 465),
        smtpSecure: get(server, "SMTP_SECURE") ? get(server, "SMTP_SECURE") === "true" : Number(get(server, "SMTP_PORT") || 465) === 465,
      },
    });
  }
  return list;
}

const yes = async (question, byDefault) => {
  const answer = (await ask(question)).trim().toLowerCase();
  return answer ? ["y", "yes", "j", "ja"].includes(answer) : byDefault;
};

async function main() {
  console.log(remote ? "Always-on mail connector setup\n" : "Mail connector setup for Claude Desktop\n");

  let mailboxes = [];
  const existing = existingMailboxes();
  if (existing.length) {
    console.log("Mailboxes already connected:");
    for (const m of existing) console.log(`  - ${m.acc.email}`);
    if (await yes("Keep them? [Y/n] ", true)) mailboxes = existing;
  }

  let found;
  let adding = mailboxes.length === 0 || (await yes("\nAdd a mailbox now? [y/N] ", false));
  while (adding) {
    found ??= await thunderbirdAccounts();
    const mailbox = await verifiedMailbox(found);
    if (mailbox) {
      const same = mailboxes.findIndex((m) => m.acc.email.toLowerCase() === mailbox.acc.email.toLowerCase());
      if (same >= 0) {
        mailboxes[same] = mailbox;
        console.log("Updated the settings of that mailbox.");
      } else {
        mailboxes.push(mailbox);
      }
    }
    if (mailboxes.length >= 9) break;
    console.log(`\nConnected so far: ${mailboxes.map((m) => m.acc.email).join(", ") || "none"}`);
    adding = await yes("Add another mailbox (e.g. team, personal or invoices address)? [y/N] ", mailboxes.length === 0);
  }

  if (!mailboxes.length) {
    console.log("No mailbox set up.");
  } else {
    console.log(`\nMailboxes: ${mailboxes.map((m) => m.acc.email).join(", ")}`);
    if (remote) {
      await setupRemote(mailboxes);
    } else {
      const name = "email";
      writeClaudeConfig(name, mailboxes);
      console.log(`\nAdded "${name}" connector to ${claudeConfigPath}`);
      console.log('Quit Claude Desktop completely and reopen it. Then ask Claude: "Show my unread emails".');
    }
  }
  rl.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

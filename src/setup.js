#!/usr/bin/env node
// One-step setup: reads mail server settings from Thunderbird, checks the login,
// and registers this connector in Claude Desktop's config.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { ImapFlow } from "imapflow";
import nodemailer from "nodemailer";

const serverScript = path.join(path.dirname(fileURLToPath(import.meta.url)), "index.js");
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
  console.log("Mail connector setup for Claude Desktop\n");

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
      const name = "email";
      writeClaudeConfig(name, acc, password);
      console.log(`\nAdded "${name}" connector to ${claudeConfigPath}`);
      console.log("Quit Claude Desktop completely and reopen it. Then ask Claude: \"Show my unread emails\".");
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

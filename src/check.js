#!/usr/bin/env node
// Diagnoses the always-on connector step by step: local service, tunnel, public address and the
// OAuth endpoints Claude uses when you add the connector.

import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { envFile, projectDir } from "./config.js";

const ok = (msg) => console.log(`  OK    ${msg}`);
const bad = (msg, fix) => console.log(`  FAIL  ${msg}${fix ? `\n        → ${fix}` : ""}`);

async function get(url, init) {
  try {
    const res = await fetch(url, { ...init, signal: AbortSignal.timeout(15000) });
    return { status: res.status, headers: res.headers, body: await res.text() };
  } catch (err) {
    return { error: err.cause?.code || err.cause?.message || err.message };
  }
}

if (!fs.existsSync(envFile)) {
  console.log(`No settings file found (${envFile}). Run the always-on setup first.`);
  process.exit(1);
}
process.loadEnvFile(envFile);
const port = Number(process.env.PORT || 8787);
const publicUrl = (process.env.PUBLIC_URL || "").replace(/\/+$/, "");

console.log("Mail connector check\n");

console.log("1. Background service on this computer");
const local = await get(`http://127.0.0.1:${port}/`);
if (local.body?.includes("running")) ok(`running on port ${port}`);
else bad(`not answering on port ${port} (${local.error || local.status})`, "Restart the computer, or Task Scheduler → Claude Mail Connector → Run. Then see the log below.");

console.log("\n2. Tailscale Funnel");
const tsCandidates = process.platform === "win32" ? ["C:\\Program Files\\Tailscale\\tailscale.exe", "tailscale"] : ["tailscale"];
let funnelOk = false;
for (const bin of tsCandidates) {
  try {
    const out = execFileSync(bin, ["funnel", "status"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    console.log(out.trim().split("\n").map((l) => `        ${l}`).join("\n"));
    funnelOk = /Funnel on/i.test(out) && out.includes(String(port));
    if (funnelOk) ok("Funnel is on and points to the connector");
    else bad("Funnel is not on for this connector", `Run in an administrator prompt: "${bin}" funnel --bg ${port}`);
    break;
  } catch (err) {
    if (bin === tsCandidates.at(-1)) bad(`couldn't ask Tailscale (${err.message.split("\n")[0]})`, "Make sure Tailscale is installed, signed in and set to Run unattended.");
  }
}

console.log(`\n3. Public address ${publicUrl}`);
const pub = await get(`${publicUrl}/`);
if (pub.body?.includes("running")) ok("reachable from the internet");
else
  bad(
    `not reachable (${pub.error || `HTTP ${pub.status}`})`,
    "Turn on Funnel (step 2). Right after enabling it can take up to 10 minutes. Test on your phone with Wi-Fi off: " + `${publicUrl}/`,
  );

console.log("\n4. Sign-in endpoints Claude uses");
const unauth = await get(`${publicUrl}/mcp`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
const wwwAuth = unauth.headers?.get("www-authenticate") || "";
if (unauth.status === 401 && wwwAuth.includes("resource_metadata")) ok("/mcp asks for sign-in");
else bad(`/mcp answered ${unauth.error || unauth.status} instead of 401`);

const prm = await get(`${publicUrl}/.well-known/oauth-protected-resource/mcp`);
let prmJson = {};
try {
  prmJson = JSON.parse(prm.body);
} catch {}
if (prmJson.resource === `${publicUrl}/mcp`) ok("resource metadata");
else bad(`resource metadata wrong (${prm.error || prm.status}): ${String(prm.body || "").slice(0, 120)}`, "PUBLIC_URL in .env must be exactly the Tailscale address. Run the setup again.");

const asm = await get(`${publicUrl}/.well-known/oauth-authorization-server`);
let asmJson = {};
try {
  asmJson = JSON.parse(asm.body);
} catch {}
if (asmJson.registration_endpoint?.startsWith(publicUrl)) ok("authorization server metadata");
else bad(`authorization server metadata wrong (${asm.error || asm.status})`);

const reg = await get(`${publicUrl}/register`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ client_name: "Connector check", redirect_uris: ["https://claude.ai/api/mcp/auth_callback"], token_endpoint_auth_method: "none" }),
});
if (reg.status === 201) ok("Claude can register");
else bad(`registration failed (${reg.error || reg.status}): ${String(reg.body || "").slice(0, 160)}`);

console.log("\n5. Last lines of the log");
const log = path.join(projectDir, "data", "connector.log");
if (fs.existsSync(log)) {
  const lines = fs.readFileSync(log, "utf8").trimEnd().split("\n").slice(-25);
  console.log(lines.map((l) => `        ${l}`).join("\n"));
} else {
  console.log(`        (no log yet at ${log})`);
}

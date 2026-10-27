#!/usr/bin/env node
// Background-service wrapper for the always-on connector: restarts it whenever it stops
// and appends all of its output to data/connector.log.

import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { projectDir } from "./config.js";

const script = path.join(projectDir, "src", "http.js");
const logDir = path.join(projectDir, "data");
const logFile = path.join(logDir, "connector.log");
fs.mkdirSync(logDir, { recursive: true });

const log = (msg) => fs.appendFileSync(logFile, `${new Date().toISOString()} [service] ${msg}\n`);

function run() {
  // Keep the log from growing without bound.
  if (fs.existsSync(logFile) && fs.statSync(logFile).size > 10 * 1024 * 1024) fs.renameSync(logFile, `${logFile}.old`);
  const out = fs.openSync(logFile, "a");
  const started = Date.now();
  const child = spawn(process.execPath, [script], { cwd: projectDir, stdio: ["ignore", out, out] });
  fs.closeSync(out);
  log(`started connector (pid ${child.pid})`);
  child.on("exit", (code, signal) => {
    // Back off if it dies right away (e.g. bad settings or mail server down).
    const delay = Date.now() - started < 10_000 ? 30_000 : 2_000;
    log(`connector stopped (${signal || `exit ${code}`}), restarting in ${delay / 1000}s`);
    setTimeout(run, delay);
  });
}

run();

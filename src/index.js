#!/usr/bin/env node
// Desktop mode: Claude Desktop or Claude Code starts this process and talks to it over stdio.

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { loadConfig } from "./config.js";
import { createServer } from "./tools.js";

await createServer(loadConfig()).connect(new StdioServerTransport());

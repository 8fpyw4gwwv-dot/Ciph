#!/usr/bin/env node
// IMAP/SMTP mail connector exposed as an MCP server over stdio.
// Works with any provider your iPhone Mail app uses (Yahoo, custom domain, ISP mail, ...).

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ImapFlow } from "imapflow";
import nodemailer from "nodemailer";
import { simpleParser } from "mailparser";
import { z } from "zod";
import { loadConfig } from "./config.js";

const config = loadConfig();

async function withImap(fn) {
  const client = new ImapFlow({
    host: config.imap.host,
    port: config.imap.port,
    secure: config.imap.secure,
    auth: { user: config.user, pass: config.password },
    logger: false,
  });
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.logout().catch(() => {});
  }
}

async function withMailbox(folder, fn) {
  return withImap(async (client) => {
    const lock = await client.getMailboxLock(folder);
    try {
      return await fn(client);
    } finally {
      lock.release();
    }
  });
}

async function findSpecialFolder(client, specialUse, fallbacks) {
  const folders = await client.list();
  const match =
    folders.find((f) => f.specialUse === specialUse) ||
    folders.find((f) => fallbacks.includes(f.name.toLowerCase()));
  return match?.path;
}

const formatAddresses = (list) =>
  (list || []).map((a) => (a.name ? `${a.name} <${a.address}>` : a.address)).join(", ");

const text = (value) => ({
  content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }],
});

const server = new McpServer({ name: "imap-mail", version: "1.0.0" });

server.registerTool(
  "list_folders",
  {
    description: "List all mail folders (Inbox, Sent, Archive, custom folders) with message counts.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () =>
    withImap(async (client) => {
      const folders = await client.list({ statusQuery: { messages: true, unseen: true } });
      return text(
        folders.map((f) => ({
          path: f.path,
          specialUse: f.specialUse || null,
          messages: f.status?.messages,
          unseen: f.status?.unseen,
        })),
      );
    }),
);

server.registerTool(
  "search_messages",
  {
    description:
      "Search a folder and return message summaries (uid, date, from, subject, flags), newest first. " +
      "Use the uid with get_message to read the full body.",
    inputSchema: {
      folder: z.string().default("INBOX").describe("Folder path from list_folders"),
      from: z.string().optional(),
      to: z.string().optional(),
      subject: z.string().optional(),
      body: z.string().optional().describe("Text that must appear in the message body"),
      since: z.string().optional().describe("ISO date, e.g. 2026-10-01"),
      before: z.string().optional().describe("ISO date, e.g. 2026-10-31"),
      unread_only: z.boolean().default(false),
      flagged_only: z.boolean().default(false),
      limit: z.number().int().min(1).max(100).default(20),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) =>
    withMailbox(args.folder, async (client) => {
      const query = {};
      if (args.from) query.from = args.from;
      if (args.to) query.to = args.to;
      if (args.subject) query.subject = args.subject;
      if (args.body) query.body = args.body;
      if (args.since) query.since = new Date(args.since);
      if (args.before) query.before = new Date(args.before);
      if (args.unread_only) query.seen = false;
      if (args.flagged_only) query.flagged = true;
      if (Object.keys(query).length === 0) query.all = true;

      const uids = (await client.search(query, { uid: true })) || [];
      const selected = uids.sort((a, b) => b - a).slice(0, args.limit);
      if (selected.length === 0) return text("No messages matched.");

      const results = [];
      for await (const msg of client.fetch(selected, { envelope: true, flags: true }, { uid: true })) {
        results.push({
          uid: msg.uid,
          date: msg.envelope.date,
          from: formatAddresses(msg.envelope.from),
          to: formatAddresses(msg.envelope.to),
          subject: msg.envelope.subject,
          unread: !msg.flags.has("\\Seen"),
          flagged: msg.flags.has("\\Flagged"),
        });
      }
      results.sort((a, b) => b.uid - a.uid);
      return text({ total_matches: uids.length, returned: results.length, messages: results });
    }),
);

server.registerTool(
  "get_message",
  {
    description: "Read one message in full: headers, plain-text body and attachment names.",
    inputSchema: {
      folder: z.string().default("INBOX"),
      uid: z.number().int(),
      mark_as_read: z.boolean().default(false),
    },
    annotations: { readOnlyHint: false },
  },
  async ({ folder, uid, mark_as_read }) =>
    withMailbox(folder, async (client) => {
      const msg = await client.fetchOne(String(uid), { source: true }, { uid: true });
      if (!msg) return text(`No message with uid ${uid} in ${folder}.`);
      const parsed = await simpleParser(msg.source);
      if (mark_as_read) await client.messageFlagsAdd(String(uid), ["\\Seen"], { uid: true });
      return text({
        uid,
        message_id: parsed.messageId,
        date: parsed.date,
        from: parsed.from?.text,
        to: parsed.to?.text,
        cc: parsed.cc?.text,
        subject: parsed.subject,
        body: parsed.text || "(no plain-text body)",
        attachments: (parsed.attachments || []).map((a) => ({
          filename: a.filename,
          contentType: a.contentType,
          size: a.size,
        })),
      });
    }),
);

server.registerTool(
  "send_message",
  {
    description:
      "Send an email over SMTP. To reply, pass reply_to_uid (and its folder) so threading headers are set " +
      "and the subject gets a Re: prefix. A copy is saved to the Sent folder.",
    inputSchema: {
      to: z.string().describe("Comma-separated recipients"),
      cc: z.string().optional(),
      bcc: z.string().optional(),
      subject: z.string().optional(),
      body: z.string().describe("Plain-text body"),
      reply_to_uid: z.number().int().optional(),
      reply_folder: z.string().default("INBOX"),
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  },
  async (args) => {
    let subject = args.subject || "";
    const headers = {};
    if (args.reply_to_uid !== undefined) {
      const original = await withMailbox(args.reply_folder, async (client) => {
        const msg = await client.fetchOne(String(args.reply_to_uid), { source: true }, { uid: true });
        return msg ? simpleParser(msg.source) : null;
      });
      if (!original) return text(`No message with uid ${args.reply_to_uid} to reply to.`);
      headers.inReplyTo = original.messageId;
      headers.references = [...[original.references || []].flat(), original.messageId].filter(Boolean);
      if (!subject) subject = /^re:/i.test(original.subject || "") ? original.subject : `Re: ${original.subject || ""}`;
    }

    const transport = nodemailer.createTransport({
      host: config.smtp.host,
      port: config.smtp.port,
      secure: config.smtp.secure,
      auth: { user: config.user, pass: config.password },
    });
    const mail = {
      from: config.from,
      to: args.to,
      cc: args.cc,
      bcc: args.bcc,
      subject,
      text: args.body,
      ...headers,
    };
    const info = await transport.sendMail(mail);

    // Most IMAP servers don't copy SMTP-sent mail to Sent automatically, so the iPhone wouldn't show it.
    let savedTo = null;
    if (config.saveSent) {
      const raw = await new Promise((resolve, reject) =>
        nodemailer
          .createTransport({ streamTransport: true, buffer: true })
          .sendMail({ ...mail, messageId: info.messageId }, (err, res) => (err ? reject(err) : resolve(res.message))),
      );
      savedTo = await withImap(async (client) => {
        const sent = await findSpecialFolder(client, "\\Sent", ["sent", "sent items", "sent messages", "sent mail"]);
        if (!sent) return null;
        await client.append(sent, raw, ["\\Seen"]);
        return sent;
      }).catch(() => null);
    }

    return text({ sent: true, message_id: info.messageId, accepted: info.accepted, saved_to: savedTo });
  },
);

server.registerTool(
  "update_flags",
  {
    description: "Mark a message read/unread and/or flagged/unflagged.",
    inputSchema: {
      folder: z.string().default("INBOX"),
      uid: z.number().int(),
      read: z.boolean().optional(),
      flagged: z.boolean().optional(),
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
  },
  async ({ folder, uid, read, flagged }) =>
    withMailbox(folder, async (client) => {
      const range = String(uid);
      const set = async (flag, on) =>
        on
          ? client.messageFlagsAdd(range, [flag], { uid: true })
          : client.messageFlagsRemove(range, [flag], { uid: true });
      if (read !== undefined) await set("\\Seen", read);
      if (flagged !== undefined) await set("\\Flagged", flagged);
      return text(`Updated message ${uid} in ${folder}.`);
    }),
);

server.registerTool(
  "move_message",
  {
    description: "Move a message to another folder (e.g. Archive). Use list_folders to find folder paths.",
    inputSchema: {
      folder: z.string().default("INBOX"),
      uid: z.number().int(),
      destination: z.string(),
    },
    annotations: { readOnlyHint: false, destructiveHint: false },
  },
  async ({ folder, uid, destination }) =>
    withMailbox(folder, async (client) => {
      await client.messageMove(String(uid), destination, { uid: true });
      return text(`Moved message ${uid} from ${folder} to ${destination}.`);
    }),
);

server.registerTool(
  "trash_message",
  {
    description: "Move a message to the Trash folder. It can be recovered from Trash until the provider empties it.",
    inputSchema: {
      folder: z.string().default("INBOX"),
      uid: z.number().int(),
    },
    annotations: { readOnlyHint: false, destructiveHint: true },
  },
  async ({ folder, uid }) =>
    withMailbox(folder, async (client) => {
      const trash = await findSpecialFolder(client, "\\Trash", ["trash", "deleted items", "deleted messages", "bin"]);
      if (!trash) return text("Couldn't find a Trash folder; use move_message with an explicit destination.");
      await client.messageMove(String(uid), trash, { uid: true });
      return text(`Moved message ${uid} to ${trash}.`);
    }),
);

await server.connect(new StdioServerTransport());

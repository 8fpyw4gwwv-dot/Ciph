// Mail tools shared by the desktop (stdio) and always-on (HTTP) entry points.
// Works with any IMAP/SMTP provider (Thunderbird, iPhone Mail, Yahoo, custom domain, ...).

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { ImapFlow } from "imapflow";
import nodemailer from "nodemailer";
import MailComposer from "nodemailer/lib/mail-composer";
import { simpleParser } from "mailparser";
import { extractText, getDocumentProxy } from "unpdf";
import { z } from "zod";

const MAX_BODY_CHARS = 50_000;
const MAX_ATTACHMENT_TEXT_CHARS = 100_000;
const MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024;

// ---------- IMAP helpers ----------

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

async function inMailbox(client, folder, fn) {
  const lock = await client.getMailboxLock(folder);
  try {
    return await fn();
  } finally {
    lock.release();
  }
}

const withMailbox = (folder, fn) => withImap((client) => inMailbox(client, folder, () => fn(client)));

async function findSpecialFolder(client, specialUse, fallbacks) {
  const folders = await client.list();
  const match =
    folders.find((f) => f.specialUse === specialUse) ||
    folders.find((f) => fallbacks.includes(f.name.toLowerCase()));
  return match?.path;
}

const findSent = (client) => findSpecialFolder(client, "\\Sent", ["sent", "sent items", "sent messages", "sent mail", "gesendet", "gesendete elemente", "gesendete objekte"]);
const findDrafts = (client) => findSpecialFolder(client, "\\Drafts", ["drafts", "draft", "entwürfe", "entwurf"]);
const findTrash = (client) => findSpecialFolder(client, "\\Trash", ["trash", "deleted items", "deleted messages", "bin", "papierkorb", "gelöschte elemente", "gelöschte objekte"]);

async function fetchParsed(client, uid) {
  const msg = await client.fetchOne(String(uid), { source: true }, { uid: true });
  return msg ? simpleParser(msg.source) : null;
}

function hasAttachments(node) {
  if (!node) return false;
  if (node.childNodes) return node.childNodes.some(hasAttachments);
  return node.disposition === "attachment" || Boolean(node.dispositionParameters?.filename);
}

// ---------- formatting helpers ----------

const formatAddresses = (list) =>
  (list || []).map((a) => (a.name ? `${a.name} <${a.address}>` : a.address)).join(", ");

const text = (value) => ({
  content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }],
});

const truncate = (s, max) => (s.length > max ? `${s.slice(0, max)}\n\n[... truncated, ${s.length - max} more characters]` : s);

const expandHome = (p) => (p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p);

const defaultFileDir = () => config.attachmentDir || path.join(os.homedir(), "Downloads");

// Resolves a path given by Claude. When config.fileRoot is set (always-on mode), only paths inside
// that folder are allowed, so a malicious email can't get Claude to send out e.g. the .env file.
function resolveUserPath(p) {
  const root = config.fileRoot && path.resolve(config.fileRoot);
  const full = path.resolve(root || "", expandHome(p));
  if (!root) return { full };
  const real = (q) => {
    try {
      return fs.realpathSync(q);
    } catch {
      return q;
    }
  };
  const rel = path.relative(real(root), real(full));
  if (rel.startsWith("..") || path.isAbsolute(rel)) return { error: `Only files inside ${root} can be used.` };
  return { full };
}

function htmlToText(html) {
  return html
    .replace(/<(style|script)[\s\S]*?<\/\1>/gi, "")
    .replace(/<br\s*\/?>|<\/(p|div|tr|li|h\d)>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function attachmentList(parsed) {
  return (parsed.attachments || []).map((a, index) => ({
    index,
    filename: a.filename || `attachment-${index + 1}`,
    contentType: a.contentType,
    size: a.size,
  }));
}

function uniquePath(dir, filename) {
  const safe = filename.replace(/[\\/:*?"<>|]/g, "_");
  const { name, ext } = path.parse(safe);
  let candidate = path.join(dir, safe);
  for (let i = 1; fs.existsSync(candidate); i++) candidate = path.join(dir, `${name} (${i})${ext}`);
  return candidate;
}

// ---------- composing ----------

const composeSchema = {
  to: z.string().describe("Comma-separated recipients"),
  cc: z.string().optional(),
  bcc: z.string().optional(),
  subject: z.string().optional(),
  body: z.string().describe("Plain-text body. Always required, also used as the fallback for html."),
  html: z.string().optional().describe("Optional HTML version of the body for formatted email (bold, lists, tables, links)"),
  attachments: z.array(z.string()).optional().describe("Paths of files on the connector's computer to attach (in always-on mode: inside the shared files folder)"),
  forward_attachments: z
    .object({
      folder: z.string().default("INBOX"),
      uid: z.number().int(),
      indexes: z.array(z.number().int()).optional().describe("Attachment indexes from get_message; omit for all"),
    })
    .optional()
    .describe("Attach files from an existing email, e.g. to forward an invoice PDF"),
  reply_to_uid: z.number().int().optional().describe("uid of the message being replied to; sets threading headers and Re: subject"),
  reply_folder: z.string().default("INBOX"),
};

// Builds a nodemailer message from tool arguments. Returns { mail } or { error }.
async function prepareMail(args) {
  const mail = {
    from: config.from,
    to: args.to,
    cc: args.cc,
    bcc: args.bcc,
    subject: args.subject || "",
    text: args.body,
    html: args.html,
    attachments: [],
  };

  for (const file of args.attachments || []) {
    const { full, error } = resolveUserPath(file);
    if (error) return { error };
    if (!fs.existsSync(full) || !fs.statSync(full).isFile()) return { error: `Attachment not found: ${full}` };
    mail.attachments.push({ filename: path.basename(full), path: full });
  }

  if (args.reply_to_uid !== undefined || args.forward_attachments) {
    const fwd = args.forward_attachments;
    const result = await withImap(async (client) => {
      const out = {};
      if (args.reply_to_uid !== undefined) {
        out.original = await inMailbox(client, args.reply_folder, () => fetchParsed(client, args.reply_to_uid));
      }
      if (fwd) out.source = await inMailbox(client, fwd.folder, () => fetchParsed(client, fwd.uid));
      return out;
    });

    if (args.reply_to_uid !== undefined) {
      const original = result.original;
      if (!original) return { error: `No message with uid ${args.reply_to_uid} in ${args.reply_folder} to reply to.` };
      mail.inReplyTo = original.messageId;
      mail.references = [...[original.references || []].flat(), original.messageId].filter(Boolean);
      if (!mail.subject) mail.subject = /^(re|aw):/i.test(original.subject || "") ? original.subject : `Re: ${original.subject || ""}`;
    }

    if (fwd) {
      if (!result.source) return { error: `No message with uid ${fwd.uid} in ${fwd.folder} to take attachments from.` };
      const all = result.source.attachments || [];
      const indexes = fwd.indexes ?? all.map((_, i) => i);
      for (const i of indexes) {
        const a = all[i];
        if (!a) return { error: `Message ${fwd.uid} has no attachment with index ${i}.` };
        mail.attachments.push({ filename: a.filename || `attachment-${i + 1}`, content: a.content, contentType: a.contentType });
      }
    }
  }

  return { mail };
}

const buildRaw = (mail) => new MailComposer(mail).compile().build();

const summarizeMail = (mail) => ({
  to: mail.to,
  cc: mail.cc,
  subject: mail.subject,
  html: Boolean(mail.html),
  attachments: mail.attachments.map((a) => a.filename),
});

// ---------- server ----------

let config;

export function createServer(cfg) {
  config = cfg;
  const server = new McpServer({ name: "imap-mail", version: "1.2.0" });


  server.registerTool(
    "list_folders",
    {
      description: "List all mail folders (Inbox, Sent, Drafts, Archive, custom folders) with message and unread counts.",
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
        "Search one folder, or every folder with all_folders, and return message summaries newest first " +
        "(folder, uid, date, from, subject, flags, has_attachments). Page through results with offset. " +
        "Use folder + uid with get_message to read the full email.",
      inputSchema: {
        folder: z.string().default("INBOX").describe("Folder path from list_folders; ignored when all_folders is true"),
        all_folders: z.boolean().default(false).describe("Search every folder (Trash and Junk only if include_trash_and_junk)"),
        include_trash_and_junk: z.boolean().default(false),
        from: z.string().optional(),
        to: z.string().optional(),
        subject: z.string().optional(),
        body: z.string().optional().describe("Text that must appear in the message body"),
        since: z.string().optional().describe("ISO date, e.g. 2026-10-01"),
        before: z.string().optional().describe("ISO date, e.g. 2026-10-31"),
        unread_only: z.boolean().default(false),
        flagged_only: z.boolean().default(false),
        limit: z.number().int().min(1).max(200).default(20),
        offset: z.number().int().min(0).default(0).describe("Skip this many results, for paging"),
      },
      annotations: { readOnlyHint: true },
    },
    async (args) => {
      const query = {};
      if (args.from) query.from = args.from;
      if (args.to) query.to = args.to;
      if (args.subject) query.subject = args.subject;
      if (args.body) query.body = args.body;
      for (const key of ["since", "before"]) {
        if (!args[key]) continue;
        const date = new Date(args[key]);
        if (Number.isNaN(date.getTime())) return text(`Invalid date for ${key}: ${args[key]}`);
        query[key] = date;
      }
      if (args.unread_only) query.seen = false;
      if (args.flagged_only) query.flagged = true;
      if (Object.keys(query).length === 0) query.all = true;

      const wanted = args.offset + args.limit;

      return withImap(async (client) => {
        let folders = [args.folder];
        if (args.all_folders) {
          const skip = args.include_trash_and_junk ? [] : ["\\Trash", "\\Junk"];
          folders = (await client.list())
            .filter((f) => !f.flags?.has("\\Noselect") && !skip.includes(f.specialUse))
            .map((f) => f.path);
        }

        let total = 0;
        const results = [];
        for (const folder of folders) {
          await inMailbox(client, folder, async () => {
            const uids = ((await client.search(query, { uid: true })) || []).sort((a, b) => b - a);
            total += uids.length;
            // In a single folder uids are in arrival order, so page directly; across folders, merge by date below.
            const selected = folders.length === 1 ? uids.slice(args.offset, wanted) : uids.slice(0, wanted);
            if (selected.length === 0) return;
            const fetchOpts = { envelope: true, flags: true, internalDate: true, bodyStructure: true };
            for await (const msg of client.fetch(selected, fetchOpts, { uid: true })) {
              results.push({
                folder,
                uid: msg.uid,
                date: msg.envelope.date || msg.internalDate,
                from: formatAddresses(msg.envelope.from),
                to: formatAddresses(msg.envelope.to),
                subject: msg.envelope.subject,
                unread: !msg.flags.has("\\Seen"),
                flagged: msg.flags.has("\\Flagged"),
                has_attachments: hasAttachments(msg.bodyStructure),
              });
            }
          });
        }

        let page;
        if (folders.length === 1) {
          page = results.sort((a, b) => b.uid - a.uid);
        } else {
          page = results.sort((a, b) => new Date(b.date) - new Date(a.date)).slice(args.offset, wanted);
        }
        if (page.length === 0) return text(total ? `No more results (total matches: ${total}).` : "No messages matched.");
        return text({
          total_matches: total,
          offset: args.offset,
          returned: page.length,
          more_available: args.offset + page.length < total,
          messages: page,
        });
      });
    },
  );

  server.registerTool(
    "get_message",
    {
      description: "Read one message in full: headers, body text and a numbered list of attachments.",
      inputSchema: {
        folder: z.string().default("INBOX"),
        uid: z.number().int(),
        mark_as_read: z.boolean().default(false),
      },
      annotations: { readOnlyHint: false },
    },
    async ({ folder, uid, mark_as_read }) =>
      withMailbox(folder, async (client) => {
        const parsed = await fetchParsed(client, uid);
        if (!parsed) return text(`No message with uid ${uid} in ${folder}.`);
        if (mark_as_read) await client.messageFlagsAdd(String(uid), ["\\Seen"], { uid: true });
        const body = parsed.text || (parsed.html ? htmlToText(parsed.html) : "(empty body)");
        return text({
          folder,
          uid,
          message_id: parsed.messageId,
          date: parsed.date,
          from: parsed.from?.text,
          to: parsed.to?.text,
          cc: parsed.cc?.text,
          subject: parsed.subject,
          body: truncate(body.replace(/\u00a0/g, " "), MAX_BODY_CHARS),
          attachments: attachmentList(parsed),
        });
      }),
  );

  server.registerTool(
    "get_attachment",
    {
      description:
        "Open an attachment so Claude can read it: PDFs are returned as text, images as images, " +
        "and text files (txt, csv, xml, json, html, ics) as text. Other types (Word, Excel, zip) " +
        "can be stored with save_attachments.",
      inputSchema: {
        folder: z.string().default("INBOX"),
        uid: z.number().int(),
        index: z.number().int().describe("Attachment index from get_message"),
      },
      annotations: { readOnlyHint: true },
    },
    async ({ folder, uid, index }) => {
      const parsed = await withMailbox(folder, (client) => fetchParsed(client, uid));
      if (!parsed) return text(`No message with uid ${uid} in ${folder}.`);
      const att = parsed.attachments?.[index];
      if (!att) return text(`Message ${uid} has no attachment with index ${index}. Attachments: ${JSON.stringify(attachmentList(parsed))}`);

      const name = att.filename || `attachment-${index + 1}`;
      const type = (att.contentType || "").toLowerCase();
      const ext = path.extname(name).toLowerCase();

      if (type === "application/pdf" || ext === ".pdf") {
        const pdf = await getDocumentProxy(new Uint8Array(att.content));
        const { totalPages, text: pdfText } = await extractText(pdf, { mergePages: true });
        if (!pdfText.trim()) {
          return text(`${name} (${totalPages} pages) has no text layer, it's probably a scan. Use save_attachments to store it and open it yourself.`);
        }
        return text(`${name} (${totalPages} pages):\n\n${truncate(pdfText, MAX_ATTACHMENT_TEXT_CHARS)}`);
      }

      if (["image/png", "image/jpeg", "image/gif", "image/webp"].includes(type)) {
        if (att.size > MAX_INLINE_IMAGE_BYTES) return text(`${name} is too large to display (${att.size} bytes). Use save_attachments.`);
        return {
          content: [
            { type: "text", text: name },
            { type: "image", data: att.content.toString("base64"), mimeType: type },
          ],
        };
      }

      const textTypes = ["application/json", "application/xml", "text/calendar", "application/csv"];
      const textExts = [".txt", ".csv", ".xml", ".json", ".html", ".htm", ".ics", ".md", ".log"];
      if (type.startsWith("text/") || textTypes.includes(type) || textExts.includes(ext)) {
        let content = att.content.toString("utf8");
        if (type === "text/html" || ext === ".html" || ext === ".htm") content = htmlToText(content);
        return text(`${name}:\n\n${truncate(content, MAX_ATTACHMENT_TEXT_CHARS)}`);
      }

      return text(`${name} (${type || "unknown type"}) can't be displayed here. Use save_attachments to store it on this computer.`);
    },
  );

  server.registerTool(
    "save_attachments",
    {
      description: "Save attachments from a message to a folder on this computer. Existing files are never overwritten.",
      inputSchema: {
        folder: z.string().default("INBOX"),
        uid: z.number().int(),
        indexes: z.array(z.number().int()).optional().describe("Attachment indexes from get_message; omit for all"),
        directory: z.string().optional().describe("Where to save; defaults to the Downloads folder (in always-on mode it must be inside the shared files folder)"),
      },
      annotations: { readOnlyHint: false, destructiveHint: false },
    },
    async ({ folder, uid, indexes, directory }) => {
      const parsed = await withMailbox(folder, (client) => fetchParsed(client, uid));
      if (!parsed) return text(`No message with uid ${uid} in ${folder}.`);
      const all = parsed.attachments || [];
      if (all.length === 0) return text(`Message ${uid} has no attachments.`);

      const { full: dir, error } = resolveUserPath(directory || defaultFileDir());
      if (error) return text(error);
      fs.mkdirSync(dir, { recursive: true });
      const saved = [];
      for (const i of indexes ?? all.map((_, n) => n)) {
        const att = all[i];
        if (!att) return text(`Message ${uid} has no attachment with index ${i}.`);
        const file = uniquePath(dir, att.filename || `attachment-${i + 1}`);
        fs.writeFileSync(file, att.content);
        saved.push(file);
      }
      return text({ saved });
    },
  );

  server.registerTool(
    "send_message",
    {
      description:
        "Send an email immediately over SMTP. Supports HTML formatting, file attachments from this computer, " +
        "forwarding attachments from another email, and threaded replies (reply_to_uid). " +
        "A copy is saved to the Sent folder. To let the user review first, use save_draft instead.",
      inputSchema: composeSchema,
      annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
    },
    async (args) => {
      const { mail, error } = await prepareMail(args);
      if (error) return text(error);

      const transport = nodemailer.createTransport({
        host: config.smtp.host,
        port: config.smtp.port,
        secure: config.smtp.secure,
        auth: { user: config.user, pass: config.password },
      });
      const info = await transport.sendMail(mail);

      // Most IMAP servers don't copy SMTP-sent mail to Sent automatically, so mail clients wouldn't show it.
      let savedTo = null;
      if (config.saveSent) {
        const raw = await buildRaw({ ...mail, messageId: info.messageId });
        savedTo = await withImap(async (client) => {
          const sent = await findSent(client);
          if (!sent) return null;
          await client.append(sent, raw, ["\\Seen"]);
          return sent;
        }).catch(() => null);
      }

      return text({ sent: true, message_id: info.messageId, accepted: info.accepted, saved_to: savedTo, ...summarizeMail(mail) });
    },
  );

  server.registerTool(
    "save_draft",
    {
      description:
        "Save an email to the Drafts folder without sending it, so the user can review and send it from " +
        "Thunderbird or their phone. Takes the same options as send_message.",
      inputSchema: composeSchema,
      annotations: { readOnlyHint: false, destructiveHint: false },
    },
    async (args) => {
      const { mail, error } = await prepareMail(args);
      if (error) return text(error);
      const raw = await buildRaw(mail);
      return withImap(async (client) => {
        const drafts = await findDrafts(client);
        if (!drafts) return text("Couldn't find a Drafts folder on the server.");
        await client.append(drafts, raw, ["\\Draft", "\\Seen"]);
        return text({ saved_to: drafts, ...summarizeMail(mail) });
      });
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
        const trash = await findTrash(client);
        if (!trash) return text("Couldn't find a Trash folder; use move_message with an explicit destination.");
        await client.messageMove(String(uid), trash, { uid: true });
        return text(`Moved message ${uid} to ${trash}.`);
      }),
  );

  return server;
}

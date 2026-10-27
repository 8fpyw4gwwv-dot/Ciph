# IMAP Mail connector for Claude

An MCP server that lets Claude read, search, send and organize mail for any IMAP/SMTP account, such as Yahoo, a custom domain or your ISP's mail. It connects to the same mail server your iPhone Mail app uses, so changes show up on your phone as soon as it syncs.

Apple doesn't let anything plug into the iPhone Mail app directly. This connector talks to your mail provider instead.

## Tools

| Tool | What it does |
| --- | --- |
| `list_folders` | Lists folders with total and unread counts |
| `search_messages` | Searches one folder or all folders by sender, recipient, subject, body text, date range, unread or flagged. Up to 200 results per page, with `offset` for further pages |
| `get_message` | Reads a full message: headers, body (HTML-only mail converted to text) and a numbered attachment list. Can mark it read |
| `get_attachment` | Opens an attachment for Claude to read: PDFs as text, images as images, and txt/csv/xml/json/html/ics as text |
| `save_attachments` | Saves attachments to a folder on your computer (Downloads by default) without overwriting existing files |
| `send_message` | Sends mail with an optional HTML version, files from your computer, and attachments forwarded from another email. Threaded replies with `reply_to_uid`. Saves a copy to Sent |
| `save_draft` | Same options as `send_message`, but puts the email in Drafts for you to review and send from Thunderbird or your phone |
| `update_flags` | Marks read/unread and flagged/unflagged |
| `move_message` | Moves a message to another folder, such as Archive |
| `trash_message` | Moves a message to Trash. There is no permanent delete |

Scanned PDFs have no text layer. Claude will tell you to save them and open them yourself. Word, Excel and zip files can be saved but not read directly.

## Updating

Download the ZIP again, unzip it over your existing folder (replace the files), and double-click the setup file again. Then quit and reopen Claude Desktop.

## Quick setup (Thunderbird users)

If your account is already set up in Thunderbird, the setup script does everything for you. It reads your server settings from Thunderbird, asks for your password, checks that the login works, and adds the connector to Claude Desktop.

1. Install [Node.js](https://nodejs.org) (the LTS version).
2. [Download this repository as a ZIP](https://github.com/8fpyw4gwwv-dot/Ciph/archive/refs/heads/claude/iphone-mail-connection-vjni59.zip) and unzip it somewhere permanent, such as your Documents folder. Claude runs the connector from there, so don't delete the folder afterwards.
3. Double-click `setup-windows.bat` (Windows) or `setup-mac.command` (Mac). In a terminal, `npm install && npm run setup` does the same.
4. Quit Claude Desktop completely and reopen it.

If Thunderbird isn't on that computer, the script asks for the server names instead. Your previous Claude config is saved as `claude_desktop_config.json.bak`.

## Manual setup

### 1. Get your server settings

On your iPhone, open **Settings → Apps → Mail → Mail Accounts → (your account) → Account → Advanced**. On older iOS versions it's **Settings → Mail → Accounts**. Note the incoming (IMAP) and outgoing (SMTP) server names.

Common providers:

| Provider | IMAP host | SMTP host / port |
| --- | --- | --- |
| Yahoo | `imap.mail.yahoo.com` | `smtp.mail.yahoo.com` / 465 |
| AOL | `imap.aol.com` | `smtp.aol.com` / 465 |
| iCloud | `imap.mail.me.com` | `smtp.mail.me.com` / 587 |
| Zoho | `imap.zoho.com` | `smtp.zoho.com` / 465 |
| Fastmail | `imap.fastmail.com` | `smtp.fastmail.com` / 465 |

**Use an app password, not your normal password.** Yahoo, AOL, iCloud, Zoho and Fastmail all require one when two-factor sign-in is on. Create it in your provider's account security settings.

### 2. Install

Requires Node.js 18 or newer.

```sh
git clone https://github.com/8fpyw4gwwv-dot/Ciph.git
cd Ciph
npm install
```

### 3. Add it to Claude

#### Claude Desktop

Edit `claude_desktop_config.json` (**Settings → Developer → Edit Config**):

```json
{
  "mcpServers": {
    "mail": {
      "command": "node",
      "args": ["/full/path/to/Ciph/src/index.js"],
      "env": {
        "MAIL_USER": "you@example.com",
        "MAIL_PASSWORD": "your-app-password",
        "IMAP_HOST": "imap.mail.yahoo.com",
        "SMTP_HOST": "smtp.mail.yahoo.com"
      }
    }
  }
}
```

Restart Claude Desktop.

#### Claude Code

```sh
claude mcp add mail \
  -e MAIL_USER=you@example.com \
  -e MAIL_PASSWORD=your-app-password \
  -e IMAP_HOST=imap.mail.yahoo.com \
  -e SMTP_HOST=smtp.mail.yahoo.com \
  -- node /full/path/to/Ciph/src/index.js
```

## Settings

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `MAIL_USER` | yes | | Login, usually your full email address |
| `MAIL_PASSWORD` | yes | | App password |
| `IMAP_HOST` | yes | | Incoming server |
| `SMTP_HOST` | yes | | Outgoing server |
| `MAIL_FROM` | no | `MAIL_USER` | From address, e.g. `Jane Doe <jane@example.com>` |
| `IMAP_PORT` | no | 993 | |
| `IMAP_SECURE` | no | `true` | Set `false` only for port 143 with STARTTLS |
| `SMTP_PORT` | no | 465 | Use 587 if your provider says so |
| `SMTP_SECURE` | no | `true` on 465, else `false` | Port 587 upgrades to TLS with STARTTLS automatically |
| `SAVE_SENT` | no | `true` | Saves sent mail to your Sent folder so it appears on your phone |

Your password stays in your local Claude config and is only sent to your own mail server.

# IMAP Mail connector for Claude

An MCP server that lets Claude read, search, send and organize mail for any IMAP/SMTP account, such as Yahoo, a custom domain or your ISP's mail. It connects to the same mail server your iPhone Mail app uses, so changes show up on your phone as soon as it syncs.

Apple doesn't let anything plug into the iPhone Mail app directly. This connector talks to your mail provider instead.

## Tools

| Tool | What it does |
| --- | --- |
| `list_accounts` | Shows which mailboxes are connected and whether each can log in right now |
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

With several mailboxes (for example a team address and a personal one), every tool takes an `account` argument naming the mailbox's email address. Without it, the first mailbox is used. Just tell Claude which address you mean.

Scanned PDFs have no text layer. Claude will tell you to save them and open them yourself. Word, Excel and zip files can be saved but not read directly.

## Two ways to run it

- **Desktop:** Claude Desktop starts the connector on your computer. It only works while that computer is on, and only in Claude Desktop there. See [Quick setup](#quick-setup-thunderbird-users).
- **Always on:** the connector runs as a background service on a computer that never switches off. It's reachable over a secure HTTPS address and added to claude.ai as a custom connector, so it works in the browser, in Claude Desktop anywhere, and in the Claude app on your iPhone. See [Always-on setup](#always-on-setup-windows).

In always-on mode, Claude can only use files in one folder on the always-on computer: the Downloads folder of the user who ran the setup. Saved attachments go there, and files to send must be put there first.

## Always-on setup (Windows)

### 1. Prepare the computer

- Install [Node.js](https://nodejs.org) (LTS).
- Thunderbird is optional. If it's installed, the setup copies the server settings from it. Otherwise you type the email address, login name and password. Server settings for easyApotheken, GMX, WEB.DE, T-Online, Yahoo and iCloud are filled in automatically.
- In **Settings → System → Power**, set sleep to **Never**.

### 2. Get a secure public address with Tailscale (free)

1. Install [Tailscale](https://tailscale.com/download/windows) and sign in.
2. Right-click the Tailscale icon in the taskbar → **Preferences** → turn on **Run unattended**, so it keeps running when nobody is logged in.
3. On first setup, Tailscale asks you to allow HTTPS and Funnel for your network. The setup script starts this and shows a link if needed. Open it and click Enable.

Tailscale Funnel gives the computer a fixed address like `https://pc-name.tail1234.ts.net` without opening ports on your router.

### 3. Run the setup

1. [Download this repository as a ZIP](https://github.com/8fpyw4gwwv-dot/Ciph/archive/refs/heads/claude/iphone-mail-connection-vjni59.zip) and unzip it somewhere permanent, such as `C:\Users\<you>\Documents\mail-connector`.
2. Right-click `setup-always-on-windows.bat` → **Run as administrator**.
3. Pick the mailbox from Thunderbird, or type its email address and **login name**. For easyApotheken the login name is the "Benutzername" on the easyWebmail access sheet (e.g. `info@nedderfeld.easyapotheken.de`), which differs from the email address.
4. Enter the mailbox password. The script checks it against your mail server.
5. Answer `y` to **Add another mailbox?** to connect more addresses (for example the Info address and your personal one) to the same connector.
6. Choose a **connector password** (at least 12 characters), or press Enter to generate one. Save it in your password manager. It is what stops anyone else from connecting to your mailbox.

The script saves the settings in `.env`, turns on Tailscale Funnel, installs the background task **Claude Mail Connector** (it starts with Windows and restarts itself if it stops), and checks that the address is reachable.

### 4. Add it to Claude

1. On [claude.ai](https://claude.ai) go to **Settings → Connectors → Add custom connector**.
2. Name: `Email`. URL: the address the setup printed, ending in `/mcp`.
3. Click **Connect**. A sign-in page from your own computer opens. Enter your connector password.

The connector is then available in every Claude app signed in to your account, including the iPhone app. The sign-in stays valid for 90 days of inactivity and renews automatically while in use.

### Security

- The connector only listens on the computer itself. Tailscale Funnel is the only way in, always over HTTPS.
- Nobody can use it without the connector password. After 5 wrong passwords, sign-in is locked for 15 minutes. Failed attempts are logged in `data\connector.log`.
- Only Claude (claude.ai / claude.com) and local apps can be registered as sign-in targets.
- Claude can only attach or save files inside the Downloads folder (`FILE_ROOT` in `.env` changes it). A malicious email can't make Claude send out other files, such as `.env`. Don't put the connector folder inside Downloads; it refuses to start if you do.
- Your mail password and connector password are stored in `.env` in the connector folder. Sign-ins are stored as hashes in `data\auth.json`.
- **To sign out every device**, delete `data\auth.json` and restart the computer (or the task).
- **To change the connector password**, run the setup again and answer `n` when asked to keep it, then delete `data\auth.json`.

### If Claude won't connect

Double-click `check-connector-windows.bat`. It tests each step (background service, Tailscale Funnel, public address, sign-in endpoints), says what's wrong and how to fix it, and shows the end of the log. It shows no passwords.

### Maintenance

- Log: `data\connector.log`
- Restart: Task Scheduler → **Claude Mail Connector** → End, then Run (or restart the computer)
- Update: unzip the new version over the folder, then run `setup-always-on-windows.bat` as administrator again and keep your connector password
- Remove: run `schtasks /Delete /TN "Claude Mail Connector" /F` and `tailscale funnel reset` in an administrator prompt, then delete the folder

### Mac or Linux

Run `npm install && npm run setup-remote`, then keep `npm run service` running, e.g. with a systemd unit (`ExecStart=/usr/bin/node /path/to/src/service.js`) or a launchd agent.

## Updating (desktop mode)

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

**More mailboxes:** add the same settings with a number for mailbox 2 to 9, e.g. `MAIL2_USER`, `MAIL2_PASSWORD`, `MAIL2_FROM`, `MAIL2_IMAP_HOST`, `MAIL2_SMTP_HOST` (and optionally `MAIL2_IMAP_PORT`, `MAIL2_SMTP_PORT`, ...). The mailbox's name for Claude is the email address in `MAILn_FROM`.

Your password stays in your local Claude config and is only sent to your own mail server.

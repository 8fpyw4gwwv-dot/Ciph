// Reads connection settings from environment variables.

const bool = (value, fallback) =>
  value === undefined || value === "" ? fallback : ["1", "true", "yes"].includes(value.toLowerCase());

export function loadConfig(env = process.env) {
  const missing = ["MAIL_USER", "MAIL_PASSWORD", "IMAP_HOST", "SMTP_HOST"].filter((k) => !env[k]);
  if (missing.length) {
    console.error(`Missing required environment variables: ${missing.join(", ")}. See README.md.`);
    process.exit(1);
  }

  const imapSecure = bool(env.IMAP_SECURE, true);
  const smtpPort = Number(env.SMTP_PORT || 465);

  return {
    user: env.MAIL_USER,
    password: env.MAIL_PASSWORD,
    from: env.MAIL_FROM || env.MAIL_USER,
    saveSent: bool(env.SAVE_SENT, true),
    imap: {
      host: env.IMAP_HOST,
      port: Number(env.IMAP_PORT || (imapSecure ? 993 : 143)),
      secure: imapSecure,
    },
    smtp: {
      host: env.SMTP_HOST,
      port: smtpPort,
      // Port 465 uses implicit TLS; 587 starts plain and upgrades with STARTTLS.
      secure: bool(env.SMTP_SECURE, smtpPort === 465),
    },
  };
}

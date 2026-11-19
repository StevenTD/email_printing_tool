# MailPrint

MailPrint is a Windows-hosted Django application for receiving Gmail messages and
printing their bodies and supported attachments. It includes a Gmail IMAP
fetcher, PDF conversion, sequential print queue, responsive dashboard/inbox,
filtering rules, auto-print controls, threaded acknowledgements, printer
management, role-based access, login throttling, and Windows startup scripts.

## Requirements

- Windows 10/11
- Python 3.11 or newer
- pip

## Setup

Open PowerShell in this folder and create the environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python -m playwright install chromium
```

Use `py -3.11` instead if that is the Python version installed on the PC. Set a
private `DJANGO_SECRET_KEY` in `.env`. Generate a Fernet key and paste it into
`MAILPRINT_FERNET_KEY`:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the Fernet key private and backed up. Credentials encrypted with a lost key
cannot be recovered. Initialize the database, create the initial administrator, and seed the MailPrint
Admin/Operator groups, a disabled sample rule, and acknowledgement templates:

```powershell
python manage.py migrate
python manage.py seed_mailprint
python manage.py runserver
```

The seed command prompts for a username, email address, and password when it
needs to create the initial administrator; it never uses a default password.
Alternatively, run `python manage.py createsuperuser` first and then
`python manage.py seed_mailprint` to add that staff account to the MailPrint
Admin group. The Django admin site is at `http://127.0.0.1:8000/admin/`.
`runserver` is for local development only; use Waitress for remote access.

The MailPrint dashboard and inbox are at `http://127.0.0.1:8000/` and
`http://127.0.0.1:8000/inbox/`. All application pages require Django login.
The inbox supports search, status and attachment filters, bulk actions, and
15-second HTMX refresh. Email detail pages include sanitized previews, attachment
downloads, print history, and manual print/status controls.

## Gmail fetching (Stage 2)

1. Enable 2-Step Verification on the Gmail account and create a Gmail App
   Password. Do not use the account's normal password.
2. Store the address and App Password securely. Password input is hidden, and
   MailPrint encrypts the credential in the database with `MAILPRINT_FERNET_KEY`:

   ```powershell
   python manage.py configure_mail
   ```

3. Fetch one batch of unseen messages:

   ```powershell
   python manage.py run_worker --once
   ```

4. Run continuous polling in a second PowerShell window:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   python manage.py run_worker
   ```

The worker polls the configured IMAP folder (default `INBOX`) at the interval
stored in the application configuration (default 60 seconds). It saves `.eml`
files and attachments under `media/mail/<year>/<month>/<message-key>/`. The
message key and stored filenames are shortened and hashed to fit Windows and
Django file-path limits; original attachment names remain in the inbox and are
used for downloads. The worker stores
message metadata and bodies in SQLite, and marks successfully stored messages
as read. Duplicate Message-IDs and IMAP UIDs are constrained in the database.
Set `MAILPRINT_FETCH_SINCE_DATE` in `.env` to an inclusive date in `YYYY-MM-DD`
format (for example, `2025-01-01`) to fetch messages whose Gmail mailbox date is
on or after that cutoff. This filter is applied in Gmail's IMAP search, so older
messages are not downloaded. By default, only unread messages are fetched; set
`MAILPRINT_FETCH_SEEN_EMAILS=True` to include read messages too. Existing
messages already stored by MailPrint are deduplicated. Restart the worker after
changing either setting.
Worker errors are available in Django admin under **Worker logs**. Attachment
downloads are capped at 25 MiB by default; oversized files remain listed but
their content is not saved. Set `MAILPRINT_MAX_EMAIL_BYTES` and
`MAILPRINT_MAX_ATTACHMENT_BYTES` in `.env` to adjust the limits.

## Filtering rules and auto-print (Stage 5)

MailPrint Admin users can manage rules at `http://127.0.0.1:8000/rules/`. Rules are
evaluated by ascending priority, and the first matching rule wins. Sender address
and domain, case-insensitive subject text or regular expression, and attachment
MIME type or extension can be combined. Each rule selects Print, Ignore, or Hold,
and can set the printer, copies, and whether to include the body and attachments.
The default action for unmatched messages is configurable on the Rules page.

The header's **Auto-print ON/OFF** switch controls automatic printing. When OFF,
messages are normally held and already-queued automatic jobs are cancelled.
Exception: a matching **Print** rule with **Print attachments** enabled queues
an email if it has at least one printable attachment, even while the global
switch is OFF. The rule's attachment setting, explicit Hold/Ignore rules, and
the optional weekly schedule still apply. The schedule uses
`DJANGO_TIME_ZONE` from `.env` (UTC by default). A rule may disable automatic
printing or bypass the schedule.

## Acknowledgements (Stage 6)

Open an email and choose **Send acknowledgement** to select a template, preview
the rendered message, then send it as a reply in the original Gmail thread.
Replies include the original `Message-ID` in `In-Reply-To` and preserve the
message's `References` chain. The first send is available to any signed-in
operator; a repeat send requires an explicit confirmation.

MailPrint Admin users manage templates at `http://127.0.0.1:8000/templates/`. Template
bodies support `{sender_name}`, `{subject}`, `{date}`, and `{office_name}`.
Choose one enabled template as the default. The optional **Automatic
acknowledgement after printing** setting is OFF by default and sends that
template only after a successful physical print; dry-run jobs never send replies.
The existing `configure_mail` command encrypts the Gmail App Password for both
IMAP and SMTP. SMTP uses Gmail SSL on port 465. Failed sends are retained in the
email acknowledgement history and worker error log. Automated tests mock SMTP
and do not send real email.

## Printer management (Stage 7)

MailPrint Admin users can open `http://127.0.0.1:8000/printers/` to refresh Windows
printer status, inspect each device's Windows queue, and cancel a queued Windows
job. Create a profile to set its friendly alias, dedicated purpose, MailPrint
default status, paper size, duplex, and color/monochrome mode. MailPrint asks
SumatraPDF to fit each printed PDF page, preserving its aspect ratio, to that
profile's paper size; set the profile to `Letter` if that is the paper loaded in
the printer. PDF attachments are sent directly to SumatraPDF without being
re-rendered. Profiles appear as named choices in filtering-rule printer
settings. The SumatraPDF executable can be configured on this page; otherwise
MailPrint uses `SUMATRA_PDF_PATH` or searches `PATH`.

The **Print test page** action physically prints when dry-run is disabled. With
dry-run enabled, it saves the generated PDF under
`dry_run_output\test_pages` instead. Stage tests mock printer actions; no
physical test page was printed during development.

## PDF conversion and printing (Stage 3)

- Email bodies are rendered with headless Playwright Chromium. Email HTML is
  sanitized, scripts are disabled, and external page requests are blocked.
- The email-body PDF uses a consistent MailPrint print layout with the subject,
  sender, email date, time received by MailPrint, recipients (To/Cc/Bcc),
  message ID, mailbox, attachment list, and local print-generation time. A
  page-margin footer identifies the source sender and MailPrint and includes
  page numbers. Short messages fit on one page when their content allows;
  longer messages continue across pages. The sanitized original HTML/text
  remains in the body area; its structure is preserved within the shared
  layout. Separate recipient fields are captured for newly fetched mail; older
  records show their stored recipients together.
- PDFs are printed using SumatraPDF's silent CLI. Install SumatraPDF and set
  `SUMATRA_PDF_PATH` in `.env` if it is not on `PATH`.
- Images are converted with Pillow. Office files (`doc`, `docx`, `xls`, `xlsx`,
  `odt`, `ods`, `rtf`) require LibreOffice; set `LIBREOFFICE_PATH` if `soffice`
  is not on `PATH`.

The worker processes queued print jobs sequentially. To test without paper, set
**Dry run enabled** on the application configuration in Django admin. A dry-run
job writes its body and attachment PDFs to `dry_run_output` (or the path in
`MAILPRINT_DRY_RUN_DIR`) and is not marked as physically printed. Create a
`PrinterProfile` with the Windows printer name and mark it as the default for
real printing. Queue a `PrintJob` in admin and run:

For a multi-document job, MailPrint waits for the Windows printer queue to
clear between the email body and each attachment. If a previous document is
still queued after five minutes, remaining documents are marked failed instead
of being submitted into a busy queue.

```powershell
python manage.py run_worker --once
```

Non-printable attachments are logged as skipped. A failed print attempt is
recorded in print history; already printed emails require an explicit reprint
before they can be sent again.

SQLite is used by default. To use PostgreSQL later, set `DATABASE_URL` in `.env`
and install the corresponding PostgreSQL database adapter.

## Roles, remote access, and Windows startup (Stage 8)

MailPrint Admin users (Django staff/superusers or members of the `MailPrint
Admin` group) can manage application settings, rules, templates, and printers.
Members of `MailPrint Operator` can view the inbox and perform print and
acknowledgement actions, but cannot administer settings or change email status
without a print action. Manage group membership in Django Admin under
**Authentication and Authorization → Groups**. Keep Django's `/admin/` limited
to staff accounts.

Login attempts are limited by username and IP address (five failures in 15
minutes by default). Successful login and logout events, as well as selected
administrative actions, appear in the Audit Log. Application logs are written
under `logs\mailprint.log` and rotate at 10 MiB with five backups. Set
`MAILPRINT_LOG_DIR` to change the log directory.

### Tailscale Serve (recommended)

Use Tailscale Serve to make the GUI reachable over your private tailnet without
publishing the PC to the public internet:

1. Install Tailscale on the print PC and on each authorized device, then sign in
   to the same tailnet. In the Tailscale admin console, enable HTTPS certificates
   for the tailnet if prompted.
2. Set production values in `.env`: `DJANGO_DEBUG=False`,
   `DJANGO_ALLOWED_HOSTS` to the exact MagicDNS hostname (without a scheme),
   `DJANGO_CSRF_TRUSTED_ORIGINS` to its exact `https://` origin, and both
   `DJANGO_SESSION_COOKIE_SECURE` and `DJANGO_CSRF_COOKIE_SECURE` to `True`.
   Keep the loopback hostnames if local access is also needed. Generate a
   non-development `DJANGO_SECRET_KEY` and a private Fernet key; do not share
   `.env` or either key.
3. Start the Waitress server as described below, then run in an elevated or
   ordinary PowerShell window:

   ```powershell
   tailscale serve 8000
   tailscale serve status
   ```

   Open the HTTPS tailnet URL shown by `tailscale serve status`. Restrict access
   with your tailnet ACLs to the users/devices that need MailPrint. Do **not**
   enable Tailscale Funnel. Do not configure router port forwarding or expose
   port 8000 to the public internet; use Windows Firewall to block direct
   untrusted inbound access to that port.

### Waitress and automatic startup

The included Waitress script listens on `0.0.0.0:8000` as required for the
Tailscale proxy. In the project PowerShell window:

```powershell
.\scripts\run_web.ps1
```

In a second window, start the Gmail/print worker:

```powershell
.\scripts\run_worker.ps1
```

To register both as Windows Task Scheduler tasks that start when the current
Windows user signs in and restart up to three times after failure, run:

```powershell
.\scripts\register_tasks.ps1
```

The tasks run interactively under that user so Windows printer access remains
available. They start after sign-in (not before anyone logs in); keep that
account signed in on the print PC. Start them immediately or inspect their
state with:

```powershell
Start-ScheduledTask -TaskName "MailPrint-Web"
Start-ScheduledTask -TaskName "MailPrint-Worker"
Get-ScheduledTask -TaskName "MailPrint-Web","MailPrint-Worker"
```

Use `-TaskPrefix` with `register_tasks.ps1` if you need different task names.
The Python application logs, including Waitress and worker logs, rotate in the
configured log directory. Task Scheduler's **Last Run Result** is useful for
startup failures; the scripts also fail explicitly if the virtual environment
or Waitress executable is missing.

### Stage 8 verification

Run the application checks and tests without connecting to Gmail or printing:

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

## Implementation stages

1. Project scaffold and data models (complete)
2. Gmail IMAP fetching and message storage (complete)
3. PDF conversion and print pipeline (complete)
4. Dashboard and inbox GUI (complete)
5. Filtering rules and auto-print controls (complete)
6. Acknowledgement replies (complete)
7. Printer management (complete)
8. Authentication hardening, remote access, and Windows startup setup (complete)

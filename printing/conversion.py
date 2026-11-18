import shutil
import subprocess
from html import escape
from pathlib import Path

from decouple import config
from django.utils import timezone
from mails.rendering import sanitize_email_html
from PIL import Image, UnidentifiedImageError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


OFFICE_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods", ".rtf"}
IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


class ConversionError(Exception):
    pass


def build_email_html(email):
    if email.body_html:
        body = sanitize_email_html(email.body_html)
    else:
        body = f"<pre>{escape(email.body_text or '(No body content.)')}</pre>"

    metadata = [
        ("From", email.sender),
        ("Email date", _format_datetime(email.date_received)),
        ("Received by MailPrint", _format_datetime(email.fetched_at)),
    ]
    to_recipients = getattr(email, "to_recipients", [])
    cc_recipients = getattr(email, "cc_recipients", [])
    bcc_recipients = getattr(email, "bcc_recipients", [])
    if to_recipients or cc_recipients or bcc_recipients:
        metadata.extend(
            [
                ("To", ", ".join(to_recipients)),
                ("Cc", ", ".join(cc_recipients)),
                ("Bcc", ", ".join(bcc_recipients)),
            ]
        )
    elif email.recipients:
        metadata.append(("Recipients", ", ".join(email.recipients)))
    if email.message_id:
        metadata.append(("Message ID", email.message_id))
    if email.imap_folder:
        metadata.append(("Mailbox", email.imap_folder))

    attachments = []
    if email.pk:
        attachments = [
            f"{attachment.filename} ({attachment.size_bytes:,} bytes; "
            f"{'printable' if attachment.printable else 'not printable'})"
            for attachment in email.attachments.all()
        ]
    if attachments:
        metadata.append(("Attachments", "\n".join(attachments)))

    metadata_html = "".join(
        _metadata_row(label, value)
        for label, value in metadata
        if value
    )
    generated_at = timezone.localtime().strftime("%b %d, %Y %I:%M:%S %p %Z")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; img-src data:; style-src 'unsafe-inline';">
<style>
@page {{ size: A4; margin: 15mm 15mm 22mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: Arial, sans-serif; font-size: 10pt; color: #17212b; }}
.brand {{ color: #173b63; font-size: 10pt; font-weight: 700; letter-spacing: 1.4px; }}
.document-header {{ border-top: 5px solid #173b63; border-bottom: 1px solid #cbd5df; padding: 12px 0 14px; }}
h1 {{ margin: 9px 0 14px; font-size: 19pt; line-height: 1.25; overflow-wrap: anywhere; }}
.metadata {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
.metadata th, .metadata td {{ border: 0; border-top: 1px solid #e3e8ed; padding: 6px 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; white-space: pre-wrap; }}
.metadata th {{ width: 24%; color: #526170; font-weight: 600; }}
.metadata td {{ color: #17212b; }}
.generated {{ color: #526170; font-size: 8.5pt; margin-top: 12px; }}
.email-content {{ padding-top: 14px; overflow-wrap: anywhere; }}
h2 {{ color: #173b63; font-size: 11pt; margin: 0 0 10px; text-transform: uppercase; letter-spacing: .5px; }}
.email-content img {{ max-width: 100%; height: auto; }}
.email-content table {{ border-collapse: collapse; max-width: 100%; }}
.email-content td, .email-content th {{ border: 1px solid #aeb8c2; padding: 4px; }}
.email-content pre {{ white-space: pre-wrap; overflow-wrap: anywhere; font-family: Arial, sans-serif; }}
.email-content a {{ color: #173b63; overflow-wrap: anywhere; }}
</style>
</head>
<body>
<header class="document-header">
  <div class="brand">MAILPRINT <span>EMAIL RECORD</span></div>
  <h1>{escape(email.subject or "(No subject)")}</h1>
  <table class="metadata"><tbody>{metadata_html}</tbody></table>
  <div class="generated">Print copy generated: {escape(generated_at)}</div>
</header>
<main class="email-content">
  <h2>Message body</h2>
  {body}
</main>
</body>
</html>"""


def build_email_footer_html(email):
    sender = escape(email.sender or "Unknown sender")
    return f"""<div style="font-family:Arial,sans-serif;font-size:8px;color:#526170;width:100%;padding:0 15mm;box-sizing:border-box;">
<div style="border-top:1px solid #cbd5df;padding-top:5px;">
Source email: {sender}
</div>
<div style="padding-top:3px;">
Generated from this email and printed using MailPrint Email Monitoring Tool.
<span style="float:right;">Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>
</div>
</div>"""


def _format_datetime(value):
    if value is None:
        return ""
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return timezone.localtime(value).strftime("%b %d, %Y %I:%M:%S %p %Z")


def _metadata_row(label, value):
    return (
        f"<tr><th>{escape(label)}</th>"
        f"<td>{escape(str(value))}</td></tr>"
    )


def render_email_to_pdf(email):
    html = build_email_html(email)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(java_script_enabled=False)
                page = context.new_page()
                page.route("**/*", lambda route: route.abort())
                page.set_content(html, wait_until="domcontentloaded", timeout=30000)
                return page.pdf(
                    format="A4",
                    print_background=True,
                    display_header_footer=True,
                    header_template="<div></div>",
                    footer_template=build_email_footer_html(email),
                    margin={
                        "top": "15mm",
                        "right": "15mm",
                        "bottom": "22mm",
                        "left": "15mm",
                    },
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise ConversionError(
            "Could not render the email body to PDF. Install Chromium with "
            "'python -m playwright install chromium'."
        ) from exc


def convert_attachment_to_pdf(attachment, output_directory):
    if not attachment.file:
        raise ConversionError(f"Attachment {attachment.filename!r} has no saved file.")
    source = Path(attachment.file.path)
    extension = source.suffix.lower()
    if extension == ".pdf":
        return source
    if extension in IMAGE_EXTENSIONS:
        return _image_to_pdf(source, output_directory)
    if extension in OFFICE_EXTENSIONS:
        return _office_to_pdf(source, output_directory)
    raise ConversionError(f"Unsupported printable attachment type: {extension or 'unknown'}.")


def _image_to_pdf(source, output_directory):
    Path(output_directory).mkdir(parents=True, exist_ok=True)
    destination = Path(output_directory) / f"{source.stem}.pdf"
    try:
        with Image.open(source) as image:
            image.seek(0)
            rgba = image.convert("RGBA")
            page = Image.new("RGB", rgba.size, "white")
            page.paste(rgba, mask=rgba.getchannel("A"))
            page.save(destination, format="PDF", resolution=300)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ConversionError(f"Could not convert image {source.name!r}: {exc}") from exc
    return destination


def _office_to_pdf(source, output_directory):
    executable = config("LIBREOFFICE_PATH", default="soffice")
    resolved_executable = shutil.which(executable) if not Path(executable).is_absolute() else executable
    if not resolved_executable or (Path(executable).is_absolute() and not Path(executable).is_file()):
        raise ConversionError(
            "LibreOffice is required to print Office documents. "
            "Install it or set LIBREOFFICE_PATH in .env."
        )
    target_directory = Path(output_directory) / f"office-{source.stem}"
    target_directory.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                resolved_executable,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(target_directory),
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConversionError(f"LibreOffice conversion failed for {source.name!r}: {exc}") from exc
    output = target_directory / f"{source.stem}.pdf"
    if result.returncode != 0 or not output.is_file():
        detail = (result.stderr or result.stdout or "no conversion output").strip()
        raise ConversionError(
            f"LibreOffice conversion failed for {source.name!r}: {detail[:1000]}"
        )
    return output

import shutil
import subprocess
from html import escape
from pathlib import Path

from decouple import config
from django.utils import timezone

from core.models import get_user_department
from mails.models import Acknowledgement
from mails.rendering import build_email_display_content, sanitize_email_display_html
from PIL import Image, UnidentifiedImageError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


OFFICE_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods", ".rtf"}
IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
PDF_PAGE_MARGINS = {
    "top": "10mm",
    "right": "12mm",
    "bottom": "16mm",
    "left": "12mm",
}


class ConversionError(Exception):
    pass


def build_email_html(email, *, include_replies=False):
    body = build_email_display_content(email.body_html, email.body_text)

    replies_html = ""
    if include_replies and email.pk:
        replies = list(
            email.acknowledgements.filter(
                status=Acknowledgement.Status.SENT,
            ).order_by("sent_at", "pk")
        )
        rendered_replies = "".join(
            _reply_html(reply)
            for reply in replies
        )
        if rendered_replies:
            replies_html = (
                '<section class="reply-thread">'
                f"<h2>Replies ({len(replies)})</h2>"
                f"{rendered_replies}</section>"
            )

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
    page_margin_css = " ".join(
        PDF_PAGE_MARGINS[side] for side in ("top", "right", "bottom", "left")
    )
    generated_at = timezone.localtime().strftime("%b %d, %Y %I:%M:%S %p %Z")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; img-src data:; style-src 'unsafe-inline';">
<style>
@page {{ size: A4; margin: {page_margin_css}; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: Arial, sans-serif; font-size: 10pt; color: #17212b; }}
.brand {{ color: #173b63; font-size: 9pt; font-weight: 700; letter-spacing: 1px; }}
.document-header {{ border-top: 3px solid #173b63; border-bottom: 1px solid #cbd5df; padding: 6px 0 8px; }}
h1 {{ margin: 5px 0 8px; font-size: 18pt; line-height: 1.2; overflow-wrap: anywhere; }}
.metadata {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 12px; margin: 0; }}
.metadata-item {{ display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 3px 6px; min-width: 0; border-top: 1px solid #e3e8ed; padding: 3px 0; font-size: 9pt; }}
.metadata-item-wide {{ grid-column: 1 / -1; }}
.metadata-item dt {{ margin: 0; color: #526170; font-weight: 600; }}
.metadata-item dd {{ min-width: 0; margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; }}
.generated {{ color: #526170; font-size: 8pt; margin-top: 6px; }}
.email-content {{ padding-top: 8px; overflow-wrap: anywhere; }}
h2 {{ color: #173b63; font-size: 10pt; margin: 0 0 6px; text-transform: uppercase; letter-spacing: .4px; }}
.email-content img {{ max-width: 100%; height: auto; }}
.email-content table {{ border-collapse: collapse; max-width: 100%; }}
.email-content td, .email-content th {{ border: 1px solid #aeb8c2; padding: 3px; }}
.email-content p {{ margin: 0 0 6px; }}
.email-content > :first-child {{ margin-top: 0; }}
.email-content > :last-child {{ margin-bottom: 0; }}
.email-content h1, .email-content h2, .email-content h3, .email-content h4 {{ margin: 10px 0 5px; break-after: avoid-page; }}
.email-content ul, .email-content ol {{ margin: 4px 0; padding-left: 20px; }}
.email-content li {{ margin-bottom: 2px; }}
.email-content blockquote {{ margin: 6px 0; padding-left: 10px; }}
.email-content pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; }}
.email-content a {{ color: #173b63; overflow-wrap: anywhere; }}
.reply-thread {{ margin-top: 12px; }}
.reply {{ border-top: 1px solid #cbd5df; padding: 6px 0; }}
.reply-header {{ display: flex; justify-content: space-between; gap: 12px; font-size: 9pt; overflow-wrap: anywhere; }}
.reply-body {{ padding-top: 4px; }}
</style>
</head>
<body>
<header class="document-header">
  <div class="brand">MAILPRINT <span>EMAIL RECORD</span></div>
  <h1>{escape(email.subject or "(No subject)")}</h1>
  <dl class="metadata">{metadata_html}</dl>
  <div class="generated">Print copy generated: {escape(generated_at)}</div>
</header>
<main class="email-content">
  <h2>Message body</h2>
  {body}
  {replies_html}
</main>
</body>
</html>"""


def build_email_footer_html(email, *, user=None):
    sender = escape(email.sender or "Unknown sender")
    department = escape(get_user_department(user) or "ICT Unit")
    return f"""<div style="font-family:Arial,sans-serif;font-size:8px;color:#526170;width:100%;padding:0 12mm;box-sizing:border-box;">
<div style="border-top:1px solid #cbd5df;padding-top:5px;">
Source email: {sender}
</div>
<div style="padding-top:3px;">
MAILPRINT EMAIL MONITORING SYSTEM DEVELOPED BY ICT UNIT - <span style="font-weight:600;color:#405d78;background:#f1f5f8;padding:1px 3px;border-radius:2px;">printed by {department}</span>
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
    wide_labels = {"To", "Cc", "Bcc", "Recipients", "Message ID", "Attachments"}
    class_name = "metadata-item metadata-item-wide" if label in wide_labels else "metadata-item"
    return (
        f'<div class="{class_name}"><dt>{escape(label)}</dt>'
        f"<dd>{escape(str(value))}</dd></div>"
    )


def _reply_html(reply):
    sender = escape(reply.sender or "You")
    recipient = escape(reply.recipient or "Unknown recipient")
    sent_at = escape(_format_datetime(reply.sent_at) or "Date unavailable")
    safe_body_html = sanitize_email_display_html(reply.body_html)
    if safe_body_html:
        body = safe_body_html
    elif reply.body and (not reply.body_html or reply.body != reply.body_html):
        body = f"<pre>{escape(reply.body)}</pre>"
    else:
        body = "<p>(No displayable body content.)</p>"
    return (
        '<article class="reply">'
        f'<header class="reply-header"><div><strong>From:</strong> {sender}'
        f"<br><strong>To:</strong> {recipient}</div><time>{sent_at}</time></header>"
        f'<div class="email-content reply-body">{body}</div></article>'
    )


def render_email_to_pdf(email, *, include_replies=False, user=None):
    html = build_email_html(email, include_replies=include_replies)
    footer = build_email_footer_html(email, user=user)
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
                    footer_template=footer,
                    margin=PDF_PAGE_MARGINS,
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

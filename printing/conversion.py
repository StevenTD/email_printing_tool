import shutil
import subprocess
from html import escape
from pathlib import Path

from decouple import config
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

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; img-src data:; style-src 'unsafe-inline';">
<style>
body {{ font-family: Arial, sans-serif; font-size: 11pt; color: #111; }}
img {{ max-width: 100%; }}
table {{ border-collapse: collapse; max-width: 100%; }}
td, th {{ border: 1px solid #999; padding: 4px; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
</style>
</head>
<body>{body}</body>
</html>"""


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
                    margin={
                        "top": "15mm",
                        "right": "15mm",
                        "bottom": "15mm",
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

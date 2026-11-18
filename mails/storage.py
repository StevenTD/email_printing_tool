import hashlib
import re
from pathlib import Path

from django.utils import timezone


def message_directory_key(email):
    identifier = email.message_id or f"uid-{email.imap_uid}"
    safe_identifier = re.sub(r"[^A-Za-z0-9._-]+", "_", identifier.strip("<>"))
    safe_identifier = safe_identifier.strip("._-")[:100] or "message"
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
    return f"{safe_identifier}-{digest}"


def email_file_upload_to(instance, filename):
    email = getattr(instance, "email", instance)
    received = email.date_received or timezone.now()
    safe_filename = Path(filename.replace("\\", "/")).name or "attachment"
    return (
        f"mail/{received:%Y}/{received:%m}/"
        f"{message_directory_key(email)}/{safe_filename}"
    )

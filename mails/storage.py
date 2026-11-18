import hashlib
import re
from pathlib import Path

from django.utils import timezone


MAX_DIRECTORY_KEY_LENGTH = 40
DIRECTORY_HASH_LENGTH = 12
MAX_STORED_FILENAME_LENGTH = 45
FILENAME_HASH_LENGTH = 8


def message_directory_key(email):
    identifier = email.message_id or f"uid-{email.imap_uid}"
    safe_identifier = re.sub(r"[^A-Za-z0-9._-]+", "_", identifier.strip("<>"))
    safe_identifier = (
        safe_identifier.strip("._-")[
            : MAX_DIRECTORY_KEY_LENGTH - DIRECTORY_HASH_LENGTH - 1
        ]
        or "message"
    )
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[
        :DIRECTORY_HASH_LENGTH
    ]
    return f"{safe_identifier}-{digest}"


def _stored_filename(filename):
    original_name = Path(filename.replace("\\", "/")).name or "attachment"
    suffix = Path(original_name).suffix
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:12]
    original_stem = original_name[: -len(suffix)] if suffix else original_name
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", original_stem).strip("._-")
    safe_stem = safe_stem or "attachment"
    digest = hashlib.sha256(original_name.encode("utf-8")).hexdigest()[
        :FILENAME_HASH_LENGTH
    ]
    stem_length = (
        MAX_STORED_FILENAME_LENGTH - len(safe_suffix) - FILENAME_HASH_LENGTH - 1
    )
    return f"{safe_stem[:stem_length]}-{digest}{safe_suffix}"


def email_file_upload_to(instance, filename):
    email = getattr(instance, "email", instance)
    received = email.date_received or timezone.now()
    return (
        f"mail/{received:%Y}/{received:%m}/"
        f"{message_directory_key(email)}/{_stored_filename(filename)}"
    )

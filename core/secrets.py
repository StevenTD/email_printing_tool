from cryptography.fernet import Fernet, InvalidToken
from decouple import config
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    key = config("MAILPRINT_FERNET_KEY", default="")
    if not key:
        raise ImproperlyConfigured(
            "MAILPRINT_FERNET_KEY is required to encrypt stored Gmail credentials."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ImproperlyConfigured(
            "MAILPRINT_FERNET_KEY must be a valid Fernet key."
        ) from exc


def encrypt_secret(value):
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value):
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "Stored credential cannot be decrypted; check MAILPRINT_FERNET_KEY."
        ) from exc

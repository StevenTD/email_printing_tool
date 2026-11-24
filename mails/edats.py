import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings


class EdatsLookupError(Exception):
    pass


def build_public_tracking_url(tracking_number, sender_name):
    base_url = settings.EDATS_PUBLIC_TRACKING_URL
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise EdatsLookupError("EDATS public tracking URL must use HTTPS.")
    if not sender_name:
        raise EdatsLookupError(
            "EDATS document is missing the sender name required for public tracking."
        )
    query = urlencode({"sender_name": sender_name})
    url = f"{base_url}/track/{quote(tracking_number, safe='')}/"
    return f"{url}?{query}"


def fetch_edats_document(tracking_number):
    token = settings.EDATS_API_TOKEN.strip()
    if not token:
        raise EdatsLookupError("EDATS_API_TOKEN is not configured in the .env file.")

    base_url = settings.EDATS_API_BASE_URL
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise EdatsLookupError("EDATS_API_BASE_URL must be a valid HTTPS URL.")

    endpoint = f"{base_url}/document/tracking/{quote(tracking_number, safe='')}"
    request = Request(
        endpoint,
        headers={
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "User-Agent": "MailPrint EDATS integration",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            response_body = response.read(1024 * 1024 + 1)
            if len(response_body) > 1024 * 1024:
                raise EdatsLookupError("EDATS returned an unexpectedly large response.")
    except HTTPError as exc:
        if exc.code == 404:
            raise EdatsLookupError(
                f"EDATS tracking number {tracking_number} was not found or is not accessible."
            ) from exc
        if exc.code in (401, 403):
            raise EdatsLookupError(
                "EDATS rejected the configured token or the account cannot access this document."
            ) from exc
        raise EdatsLookupError(
            f"EDATS document lookup failed with HTTP {exc.code}."
        ) from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise EdatsLookupError(f"Could not connect to EDATS: {exc}") from exc

    try:
        document = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EdatsLookupError("EDATS returned an invalid JSON response.") from exc
    if not isinstance(document, dict):
        raise EdatsLookupError("EDATS returned an unexpected document response.")

    returned_tracking_number = document.get("tracking_number")
    subject = document.get("subject")
    sender_name = document.get("sender")
    if not isinstance(returned_tracking_number, str) or not isinstance(subject, str):
        raise EdatsLookupError(
            "EDATS response is missing the document tracking number or subject."
        )
    if not isinstance(sender_name, str) or not sender_name.strip():
        raise EdatsLookupError(
            "EDATS response is missing the sender name required for public tracking."
        )
    sender_name = sender_name.strip()
    if returned_tracking_number.casefold() != tracking_number.casefold():
        raise EdatsLookupError(
            "EDATS returned a different tracking number than the one requested."
        )

    return {
        "tracking_number": returned_tracking_number,
        "subject": subject,
        "sender": sender_name,
        "tracking_base_url": settings.EDATS_PUBLIC_TRACKING_URL,
        "tracking_url": build_public_tracking_url(
            returned_tracking_number,
            sender_name,
        ),
    }


def subjects_match(email_subject, edats_subject):
    def normalize(value):
        return " ".join((value or "").split()).casefold()

    return bool(normalize(email_subject)) and normalize(email_subject) == normalize(
        edats_subject
    )

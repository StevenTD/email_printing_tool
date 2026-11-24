import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


EDATS_SEARCH_PAGE_SIZE = 5


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


def _relation_label(value, label):
    if isinstance(value, dict):
        for key in ("display_name", "name", "full_name", "username", "email"):
            if value.get(key):
                return str(value[key])
        value = value.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{label} ID {value}"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _document_receiver(document):
    if document.get("receiver"):
        return _relation_label(document["receiver"], "Receiver")

    department = next(
        (
            document[key]
            for key in ("to_department_data", "to_department_name", "to_department")
            if document.get(key) is not None
        ),
        None,
    )
    user = next(
        (
            document[key]
            for key in ("to_user_data", "to_user_name", "to_user")
            if document.get(key) is not None
        ),
        None,
    )
    receiver_parts = [
        value
        for value in (
            _relation_label(department, "Department"),
            _relation_label(user, "User"),
        )
        if value
    ]
    return " · ".join(receiver_parts) or "Not specified"


def _format_document_date(value):
    if not isinstance(value, str) or not value.strip():
        return "Date unavailable"
    parsed = parse_datetime(value)
    if parsed is None:
        return value
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return timezone.localtime(parsed).strftime("%b %d, %Y %I:%M %p")


def search_edats_documents(search_term, *, page=1):
    search_term = search_term.strip()
    if not search_term:
        raise EdatsLookupError("Enter a subject, sender, or tracking number to search.")
    if len(search_term) > 250:
        raise EdatsLookupError("EDATS search text must be 250 characters or fewer.")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise EdatsLookupError("EDATS search page must be a positive number.")

    token = settings.EDATS_API_TOKEN.strip()
    if not token:
        raise EdatsLookupError("EDATS_API_TOKEN is not configured in the .env file.")

    base_url = settings.EDATS_DOCUMENT_SEARCH_URL
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise EdatsLookupError("EDATS_DOCUMENT_SEARCH_URL must be a valid HTTPS URL.")

    query = urlencode(
        {
            "page": page,
            "page_size": EDATS_SEARCH_PAGE_SIZE,
            "search": search_term,
        }
    )
    request = Request(
        f"{base_url}?{query}",
        headers={
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "User-Agent": "MailPrint EDATS integration",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            response_body = response.read(5 * 1024 * 1024 + 1)
            if len(response_body) > 5 * 1024 * 1024:
                raise EdatsLookupError(
                    "EDATS search returned an unexpectedly large response."
                )
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise EdatsLookupError(
                "EDATS rejected the configured token or the account cannot search these documents."
            ) from exc
        raise EdatsLookupError(
            f"EDATS document search failed with HTTP {exc.code}."
        ) from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise EdatsLookupError(f"Could not connect to EDATS: {exc}") from exc

    try:
        response_data = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EdatsLookupError("EDATS returned an invalid search response.") from exc
    if not isinstance(response_data, dict):
        raise EdatsLookupError("EDATS returned an unexpected search response.")

    count = response_data.get("count")
    results = response_data.get("results")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(results, list)
        or "next" not in response_data
        or "previous" not in response_data
    ):
        raise EdatsLookupError(
            "EDATS search response is missing pagination data or results."
        )
    if len(results) > EDATS_SEARCH_PAGE_SIZE:
        raise EdatsLookupError(
            "EDATS returned more than five results for a single page."
        )

    normalized_results = []
    for document in results:
        if not isinstance(document, dict):
            raise EdatsLookupError("EDATS returned an invalid document in search results.")
        tracking_number = document.get("tracking_number")
        subject = document.get("subject")
        if not isinstance(tracking_number, str) or not isinstance(subject, str):
            raise EdatsLookupError(
                "An EDATS search result is missing its tracking number or subject."
            )
        sender = document.get("sender")
        normalized_results.append(
            {
                "tracking_number": tracking_number,
                "subject": subject,
                "sender": sender if isinstance(sender, str) and sender else "—",
                "receiver": _document_receiver(document),
                "created_at": _format_document_date(document.get("created_at")),
            }
        )

    next_page = response_data.get("next")
    previous_page = response_data.get("previous")
    if next_page is not None and not isinstance(next_page, str):
        raise EdatsLookupError("EDATS returned invalid next-page information.")
    if previous_page is not None and not isinstance(previous_page, str):
        raise EdatsLookupError("EDATS returned invalid previous-page information.")

    return {
        "count": count,
        "page": page,
        "page_size": EDATS_SEARCH_PAGE_SIZE,
        "page_count": math.ceil(count / EDATS_SEARCH_PAGE_SIZE),
        "has_next": bool(next_page),
        "has_previous": bool(previous_page),
        "results": normalized_results,
    }


def subjects_match(email_subject, edats_subject):
    def normalize(value):
        return " ".join((value or "").split()).casefold()

    return bool(normalize(email_subject)) and normalize(email_subject) == normalize(
        edats_subject
    )

import imaplib
import logging
import re
from dataclasses import dataclass
from email import message_from_bytes
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import WorkerLog
from rules.processing import process_incoming_email

from .models import Acknowledgement, EmailAttachment, ReceivedEmail


logger = logging.getLogger(__name__)

PRINTABLE_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/rtf",
    "text/rtf",
}
PRINTABLE_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".odt",
    ".ods",
    ".rtf",
}
IMAP_MONTH_NAMES = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
MESSAGE_ID_PATTERN = re.compile(r"<[^<>\s]+>")
IMAP_UID_PATTERN = re.compile(rb"\bUID\s+(\d+)\b", re.IGNORECASE)
SENT_HEADER_BATCH_SIZE = 50


@dataclass
class FetchSummary:
    discovered: int = 0
    stored: int = 0
    duplicates: int = 0
    replies: int = 0
    errors: int = 0


def _log(level, message, email=None):
    WorkerLog.objects.create(
        level=level,
        component="imap",
        message=str(message),
        email=email,
    )


def _is_printable(filename, content_type):
    extension = filename.rpartition(".")[2].lower()
    extension = f".{extension}" if extension else ""
    return (
        content_type in PRINTABLE_MIME_TYPES
        or content_type.startswith("image/")
        or extension in PRINTABLE_EXTENSIONS
    )


def _message_datetime(message):
    try:
        value = parsedate_to_datetime(message["Date"])
    except (TypeError, ValueError, OverflowError):
        return None
    if value is not None and timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _message_header_recipients(message, header):
    headers = message.get_all(header, [])
    return [address for _, address in getaddresses(headers) if address]


def _message_recipients(message):
    return [
        address
        for header in ("To", "Cc", "Bcc")
        for address in _message_header_recipients(message, header)
    ]


def _extract_message_content(message):
    body_text = []
    body_html = []
    attachments = []
    warnings = []

    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if filename or disposition == "attachment":
            payload = part.get_payload(decode=True)
            if payload is None:
                raw_payload = part.get_payload()
                if isinstance(raw_payload, str):
                    charset = part.get_content_charset() or "utf-8"
                    payload = raw_payload.encode(charset, errors="replace")
                else:
                    payload = b""
            attachments.append(
                {
                    "filename": filename or "attachment",
                    "content_type": part.get_content_type().lower(),
                    "payload": payload,
                }
            )
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
            if isinstance(content, bytes):
                content = content.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, UnicodeError, TypeError, ValueError) as exc:
            warnings.append(f"Could not decode {part.get_content_type()} body part: {exc}")
            continue
        if part.get_content_type() == "text/html":
            body_html.append(content)
        else:
            body_text.append(content)
    return "\n".join(body_text), "\n".join(body_html), attachments, warnings


def store_message(raw_message, *, folder, uid, uidvalidity, max_attachment_bytes):
    message = message_from_bytes(raw_message, policy=default)
    message_id = str(message.get("Message-ID", "")).strip()
    existing = _find_existing_email(folder, uid, uidvalidity, message_id)
    if existing is not None:
        return existing, False

    body_text, body_html, parsed_attachments, warnings = _extract_message_content(message)
    email = ReceivedEmail(
        message_id=message_id,
        imap_folder=folder,
        imap_uidvalidity=uidvalidity,
        imap_uid=uid,
        sender=str(message.get("From", "")),
        recipients=_message_recipients(message),
        to_recipients=_message_header_recipients(message, "To"),
        cc_recipients=_message_header_recipients(message, "Cc"),
        bcc_recipients=_message_header_recipients(message, "Bcc"),
        subject=str(message.get("Subject", "")),
        date_received=_message_datetime(message),
        body_text=body_text,
        body_html=body_html,
    )
    stored_files = []
    try:
        with transaction.atomic():
            email.save()
            email.raw_email_file.save(
                "message.eml",
                ContentFile(raw_message),
                save=False,
            )
            stored_files.append(email.raw_email_file.name)
            email.save(update_fields=["raw_email_file"])

            for item in parsed_attachments:
                payload = item["payload"]
                attachment = EmailAttachment(
                    email=email,
                    filename=item["filename"],
                    content_type=item["content_type"],
                    size_bytes=len(payload),
                    printable=_is_printable(item["filename"], item["content_type"]),
                )
                if len(payload) > max_attachment_bytes:
                    attachment.conversion_error = (
                        f"Attachment exceeds the configured {max_attachment_bytes}-byte limit."
                    )
                    warnings.append(
                        f"Attachment {item['filename']!r} exceeds the download size limit."
                    )
                elif not payload:
                    attachment.conversion_error = "Attachment payload is empty or could not be decoded."
                    warnings.append(
                        f"Attachment {item['filename']!r} is empty or could not be decoded."
                    )
                else:
                    attachment.file.save(
                        item["filename"],
                        ContentFile(payload),
                        save=False,
                    )
                    stored_files.append(attachment.file.name)
                attachment.save()
    except IntegrityError:
        existing = _find_existing_email(folder, uid, uidvalidity, message_id)
        if existing is not None:
            for name in stored_files:
                default_storage.delete(name)
            return existing, False
        raise
    except Exception:
        for name in stored_files:
            default_storage.delete(name)
        raise

    for warning in warnings:
        _log(WorkerLog.Level.WARNING, warning, email=email)
    return email, True


def _find_existing_email(folder, uid, uidvalidity, message_id):
    duplicate = Q(imap_uidvalidity=uidvalidity, imap_uid=uid)
    if message_id:
        duplicate |= Q(message_id=message_id)
    return ReceivedEmail.objects.filter(imap_folder=folder).filter(duplicate).first()


def _quote_mailbox(folder):
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _uid_validity(connection):
    try:
        _, values = connection.response("UIDVALIDITY")
        return int(values[0]) if values and values[0] else 0
    except (TypeError, ValueError, IndexError, imaplib.IMAP4.error):
        return 0


class MailFetcher:
    def __init__(
        self,
        configuration,
        *,
        connection_factory=None,
        max_email_bytes=25 * 1024 * 1024,
        max_attachment_bytes=25 * 1024 * 1024,
        fetch_since_date=None,
        fetch_seen_emails=False,
        status_callback=None,
    ):
        self.configuration = configuration
        self.connection_factory = connection_factory or imaplib.IMAP4_SSL
        self.max_email_bytes = max_email_bytes
        self.max_attachment_bytes = max_attachment_bytes
        self.fetch_since_date = fetch_since_date
        self.fetch_seen_emails = fetch_seen_emails
        self.status_callback = status_callback

    def fetch_messages(self):
        self._report_status("Connecting to Gmail IMAP (imap.gmail.com:993).")
        connection = self.connection_factory("imap.gmail.com", 993, timeout=30)
        summary = FetchSummary()
        try:
            self._require_ok(connection.login(
                self.configuration.imap_email,
                self.configuration.get_imap_app_password(),
            ), "Gmail login")
            self._report_status("Gmail IMAP login succeeded.")
            summary.replies, reply_errors = self._fetch_sent_replies(connection)
            summary.errors += reply_errors

            folder = self.configuration.imap_folder
            self._report_status(f"Selecting Gmail inbox folder {folder!r}.")
            self._require_ok(connection.select(_quote_mailbox(folder)), f"Select folder {folder!r}")
            uidvalidity = _uid_validity(connection)

            search_criteria = ["ALL" if self.fetch_seen_emails else "UNSEEN"]
            if self.fetch_since_date is not None:
                date = self.fetch_since_date
                month_name = IMAP_MONTH_NAMES[date.month - 1]
                imap_date = f"{date.day:02d}-{month_name}-{date.year:04d}"
                search_criteria.extend(["SINCE", imap_date])
            status, data = connection.uid("SEARCH", None, *search_criteria)
            self._require_ok((status, data), "Search for messages")
            message_uids = data[0].split() if data and data[0] else []
            summary.discovered = len(message_uids)
            self._report_status(
                f"Found {summary.discovered} inbox message(s) to check."
            )

            message_uids.sort(key=int, reverse=True)
            for index, uid_bytes in enumerate(message_uids, start=1):
                uid = int(uid_bytes)
                self._report_status(
                    f"Checking inbox messages: {index} of {summary.discovered} "
                    f"(UID {uid})."
                )
                try:
                    existing = ReceivedEmail.objects.filter(
                        imap_folder=folder,
                        imap_uidvalidity=uidvalidity,
                        imap_uid=uid,
                    ).first()
                    if existing is not None:
                        if existing.status == ReceivedEmail.Status.NEW:
                            process_incoming_email(existing)
                        self._mark_seen(connection, uid)
                        summary.duplicates += 1
                        continue

                    raw_message = self._fetch_raw_message(connection, uid)
                    if len(raw_message) > self.max_email_bytes:
                        raise ValueError(
                            f"Email exceeds the configured {self.max_email_bytes}-byte download limit."
                        )
                    email, created = store_message(
                        raw_message,
                        folder=folder,
                        uid=uid,
                        uidvalidity=uidvalidity,
                        max_attachment_bytes=self.max_attachment_bytes,
                    )
                    if created or email.status == ReceivedEmail.Status.NEW:
                        process_incoming_email(email)
                    self._mark_seen(connection, uid)
                    if created:
                        summary.stored += 1
                    else:
                        summary.duplicates += 1
                except imaplib.IMAP4.abort:
                    raise
                except Exception as exc:
                    summary.errors += 1
                    _log(WorkerLog.Level.ERROR, f"UID {uid}: {exc}")
                    self._report_status(
                        f"Could not process inbox UID {uid}: {exc}; continuing."
                    )
            self._report_status(
                f"Inbox check complete: {summary.stored} stored, "
                f"{summary.duplicates} already stored."
            )
            return summary
        finally:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                logger.warning("IMAP logout failed", exc_info=True)

    def _fetch_sent_replies(self, connection):
        folder = self.configuration.imap_sent_folder.strip()
        if not folder:
            self._report_status("Gmail Sent-folder reply sync is disabled.")
            return 0, 0

        self._report_status(f"Checking Gmail Sent folder {folder!r}.")
        status, data = connection.select(_quote_mailbox(folder))
        if status != "OK":
            detail = data[0] if data else "no server detail"
            self._report_status(
                f"Could not select Gmail Sent folder {folder!r}: {detail!r}"
            )
            _log(
                WorkerLog.Level.ERROR,
                f"Could not select Gmail Sent folder {folder!r}: {detail!r}",
            )
            return 0, 1

        uidvalidity = _uid_validity(connection)
        same_mailbox = (
            self.configuration.imap_sent_sync_folder == folder
            and self.configuration.imap_sent_uidvalidity == uidvalidity
        )
        last_uid = self.configuration.imap_sent_last_uid if same_mailbox else 0
        search_criteria = (
            ["UID", f"{last_uid + 1}:*"] if last_uid else ["ALL"]
        )
        result = connection.uid("SEARCH", None, *search_criteria)
        if result[0] != "OK":
            detail = result[1][0] if result[1] else "no server detail"
            self._report_status(
                f"Could not search Gmail Sent folder {folder!r}: {detail!r}"
            )
            _log(
                WorkerLog.Level.ERROR,
                f"Could not search Gmail Sent folder {folder!r}: {detail!r}",
            )
            return 0, 1

        uids = result[1][0].split() if result[1] and result[1][0] else []
        self._report_status(
            f"Found {len(uids)} Gmail Sent message(s) to check for replies."
        )
        latest_uid = last_uid
        reply_count = 0
        error_count = 0
        uids = sorted(uids, key=int)
        stop_at_error = False
        for batch_start in range(0, len(uids), SENT_HEADER_BATCH_SIZE):
            batch = uids[batch_start : batch_start + SENT_HEADER_BATCH_SIZE]
            batch_end = batch_start + len(batch)
            self._report_status(
                f"Fetching headers for Gmail Sent messages "
                f"{batch_start + 1}-{batch_end} of {len(uids)}."
            )
            try:
                headers_by_uid = self._fetch_sent_headers(connection, batch)
            except imaplib.IMAP4.abort:
                raise
            except Exception as exc:
                error_count += 1
                _log(
                    WorkerLog.Level.ERROR,
                    f"Gmail Sent UIDs {batch_start + 1}-{batch_end}: {exc}",
                )
                self._report_status(
                    f"Could not fetch Gmail Sent headers for messages "
                    f"{batch_start + 1}-{batch_end}: {exc}"
                )
                break

            for index, uid_bytes in enumerate(batch, start=batch_start + 1):
                uid = int(uid_bytes)
                raw_headers = headers_by_uid.get(uid_bytes.decode("ascii"))
                if raw_headers is None:
                    error_count += 1
                    _log(
                        WorkerLog.Level.ERROR,
                        f"Gmail Sent UID {uid}: Gmail returned no message headers.",
                    )
                    stop_at_error = True
                    break
                if index == 1 or index % 100 == 0:
                    self._report_status(
                        f"Checking Gmail Sent messages: {index} of {len(uids)}."
                    )
                try:
                    if self._record_sent_reply(connection, uid, raw_headers):
                        reply_count += 1
                except imaplib.IMAP4.abort:
                    raise
                except Exception as exc:
                    error_count += 1
                    _log(
                        WorkerLog.Level.ERROR,
                        f"Gmail Sent UID {uid}: {exc}",
                    )
                    self._report_status(
                        f"Could not check Gmail Sent UID {uid}: {exc}"
                    )
                    stop_at_error = True
                    break
                latest_uid = max(latest_uid, uid)
            if stop_at_error:
                break

        if latest_uid > last_uid or not error_count:
            self.configuration.imap_sent_sync_folder = folder
            self.configuration.imap_sent_uidvalidity = uidvalidity
            self.configuration.imap_sent_last_uid = latest_uid
            if not self.configuration._state.adding:
                self.configuration.save(
                    update_fields=[
                        "imap_sent_sync_folder",
                        "imap_sent_uidvalidity",
                        "imap_sent_last_uid",
                        "updated_at",
                    ]
                )
        self._report_status(
            f"Gmail Sent check complete: {reply_count} reply/replies recorded, "
            f"{error_count} errors."
        )
        return reply_count, error_count

    def _fetch_sent_headers(self, connection, uid_bytes_list):
        uid_set = ",".join(uid.decode("ascii") for uid in uid_bytes_list)
        result = connection.uid(
            "FETCH",
            uid_set,
            "(UID BODY.PEEK[HEADER.FIELDS "
            "(MESSAGE-ID IN-REPLY-TO REFERENCES TO SUBJECT DATE)])",
        )
        self._require_ok(result, f"Fetch Gmail Sent headers for UIDs {uid_set}")
        headers_by_uid = {}
        for item in result[1] or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            match = IMAP_UID_PATTERN.search(item[0])
            if match is None:
                raise ValueError("Gmail Sent header response did not include a UID.")
            headers_by_uid[match.group(1).decode("ascii")] = item[1]
        return headers_by_uid

    def _record_sent_reply(self, connection, uid, raw_headers):
        message = message_from_bytes(raw_headers, policy=default)
        message_id = str(message.get("Message-ID", "")).strip()
        if not message_id:
            _log(
                WorkerLog.Level.WARNING,
                "Skipped a Gmail Sent message without a Message-ID.",
            )
            return False

        parent_message_ids = []
        for header in ("In-Reply-To", "References"):
            for value in message.get_all(header, []):
                parent_message_ids.extend(reversed(MESSAGE_ID_PATTERN.findall(str(value))))
        email = None
        for parent_message_id in dict.fromkeys(parent_message_ids):
            email = ReceivedEmail.objects.filter(
                imap_folder=self.configuration.imap_folder,
                message_id__iexact=parent_message_id,
            ).first()
            if email is not None:
                break
        if email is None or email.acknowledgements.filter(message_id=message_id).exists():
            return False

        self._report_status(
            f"Matched Gmail Sent UID {uid} to stored email {email.pk}; "
            "fetching reply body."
        )
        raw_message = self._fetch_raw_message(connection, uid)
        message = message_from_bytes(raw_message, policy=default)
        body_text, body_html, _, _ = _extract_message_content(message)
        recipients = _message_header_recipients(message, "To")
        Acknowledgement.objects.create(
            email=email,
            source=Acknowledgement.Source.GMAIL,
            recipient=", ".join(recipients) or "Unknown recipient",
            sender=str(message.get("From", "")),
            subject=str(message.get("Subject", "")),
            body=body_text or body_html,
            body_html=body_html,
            message_id=message_id,
            status=Acknowledgement.Status.SENT,
            sent_at=_message_datetime(message),
        )
        email.acknowledged = True
        email.acknowledged_at = _message_datetime(message) or timezone.now()
        email.save(update_fields=["acknowledged", "acknowledged_at"])
        return True

    @staticmethod
    def _require_ok(result, operation):
        status, data = result
        if status != "OK":
            detail = data[0] if data else "no server detail"
            raise ConnectionError(f"{operation} failed: {detail!r}")

    def _report_status(self, message):
        if self.status_callback is not None:
            self.status_callback(message)

    def _fetch_raw_message(self, connection, uid):
        result = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
        self._require_ok(result, f"Fetch UID {uid}")
        for item in result[1] or []:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                return item[1]
        raise ValueError(f"Gmail returned no message body for UID {uid}.")

    def _mark_seen(self, connection, uid):
        self._require_ok(
            connection.uid("STORE", str(uid), "+FLAGS", r"(\Seen)"),
            f"Mark UID {uid} as seen",
        )

import re
from email.utils import parseaddr
from pathlib import PurePath

from .models import FilterRule


def rule_matches(rule, email):
    sender_address = parseaddr(email.sender)[1].casefold()
    if rule.sender_match == FilterRule.SenderMatch.EXACT:
        expected = rule.sender_value.strip().casefold()
        if not expected or sender_address != expected:
            return False
    elif rule.sender_match == FilterRule.SenderMatch.DOMAIN:
        expected_domain = rule.sender_value.strip().lstrip("@").casefold()
        sender_domain = sender_address.rpartition("@")[2]
        if not expected_domain or sender_domain != expected_domain:
            return False

    subject = email.subject or ""
    if rule.subject_match == FilterRule.SubjectMatch.CONTAINS:
        if not rule.subject_value or rule.subject_value.casefold() not in subject.casefold():
            return False
    elif rule.subject_match == FilterRule.SubjectMatch.REGEX:
        if not re.search(rule.subject_value, subject, flags=re.IGNORECASE):
            return False

    if rule.attachment_type:
        expected_type = rule.attachment_type.strip().casefold()
        attachments = email.attachments.all()
        if not any(_attachment_matches(attachment, expected_type) for attachment in attachments):
            return False
    return True


def _attachment_matches(attachment, expected_type):
    filename = attachment.filename.casefold()
    extension = PurePath(filename).suffix
    content_type = attachment.content_type.casefold()
    if expected_type.startswith("."):
        return extension == expected_type
    if expected_type.endswith("/*"):
        return content_type.startswith(expected_type[:-1])
    if "/" in expected_type:
        return content_type == expected_type
    return extension == f".{expected_type}"


def find_matching_rule(email):
    for rule in FilterRule.objects.filter(enabled=True).order_by("priority", "id"):
        if rule_matches(rule, email):
            return rule
    return None

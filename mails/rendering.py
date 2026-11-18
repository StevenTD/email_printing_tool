import bleach


ALLOWED_EMAIL_HTML_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "div", "dl",
    "dt", "em", "h1", "h2", "h3", "h4", "hr", "i", "img", "li", "ol", "p",
    "pre", "span", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead",
    "tr", "u", "ul",
}
ALLOWED_EMAIL_HTML_ATTRIBUTES = {
    "a": ["href", "title"],
    "img": ["alt", "height", "src", "width"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}


def _allow_email_attribute(tag, name, value):
    if name not in ALLOWED_EMAIL_HTML_ATTRIBUTES.get(tag, []):
        return False
    if tag == "img" and name == "src" and not value.lower().startswith("data:"):
        return False
    return True


def sanitize_email_html(value):
    return bleach.clean(
        value,
        tags=ALLOWED_EMAIL_HTML_TAGS,
        attributes=_allow_email_attribute,
        protocols=["data", "http", "https", "mailto"],
        strip=True,
    )

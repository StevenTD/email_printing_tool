from html import escape, unescape
from html.parser import HTMLParser

import bleach
from bleach.css_sanitizer import CSSSanitizer


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
ALLOWED_EMAIL_DISPLAY_TAGS = ALLOWED_EMAIL_HTML_TAGS | {
    "center",
    "font",
    "small",
    "style",
    "s",
    "strike",
    "tfoot",
}
ALLOWED_EMAIL_DISPLAY_ATTRIBUTES = {
    "align",
    "bgcolor",
    "border",
    "cellpadding",
    "cellspacing",
    "class",
    "dir",
    "height",
    "id",
    "lang",
    "role",
    "style",
    "valign",
    "width",
}
EMAIL_DISPLAY_CSS_SANITIZER = CSSSanitizer(
    allowed_css_properties={
        "background",
        "background-color",
        "border",
        "border-bottom",
        "border-bottom-color",
        "border-bottom-style",
        "border-bottom-width",
        "border-collapse",
        "border-color",
        "border-left",
        "border-left-color",
        "border-left-style",
        "border-left-width",
        "border-radius",
        "border-right",
        "border-right-color",
        "border-right-style",
        "border-right-width",
        "border-spacing",
        "border-style",
        "border-top",
        "border-top-color",
        "border-top-style",
        "border-top-width",
        "border-width",
        "clear",
        "color",
        "display",
        "float",
        "font",
        "font-family",
        "font-size",
        "font-style",
        "font-variant",
        "font-weight",
        "height",
        "letter-spacing",
        "line-height",
        "list-style",
        "list-style-position",
        "list-style-type",
        "margin",
        "margin-bottom",
        "margin-left",
        "margin-right",
        "margin-top",
        "max-height",
        "max-width",
        "min-height",
        "min-width",
        "opacity",
        "overflow",
        "padding",
        "padding-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "text-align",
        "text-decoration",
        "text-indent",
        "text-transform",
        "vertical-align",
        "white-space",
        "width",
        "word-break",
        "word-spacing",
    }
)


class _HTMLMarkupDetector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.has_markup = False

    def handle_starttag(self, tag, attrs):
        self.has_markup = True

    def handle_startendtag(self, tag, attrs):
        self.has_markup = True

    def handle_endtag(self, tag):
        self.has_markup = True


def _contains_html_markup(value):
    parser = _HTMLMarkupDetector()
    parser.feed(value)
    parser.close()
    return parser.has_markup


def _allow_email_attribute(tag, name, value):
    if name not in ALLOWED_EMAIL_HTML_ATTRIBUTES.get(tag, []):
        return False
    if tag == "img" and name == "src" and not value.lower().startswith("data:"):
        return False
    return True


def _allow_email_display_attribute(tag, name, value):
    if name in ALLOWED_EMAIL_DISPLAY_ATTRIBUTES:
        return True
    if tag == "font" and name in {"color", "face", "size"}:
        return True
    return _allow_email_attribute(tag, name, value)


def sanitize_email_html(value):
    return bleach.clean(
        value,
        tags=ALLOWED_EMAIL_HTML_TAGS,
        attributes=_allow_email_attribute,
        protocols=["data", "http", "https", "mailto"],
        strip=True,
    )


def sanitize_email_display_html(value):
    return bleach.clean(
        value,
        tags=ALLOWED_EMAIL_DISPLAY_TAGS,
        attributes=_allow_email_display_attribute,
        protocols=["data", "http", "https", "mailto"],
        strip=True,
        css_sanitizer=EMAIL_DISPLAY_CSS_SANITIZER,
    )


def build_email_display_content(body_html, body_text):
    content = body_html
    if not content:
        content = body_text or "(No body content.)"
        decoded_content = unescape(content)
        if _contains_html_markup(decoded_content):
            content = decoded_content
        else:
            content = f"<pre>{escape(content)}</pre>"
    elif not _contains_html_markup(content):
        decoded_content = unescape(content)
        if _contains_html_markup(decoded_content):
            content = decoded_content

    return sanitize_email_display_html(content)


def build_email_display_document(body_html, body_text):
    safe_content = build_email_display_content(body_html, body_text)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'">
<style>
html {{ font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #202124; }}
body {{ margin: 12px; overflow-wrap: anywhere; }}
img, table {{ max-width: 100%; }}
img {{ height: auto; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
</style>
</head>
<body>{safe_content}</body>
</html>"""

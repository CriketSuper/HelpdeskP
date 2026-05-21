import re
from html import unescape

import bleach
from django.utils.html import strip_tags


ALLOWED_TAGS = [
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "ul",
    "ol",
    "li",
    "blockquote",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
]
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
    "p": ["align"],
    "li": ["align"],
    "blockquote": ["align"],
    "table": ["align"],
    "th": ["align", "colspan", "rowspan"],
    "td": ["align", "colspan", "rowspan"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def sanitize_rich_text(value):
    prepared = _prepare_rich_text_html(value)
    cleaned = bleach.clean(
        prepared,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    return _normalize_rich_text(cleaned)


def rich_text_to_plain_text(value):
    normalized = sanitize_rich_text(value)
    normalized = normalized.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    normalized = re.sub(r"</p\s*>", "\n\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</blockquote\s*>", "\n\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<li[^>]*>", "- ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</li\s*>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</t[dh]\s*>\s*<t[dh][^>]*>", "\t", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</tr\s*>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</table\s*>", "\n\n", normalized, flags=re.IGNORECASE)
    normalized = strip_tags(normalized)
    normalized = unescape(normalized)
    normalized = normalized.replace("\xa0", " ")
    normalized = normalized.replace("\r\n", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    return normalized.strip()


def rich_text_to_audit_text(value):
    normalized = rich_text_to_plain_text(value)
    normalized = normalized.replace("\t", " | ")
    normalized = re.sub(r"\n+", " / ", normalized)
    normalized = re.sub(r"\s{2,}", " ", normalized)
    normalized = re.sub(r"\s*/\s*", " / ", normalized)
    normalized = re.sub(r"\s*\|\s*", " | ", normalized)
    return normalized.strip(" /|")


def rich_text_has_text(value):
    return bool(rich_text_to_plain_text(value))


def _normalize_rich_text(value):
    normalized = (value or "").replace("\r\n", "\n").strip()
    normalized = re.sub(r"<p>\s*</p>", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\s+(</(p|li|ul|ol|blockquote|table|thead|tbody|tr|th|td)>)",
        r"\1",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(<(p|li|ul|ol|blockquote|table|thead|tbody|tr|th|td)[^>]*>)\s+",
        r"\1",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized


def _prepare_rich_text_html(value):
    prepared = (value or "").replace("\r\n", "\n")
    prepared = re.sub(r"<div>\s*<br\s*/?>\s*</div>", "<p></p>", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"</div>\s*<div([^>]*)>", r"</p><p\1>", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"<div([^>]*)>", r"<p\1>", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"</div>", "</p>", prepared, flags=re.IGNORECASE)

    def replace_align(match):
        tag = match.group("tag")
        before = match.group("before") or ""
        align = match.group("align").lower()
        after = match.group("after") or ""
        attrs = f"{before}{after}"
        attrs = re.sub(r'\sstyle="[^"]*"', "", attrs, flags=re.IGNORECASE)
        attrs = re.sub(r"\s+", " ", attrs).strip()
        attrs = f" {attrs}" if attrs else ""
        return f"<{tag}{attrs} align=\"{align}\">"

    prepared = re.sub(
        r'<(?P<tag>p|li|blockquote|table|th|td)(?P<before>[^>]*)\sstyle="[^"]*text-align\s*:\s*(?P<align>left|center|right)\s*;?[^"]*"(?P<after>[^>]*)>',
        replace_align,
        prepared,
        flags=re.IGNORECASE,
    )
    return prepared

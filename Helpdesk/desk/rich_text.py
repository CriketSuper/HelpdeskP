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
]
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
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
    cleaned = _normalize_rich_text(cleaned)
    return cleaned


def rich_text_to_plain_text(value):
    normalized = (value or "").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    normalized = re.sub(r"</p\s*>", "\n\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</li\s*>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<li[^>]*>", "• ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</blockquote\s*>", "\n\n", normalized, flags=re.IGNORECASE)
    normalized = strip_tags(normalized)
    normalized = unescape(normalized)
    normalized = normalized.replace("\r\n", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def rich_text_has_text(value):
    return bool(rich_text_to_plain_text(value))


def _normalize_rich_text(value):
    normalized = (value or "").replace("\r\n", "\n").strip()
    normalized = re.sub(r"<p>\s*</p>", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+(</(p|li|ul|ol|blockquote)>)", r"\1", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"(<(p|li|ul|ol|blockquote)[^>]*>)\s+", r"\1", normalized, flags=re.IGNORECASE)
    return normalized


def _prepare_rich_text_html(value):
    prepared = (value or "").replace("\r\n", "\n")
    prepared = re.sub(r"<div>\s*<br\s*/?>\s*</div>", "<p></p>", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"</div>\s*<div[^>]*>", "</p><p>", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"<div[^>]*>", "<p>", prepared, flags=re.IGNORECASE)
    prepared = re.sub(r"</div>", "</p>", prepared, flags=re.IGNORECASE)
    return prepared

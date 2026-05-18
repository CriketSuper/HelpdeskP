import re
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from docxtpl import DocxTemplate, RichText
from pymorphy3 import MorphAnalyzer
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .rich_text import rich_text_has_text, rich_text_to_plain_text, sanitize_rich_text
from .models import UserProfile


TICKET_TEMPLATE_PATH = Path(settings.BASE_DIR) / "static" / "docs" / "ticket_template.docx"
MONTH_NAMES = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}
CYRILLIC_RE = re.compile(r"^[А-Яа-яЁё-]+$")


class _RichTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self._current_segments = []
        self._inline_depth = {"bold": 0, "italic": 0, "underline": 0}
        self._blockquote_depth = 0
        self._list_stack = []

    def handle_starttag(self, tag, attrs):
        if tag in {"strong", "b"}:
            self._inline_depth["bold"] += 1
        elif tag in {"em", "i"}:
            self._inline_depth["italic"] += 1
        elif tag == "u":
            self._inline_depth["underline"] += 1
        elif tag == "blockquote":
            self._flush_block("paragraph")
            self._blockquote_depth += 1
        elif tag in {"ul", "ol"}:
            self._flush_block("paragraph")
            self._list_stack.append({"type": tag, "index": 0})
        elif tag == "p":
            self._flush_block("paragraph")
        elif tag == "li":
            marker = "- "
            if self._list_stack and self._list_stack[-1]["type"] == "ol":
                self._list_stack[-1]["index"] += 1
                marker = f"{self._list_stack[-1]['index']}. "
            self._append_text(marker)
        elif tag == "br":
            self._append_text("\n")

    def handle_endtag(self, tag):
        if tag in {"strong", "b"}:
            self._inline_depth["bold"] = max(0, self._inline_depth["bold"] - 1)
        elif tag in {"em", "i"}:
            self._inline_depth["italic"] = max(0, self._inline_depth["italic"] - 1)
        elif tag == "u":
            self._inline_depth["underline"] = max(0, self._inline_depth["underline"] - 1)
        elif tag == "blockquote":
            self._flush_block("blockquote")
            self._blockquote_depth = max(0, self._blockquote_depth - 1)
        elif tag == "li":
            self._flush_block("list_item")
        elif tag == "p":
            self._flush_block("paragraph")
        elif tag in {"ul", "ol"} and self._list_stack:
            self._list_stack.pop()

    def handle_data(self, data):
        self._append_text(data)

    def close(self):
        super().close()
        self._flush_block("paragraph")

    def _append_text(self, text):
        if not text:
            return
        self._current_segments.append(
            {
                "text": text,
                "bold": self._inline_depth["bold"] > 0,
                "italic": self._inline_depth["italic"] > 0 or self._blockquote_depth > 0,
                "underline": self._inline_depth["underline"] > 0,
            }
        )

    def _flush_block(self, block_type):
        if not self._current_segments:
            return
        if not any(segment["text"].strip() for segment in self._current_segments):
            self._current_segments = []
            return
        self.blocks.append({"type": block_type, "segments": self._current_segments})
        self._current_segments = []


def _get_user_source_name(user):
    if user is None:
        return ""
    try:
        return user.profile.verbose_name
    except UserProfile.DoesNotExist:
        return user.get_username()


def _get_user_position(user):
    if user is None:
        return ""
    try:
        return (user.profile.position or "").strip()
    except UserProfile.DoesNotExist:
        return ""


@lru_cache(maxsize=1)
def _morph():
    return MorphAnalyzer()


def _split_person_name(full_name):
    parts = [part for part in (full_name or "").split() if part]
    surname = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    patronymic = parts[2] if len(parts) > 2 else ""
    remainder = parts[3:] if len(parts) > 3 else []
    return surname, name, patronymic, remainder


def _build_initials(name, patronymic):
    initials = []
    if name:
        initials.append(f"{name[0]}.")
    if patronymic:
        initials.append(f"{patronymic[0]}.")
    return "".join(initials)


def _apply_casing(source, value):
    if source.isupper():
        return value.upper()
    if source.istitle():
        return value.capitalize()
    return value


def _pick_parse(word, allowed_pos=None, require_grammeme=None, preferred_grammemes=None):
    allowed_pos = set(allowed_pos or [])
    preferred_grammemes = set(preferred_grammemes or [])
    parses = _morph().parse(word)

    for parse in parses:
        if require_grammeme and require_grammeme not in parse.tag:
            continue
        if allowed_pos and parse.tag.POS not in allowed_pos:
            continue
        if preferred_grammemes and not preferred_grammemes.issubset(set(parse.tag.grammemes)):
            continue
        return parse

    for parse in parses:
        if allowed_pos and parse.tag.POS not in allowed_pos:
            continue
        return parse

    return parses[0] if parses else None


def _inflect_word(
    word,
    grammatical_case,
    allowed_pos=None,
    require_grammeme=None,
    preferred_grammemes=None,
):
    if not word or not CYRILLIC_RE.match(word):
        return word
    parse = _pick_parse(
        word,
        allowed_pos=allowed_pos,
        require_grammeme=require_grammeme,
        preferred_grammemes=preferred_grammemes,
    )
    if parse is None:
        return word
    inflected = parse.inflect({grammatical_case})
    if inflected is None:
        return word
    return _apply_casing(word, inflected.word)


def _infer_person_gender(name, patronymic):
    patronymic_normalized = (patronymic or "").strip().lower()
    if patronymic_normalized.endswith("вна"):
        return "femn"
    if patronymic_normalized.endswith("ич"):
        return "masc"

    name_normalized = (name or "").strip()
    if not name_normalized or not CYRILLIC_RE.match(name_normalized):
        return None

    parses = _morph().parse(name_normalized)
    for parse in parses:
        grammemes = set(parse.tag.grammemes)
        if "Name" not in grammemes:
            continue
        if "femn" in grammemes:
            return "femn"
        if "masc" in grammemes:
            return "masc"
    return None


def _inflect_position(position, grammatical_case):
    words = [word for word in (position or "").split() if word]
    if not words:
        return ""

    parsed_words = [_pick_parse(word) for word in words]
    head_index = None
    for index, parse in enumerate(parsed_words):
        if parse and parse.tag.POS == "NOUN":
            head_index = index
            break

    if head_index is None:
        return " ".join(_inflect_word(word, grammatical_case) for word in words)

    inflected_words = []
    for index, word in enumerate(words):
        parse = parsed_words[index]
        if parse is None:
            inflected_words.append(word)
            continue
        if index < head_index and parse.tag.POS in {"ADJF", "PRTF"}:
            inflected_words.append(_inflect_word(word, grammatical_case, allowed_pos={"ADJF", "PRTF"}))
        elif index == head_index:
            inflected_words.append(_inflect_word(word, grammatical_case, allowed_pos={"NOUN"}))
        else:
            inflected_words.append(word)
    return " ".join(inflected_words)


def _format_person_for_document(user, grammatical_case, initials_first=False):
    full_name = _get_user_source_name(user)
    surname, name, patronymic, remainder = _split_person_name(full_name)
    if not surname:
        return full_name

    gender = _infer_person_gender(name, patronymic)
    inflected_surname = _inflect_word(
        surname,
        grammatical_case,
        allowed_pos={"NOUN", "ADJF"},
        require_grammeme="Surn",
        preferred_grammemes={gender} if gender else None,
    )
    initials = _build_initials(name, patronymic)
    surname_tail = " ".join([inflected_surname, *remainder]).strip()

    if not initials:
        return surname_tail
    if initials_first:
        return f"{initials} {surname_tail}".strip()
    return f"{surname_tail} {initials}".strip()


def _format_ticket_document_date(ticket):
    source_date = timezone.localtime(ticket.created_at) if ticket.created_at else timezone.localtime()
    return f"«{source_date.day:02d}» {MONTH_NAMES[source_date.month]} {source_date.year} г."


def _parse_rich_text_blocks(value):
    sanitized = sanitize_rich_text(value)
    if not rich_text_has_text(sanitized):
        return []
    parser = _RichTextParser()
    parser.feed(sanitized)
    parser.close()
    return parser.blocks


def _build_docx_rich_text(value):
    rich_text = RichText()
    blocks = _parse_rich_text_blocks(value)
    if not blocks:
        rich_text.add(rich_text_to_plain_text(value), font="Times New Roman", size=28)
        return rich_text

    for block_index, block in enumerate(blocks):
        if block_index:
            rich_text.add("\a")
        for segment in block["segments"]:
            _append_docx_segment(rich_text, segment)
    return rich_text


def _append_docx_segment(rich_text, segment):
    parts = segment["text"].split("\n")
    for part_index, part in enumerate(parts):
        rich_text.add(
            part,
            bold=segment["bold"],
            italic=segment["italic"],
            underline=segment["underline"],
            font="Times New Roman",
            size=28,
        )
        if part_index < len(parts) - 1:
            rich_text.add("\n", font="Times New Roman", size=28)


def _segment_to_pdf_markup(segment, regular_font_name, bold_font_name, italic_font_name, bold_italic_font_name):
    text = escape(segment["text"]).replace("\n", "<br/>")
    if segment["underline"]:
        text = f"<u>{text}</u>"
    font_name = regular_font_name
    if segment["bold"] and segment["italic"]:
        font_name = bold_italic_font_name
    elif segment["bold"]:
        font_name = bold_font_name
    elif segment["italic"]:
        font_name = italic_font_name
    text = f'<font name="{font_name}">{text}</font>'
    return text


def _build_pdf_content_flowables(
    value,
    regular_style,
    quote_style,
    regular_font_name,
    bold_font_name,
    italic_font_name,
    bold_italic_font_name,
):
    blocks = _parse_rich_text_blocks(value)
    if not blocks:
        plain_text = rich_text_to_plain_text(value)
        return [
            Paragraph(
                f'<font name="{regular_font_name}">{escape(plain_text).replace("\n", "<br/>")}</font>',
                regular_style,
            )
        ]

    flowables = []
    for block in blocks:
        markup = "".join(
            _segment_to_pdf_markup(
                segment,
                regular_font_name,
                bold_font_name,
                italic_font_name,
                bold_italic_font_name,
            )
            for segment in block["segments"]
        )
        style = quote_style if block["type"] == "blockquote" else regular_style
        flowables.append(Paragraph(markup, style))
        flowables.append(Spacer(1, 6))
    if flowables:
        flowables.pop()
    return flowables


def build_ticket_document_context(ticket):
    return {
        "technician_position_dative": _inflect_position(_get_user_position(ticket.technician), "datv"),
        "organization_name": getattr(settings, "ORGANIZATION_NAME", "Helpdesk"),
        "technician_name_dative": _format_person_for_document(ticket.technician, "datv", initials_first=True),
        "author_position_accusative": _inflect_position(_get_user_position(ticket.created_by), "accs"),
        "author_name_accusative": _format_person_for_document(ticket.created_by, "accs", initials_first=False),
        "ticket_content_plain": rich_text_to_plain_text(ticket.content),
        "ticket_content_rich": _build_docx_rich_text(ticket.content),
        "ticket_date": _format_ticket_document_date(ticket),
    }


def build_ticket_docx_filename(ticket):
    return f"ticket_{ticket.pk}.docx"


def build_ticket_pdf_filename(ticket):
    return f"ticket_{ticket.pk}.pdf"


def render_ticket_docx(ticket):
    document_template = DocxTemplate(str(TICKET_TEMPLATE_PATH))
    document_template.render(build_ticket_document_context(ticket))
    output = BytesIO()
    document_template.save(output)
    output.seek(0)
    return output.getvalue()


def _find_font_path(candidates):
    for candidate in candidates:
        font_path = Path(candidate)
        if font_path.exists():
            return font_path
    return None


@lru_cache(maxsize=1)
def _register_pdf_fonts():
    regular_font_path = _find_font_path(
        [
            Path(settings.BASE_DIR) / "static" / "fonts" / "times.ttf",
            Path("C:/Windows/Fonts/times.ttf"),
            Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
        ]
    )
    bold_font_path = _find_font_path(
        [
            Path(settings.BASE_DIR) / "static" / "fonts" / "timesbd.ttf",
            Path("C:/Windows/Fonts/timesbd.ttf"),
            Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
        ]
    )
    italic_font_path = _find_font_path(
        [
            Path(settings.BASE_DIR) / "static" / "fonts" / "timesi.ttf",
            Path("C:/Windows/Fonts/timesi.ttf"),
            Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Italic.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSerif-Italic.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"),
        ]
    )
    bold_italic_font_path = _find_font_path(
        [
            Path(settings.BASE_DIR) / "static" / "fonts" / "timesbi.ttf",
            Path("C:/Windows/Fonts/timesbi.ttf"),
            Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Bold_Italic.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSerif-BoldItalic.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf"),
        ]
    )

    if regular_font_path is None:
        raise FileNotFoundError("No serif font with Cyrillic support was found for PDF export.")

    regular_name = "TicketDocRegular"
    bold_name = "TicketDocBold"
    italic_name = "TicketDocItalic"
    bold_italic_name = "TicketDocBoldItalic"
    if regular_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular_name, str(regular_font_path)))
    if bold_font_path is not None and bold_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold_name, str(bold_font_path)))
    if italic_font_path is not None and italic_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(italic_name, str(italic_font_path)))
    if bold_italic_font_path is not None and bold_italic_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold_italic_name, str(bold_italic_font_path)))
    if bold_font_path is None:
        bold_name = regular_name
    if italic_font_path is None:
        italic_name = regular_name
    if bold_italic_font_path is None:
        bold_italic_name = bold_name if italic_name == regular_name else italic_name

    return regular_name, bold_name, italic_name, bold_italic_name


def render_ticket_pdf(ticket):
    regular_font_name, bold_font_name, italic_font_name, bold_italic_font_name = _register_pdf_fonts()
    context = build_ticket_document_context(ticket)
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=30 * mm,
        rightMargin=15 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    regular_style = ParagraphStyle(
        "TicketRegular",
        parent=styles["Normal"],
        fontName=regular_font_name,
        fontSize=14,
        leading=18,
    )
    right_style = ParagraphStyle(
        "TicketRight",
        parent=regular_style,
        alignment=TA_RIGHT,
    )
    title_style = ParagraphStyle(
        "TicketTitle",
        parent=regular_style,
        fontName=bold_font_name,
        alignment=TA_CENTER,
        spaceAfter=12,
    )
    content_style = ParagraphStyle(
        "TicketContent",
        parent=regular_style,
        firstLineIndent=14 * mm,
        spaceAfter=6,
    )
    quote_style = ParagraphStyle(
        "TicketQuote",
        parent=content_style,
        leftIndent=8 * mm,
        borderPadding=3,
        textColor="#495057",
    )

    story = []
    top_right_block = [
        Paragraph(context["technician_position_dative"], right_style),
        Paragraph(context["organization_name"], right_style),
        Paragraph(context["technician_name_dative"], right_style),
        Spacer(1, 4),
        Paragraph(context["author_position_accusative"], right_style),
        Paragraph(context["author_name_accusative"], right_style),
    ]
    header_table = Table([["", top_right_block]], colWidths=[95 * mm, 70 * mm])
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 14))
    story.append(Paragraph("СЛУЖЕБНАЯ ЗАПИСКА", title_style))
    story.append(Spacer(1, 8))
    story.extend(
        _build_pdf_content_flowables(
            ticket.content,
            content_style,
            quote_style,
            regular_font_name,
            bold_font_name,
            italic_font_name,
            bold_italic_font_name,
        )
    )
    story.append(Spacer(1, 28))
    footer_table = Table(
        [[Paragraph(context["ticket_date"], regular_style), Paragraph("________________", right_style)]],
        colWidths=[80 * mm, 85 * mm],
    )
    footer_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(footer_table)

    document.build(story)
    buffer.seek(0)
    return buffer.getvalue()

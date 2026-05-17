import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from docxtpl import DocxTemplate
from pymorphy3 import MorphAnalyzer
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


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


def _get_user_source_name(user):
    if user is None:
        return ""

    try:
        return user.profile.verbose_name
    except Exception:
        return user.get_username()


def _get_user_position(user):
    if user is None:
        return ""

    try:
        return (user.profile.position or "").strip()
    except Exception:
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


def _pick_parse(word, allowed_pos=None, require_grammeme=None):
    allowed_pos = set(allowed_pos or [])
    parses = _morph().parse(word)

    for parse in parses:
        if require_grammeme and require_grammeme not in parse.tag:
            continue
        if allowed_pos and parse.tag.POS not in allowed_pos:
            continue
        return parse

    for parse in parses:
        if allowed_pos and parse.tag.POS not in allowed_pos:
            continue
        return parse

    return parses[0] if parses else None


def _inflect_word(word, grammatical_case, allowed_pos=None, require_grammeme=None):
    if not word or not CYRILLIC_RE.match(word):
        return word

    parse = _pick_parse(word, allowed_pos=allowed_pos, require_grammeme=require_grammeme)
    if parse is None:
        return word

    inflected = parse.inflect({grammatical_case})
    if inflected is None:
        return word
    return _apply_casing(word, inflected.word)


def _inflect_position(position, grammatical_case):
    words = [word for word in (position or "").split() if word]
    if not words:
        return ""

    head_index = None
    parsed_words = []
    for word in words:
        parse = _pick_parse(word)
        parsed_words.append(parse)
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

    inflected_surname = _inflect_word(
        surname,
        grammatical_case,
        allowed_pos={"NOUN", "ADJF"},
        require_grammeme="Surn",
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


def build_ticket_document_context(ticket):
    return {
        "technician_position_dative": _inflect_position(_get_user_position(ticket.technician), "datv"),
        "organization_name": getattr(settings, "ORGANIZATION_NAME", "Helpdesk"),
        "technician_name_dative": _format_person_for_document(ticket.technician, "datv", initials_first=True),
        "author_position_accusative": _inflect_position(_get_user_position(ticket.created_by), "accs"),
        "author_name_accusative": _format_person_for_document(ticket.created_by, "accs", initials_first=False),
        "ticket_content": ticket.content,
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

    if regular_font_path is None:
        raise FileNotFoundError("No serif font with Cyrillic support was found for PDF export.")

    regular_name = "TicketDocRegular"
    bold_name = "TicketDocBold"

    if regular_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular_name, str(regular_font_path)))

    if bold_font_path is not None and bold_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold_name, str(bold_font_path)))

    if bold_font_path is None:
        bold_name = regular_name

    return regular_name, bold_name


def render_ticket_pdf(ticket):
    regular_font_name, bold_font_name = _register_pdf_fonts()
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
    story.append(Paragraph(context["ticket_content"].replace("\n", "<br/>"), content_style))
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

from os.path import basename

from django import template

register = template.Library()


def _extension(value):
    file_name = basename(str(value or ""))
    if "." not in file_name:
        return ""
    return file_name.rsplit(".", 1)[-1].lower()


@register.filter
def custom_basename(value):
    return basename(value)


@register.filter
def document_icon(value):
    extension = _extension(value)
    icon_map = {
        "doc": "images/doc_icons/icon-docx.png",
        "docx": "images/doc_icons/icon-docx.png",
        "pdf": "images/doc_icons/icon-pdf.png",
        "xls": "images/doc_icons/icon-xlsx.png",
        "xlsx": "images/doc_icons/icon-xlsx.png",
        "png": "images/doc_icons/icon-png.png",
        "jpg": "images/doc_icons/icon-jpg.png",
        "jpeg": "images/doc_icons/icon-jpg.png",
        "zip": "images/doc_icons/zip.png",
        "rar": "images/doc_icons/zip.png",
        "7z": "images/doc_icons/zip.png",
        "ppt": "images/doc_icons/icon-pptx.png",
        "pptx": "images/doc_icons/icon-pptx.png",
    }
    return icon_map.get(extension, "images/doc_icons/basic_file.png")


@register.filter
def document_extension(value):
    extension = _extension(value)
    return extension.upper() if extension else "FILE"


register.filter("custom_basename", custom_basename)
register.filter("document_icon", document_icon)
register.filter("document_extension", document_extension)

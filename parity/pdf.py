"""A small PDF writer for printable set lists.

The set list is text: a title, a column header and one line per track.  Pulling
in ReportLab (and its imaging stack) to draw that would be a large dependency
for a page nobody edits, so this writes the PDF directly.  It is the subset the
format needs and no more: Type1 Courier and Helvetica-Bold, one text stream per
page, absolute positioning, an xref table and a trailer.

The output is deliberately uncompressed.  A set list is a few pages; the bytes
being readable in a text editor is worth more than the few kilobytes gzip would
save, and it means a test can assert the names are really in the file.
"""

from __future__ import annotations

import time

#: A4 in points; the format DJs print on outside the US.
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
MARGIN = 48.0
BODY_SIZE = 9.0
LEADING = 13.0
TITLE_SIZE = 17.0


def _escape(text: str) -> str:
    """A PDF literal string: parentheses, backslash and nothing exotic.

    Text outside Latin-1 is replaced rather than dropped silently -- a title in
    Greek or Japanese becomes ``?`` characters, which is what the base-14 fonts
    can actually draw.  The alternative (an embedded font) is a different
    project than a printable set list.
    """
    cleaned = (
        str(text)
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )
    return cleaned.encode("latin-1", "replace").decode("latin-1")


def _text_line(x: float, y: float, text: str, font: str, size: float) -> str:
    return (
        f"BT /{font} {size:g} Tf {x:.2f} {y:.2f} Td "
        f"({_escape(text)}) Tj ET\n"
    )


def text_pdf(
    title: str,
    lines: list[tuple[str, bool]],
    *,
    subtitle: str = "",
    footer: str = "",
    page_width: float = PAGE_WIDTH,
    page_height: float = PAGE_HEIGHT,
    margin: float = MARGIN,
) -> bytes:
    """Render title + ``(text, bold)`` lines as a paginated PDF.

    The body font is Courier so the caller can pad columns and have them line
    up; the title and the bold header rows use Helvetica-Bold.  A line too long
    for the page is truncated, not wrapped: a set list row is one track, and a
    wrapped one would break the column alignment the font was chosen for.
    """
    # Courier at 9 pt is 5.4 pt per character; that is how many fit inside the
    # margins.  A character count is more predictable than measuring glyphs.
    usable = page_width - margin * 2
    columns = max(20, int(usable / (BODY_SIZE * 0.6)))
    top = page_height - margin
    first_y = top - TITLE_SIZE - 10.0 - (12.0 if subtitle else 0.0)
    per_page = max(1, int((first_y - margin - 14.0) / LEADING))
    pages = [lines[index : index + per_page] for index in range(0, len(lines), per_page)] or [[]]

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    # Reserve 1 = catalog, 2 = pages, 3/4/5 = the three fonts; pages follow.
    catalog_number, pages_number = 1, 2
    body_font, bold_font, courier_font = 3, 4, 5
    objects.extend([b"", b"", b"", b"", b""])
    page_numbers: list[int] = []
    for index, page_lines in enumerate(pages):
        content = [
            _text_line(margin, top - TITLE_SIZE, title, "F2", TITLE_SIZE),
        ]
        y = top - TITLE_SIZE - 10.0
        if subtitle:
            content.append(_text_line(margin, y, subtitle, "F1", 8.5))
            y -= 12.0
        y -= LEADING
        for text, bold in page_lines:
            content.append(
                _text_line(margin, y, str(text)[:columns], "F2" if bold else "F3", BODY_SIZE)
            )
            y -= LEADING
        foot = footer or time.strftime("%Y-%m-%d")
        foot = f"{foot}   ·   page {index + 1} of {len(pages)}"
        content.append(_text_line(margin, margin - 10.0, foot, "F1", 7.5))
        stream = "".join(content).encode("latin-1", "replace")
        content_number = add(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream
            + b"endstream"
        )
        page_number = add(
            (
                f"<< /Type /Page /Parent {pages_number} 0 R "
                f"/MediaBox [0 0 {page_width:.2f} {page_height:.2f}] "
                f"/Resources << /Font << /F1 {body_font} 0 R /F2 {bold_font} 0 R "
                f"/F3 {courier_font} 0 R >> >> "
                f"/Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        page_numbers.append(page_number)

    # Fonts: F1 Helvetica (footer/subtitle), F2 Helvetica-Bold (title/header),
    # F3 Courier (body columns).
    objects[body_font - 1] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    objects[bold_font - 1] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>"
    )
    objects[courier_font - 1] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
        b"/Encoding /WinAnsiEncoding >>"
    )
    objects[catalog_number - 1] = f"<< /Type /Catalog /Pages {pages_number} 0 R >>".encode("ascii")
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects[pages_number - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode("ascii")
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_number} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)

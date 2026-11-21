from contextlib import closing
from io import BytesIO
from itertools import groupby
from math import ceil
from threading import Lock

from pypdf import PdfReader, PdfWriter, Transformation
from pypdfium2 import PdfDocument
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

FONT = "LabelTitle"
FONT_SIZE = 7
LINE_HEIGHT = 8
MARGIN = 3
CLEARANCE = 5
SCALE = 2
PDFIUM_LOCK = Lock()
pdfmetrics.registerFont(TTFont(FONT, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))


class NoTitleSpace(ValueError):
    pass


def fit_prefix(text, width):
    for index in range(1, len(text) + 1):
        if pdfmetrics.stringWidth(text[:index], FONT, FONT_SIZE) > width:
            return text[:index - 1]
    return text


def title_lines(title, width):
    first = fit_prefix(title, width)
    if not first or first == title:
        return [first] if first else []
    if not first.endswith(" ") and title[len(first)] != " ":
        first = first.rstrip().rpartition(" ")[0] or first
    second = fit_prefix(title[len(first):].lstrip(), width)
    return [line.rstrip() for line in (first, second) if line]


def bottom_space(image, lines):
    height = ceil((MARGIN + (lines - 1) * LINE_HEIGHT + FONT_SIZE + CLEARANCE) * SCALE)
    if height >= image.height:
        return None
    nonwhite = image.crop((0, image.height - height, image.width, image.height)).point(
        lambda shade: 255 if shade < 255 else 0,
    )
    columns, _ = nonwhite.getprojection()
    left_margin = MARGIN * SCALE
    best = None
    for occupied, pixels in groupby(enumerate(columns), key=lambda column: column[1]):
        if occupied:
            continue
        run = list(pixels)
        left = max(left_margin, run[0][0] + CLEARANCE * SCALE)
        right = min(image.width - left_margin, run[-1][0] + 1 - CLEARANCE * SCALE)
        if right - left >= 30 * SCALE and (best is None or right - left > best[1]):
            best = left, right - left
    if best:
        return best[0] / SCALE, best[1] / SCALE
    return None


def title_overlay(title, image, width, height):
    for line_count in (2, 1):
        space = bottom_space(image, line_count)
        if space is None:
            continue
        left, available_width = space
        lines = title_lines(title, available_width)
        if lines and len(lines) <= line_count:
            output = BytesIO()
            canvas = Canvas(output, pagesize=(width, height))
            canvas.setFont(FONT, FONT_SIZE)
            for index, line in enumerate(reversed(lines)):
                canvas.drawString(left, MARGIN + index * LINE_HEIGHT, line)
            canvas.save()
            output.seek(0)
            return PdfReader(output).pages[0]
    raise NoTitleSpace


def add_title(source: bytes, title: str) -> bytes:
    writer = PdfWriter(clone_from=BytesIO(source))
    if not 1 <= len(writer.pages) <= 20:
        raise NoTitleSpace
    with PDFIUM_LOCK, PdfDocument(source) as document:
        for index, page in enumerate(writer.pages):
            with closing(document[index]) as rendered_page:
                width, height = rendered_page.get_size()
                if page.user_unit != 1 or not (0 < width <= 500 and 0 < height <= 500):
                    raise NoTitleSpace
                with closing(rendered_page.render(scale=SCALE, grayscale=True)) as bitmap:
                    image = bitmap.to_pil().copy()
            box = page.cropbox
            x, y = map(float, box.lower_left)
            w, h = float(box.width), float(box.height)
            transform = {
                0: (1, 0, 0, 1, x, y),
                90: (0, 1, -1, 0, x + w, y),
                180: (-1, 0, 0, -1, x + w, y + h),
                270: (0, -1, 1, 0, x, y + h),
            }.get(page.rotation % 360)
            if transform is None:
                raise NoTitleSpace
            overlay = title_overlay(title, image, width, height)
            page.merge_transformed_page(overlay, Transformation(transform), expand=False)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()

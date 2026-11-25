from io import BytesIO

from PIL import Image
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

STICKER_SIZE = (58 * mm, 40 * mm)


def sticker_pdf(png, label_number):
    output = BytesIO()
    with Image.open(BytesIO(png), formats=["PNG"]) as image:
        if image.size != (580, 400) or getattr(image, "is_animated", False):
            raise ValueError("Некорректные размеры стикера WB")
        canvas = Canvas(output, pagesize=STICKER_SIZE)
        canvas.setTitle(label_number)
        canvas.drawImage(ImageReader(image), 0, 0, *STICKER_SIZE, mask="auto")
        canvas.showPage()
        canvas.save()
    return output.getvalue()

"""Build a review-only preview of a plan set.

Every page is rendered to a bitmap, covered with a tiled diagonal watermark plus a solid banner,
and reassembled into an image-only PDF. The result has no text layer and no vector geometry, so it
can't be edited back into a usable plan, measured precisely, or traced in CAD.
"""
import io
import math
from functools import lru_cache

import img2pdf
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

BANNER_TEXT = "PRELIMINARY – NOT FOR CONSTRUCTION – PREVIEW ONLY"
MAX_PIXELS = 40_000_000  # cap per page so a giant sheet can't exhaust memory


@lru_cache(maxsize=16)
def _font(size):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _parse_box(box):
    if not box:
        return None
    if isinstance(box, str):
        box = [float(x) for x in box.split(",")]
    left, top, right, bottom = box
    return left, top, right, bottom


def watermark_page(img, text, seal_box=None):
    """Return an RGB copy of ``img`` with tiled watermark, top/bottom banners and optional seal cover."""
    img = img.convert("RGB")
    w, h = img.size
    short = min(w, h)

    # 1. Tiled diagonal watermark over the whole sheet.
    font = _font(max(14, short // 28))
    diag = int(math.hypot(w, h)) + 10
    layer = Image.new("RGBA", (diag, diag), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    tw, th = draw.textbbox((0, 0), text, font=font)[2:]
    step_x, step_y = tw + short // 10, th * 4
    for row, y in enumerate(range(0, diag, step_y)):
        offset = (row % 2) * (step_x // 2)
        for x in range(-step_x, diag, step_x):
            draw.text((x + offset, y), text, font=font, fill=(200, 30, 30, 80))
    layer = layer.rotate(32, resample=Image.BICUBIC)
    left, top = (diag - w) // 2, (diag - h) // 2
    layer = layer.crop((left, top, left + w, top + h))
    out = Image.alpha_composite(img.convert("RGBA"), layer)

    # 2. Solid banners top and bottom.
    draw = ImageDraw.Draw(out)
    band = max(24, h // 30)
    bfont = _font(int(band * 0.6))
    for y0 in (0, h - band):
        draw.rectangle([0, y0, w, y0 + band], fill=(200, 30, 30, 255))
        bw = draw.textbbox((0, 0), BANNER_TEXT, font=bfont)[2]
        draw.text(((w - bw) // 2, y0 + band * 0.18), BANNER_TEXT, font=bfont, fill=(255, 255, 255, 255))

    # 3. Optional solid cover over the seal/signature area.
    box = _parse_box(seal_box)
    if box:
        l, t, r, b = box
        rect = [int(l * w), int(t * h), int(r * w), int(b * h)]
        draw.rectangle(rect, fill=(235, 235, 235, 255), outline=(200, 30, 30, 255), width=4)
        label = "SEAL APPLIED ON PAID RELEASE"
        sfont = _font(max(12, (rect[2] - rect[0]) // 22))
        lw, lh = draw.textbbox((0, 0), label, font=sfont)[2:]
        draw.text(
            (rect[0] + (rect[2] - rect[0] - lw) // 2, rect[1] + (rect[3] - rect[1] - lh) // 2),
            label, font=sfont, fill=(200, 30, 30, 255),
        )

    return out.convert("RGB")


def build_preview(source, watermark_text, dpi=100, jpeg_quality=70, seal_box=None):
    """Render ``source`` (path, bytes, or binary file object) into a watermarked image-only PDF; return bytes."""
    if hasattr(source, "read"):
        source = source.read()
    pdf = pdfium.PdfDocument(source)
    pages = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            pw, ph = page.get_size()  # points (1/72 in)
            scale = dpi / 72
            if pw * scale * ph * scale > MAX_PIXELS:
                scale = math.sqrt(MAX_PIXELS / (pw * ph))
            bitmap = page.render(scale=scale)
            img = bitmap.to_pil()
            page.close()
            img = watermark_page(img, watermark_text, seal_box)
            buf = io.BytesIO()
            eff_dpi = round(scale * 72, 3)
            # Embedding the DPI makes img2pdf keep the true sheet size (e.g. 24x36 in).
            img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True, dpi=(eff_dpi, eff_dpi))
            pages.append(buf.getvalue())
            del img, bitmap
    finally:
        pdf.close()
    return img2pdf.convert(pages)

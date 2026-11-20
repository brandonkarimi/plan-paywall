"""Import the pictures used in client emails (payment QR codes) and on previews (watermark logo)."""
import base64
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from desktop import config
from plancore.preview import transparent_logo

QR_MAX_WIDTH = 600  # plenty to scan, small enough to keep the email light


class ImageError(Exception):
    pass


def store(slot, source) -> Path:
    """Copy the picture at ``source`` into the app as ``slot`` and return the stored path.

    QR codes are flattened onto white and shrunk; the watermark logo gets its white background removed.
    """
    if slot not in config.IMAGES:
        raise ImageError("Unknown picture.")
    try:
        img = Image.open(source)
        img.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise ImageError("That file isn't a picture this app can read. Use a PNG or JPG.") from exc
    if slot == "watermark":
        img = transparent_logo(img)
    else:
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
        if img.width > QR_MAX_WIDTH:
            img = img.resize((QR_MAX_WIDTH, round(img.height * QR_MAX_WIDTH / img.width)), Image.LANCZOS)
    out = config.images_dir() / f"{slot}.png"
    img.save(out, optimize=True)
    return out


def remove(slot):
    path = config.image_path(slot)
    if path:
        path.unlink()


def thumbnails():
    """{slot: data URL or ""} for showing the current pictures in Settings."""
    out = {}
    for slot in config.IMAGES:
        path = config.image_path(slot)
        out[slot] = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode() if path else ""
    return out

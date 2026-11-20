"""Draws the app icon: desktop/ui/icon.png (tray) and build/PlanSender.ico (exe)."""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent


def draw(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle([2 * s, 2 * s, 62 * s, 62 * s], radius=14 * s, fill=(29, 78, 216, 255))
    d.polygon([(18 * s, 12 * s), (38 * s, 12 * s), (46 * s, 20 * s), (46 * s, 52 * s), (18 * s, 52 * s)], fill=(255, 255, 255, 255))
    for y in (26, 33, 40):
        d.rectangle([24 * s, y * s, 40 * s, (y + 3) * s], fill=(29, 78, 216, 255))
    d.ellipse([36 * s, 38 * s, 56 * s, 58 * s], fill=(22, 163, 74, 255))
    d.text((41.5 * s, 39 * s), "$", fill=(255, 255, 255, 255), font_size=int(16 * s))
    return img


if __name__ == "__main__":
    img = draw()
    img.resize((128, 128), Image.LANCZOS).save(ROOT / "desktop" / "ui" / "icon.png")
    (ROOT / "build").mkdir(exist_ok=True)
    img.save(ROOT / "build" / "PlanSender.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icons written")

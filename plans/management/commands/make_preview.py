"""Try the preview generator on a real plan set: python manage.py make_preview in.pdf [-o out.pdf] [--dpi 100]"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from plans.services.pdfinfo import inspect_pdf
from plans.services.preview import build_preview


class Command(BaseCommand):
    help = "Build a watermarked, image-only preview of a PDF so you can tune DPI and watermark settings."

    def add_arguments(self, parser):
        parser.add_argument("pdf")
        parser.add_argument("-o", "--output")
        parser.add_argument("--dpi", type=int, default=settings.PREVIEW_DPI)
        parser.add_argument("--text", default="PRELIMINARY – NOT FOR CONSTRUCTION  ·  SAMPLE CLIENT")
        parser.add_argument("--seal-box", default=settings.PREVIEW_SEAL_BOX,
                            help='Fractions "left,top,right,bottom" to blank out, e.g. 0.80,0.70,1.0,1.0')

    def handle(self, pdf, output, dpi, text, seal_box, **_):
        src = Path(pdf)
        out = Path(output) if output else src.with_name(src.stem + "_PREVIEW.pdf")
        with src.open("rb") as f:
            info = inspect_pdf(f)
            data = build_preview(f, text, dpi=dpi, jpeg_quality=settings.PREVIEW_JPEG_QUALITY,
                                 seal_box=seal_box or None)
        out.write_bytes(data)
        self.stdout.write(self.style.SUCCESS(f"Wrote {out} ({len(data) / 1e6:.1f} MB)"))
        self.stdout.write(f"Source: {info['page_count']} pages, SHA-256 {info['sha256']}")
        for s in info["sheet_index"]:
            self.stdout.write(f"  - {s}")

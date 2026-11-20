"""Find the amount due on an invoice PDF (best effort).

Invoices from QuickBooks, Word, Excel etc. usually print the amount next to a label like "Balance Due",
"Amount Due" or "Total". Scanned (image-only) invoices have no text, so nothing is found and the engineer
types the price instead.
"""
import io
import re

from pypdf import PdfReader

AMOUNT = r"\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?)"
# Most specific labels first; the first label that matches decides.
LABELS = [
    r"balance\s+due",
    r"amount\s+due",
    r"total\s+due",
    r"amount\s+payable",
    r"grand\s+total",
    r"invoice\s+total",
    r"total\s+amount",
    r"(?<!sub)(?<!sub-)(?<!sub\s)total",
]


def _text(source) -> str:
    data = source.read() if hasattr(source, "read") else source
    if isinstance(data, str):
        with open(data, "rb") as f:
            data = f.read()
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:5])


def find_amount_cents(source):
    """Return the amount due in cents, or None if no clear amount is found."""
    try:
        text = _text(source)
    except Exception:
        return None
    flat = re.sub(r"[ \t]+", " ", text)
    for label in LABELS:
        # Label, then up to ~40 characters (a colon, "USD", spaces or a line break), then the amount.
        pattern = re.compile(label + r"[^\d$\n]{0,40}\n?[^\d$\n]{0,40}" + AMOUNT, re.IGNORECASE)
        amounts = []
        for m in pattern.finditer(flat):
            value = float(m.group(1).replace(",", ""))
            if value > 0:
                amounts.append(round(value * 100))
        if amounts:
            return max(amounts)  # e.g. "Total" printed twice, or a total above a smaller line item
    return None

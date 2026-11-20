"""Fingerprint and inspect plan PDFs (SHA-256, page count, sheet index)."""
import hashlib
import io

from pypdf import PdfReader


def sha256_of(fileobj, chunk=1024 * 1024):
    fileobj.seek(0)
    h = hashlib.sha256()
    while True:
        block = fileobj.read(chunk)
        if not block:
            break
        h.update(block)
    fileobj.seek(0)
    return h.hexdigest()


def _flatten_outline(reader, items, out):
    for item in items:
        if isinstance(item, list):
            _flatten_outline(reader, item, out)
        else:
            try:
                out.append((reader.get_destination_page_number(item), str(item.title).strip()))
            except Exception:  # malformed bookmark; skip it
                continue


def sheet_index(reader):
    """Best-effort sheet list: PDF bookmarks first, then page labels, then "Sheet N"."""
    n = len(reader.pages)
    try:
        entries = []
        _flatten_outline(reader, reader.outline, entries)
        by_page = {}
        for page_no, title in entries:
            if 0 <= page_no < n and title and page_no not in by_page:
                by_page[page_no] = title
        if len(by_page) >= max(1, n // 2):
            return [by_page.get(i, f"Sheet {i + 1}") for i in range(n)]
    except Exception:
        pass
    try:
        labels = list(reader.page_labels)
        if labels and any(not lbl.isdigit() for lbl in labels):
            return [str(lbl) for lbl in labels]
    except Exception:
        pass
    return [f"Sheet {i + 1}" for i in range(n)]


def inspect_pdf(fileobj):
    """Return dict(sha256, size_bytes, page_count, sheet_index). Raises ValueError if it isn't a readable PDF."""
    digest = sha256_of(fileobj)
    data = fileobj.read()
    fileobj.seek(0)
    if not data.startswith(b"%PDF"):
        raise ValueError("That file isn't a PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("Upload the PDF without a password; the portal handles access control.")
        pages = len(reader.pages)
        sheets = sheet_index(reader)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Couldn't read that PDF: {exc}") from exc
    return {"sha256": digest, "size_bytes": len(data), "page_count": pages, "sheet_index": sheets}

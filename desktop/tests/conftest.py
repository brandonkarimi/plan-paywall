import io

import keyring
import pytest
from keyring.backend import KeyringBackend
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


class FakeSMTP:
    sent = []
    fail_with = None

    def __init__(self, host, port, context=None, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        FakeSMTP.sent.append(msg)


def make_pdf(pages=3, size=(17 * inch, 11 * inch)):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    for i in range(pages):
        name = f"C-{101 + i} Sheet {i}"
        c.setFont("Helvetica", 22)
        c.drawString(72, size[1] - 100, f"{name} GRADING ELEV 100.{i}")
        c.bookmarkPage(f"p{i}")
        c.addOutlineEntry(name, f"p{i}", level=0)
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data dir, in-memory keyring, fake SMTP, one Gmail account, Zelle + Venmo enabled."""
    monkeypatch.setenv("PLANSENDER_HOME", str(tmp_path / "home"))
    keyring.set_keyring(MemoryKeyring())
    FakeSMTP.sent = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)

    from desktop import config, db

    db.init()
    settings = config.load_settings()
    settings["accounts"] = [{"email": "pat@acmecivil.test", "name": "Pat Lee", "preset": "gmail",
                             "host": "smtp.gmail.com", "port": 465, "security": "ssl"}]
    settings["default_from"] = "pat@acmecivil.test"
    settings["signature"] = {"name": "Pat Lee", "firm": "Acme Civil", "license": "12345", "phone": ""}
    settings["zelle"] = {"enabled": True, "recipient": "pat@acmecivil.test", "name": "Pat Lee"}
    settings["venmo"] = {"enabled": True, "username": "AcmeCivil-Eng"}
    settings["preview_dpi"] = 40
    config.save_settings(settings)
    config.set_secret("smtp:pat@acmecivil.test", "app-password")

    pdf = tmp_path / "Lot_7_Grading.pdf"
    pdf.write_bytes(make_pdf())
    return {"pdf": pdf, "tmp": tmp_path, "smtp": FakeSMTP}

import io
import itertools

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
        self.user = user

    def send_message(self, msg):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        FakeSMTP.sent.append(msg)


class FakeStripe:
    def __init__(self):
        self.counter = itertools.count(1)
        self.links = {}          # link_id -> tx snapshot
        self.results = {}        # link_id -> poll result
        self.deactivated = []
        self.refunds = {}        # payment_intent -> status

    def create_payment_link(self, tx, allow_ach=True):
        link_id = f"plink_{next(self.counter)}"
        self.links[link_id] = dict(tx)
        return link_id, f"https://buy.stripe.com/test_{link_id}?prefilled_email=x"

    def poll(self, link_id):
        return self.results.get(link_id, {"state": "open"})

    def deactivate(self, link_id):
        self.deactivated.append(link_id)

    def refund_status(self, pi):
        return self.refunds.get(pi, "none")

    def pay(self, link_id, amount, state="paid", currency="usd"):
        self.results[link_id] = {"state": state, "session_id": f"cs_{link_id}", "amount": amount,
                                 "currency": currency, "payment_intent": f"pi_{link_id}"}


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
    """Isolated app data dir, fake keyring/SMTP/Stripe, one configured Gmail account."""
    monkeypatch.setenv("PLANSENDER_HOME", str(tmp_path / "home"))
    keyring.set_keyring(MemoryKeyring())
    FakeSMTP.sent = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)

    from desktop import config, db, payments

    fake = FakeStripe()
    for name in ("create_payment_link", "poll", "deactivate", "refund_status"):
        monkeypatch.setattr(payments, name, getattr(fake, name))

    db.init()
    settings = config.load_settings()
    settings["accounts"] = [{"email": "pat@acmecivil.test", "name": "Pat Lee", "preset": "gmail",
                             "host": "smtp.gmail.com", "port": 465, "security": "ssl"}]
    settings["default_from"] = "pat@acmecivil.test"
    settings["signature"] = {"name": "Pat Lee", "firm": "Acme Civil", "license": "12345", "phone": ""}
    settings["preview_dpi"] = 40
    config.save_settings(settings)
    config.set_secret("stripe_secret_key", "sk_test_dummy")
    config.set_secret("smtp:pat@acmecivil.test", "app-password")

    pdf = tmp_path / "Lot_7_Grading.pdf"
    pdf.write_bytes(make_pdf())
    return {"fake": fake, "pdf": pdf, "tmp": tmp_path, "smtp": FakeSMTP}

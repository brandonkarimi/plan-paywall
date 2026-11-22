"""Automatic payments: payment emails in a fake inbox are checked, matched to waiting plans and release them."""
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timedelta, timezone

import pytest

from desktop import background, config, db, payments
from desktop.tests.test_desktop import attachments, body, log_texts, send


def payment_email(method="Venmo", amount="1,500.00", payer="Jo Smith", note="PLN-1", signed=True,
                  sender_domain=None, subject=None, text=None, when=None, auth_domain=None):
    msg = EmailMessage()
    domain = sender_domain or ("venmo.com" if method == "Venmo" else "notify.wellsfargo.com")
    if signed:
        d = auth_domain or domain
        msg["Authentication-Results"] = f"mx.google.com; dkim=pass header.i=@{d} header.s=s1; spf=pass"
    msg["From"] = f"Alerts <alerts@{domain}>"
    msg["To"] = "pat@gmail.test"
    msg["Date"] = format_datetime(when or datetime.now(timezone.utc) + timedelta(minutes=1))
    msg["Message-ID"] = make_msgid(domain="example.test")
    if method == "Venmo":
        msg["Subject"] = subject or f"{payer} paid you ${amount}"
        msg.set_content(text or f"{payer} paid you ${amount}\nNote: {note}\nMoney credited to your Venmo account.")
    else:
        msg["Subject"] = subject or "You received money with Zelle(R)"
        msg.set_content(text or f"{payer} sent you ${amount} with Zelle(R).\nMemo: {note}\nIt has been deposited.")
    return msg


class FakeIMAP:
    messages = []        # the main inbox (pat@gmail.test)
    others = {}          # other inboxes: {address: [messages]}
    password_ok = True
    bad_logins = set()   # addresses whose password is wrong
    searched = []        # (inbox, domain) searches made

    def __init__(self, host, port, ssl_context=None):
        self.host = host

    def login(self, user, password):
        import imaplib
        if not FakeIMAP.password_ok or user in FakeIMAP.bad_logins:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.user = user

    def _box(self):
        return FakeIMAP.others.get(self.user, FakeIMAP.messages)

    def select(self, box, readonly=False):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            domain = args[-1]
            FakeIMAP.searched.append((self.user, domain))
            ids = [str(i + 1).encode() for i, m in enumerate(self._box()) if domain in m["From"]]
            return "OK", [b" ".join(ids)]
        raw = self._box()[int(args[0]) - 1].as_bytes()
        return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]

    def logout(self):
        pass


@pytest.fixture
def inbox(env, monkeypatch):
    FakeIMAP.messages, FakeIMAP.others, FakeIMAP.bad_logins, FakeIMAP.searched = [], {}, set(), []
    FakeIMAP.password_ok = True
    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeIMAP)
    settings = config.load_settings()
    settings["auto_payments"] = {"enabled": True, "zelle": {"email": "pat@gmail.test"}, "venmo": {"email": ""}}
    config.save_settings(settings)
    config.set_secret("imap:pat@gmail.test", "app-password")
    return FakeIMAP


def released(tx_id):
    tx = db.get_tx(tx_id)
    return tx["status"] == "paid" and bool(tx["delivered_at"])


def test_preview_email_asks_for_the_code(env):
    tx_id = send(env)
    assert f"Please include PLN-{tx_id} in the Zelle memo or Venmo note." in body(env["smtp"].sent[-1])
    send(env, methods=["venmo"], to_email="b@example.com")
    assert "in the Venmo note." in body(env["smtp"].sent[-1])


def test_code_and_full_amount_release_the_plans(env, inbox):
    tx_id = send(env)
    inbox.messages.append(payment_email("Venmo", note=f"plans pln {tx_id}", payer="Somebody Else"))
    result = payments.check_inbox()
    assert result["released"] == ["Lot 7 Grading"] and released(tx_id)
    final = env["smtp"].sent[-1]
    assert attachments(final)[0][0] == "Lot 7 Grading.pdf"
    assert db.get_tx(tx_id)["paid_method"] == "Venmo" and db.get_tx(tx_id)["amount_paid_cents"] == 150000
    assert "Venmo payment of $1,500.00 from Somebody Else found in your email" in log_texts(tx_id)
    # Seen once: checking again does nothing more
    sent = len(env["smtp"].sent)
    assert payments.check_inbox() == {"released": [], "review": 0} and len(env["smtp"].sent) == sent


def test_wells_fargo_zelle_email(env, inbox):
    tx_id = send(env)
    inbox.messages.append(payment_email("Zelle", note=f"PLN-{tx_id}"))
    assert payments.check_inbox()["released"] == ["Lot 7 Grading"]
    assert db.get_tx(tx_id)["paid_method"] == "Zelle"


def test_short_payment_with_code_waits_for_the_engineer(env, inbox):
    tx_id = send(env)
    inbox.messages.append(payment_email("Venmo", amount="1.00", note=f"PLN-{tx_id}"))
    assert payments.check_inbox() == {"released": [], "review": 1}
    assert db.get_tx(tx_id)["status"] == "sent"
    (review,) = db.list_payments("review")
    assert "less than the $1,500.00 price" in review["note"]


def test_no_code_but_one_plan_with_that_price(env, inbox):
    tx_id = send(env)  # client "Jo Smith"
    send(env, price="900", to_email="other@example.com", client_name="Al Other")
    inbox.messages.append(payment_email("Zelle", payer="JO SMITH", note=""))
    assert payments.check_inbox()["released"] == ["Lot 7 Grading"] and released(tx_id)


def test_no_code_and_name_differs_waits(env, inbox):
    send(env)
    inbox.messages.append(payment_email("Zelle", payer="Unknown Person", note=""))
    assert payments.check_inbox()["review"] == 1


def test_no_code_and_two_plans_with_same_price_waits(env, inbox):
    send(env)
    send(env, to_email="other@example.com")
    inbox.messages.append(payment_email("Venmo", note="thanks"))
    assert payments.check_inbox()["review"] == 1
    assert "More than one waiting plan" in db.list_payments("review")[0]["note"]


def test_plan_without_price_is_never_released_automatically(env, inbox):
    tx_id = send(env, price="")
    inbox.messages.append(payment_email("Venmo", note=f"PLN-{tx_id}"))
    assert payments.check_inbox()["review"] == 1 and db.get_tx(tx_id)["status"] == "sent"


@pytest.mark.parametrize("fake", [
    {"signed": False},                                              # no authentication result at all
    {"sender_domain": "venmo-payments.example"},                    # look-alike sender
    {"auth_domain": "scammer.example"},                             # signed, but by someone else
])
def test_fake_payment_emails_are_ignored(env, inbox, fake):
    tx_id = send(env)
    inbox.messages.append(payment_email("Venmo", note=f"PLN-{tx_id}", **fake))
    assert payments.check_inbox() == {"released": [], "review": 0}
    assert db.get_tx(tx_id)["status"] == "sent"


def test_forged_lower_authentication_header_is_ignored(env, inbox):
    tx_id = send(env)
    msg = payment_email("Venmo", note=f"PLN-{tx_id}", signed=False)
    msg["Authentication-Results"] = "mx.google.com; dkim=fail header.i=@venmo.com"  # what the real server said
    # A line the sender wrote themselves sits below it and must not count:
    msg["Authentication-Results"] = "mx.google.com; dkim=pass header.i=@venmo.com"
    inbox.messages.append(msg)
    assert payments.check_inbox()["released"] == []


def test_other_emails_from_venmo_are_ignored(env, inbox):
    tx_id = send(env)
    inbox.messages.append(payment_email("Venmo", subject="You paid Jo Smith $1,500.00", note=f"PLN-{tx_id}"))
    inbox.messages.append(payment_email("Venmo", subject="Jo Smith requests $1,500.00", note=f"PLN-{tx_id}"))
    assert payments.check_inbox() == {"released": [], "review": 0}


def test_payment_from_before_the_plan_was_sent_doesnt_count(env, inbox):
    tx_id = send(env)
    inbox.messages.append(payment_email("Venmo", note=f"PLN-{tx_id}",
                                        when=datetime.now(timezone.utc) - timedelta(hours=3)))
    assert payments.check_inbox()["released"] == []


def test_off_or_nothing_waiting_does_nothing(env, inbox):
    assert payments.check_inbox() == {"released": [], "review": 0}  # nothing waiting
    tx_id = send(env)
    settings = config.load_settings()
    settings["auto_payments"]["enabled"] = False
    config.save_settings(settings)
    inbox.messages.append(payment_email("Venmo", note=f"PLN-{tx_id}"))
    assert payments.check_inbox() == {"released": [], "review": 0} and db.get_tx(tx_id)["status"] == "sent"


def test_engineer_confirms_a_payment_from_transactions(env, inbox):
    from desktop.api import Api

    tx_id = send(env)
    inbox.messages.append(payment_email("Zelle", payer="Unknown Person", note=""))
    api = Api()
    assert payments.check_inbox()["review"] == 1
    data = api.transactions()
    (item,) = data["review"]
    assert item["amount"] == "$1,500.00" and item["payer"] == "Unknown Person"
    assert data["waiting"][0]["id"] == tx_id and data["waiting"][0]["label"].startswith(f"PLN-{tx_id}")
    r = api.confirm_payment(item["id"], tx_id)
    assert r["ok"] and released(tx_id) and api.transactions()["review"] == []
    assert not api.confirm_payment(item["id"], tx_id)["ok"]  # already handled


def test_settings_check_the_inbox_login(env, inbox):
    from desktop.api import Api

    api = Api()
    inbox.password_ok = False
    r = api.save_settings({"auto_payments": {"enabled": True, "zelle": {"email": "pat@gmail.test", "password": "wrong"}}})
    assert not r["ok"] and "app password" in r["error"]
    assert config.get_secret("imap:pat@gmail.test") == "app-password"  # the wrong one wasn't saved
    inbox.password_ok = True
    r = api.save_settings({"auto_payments": {"enabled": True,
                                             "zelle": {"email": "Pat@Gmail.test", "password": "abcd efgh"}}})
    assert r["ok"] and config.get_secret("imap:pat@gmail.test") == "abcdefgh"
    auto = r["state"]["settings"]["auto_payments"]
    assert auto["enabled"] and auto["zelle"]["email"] == "pat@gmail.test" and auto["venmo"]["email"] == ""
    assert auto["zelle"]["has_password"] and auto["venmo"]["has_password"]  # Venmo uses the same inbox
    assert "password" not in auto["zelle"]
    assert api.save_settings({"auto_payments": {"enabled": False}})["ok"]
    assert not config.load_settings()["auto_payments"]["enabled"]
    r = api.save_settings({"auto_payments": {"enabled": True}})
    assert not r["ok"] and "Enter the email" in r["error"]


def test_separate_venmo_inbox(env, inbox):
    from desktop.api import Api

    api = Api()
    inbox.bad_logins = {"venmo@yahoo.test"}
    r = api.save_settings({"auto_payments": {"enabled": True, "zelle": {"email": "pat@gmail.test"},
                                             "venmo": {"email": "venmo@yahoo.test", "password": "nope"}}})
    assert not r["ok"] and "venmo@yahoo.test" in r["error"] and not config.get_secret("imap:venmo@yahoo.test")
    inbox.bad_logins = set()
    r = api.save_settings({"auto_payments": {"enabled": True, "zelle": {"email": "pat@gmail.test"},
                                             "venmo": {"email": "venmo@yahoo.test", "password": "yes"}}})
    assert r["ok"] and config.get_secret("imap:venmo@yahoo.test") == "yes"

    t1 = send(env)
    t2 = send(env, to_email="b@example.com", price="900")
    inbox.messages.append(payment_email("Zelle", note=f"PLN-{t1}"))
    inbox.others["venmo@yahoo.test"] = [payment_email("Venmo", amount="900.00", note=f"PLN-{t2}")]
    inbox.searched.clear()
    assert sorted(payments.check_inbox()["released"]) == ["Lot 7 Grading", "Lot 7 Grading"]
    assert released(t1) and released(t2)
    assert db.get_tx(t1)["paid_method"] == "Zelle" and db.get_tx(t2)["paid_method"] == "Venmo"
    # Each inbox is only searched for the alerts that go there.
    assert set(inbox.searched) == {("pat@gmail.test", "wellsfargo.com"), ("venmo@yahoo.test", "venmo.com")}


def test_one_inbox_failing_doesnt_stop_the_other(env, inbox):
    from desktop.api import Api

    settings = config.load_settings()
    settings["auto_payments"]["venmo"] = {"email": "venmo@yahoo.test"}
    config.save_settings(settings)
    config.set_secret("imap:venmo@yahoo.test", "pw")
    inbox.bad_logins = {"venmo@yahoo.test"}
    tx_id = send(env)
    inbox.messages.append(payment_email("Zelle", note=f"PLN-{tx_id}"))
    result = payments.check_inbox()
    assert result["released"] == ["Lot 7 Grading"] and "venmo@yahoo.test" in result["errors"][0]
    send(env, to_email="b@example.com")  # something still waiting, so the inboxes are read again
    assert background.tick()
    errors = Api().transactions()["payment_errors"]  # shown at the top of Transactions
    assert len(errors) == 1 and "venmo@yahoo.test" in errors[0]


def test_settings_from_the_test_version_still_work(env, inbox):
    settings = config.load_settings()
    settings["auto_payments"] = {"enabled": True, "email": "pat@gmail.test", "host": "imap.gmail.com", "port": 993}
    config.save_settings(settings)
    tx_id = send(env)
    inbox.messages.append(payment_email("Venmo", note=f"PLN-{tx_id}"))
    assert payments.check_inbox()["released"] == ["Lot 7 Grading"]


# ------------------------------------------------------------------ background checker
def test_turning_automatic_payments_on_and_off_starts_and_stops_the_checker(env, inbox):
    from desktop.api import Api

    api = Api()
    assert api.save_settings({"auto_payments": {"enabled": True, "zelle": {"email": "pat@gmail.test"}}})["ok"]
    assert env["startup"]["command"][-1] == "--background" and env["startup"]["launched"] == 1
    assert api.save_settings({"auto_payments": {"enabled": False, "zelle": {"email": "pat@gmail.test"}}})["ok"]
    assert env["startup"]["command"] is None  # no longer starts with Windows
    assert background.tick() is False         # a running checker quits
    # Other settings changes don't touch the checker
    env["startup"]["command"] = "unchanged"
    api.save_settings({"signature": {"name": "Pat"}})
    assert env["startup"]["command"] == "unchanged"


def test_checker_only_reads_the_inbox_while_plans_are_waiting(env, inbox):
    inbox.searched.clear()
    assert background.tick() is True and inbox.searched == []  # nothing waiting: inbox not read
    tx_id = send(env)
    inbox.messages.append(payment_email("Venmo", note=f"PLN-{tx_id}"))
    assert background.tick() is True and inbox.searched and released(tx_id)
    inbox.searched.clear()
    assert background.tick() is True and inbox.searched == []  # paid: nothing waiting again
    assert background.read_status()["errors"] == []


def test_checker_keeps_going_after_an_unexpected_error(env, inbox, monkeypatch):
    send(env)
    monkeypatch.setattr(payments, "check_inbox", lambda *a, **k: 1 / 0)
    assert background.tick() is True
    assert "Couldn't check for payments" in background.read_status()["errors"][0]


def test_window_hears_about_payments_found_by_the_checker(env, inbox):
    last = db.last_payment_id()
    tx_id = send(env)
    inbox.messages.append(payment_email("Venmo", note=f"PLN-{tx_id}"))
    background.tick()
    (new,) = db.payments_after(last)
    assert new["status"] == "released" and new["tx_id"] == tx_id


def test_background_command():
    cmd = background.command()
    assert cmd[-1] == "--background" and cmd[-2].endswith("run_desktop.py")

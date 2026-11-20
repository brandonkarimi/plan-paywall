import hashlib
import hmac
import io
import json
import shutil
import tempfile
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from plans.models import DownloadEvent, FirmProfile, PlanRevision, Project
from plans.services import delivery
from plans.services.pdfinfo import inspect_pdf
from plans.services.preview import build_preview

WEBHOOK_SECRET = "whsec_test_secret"


def make_plan_pdf(sheets=("C-001 Cover Sheet", "C-101 Grading Plan", "C-201 Utility Plan"), size=(11 * inch, 17 * inch)):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    for i, name in enumerate(sheets):
        c.setFont("Helvetica", 24)
        c.drawString(72, size[1] - 100, f"{name}: PROPERTY LINE BEARING N 45 E")
        c.line(72, 72, size[0] - 72, size[1] - 150)
        c.bookmarkPage(f"p{i}")
        c.addOutlineEntry(name, f"p{i}", level=0)
        c.showPage()
    c.save()
    return buf.getvalue()


def stripe_signature(payload: bytes, secret=WEBHOOK_SECRET):
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


class PreviewTests(TestCase):
    def test_preview_has_no_text_and_same_pages(self):
        src = make_plan_pdf()
        out = build_preview(src, "PRELIMINARY – NOT FOR CONSTRUCTION", dpi=60)
        reader = PdfReader(io.BytesIO(out))
        self.assertEqual(len(reader.pages), 3)
        for page in reader.pages:
            self.assertEqual(page.extract_text().strip(), "")

    def test_preview_keeps_physical_sheet_size(self):
        src = make_plan_pdf(sheets=("C-1",), size=(36 * inch, 24 * inch))
        out = build_preview(src, "WM", dpi=50, seal_box="0.8,0.7,1,1")
        box = PdfReader(io.BytesIO(out)).pages[0].mediabox
        self.assertAlmostEqual(float(box.width) / 72, 36, delta=0.1)
        self.assertAlmostEqual(float(box.height) / 72, 24, delta=0.1)

    def test_inspect_reads_hash_and_bookmarks(self):
        src = make_plan_pdf()
        info = inspect_pdf(io.BytesIO(src))
        self.assertEqual(info["sha256"], hashlib.sha256(src).hexdigest())
        self.assertEqual(info["sheet_index"], ["C-001 Cover Sheet", "C-101 Grading Plan", "C-201 Utility Plan"])

    def test_inspect_rejects_non_pdf(self):
        with self.assertRaises(ValueError):
            inspect_pdf(io.BytesIO(b"not a pdf"))


class BaseFlow(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.override = override_settings(
            STORAGES={
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage",
                            "OPTIONS": {"location": self.media}},
                "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
            },
            STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET,
            STRIPE_SECRET_KEY="sk_test_dummy",
            PREVIEW_DPI=40,
            ENGINEER_NOTIFY_EMAIL="engineer@example.com",
        )
        self.override.enable()
        FirmProfile.objects.create(firm_name="Acme Civil", engineer_name="Pat Lee", license_number="12345",
                                   license_state="TX", email="pat@acme.test",
                                   license_lookup_url="https://vo.licensing.hpc.texas.gov/")
        self.pdf = make_plan_pdf()
        self.project = Project.objects.create(title="Smith Lot Grading", project_number="24-017",
                                              client_name="Jo Smith", client_email="jo@example.com", price="1500.00")
        self.rev = PlanRevision.objects.create(
            project=self.project, final_file=SimpleUploadedFile("final.pdf", self.pdf, "application/pdf"))
        delivery.analyze_revision(self.rev)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def send(self):
        delivery.send_project(self.project)
        self.project.refresh_from_db()
        self.rev.refresh_from_db()

    def url(self, name, *args):
        return reverse(f"plans:{name}", args=[self.project.access_token, *args])

    def post_event(self, etype, obj, event_id="evt_1"):
        payload = json.dumps({"id": event_id, "type": etype, "data": {"object": obj}}).encode()
        return self.client.post(reverse("plans:stripe_webhook"), payload, content_type="application/json",
                                HTTP_STRIPE_SIGNATURE=stripe_signature(payload))

    def paid_session(self, **over):
        obj = {"id": "cs_test_1", "payment_status": "paid", "amount_total": 150000, "currency": "usd",
               "payment_intent": "pi_test_1", "client_reference_id": str(self.project.pk),
               "metadata": {"project_id": str(self.project.pk)}}
        obj.update(over)
        return obj


class LockingTests(BaseFlow):
    def test_send_locks_and_builds_preview(self):
        self.send()
        self.assertTrue(self.rev.is_locked)
        self.assertTrue(self.rev.preview_file)
        self.assertEqual(self.project.status, Project.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.rev.sha256, mail.outbox[0].body)
        self.assertIn(self.project.portal_url(), mail.outbox[0].body)

    def test_locked_revision_cannot_change_or_be_deleted(self):
        self.send()
        self.rev.final_file = SimpleUploadedFile("other.pdf", make_plan_pdf(sheets=("X",)))
        with self.assertRaises(ValidationError):
            self.rev.save()
        self.rev.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.rev.delete()

    def test_new_revision_after_send_notifies_client(self):
        self.send()
        rev2 = PlanRevision.objects.create(project=self.project, notes="Added drainage swale",
                                           final_file=SimpleUploadedFile("final2.pdf", make_plan_pdf()))
        delivery.issue_new_revision(rev2)
        self.assertEqual(self.project.current_revision.number, 2)
        self.assertIn("Rev 2", mail.outbox[-1].subject)


class PortalTests(BaseFlow):
    def test_draft_project_is_hidden(self):
        self.assertEqual(self.client.get(self.url("portal")).status_code, 404)

    def test_portal_shows_manifest_and_marks_viewed(self):
        self.send()
        resp = self.client.get(self.url("portal"))
        self.assertContains(resp, self.rev.sha256)
        self.assertContains(resp, "C-101 Grading Plan")
        self.assertContains(resp, "Verify license")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.VIEWED)

    def test_download_blocked_until_paid_but_preview_available(self):
        self.send()
        self.assertEqual(self.client.get(self.url("download")).status_code, 403)
        preview = self.client.get(self.url("preview"))
        self.assertEqual(preview.status_code, 200)
        self.assertNotEqual(b"".join(preview.streaming_content), self.pdf)

    def test_bad_token_404(self):
        self.send()
        self.assertEqual(self.client.get(reverse("plans:portal", args=["nope"])).status_code, 404)


class WebhookTests(BaseFlow):
    def test_rejects_bad_signature(self):
        self.send()
        resp = self.client.post(reverse("plans:stripe_webhook"), b"{}", content_type="application/json",
                                HTTP_STRIPE_SIGNATURE="t=1,v1=bad")
        self.assertEqual(resp.status_code, 400)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_paid)

    def test_paid_webhook_unlocks_exact_file(self):
        self.send()
        mail.outbox.clear()
        resp = self.post_event("checkout.session.completed", self.paid_session())
        self.assertEqual(resp.content, b"paid")
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_paid)
        self.assertIsNotNone(self.project.delivered_at)
        subjects = [m.subject for m in mail.outbox]
        self.assertTrue(any("PAID" in s for s in subjects))
        self.assertTrue(any("Download your plans" in s for s in subjects))

        dl = self.client.get(self.url("download"))
        self.assertEqual(dl.status_code, 200)
        body = b"".join(dl.streaming_content)
        self.assertEqual(hashlib.sha256(body).hexdigest(), self.rev.sha256)
        self.assertEqual(DownloadEvent.objects.count(), 1)

    def test_duplicate_event_processed_once(self):
        self.send()
        self.post_event("checkout.session.completed", self.paid_session())
        n = len(mail.outbox)
        resp = self.post_event("checkout.session.completed", self.paid_session())
        self.assertEqual(resp.content, b"duplicate")
        self.assertEqual(len(mail.outbox), n)

    def test_underpayment_does_not_unlock(self):
        self.send()
        resp = self.post_event("checkout.session.completed", self.paid_session(amount_total=100))
        self.assertEqual(resp.content, b"amount mismatch")
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_paid)

    def test_ach_waits_for_funds(self):
        self.send()
        self.post_event("checkout.session.completed", self.paid_session(payment_status="unpaid"), "evt_a")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.PAYMENT_PENDING)
        self.assertEqual(self.client.get(self.url("download")).status_code, 403)
        self.post_event("checkout.session.async_payment_succeeded", self.paid_session(), "evt_b")
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_paid)

    def test_success_redirect_alone_does_not_unlock(self):
        self.send()
        resp = self.client.get(self.url("portal") + "?paid=1")
        self.assertContains(resp, "Confirming your payment")
        self.assertEqual(self.client.get(self.url("download")).status_code, 403)

    def test_dashboard_refund_relocks(self):
        self.send()
        self.post_event("checkout.session.completed", self.paid_session())
        self.post_event("charge.refunded", {"payment_intent": "pi_test_1", "amount": 150000,
                                            "amount_refunded": 150000}, "evt_r")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.REFUNDED)
        self.assertEqual(self.client.get(self.url("download")).status_code, 403)


class GuaranteeTests(BaseFlow):
    def test_undeliverable_paid_project_is_refunded_after_window(self):
        self.send()
        self.post_event("checkout.session.completed", self.paid_session())
        # Simulate a storage failure: the paid file disappears and the project was never delivered.
        self.rev.final_file.storage.delete(self.rev.final_file.name)
        Project.objects.filter(pk=self.project.pk).update(
            delivered_at=None, paid_at=timezone.now() - timedelta(hours=25))
        with mock.patch("stripe.Refund.create") as refund:
            result = delivery.check_deliveries()
        refund.assert_called_once()
        self.assertEqual(refund.call_args.kwargs["payment_intent"], "pi_test_1")
        self.assertEqual(result["refunded"], 1)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.REFUNDED)
        self.assertTrue(any("Refund issued" in m.subject for m in mail.outbox))

    def test_recent_failure_retries_without_refund(self):
        self.send()
        self.post_event("checkout.session.completed", self.paid_session())
        self.rev.final_file.storage.delete(self.rev.final_file.name)
        Project.objects.filter(pk=self.project.pk).update(delivered_at=None)
        with mock.patch("stripe.Refund.create") as refund:
            result = delivery.check_deliveries()
        refund.assert_not_called()
        self.assertEqual(result["still_failing"], 1)


class AdminFlowTests(BaseFlow):
    def test_engineer_uploads_and_sends_via_admin(self):
        get_user_model().objects.create_superuser("eng", "eng@example.com", "a-long-test-password")
        self.client.login(username="eng", password="a-long-test-password")
        add = reverse("admin:plans_project_add")
        data = {
            "title": "Jones Driveway", "project_number": "24-020", "client_name": "Al Jones",
            "client_email": "al@example.com", "price": "800.00", "currency": "usd", "allow_ach": "on", "scope": "",
            # draft inline (prefix "revisions")
            "revisions-TOTAL_FORMS": "1", "revisions-INITIAL_FORMS": "0",
            "revisions-MIN_NUM_FORMS": "0", "revisions-MAX_NUM_FORMS": "1000",
            "revisions-0-final_file": SimpleUploadedFile("jones.pdf", make_plan_pdf(), "application/pdf"),
            "revisions-0-notes": "Initial issue", "revisions-0-sheet_index": "[]",
            # locked inline (prefix "revisions-2")
            "revisions-2-TOTAL_FORMS": "0", "revisions-2-INITIAL_FORMS": "0",
            "revisions-2-MIN_NUM_FORMS": "0", "revisions-2-MAX_NUM_FORMS": "1000",
        }
        resp = self.client.post(add, data)
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context.get("errors"))
        project = Project.objects.get(title="Jones Driveway")
        rev = project.revisions.get()
        self.assertEqual(rev.page_count, 3)
        self.assertFalse(rev.is_locked)

        changelist = reverse("admin:plans_project_changelist")
        self.client.post(changelist, {"action": "send_to_client", "_selected_action": [project.pk]})
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.SENT)
        self.assertTrue(project.current_revision.is_locked)
        # Edit page renders with locked inline
        self.assertEqual(self.client.get(reverse("admin:plans_project_change", args=[project.pk])).status_code, 200)


class DashboardTests(BaseFlow):
    def send_via_dashboard(self, url=None, **over):
        data = {"from_email": "pat@acmecivil.test", "to_email": "client@example.com", "client_name": "",
                "title": "", "price": "250",
                "pdf": SimpleUploadedFile("Lot_7_Grading.pdf", make_plan_pdf(), "application/pdf")}
        data.update(over)
        return self.client.post(url or reverse("dashboard"), data)

    @override_settings(DASHBOARD_KEY="")
    def test_no_login_needed_locally(self):
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    @override_settings(DASHBOARD_KEY="k" * 24)
    def test_secret_address_when_key_set(self):
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 404)
        self.assertEqual(self.client.get(reverse("export_csv")).status_code, 404)
        self.assertEqual(self.client.get(reverse("dashboard_key", args=["wrong" * 5])).status_code, 404)
        url = reverse("dashboard_key", args=["k" * 24])
        self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.send_via_dashboard(url)
        self.assertEqual(resp["Location"], url)

    @override_settings(DASHBOARD_KEY="")
    def test_send_from_chosen_address_with_paywall(self):
        resp = self.send_via_dashboard()
        self.assertEqual(resp.status_code, 302)
        project = Project.objects.get(client_email="client@example.com")
        self.assertEqual(project.title, "Lot 7 Grading")
        self.assertEqual(project.status, Project.Status.SENT)
        self.assertTrue(project.current_revision.is_locked)
        msg = mail.outbox[-1]
        self.assertEqual(msg.to, ["client@example.com"])
        self.assertIn("pat@acmecivil.test", msg.from_email)
        self.assertEqual(msg.reply_to, ["pat@acmecivil.test"])
        self.assertIn("Hi there,", msg.body)
        self.assertEqual(self.client.get(reverse("plans:download", args=[project.access_token])).status_code, 403)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "Lot 7 Grading")
        self.assertContains(page, "Not paid yet")

    @override_settings(DASHBOARD_KEY="")
    def test_activity_log_records_full_lifecycle(self):
        self.send_via_dashboard()
        project = Project.objects.get(client_email="client@example.com")
        self.client.get(project.portal_path())
        self.project = project
        self.post_event("checkout.session.completed", self.paid_session(amount_total=25000))
        self.client.get(self.url("download"))
        log = [a.text for a in project.activity.order_by("timestamp", "id")]
        self.assertTrue(log[0].startswith("Sent to client@example.com from pat@acmecivil.test"))
        self.assertIn("Client opened the link", log)
        self.assertIn("Paid $250.00 by Stripe", log)
        self.assertIn("Download link emailed to client", log)
        self.assertIn("Client downloaded the plans (Rev 1)", log)

        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "Client downloaded the plans")
        self.assertContains(page, "$250.00")  # collected total

        csv_resp = self.client.get(reverse("export_csv"))
        self.assertEqual(csv_resp["Content-Type"], "text/csv")
        body = csv_resp.content.decode()
        self.assertIn("Lot 7 Grading", body)
        self.assertIn("Paid $250.00 by Stripe", body)

    @override_settings(DASHBOARD_KEY="")
    def test_rejects_non_pdf(self):
        resp = self.send_via_dashboard(to_email="c@d.test",
                                       pdf=SimpleUploadedFile("notes.pdf", b"hello", "application/pdf"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn")  # "That file isn't a PDF."
        self.assertFalse(Project.objects.filter(client_email="c@d.test").exists())

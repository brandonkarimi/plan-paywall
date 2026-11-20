import secrets
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.conf import settings


def _token():
    return secrets.token_urlsafe(32)


def _random_path(kind, filename):
    # Random path segment so stored file names are never guessable.
    return f"{kind}/{uuid.uuid4().hex}/{filename}"


def upload_final(instance, filename):
    return _random_path("final", filename)


def upload_preview_source(instance, filename):
    return _random_path("preview_source", filename)


def upload_preview(instance, filename):
    return _random_path("preview", filename)


def upload_contract(instance, filename):
    return _random_path("contract", filename)


class FirmProfile(models.Model):
    """The engineer's verifiable identity, shown on every client portal page (single row)."""

    firm_name = models.CharField(max_length=200)
    engineer_name = models.CharField(max_length=200, help_text="Name as it appears on the PE license")
    license_number = models.CharField("PE license #", max_length=50)
    license_state = models.CharField(max_length=50)
    license_lookup_url = models.URLField(
        "License lookup URL", blank=True, help_text="State board's public license verification page"
    )
    email = models.EmailField(help_text="Support / reply-to email shown to clients")
    phone = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)
    website = models.URLField(blank=True)

    class Meta:
        verbose_name = "Firm profile"
        verbose_name_plural = "Firm profile"

    def __str__(self):
        return self.firm_name

    @classmethod
    def get(cls):
        return cls.objects.first()


class Project(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent to client"
        VIEWED = "viewed", "Viewed by client"
        PAYMENT_PENDING = "pending", "Payment pending (bank transfer)"
        PAID = "paid", "Paid"
        REFUNDED = "refunded", "Refunded"

    title = models.CharField(max_length=200)
    project_number = models.CharField("Project #", max_length=50, blank=True)
    client_name = models.CharField(max_length=200, blank=True)
    client_email = models.EmailField()
    from_email = models.EmailField(
        "Sent from", blank=True,
        help_text="Outgoing address used for this client's emails (blank = DEFAULT_FROM_EMAIL)",
    )
    price = models.DecimalField(max_digits=10, decimal_places=2, help_text="Amount the client pays to unlock the plans")
    currency = models.CharField(max_length=3, default="usd")
    allow_ach = models.BooleanField(
        "Allow bank transfer (ACH)", default=True,
        help_text="Much lower fees on large invoices. Funds take a few days to clear; files unlock once they do.",
    )
    scope = models.TextField(
        "Scope / deliverables", blank=True,
        help_text="What the client is paying for, shown on their portal page",
    )
    contract = models.FileField(upload_to=upload_contract, blank=True, help_text="Signed proposal/contract (PDF), visible to the client")

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    access_token = models.CharField(max_length=64, unique=True, default=_token, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(
        null=True, blank=True, help_text="When the paid files were verified available and the client was emailed"
    )
    refunded_at = models.DateTimeField(null=True, blank=True)
    delivery_attempts = models.PositiveIntegerField(default=0)
    last_delivery_error = models.TextField(blank=True)

    stripe_session_id = models.CharField(max_length=255, blank=True)
    stripe_payment_intent = models.CharField(max_length=255, blank=True)
    amount_paid_cents = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        num = f"{self.project_number} " if self.project_number else ""
        return f"{num}{self.title} ({self.client_display})"

    @property
    def client_display(self):
        return self.client_name or self.client_email

    @property
    def price_cents(self):
        return int((self.price * 100).quantize(Decimal("1")))

    @property
    def amount_paid(self):
        return Decimal(self.amount_paid_cents) / 100 if self.amount_paid_cents is not None else None

    @property
    def is_paid(self):
        return self.status == self.Status.PAID

    @property
    def is_sent(self):
        return self.status != self.Status.DRAFT

    def published_revisions(self):
        return self.revisions.filter(locked_at__isnull=False).order_by("-number")

    @property
    def current_revision(self):
        return self.published_revisions().first()

    def log(self, text):
        """Add an entry to this transaction's activity log."""
        return Activity.objects.create(project=self, text=text[:500])

    def portal_path(self):
        return reverse("plans:portal", args=[self.access_token])

    def portal_url(self):
        return settings.SITE_URL + self.portal_path()


class PlanRevision(models.Model):
    """One issued version of the plan set. Once locked (sent to the client) it can never change."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField(editable=False)
    final_file = models.FileField(
        upload_to=upload_final, help_text="The final (sealed/signed) PDF the client receives after paying"
    )
    preview_source = models.FileField(
        upload_to=upload_preview_source, blank=True,
        help_text="Optional: an unsealed version to build the preview from. Must have the same sheets. "
        "If blank, the preview is built from the final file.",
    )
    notes = models.CharField("Revision notes", max_length=300, blank=True)

    # Filled in automatically
    preview_file = models.FileField(upload_to=upload_preview, blank=True, editable=False)
    sha256 = models.CharField("SHA-256", max_length=64, blank=True, editable=False)
    size_bytes = models.BigIntegerField(null=True, editable=False)
    page_count = models.PositiveIntegerField(null=True, editable=False)
    sheet_index = models.JSONField(
        default=list, blank=True,
        help_text='Sheet list shown to the client, e.g. ["C-001 Cover", "C-101 Grading Plan"]. '
        "Auto-filled from PDF bookmarks; edit before sending if needed.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    locked_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-number"]
        unique_together = [("project", "number")]

    def __str__(self):
        return f"Rev {self.number}"

    @property
    def is_locked(self):
        return self.locked_at is not None

    def save(self, *args, **kwargs):
        if self.pk:
            original = PlanRevision.objects.get(pk=self.pk)
            if original.is_locked:
                changed = (
                    original.final_file.name != self.final_file.name
                    or original.sha256 != self.sha256
                    or original.preview_source.name != self.preview_source.name
                    or original.sheet_index != self.sheet_index
                )
                if changed:
                    raise ValidationError(
                        "This revision was already sent to the client and is locked. Upload a new revision instead."
                    )
        elif not self.number:
            last = PlanRevision.objects.filter(project=self.project).aggregate(m=models.Max("number"))["m"]
            self.number = (last or 0) + 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_locked:
            raise ValidationError("Locked revisions can't be deleted; the client was promised this exact file.")
        return super().delete(*args, **kwargs)

    def download_filename(self):
        base = self.project.project_number or "plans"
        title = "".join(c if c.isalnum() or c in " -_" else "" for c in self.project.title).strip().replace(" ", "_")
        return f"{base}_{title}_Rev{self.number}.pdf"


class Activity(models.Model):
    """Saved, append-only history of everything that happened to a transaction."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="activity")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    text = models.CharField(max_length=500)

    class Meta:
        ordering = ["-timestamp", "-id"]
        verbose_name_plural = "activity"

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.text}"


class DownloadEvent(models.Model):
    """Audit trail of every paid download (useful in a dispute)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="downloads")
    revision = models.ForeignKey(PlanRevision, on_delete=models.SET_NULL, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)

    class Meta:
        ordering = ["-timestamp"]


class ProcessedStripeEvent(models.Model):
    """Stripe can deliver the same webhook more than once; we process each event id only once."""

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    received_at = models.DateTimeField(auto_now_add=True)

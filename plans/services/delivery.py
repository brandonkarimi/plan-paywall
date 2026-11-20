"""Project lifecycle: analyze → publish (preview + lock) → send → paid → deliver, plus the delivery guarantee."""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from plans.models import PlanRevision, Project
from plans.services import notify
from plans.services.pdfinfo import inspect_pdf
from plans.services.preview import build_preview
from plans.services.storage import file_exists

log = logging.getLogger(__name__)


def analyze_revision(rev: PlanRevision):
    """Fingerprint the final file and fill in page count / sheet index. Call before locking."""
    with rev.final_file.open("rb") as f:
        info = inspect_pdf(f)
    if rev.preview_source:
        with rev.preview_source.open("rb") as f:
            src_info = inspect_pdf(f)
        if src_info["page_count"] != info["page_count"]:
            raise ValidationError(
                f"The preview source has {src_info['page_count']} pages but the final has "
                f"{info['page_count']}. They must be the same sheet set."
            )
    rev.sha256 = info["sha256"]
    rev.size_bytes = info["size_bytes"]
    rev.page_count = info["page_count"]
    if not rev.sheet_index:
        rev.sheet_index = info["sheet_index"]
    rev.save()
    return rev


def watermark_text(project):
    parts = ["PRELIMINARY – NOT FOR CONSTRUCTION"]
    if project.project_number:
        parts.append(project.project_number)
    parts.append(project.client_display)
    return "  ·  ".join(parts)


def publish_revision(rev: PlanRevision):
    """Generate the watermarked preview and lock the revision so it can never be swapped."""
    if rev.is_locked:
        return rev
    if not rev.sha256:
        analyze_revision(rev)
    source_field = rev.preview_source or rev.final_file
    with source_field.open("rb") as f:
        preview_bytes = build_preview(
            f,
            watermark_text(rev.project),
            dpi=settings.PREVIEW_DPI,
            jpeg_quality=settings.PREVIEW_JPEG_QUALITY,
            seal_box=settings.PREVIEW_SEAL_BOX or None,
        )
    rev.preview_file.save(f"preview_rev{rev.number}.pdf", ContentFile(preview_bytes), save=False)
    rev.locked_at = timezone.now()
    rev.save()
    return rev


def latest_unpublished(project):
    return project.revisions.filter(locked_at__isnull=True).order_by("-number").first()


def send_project(project: Project):
    """Publish the newest revision and email the client their portal link."""
    rev = latest_unpublished(project) or project.current_revision
    if rev is None:
        raise ValidationError("Upload the plan PDF (a revision) before sending.")
    publish_revision(rev)
    if project.status == Project.Status.DRAFT:
        project.status = Project.Status.SENT
    project.sent_at = project.sent_at or timezone.now()
    project.save()
    notify.to_client("invite", project)
    sender = project.from_email or "default address"
    project.log(f"Sent to {project.client_email} from {sender} (${Decimal(project.price):,.2f}, {rev.page_count} pages)")
    return project


def issue_new_revision(rev: PlanRevision):
    """A revision added after the project went out: publish it and tell the client."""
    publish_revision(rev)
    project = rev.project
    project.log(f"Rev {rev.number} issued" + (f": {rev.notes}" if rev.notes else ""))
    if project.is_paid:
        deliver(project, template="revision_paid")
    else:
        notify.to_client("revision", project, revision=rev)


def mark_paid(project: Project, *, session_id, payment_intent, amount_cents):
    """Record a confirmed payment and immediately try to deliver. Safe to call repeatedly."""
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project.pk)
        if project.is_paid or project.status == Project.Status.REFUNDED:
            return project
        project.status = Project.Status.PAID
        project.paid_at = timezone.now()
        project.stripe_session_id = session_id or project.stripe_session_id
        project.stripe_payment_intent = payment_intent or project.stripe_payment_intent
        project.amount_paid_cents = amount_cents
        project.save()
    how = "by Stripe" if payment_intent else "outside Stripe (marked manually)"
    project.log(f"Paid ${amount_cents / 100:,.2f} {how}")
    notify.to_engineer("engineer_paid", project)
    deliver(project)
    return project


def deliver(project: Project, template="files_ready"):
    """Verify the paid file is actually retrievable, then email the client. Records failures for the guarantee check."""
    rev = project.current_revision
    try:
        if rev is None or not file_exists(rev.final_file):
            raise RuntimeError("Final plan file is missing from storage.")
        notify.to_client(template, project, revision=rev)
    except Exception as exc:
        log.exception("Delivery failed for project %s", project.pk)
        Project.objects.filter(pk=project.pk).update(
            delivery_attempts=project.delivery_attempts + 1, last_delivery_error=str(exc)[:2000]
        )
        project.log(f"Could not email the unlocked files: {exc}")
        return False
    Project.objects.filter(pk=project.pk).update(
        delivered_at=project.delivered_at or timezone.now(),
        delivery_attempts=project.delivery_attempts + 1,
        last_delivery_error="",
    )
    project.log("Download link emailed to client")
    project.refresh_from_db()
    return True


def check_deliveries(now=None):
    """Retry undelivered paid projects; refund any that are past the guarantee window. Run every ~15 min."""
    from plans.services import payments

    now = now or timezone.now()
    deadline = now - timedelta(hours=settings.DELIVERY_GUARANTEE_HOURS)
    results = {"delivered": 0, "refunded": 0, "still_failing": 0}
    for project in Project.objects.filter(status=Project.Status.PAID, delivered_at__isnull=True):
        if deliver(project):
            results["delivered"] += 1
            continue
        project.refresh_from_db()
        if project.paid_at and project.paid_at <= deadline:
            try:
                payments.refund(project, reason="Delivery guarantee: files could not be delivered in time")
                results["refunded"] += 1
            except Exception:
                log.exception("Automatic refund failed for project %s", project.pk)
                notify.to_engineer("engineer_alert", project, message="AUTOMATIC REFUND FAILED. Refund this client manually in Stripe now.")
                results["still_failing"] += 1
        elif project.delivery_attempts % 4 == 1:  # alert on first failure, then roughly hourly
            notify.to_engineer(
                "engineer_alert", project,
                message=f"Paid but files could not be delivered: {project.last_delivery_error}. "
                f"The client is refunded automatically if this isn't fixed within {settings.DELIVERY_GUARANTEE_HOURS}h of payment.",
            )
            results["still_failing"] += 1
        else:
            results["still_failing"] += 1
    return results

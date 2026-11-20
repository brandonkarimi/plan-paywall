"""All outgoing email. Templates live in plans/templates/plans/emails/<name>.txt (first line = subject)."""
import logging

from django.conf import settings
from email.utils import formataddr, parseaddr

from email.utils import formataddr, parseaddr

from django.core.mail import EmailMessage
from django.template.loader import render_to_string

from plans.models import FirmProfile

log = logging.getLogger(__name__)


def _context(project, extra=None):
    firm = FirmProfile.get()
    ctx = {
        "project": project,
        "firm": firm,
        "portal_url": project.portal_url(),
        "revision": project.current_revision,
        "guarantee_hours": settings.DELIVERY_GUARANTEE_HOURS,
    }
    ctx.update(extra or {})
    return ctx


def sender_for(project):
    """The From header: the project's chosen address (with a display name), else the default sender."""
    if not project.from_email:
        return settings.DEFAULT_FROM_EMAIL
    firm = FirmProfile.get()
    name = firm.firm_name if firm else parseaddr(settings.DEFAULT_FROM_EMAIL)[0]
    return formataddr((name, project.from_email))


def _send(template, to, ctx):
    text = render_to_string(f"plans/emails/{template}.txt", ctx).strip()
    subject, _, body = text.partition("\n")
    project, firm = ctx["project"], ctx.get("firm")
    reply_to = project.from_email or (firm.email if firm else "")
    msg = EmailMessage(
        subject=subject.removeprefix("Subject:").strip(),
        body=body.strip() + "\n",
        from_email=sender_for(project),
        to=[to],
        reply_to=[reply_to] if reply_to else None,
    )
    msg.send(fail_silently=False)


def engineer_email(project=None):
    if settings.ENGINEER_NOTIFY_EMAIL:
        return settings.ENGINEER_NOTIFY_EMAIL
    if project is not None and project.from_email:
        return project.from_email
    firm = FirmProfile.get()
    return firm.email if firm else None


def to_client(template, project, **extra):
    _send(template, project.client_email, _context(project, extra))


def to_engineer(template, project, **extra):
    addr = engineer_email(project)
    if not addr:
        log.warning("No engineer email configured; skipped %s for project %s", template, project.pk)
        return
    try:
        _send(template, addr, _context(project, extra))
    except Exception:  # an alert failing must never break the client's flow
        log.exception("Failed to email engineer (%s) for project %s", template, project.pk)

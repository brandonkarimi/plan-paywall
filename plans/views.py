import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from plans.models import DownloadEvent, FirmProfile, Project
from plans.services import notify, payments
from plans.services.storage import serve_file

log = logging.getLogger(__name__)


def client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    return fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")


def rate_limit(key, limit, window):
    """Simple fixed-window limiter. Returns a decorator that 429s once ``limit`` hits occur within ``window`` seconds."""

    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            cache_key = f"rl:{key}:{client_ip(request)}"
            hits = cache.get_or_set(cache_key, 0, window)
            if hits >= limit:
                return HttpResponse("Too many requests. Please wait a few minutes and try again.", status=429)
            try:
                cache.incr(cache_key)
            except ValueError:
                cache.set(cache_key, 1, window)
            return view(request, *args, **kwargs)

        return wrapped

    return deco


def _project(token):
    project = get_object_or_404(Project, access_token=token)
    if not project.is_sent:
        raise Http404
    return project


@require_GET
@rate_limit("portal", 120, 600)
def portal(request, token):
    project = _project(token)
    if project.status == Project.Status.SENT:
        project.status = Project.Status.VIEWED
        project.viewed_at = timezone.now()
        project.save(update_fields=["status", "viewed_at"])
        project.log("Client opened the link")
    rev = project.current_revision
    awaiting_webhook = request.GET.get("paid") == "1" and project.status in (
        Project.Status.SENT, Project.Status.VIEWED,
    )
    return render(request, "plans/portal.html", {
        "project": project,
        "revision": rev,
        "past_revisions": project.published_revisions()[1:] if project.is_paid else [],
        "firm": FirmProfile.get(),
        "awaiting_webhook": awaiting_webhook,
        "guarantee_hours": settings.DELIVERY_GUARANTEE_HOURS,
        "stripe_ready": bool(settings.STRIPE_SECRET_KEY),
    })


@require_GET
@rate_limit("preview", 60, 600)
def preview(request, token):
    project = _project(token)
    rev = project.current_revision
    if not rev or not rev.preview_file:
        raise Http404
    return serve_file(rev.preview_file, f"PREVIEW_{rev.download_filename()}", inline=True)


@require_GET
def contract(request, token):
    project = _project(token)
    if not project.contract:
        raise Http404
    return serve_file(project.contract, "Agreement.pdf", inline=True)


@require_POST
@rate_limit("checkout", 20, 600)
def checkout(request, token):
    project = _project(token)
    if project.status in (Project.Status.PAID, Project.Status.PAYMENT_PENDING, Project.Status.REFUNDED):
        return redirect(project.portal_path())
    try:
        url = payments.create_checkout_session(project)
        project.log("Client went to the payment page")
    except Exception:
        log.exception("Could not start checkout for project %s", project.pk)
        messages.error(request, "Payment couldn't be started right now. Please try again shortly or contact us.")
        return redirect(project.portal_path())
    return redirect(url)


@require_GET
@rate_limit("download", 30, 600)
def download(request, token, number=None):
    project = _project(token)
    if not project.is_paid:
        return HttpResponseForbidden("These plans unlock after payment.")
    revs = project.published_revisions()
    rev = revs.filter(number=number).first() if number else revs.first()
    if rev is None:
        raise Http404
    DownloadEvent.objects.create(
        project=project, revision=rev, ip=client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:400],
    )
    project.log(f"Client downloaded the plans (Rev {rev.number})")
    return serve_file(rev.final_file, rev.download_filename())


@require_POST
@rate_limit("resend", 3, 3600)
def resend(request, token):
    """Re-email the portal link to the client address on file (never to an address typed in)."""
    project = _project(token)
    template = "files_ready" if project.is_paid else "invite"
    try:
        notify.to_client(template, project)
        messages.success(request, f"We've re-sent the link to {project.client_email}.")
    except Exception:
        log.exception("Resend failed for project %s", project.pk)
        messages.error(request, "We couldn't send the email right now. Please try again later.")
    return redirect(project.portal_path())


@csrf_exempt
@require_POST
def stripe_webhook(request):
    try:
        result = payments.handle_webhook(request.body, request.META.get("HTTP_STRIPE_SIGNATURE", ""))
    except payments.WebhookError as exc:
        log.warning("Rejected webhook: %s", exc)
        return HttpResponse(status=400)
    return HttpResponse(result)

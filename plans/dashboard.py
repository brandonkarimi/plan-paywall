"""The engineer's one-page dashboard: send a PDF behind a pay wall, and a saved log of every transaction.

No login. On your own computer it's at "/". Online, set DASHBOARD_KEY and it's only reachable at
/d/<DASHBOARD_KEY>/ (bookmark it); every other address for it returns "not found".
"""
import csv
import hmac
import logging
from email.utils import parseaddr
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.db.models import Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from plans.forms import SendPlansForm
from plans.models import Activity, PlanRevision, Project
from plans.services import delivery

log = logging.getLogger(__name__)


def _check_access(key):
    expected = settings.DASHBOARD_KEY
    if expected:
        if not key or not hmac.compare_digest(key, expected):
            raise Http404
    elif key is not None:
        raise Http404


def _home(key):
    return f"/d/{key}/" if key else "/"


def _default_from():
    last = Project.objects.exclude(from_email="").order_by("-created_at").values_list("from_email", flat=True).first()
    return last or parseaddr(settings.DEFAULT_FROM_EMAIL)[1]


def dashboard(request, key=None):
    _check_access(key)
    if request.method == "POST":
        form = SendPlansForm(request.POST, request.FILES)
        if form.is_valid():
            d = form.cleaned_data
            title = d["title"] or Path(d["pdf"].name).stem.replace("_", " ")
            project = Project.objects.create(
                title=title, client_name=d["client_name"], client_email=d["to_email"],
                from_email=d["from_email"], price=d["price"],
            )
            try:
                PlanRevision.objects.create(project=project, final_file=d["pdf"])
                delivery.send_project(project)
            except Exception as exc:
                log.exception("Send failed")
                project.delete()  # nothing reached the client, so leave no half-sent record
                messages.error(request, f"Not sent: {exc}")
                return render(request, "plans/dashboard.html", _ctx(form, key))
            messages.success(request, f"Sent “{title}” to {d['to_email']}. The PDF unlocks when they pay ${d['price']:,.2f}.")
            return redirect(_home(key))
    else:
        form = SendPlansForm(initial={"from_email": _default_from()})
    return render(request, "plans/dashboard.html", _ctx(form, key))


def _transactions():
    return (Project.objects.exclude(status=Project.Status.DRAFT)
            .prefetch_related(Prefetch("activity", queryset=Activity.objects.order_by("-timestamp", "-id"))))


def _ctx(form, key):
    projects = list(_transactions())
    paid = [p for p in projects if p.is_paid]
    return {
        "form": form,
        "projects": projects,
        "home": _home(key),
        "csv_url": _home(key) + "log.csv",
        "totals": {
            "sent": len(projects),
            "paid": len(paid),
            "collected": sum((p.amount_paid or p.price) for p in paid),
            "outstanding": sum(p.price for p in projects if p.status in (
                Project.Status.SENT, Project.Status.VIEWED, Project.Status.PAYMENT_PENDING)),
        },
    }


def export_csv(request, key=None):
    """Every update for every transaction, one row each, for records or a spreadsheet."""
    _check_access(key)
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="plans-log-{timezone.localdate():%Y-%m-%d}.csv"'
    w = csv.writer(resp)
    w.writerow(["Date", "Time", "Project", "Client email", "Client name", "Sent from", "Price", "Current status", "Update"])
    tz = timezone.get_current_timezone()
    for p in _transactions():
        for a in reversed(list(p.activity.all())):
            t = a.timestamp.astimezone(tz)
            w.writerow([f"{t:%Y-%m-%d}", f"{t:%H:%M}", p.title, p.client_email, p.client_name, p.from_email,
                        f"{p.price:.2f}", p.get_status_display(), a.text])
    return resp

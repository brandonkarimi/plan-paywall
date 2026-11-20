from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html

from plans.models import DownloadEvent, FirmProfile, PlanRevision, Project
from plans.services import delivery, notify
from plans.services.pdfinfo import inspect_pdf
from plans.services.storage import serve_file


@admin.register(FirmProfile)
class FirmProfileAdmin(admin.ModelAdmin):
    list_display = ("firm_name", "engineer_name", "license_number", "license_state", "email")

    def has_add_permission(self, request):
        return not FirmProfile.objects.exists()


class DraftRevisionForm(forms.ModelForm):
    class Meta:
        model = PlanRevision
        fields = ("final_file", "preview_source", "notes", "sheet_index")

    def _check_pdf(self, field):
        f = self.cleaned_data.get(field)
        if f and hasattr(f, "read"):
            try:
                inspect_pdf(f)
            except ValueError as exc:
                raise ValidationError(str(exc))
        return f

    def clean_final_file(self):
        return self._check_pdf("final_file")

    def clean_preview_source(self):
        return self._check_pdf("preview_source")


def _admin_file_link(rev, kind, label):
    url = reverse("admin:plans_revision_file", args=[rev.pk, kind])
    return format_html('<a href="{}" target="_blank">{}</a>', url, label)


class LockedRevisionInline(admin.TabularInline):
    """Revisions already sent to the client: read-only forever."""

    model = PlanRevision
    verbose_name = "Issued revision (locked)"
    verbose_name_plural = "Issued revisions (locked: sent to client, cannot change)"
    extra = 0
    can_delete = False
    fields = ("number", "notes", "page_count", "short_hash", "locked_at", "files")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).filter(locked_at__isnull=False)

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="SHA-256")
    def short_hash(self, rev):
        return format_html('<code title="{}">{}…</code>', rev.sha256, rev.sha256[:16])

    @admin.display(description="Files")
    def files(self, rev):
        links = [_admin_file_link(rev, "final", "Final")]
        if rev.preview_file:
            links.append(_admin_file_link(rev, "preview", "Client preview"))
        return format_html(" · ".join(["{}"] * len(links)), *links)


class DraftRevisionInline(admin.StackedInline):
    """Uploads not yet sent. Saving analyzes the PDF; sending (or saving a sent project) locks them."""

    model = PlanRevision
    form = DraftRevisionForm
    verbose_name = "New revision (not yet sent)"
    verbose_name_plural = "Upload plans: new revision (not yet sent)"
    extra = 0
    fields = ("final_file", "preview_source", "notes", "sheet_index", "info")
    readonly_fields = ("info",)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(locked_at__isnull=True)

    def get_extra(self, request, obj=None, **kwargs):
        # Offer an empty upload slot if there's nothing waiting to be sent.
        if obj is None or not obj.revisions.filter(locked_at__isnull=True).exists():
            return 1
        return 0

    @admin.display(description="Detected")
    def info(self, rev):
        if not rev.pk or not rev.sha256:
            return "Save to analyze the PDF (page count, sheet list, fingerprint)."
        return format_html(
            "{} pages · SHA-256 <code>{}</code> · {}",
            rev.page_count, rev.sha256, _admin_file_link(rev, "final", "open final"),
        )


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("title", "project_number", "client_name", "price", "status_badge", "current_rev",
                    "sent_at", "paid_at", "delivered", "download_count")
    list_filter = ("status",)
    search_fields = ("title", "project_number", "client_name", "client_email")
    readonly_fields = ("status", "portal_link", "created_at", "sent_at", "viewed_at", "paid_at", "delivered_at",
                       "refunded_at", "amount_paid", "stripe_payment_intent", "last_delivery_error", "download_log")
    fieldsets = (
        ("Client & price", {"fields": ("title", "project_number", "client_name", "client_email", "price", "currency",
                                       "allow_ach")}),
        ("What the client sees", {"fields": ("scope", "contract")}),
        ("Status", {"fields": ("status", "portal_link", "created_at", "sent_at", "viewed_at", "paid_at",
                               "delivered_at", "refunded_at", "amount_paid", "stripe_payment_intent",
                               "last_delivery_error", "download_log")}),
    )
    inlines = [DraftRevisionInline, LockedRevisionInline]
    actions = ["send_to_client", "resend_link", "mark_paid_offline"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_downloads=Count("downloads"))

    # ------------------------------------------------------------------ display helpers
    STATUS_COLORS = {"draft": "#888", "sent": "#2563eb", "viewed": "#7c3aed", "pending": "#d97706",
                     "paid": "#16a34a", "refunded": "#dc2626"}

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return format_html('<b style="color:{}">{}</b>', self.STATUS_COLORS.get(obj.status, "#000"),
                           obj.get_status_display())

    @admin.display(description="Rev")
    def current_rev(self, obj):
        rev = obj.current_revision
        return rev.number if rev else "—"

    @admin.display(description="Delivered", boolean=True)
    def delivered(self, obj):
        return obj.delivered_at is not None if obj.is_paid else None

    @admin.display(description="Downloads", ordering="_downloads")
    def download_count(self, obj):
        return getattr(obj, "_downloads", obj.downloads.count())

    @admin.display(description="Client portal link")
    def portal_link(self, obj):
        if not obj.pk:
            return "Created on save"
        if not obj.is_sent:
            return "Available after you send the project"
        url = obj.portal_url()
        return format_html('<a href="{}" target="_blank">{}</a>', url, url)

    @admin.display(description="Amount paid")
    def amount_paid(self, obj):
        return f"{obj.amount_paid_cents / 100:,.2f} {obj.currency.upper()}" if obj.amount_paid_cents else "—"

    @admin.display(description="Download log")
    def download_log(self, obj):
        events = obj.downloads.select_related("revision")[:20] if obj.pk else []
        if not events:
            return "No downloads yet"
        return format_html("<br>".join(["{}"] * len(events)), *[
            f"{e.timestamp:%Y-%m-%d %H:%M} · Rev {e.revision.number if e.revision else '?'} · {e.ip}" for e in events
        ])

    # ------------------------------------------------------------------ saving
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        project = form.instance
        pending = list(project.revisions.filter(locked_at__isnull=True).order_by("number"))
        for rev in pending:
            try:
                if not rev.sha256:
                    delivery.analyze_revision(rev)
                if project.is_sent:
                    delivery.issue_new_revision(rev)
                    self.message_user(request, f"Rev {rev.number} issued and the client was notified.", messages.SUCCESS)
            except ValidationError as exc:
                self.message_user(request, f"Rev {rev.number}: {' '.join(exc.messages)}", messages.ERROR)
            except Exception as exc:
                self.message_user(request, f"Rev {rev.number} could not be processed: {exc}", messages.ERROR)
        if pending and not project.is_sent:
            self.message_user(
                request,
                "Plans uploaded. Check the sheet list, then use the “Send to client” action to issue them.",
                messages.INFO,
            )

    # ------------------------------------------------------------------ actions
    @admin.action(description="Send to client (generate preview, lock file, email link)")
    def send_to_client(self, request, queryset):
        for project in queryset:
            if project.is_sent and not delivery.latest_unpublished(project):
                self.message_user(request, f"{project}: already sent (use “Re-send link”).", messages.WARNING)
                continue
            try:
                delivery.send_project(project)
                self.message_user(request, f"{project}: sent to {project.client_email}.", messages.SUCCESS)
            except ValidationError as exc:
                self.message_user(request, f"{project}: {' '.join(exc.messages)}", messages.ERROR)
            except Exception as exc:
                self.message_user(request, f"{project}: failed: {exc}", messages.ERROR)

    @admin.action(description="Re-send portal link to client")
    def resend_link(self, request, queryset):
        for project in queryset.exclude(status=Project.Status.DRAFT):
            notify.to_client("files_ready" if project.is_paid else "invite", project)
        self.message_user(request, "Links re-sent (draft projects skipped).", messages.SUCCESS)

    @admin.action(description="Mark paid outside Stripe (check / cash) and release files")
    def mark_paid_offline(self, request, queryset):
        for project in queryset:
            if not project.is_sent:
                self.message_user(request, f"{project}: send it first.", messages.ERROR)
                continue
            delivery.mark_paid(project, session_id="", payment_intent="", amount_cents=project.price_cents)
            self.message_user(request, f"{project}: marked paid; files released.", messages.SUCCESS)

    # ------------------------------------------------------------------ staff-only file access
    def get_urls(self):
        return [
            path("revision/<int:pk>/<str:kind>/", self.admin_site.admin_view(self.revision_file),
                 name="plans_revision_file"),
        ] + super().get_urls()

    def revision_file(self, request, pk, kind):
        rev = get_object_or_404(PlanRevision, pk=pk)
        field = {"final": rev.final_file, "preview": rev.preview_file, "source": rev.preview_source}.get(kind)
        if not field:
            raise Http404
        return serve_file(field, rev.download_filename(), inline=True)


@admin.register(DownloadEvent)
class DownloadEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "project", "revision", "ip")
    list_filter = ("timestamp",)
    readonly_fields = ("project", "revision", "timestamp", "ip", "user_agent")

    def has_add_permission(self, request):
        return False

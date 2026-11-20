from django.contrib import admin
from django.urls import include, path

from plans.dashboard import dashboard, export_csv

admin.site.site_header = "Plan Delivery: advanced settings"
admin.site.site_title = "Plan Delivery"
admin.site.index_title = "Advanced settings"

urlpatterns = [
    # Dashboard: "/" locally; "/d/<DASHBOARD_KEY>/" when a key is set (see plans/dashboard.py).
    path("", dashboard, name="dashboard"),
    path("log.csv", export_csv, name="export_csv"),
    path("d/<str:key>/", dashboard, name="dashboard_key"),
    path("d/<str:key>/log.csv", export_csv, name="export_csv_key"),
    path("admin/", admin.site.urls),
    path("", include("plans.urls")),
]

from django.urls import path

from plans import views

app_name = "plans"

urlpatterns = [
    path("p/<str:token>/", views.portal, name="portal"),
    path("p/<str:token>/preview.pdf", views.preview, name="preview"),
    path("p/<str:token>/agreement.pdf", views.contract, name="contract"),
    path("p/<str:token>/checkout/", views.checkout, name="checkout"),
    path("p/<str:token>/download/", views.download, name="download"),
    path("p/<str:token>/download/rev-<int:number>/", views.download, name="download_rev"),
    path("p/<str:token>/resend/", views.resend, name="resend"),
    path("stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),
]

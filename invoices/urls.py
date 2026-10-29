from django.urls import path

from invoices import views
from invoices import surcharges

app_name = "invoices"

urlpatterns = [
    path("surcharges/", surcharges.monitor, name="surcharges"),
    path("<int:pk>/surcharge/", surcharges.manage, name="surcharge"),
    path("", views.invoice_list, name="list"),
    path("new/", views.invoice_create, name="create"),
    path("<int:pk>/", views.invoice_detail, name="detail"),
    path("<int:pk>/edit/", views.invoice_edit, name="edit"),
    path("<int:pk>/document/", views.invoice_document, name="document"),
    path("<int:pk>/approved-copy/", views.invoice_approved_copy, name="approved_copy"),
    path("<int:pk>/approval-record/", views.invoice_approval_record, name="approval_record"),
    path("<int:pk>/submit/", views.invoice_submit, name="submit"),
    path("<int:pk>/cancel/", views.invoice_cancel, name="cancel"),
    path("<int:pk>/comment/", views.invoice_comment, name="comment"),
]

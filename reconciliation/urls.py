from django.urls import path
from reconciliation import views
from reconciliation import bank_views

app_name = "reconciliation"
urlpatterns = [
    path("bank/", bank_views.index, name="bank_index"),
    path("bank/upload/", bank_views.upload, name="bank_upload"),
    path("bank/template.csv", bank_views.template, name="bank_template"),
    path("bank/statements/<int:pk>/", bank_views.statement, name="bank_statement"),
    path("bank/statements/<int:pk>/download/", bank_views.download, name="bank_download"),
    path("bank/rows/<int:pk>/", bank_views.row, name="bank_row"),
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("template.csv", views.template, name="template"),
    path("statements/<int:pk>/", views.statement_detail, name="statement"),
    path("statements/<int:pk>/download/", views.download, name="download"),
    path("rows/<int:pk>/", views.row_detail, name="row"),
]

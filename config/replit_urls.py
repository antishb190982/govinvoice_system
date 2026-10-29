from django.urls import path

from config.urls import urlpatterns as portal_patterns
from core import production_setup

urlpatterns = [
    path("healthz/", production_setup.health, name="production_health"),
    path("accounts/initialize/", production_setup.initialize, name="initialize_production"),
    *portal_patterns,
]

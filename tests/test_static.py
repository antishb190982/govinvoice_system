"""Production static files: collectstatic must succeed (a failure there makes every page a 500)."""

from django.core.management import call_command


def test_production_collectstatic(settings, tmp_path):
    settings.STATIC_ROOT = tmp_path / "static"
    settings.STORAGES = {**settings.STORAGES, "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
    call_command("collectstatic", interactive=False, verbosity=0)
    assert (tmp_path / "static" / "staticfiles.json").exists()

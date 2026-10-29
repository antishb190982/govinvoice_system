from django.db import migrations


def retain_archived_status(apps, schema_editor):
    apps.get_model("providers", "ServiceAccount").objects.filter(is_active=False).update(status="decommissioned")


class Migration(migrations.Migration):
    dependencies = [("providers", "0005_service_account_lifecycle")]
    operations = [migrations.RunPython(retain_archived_status, migrations.RunPython.noop)]

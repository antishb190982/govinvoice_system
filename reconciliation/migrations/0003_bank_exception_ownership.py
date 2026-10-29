from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("reconciliation", "0002_bank_reconciliation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField(model_name="bankrow", name="assigned_to",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="bank_exception_cases", to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="bankrow", name="resolution_due_date",
            field=models.DateField(blank=True, null=True, db_index=True)),
        migrations.AddField(model_name="bankrow", name="case_note", field=models.TextField(blank=True)),
        migrations.AddField(model_name="bankrow", name="assigned_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="bankrow", name="assigned_at", field=models.DateTimeField(blank=True, null=True)),
    ]

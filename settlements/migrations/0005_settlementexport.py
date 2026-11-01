from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import core.files


class Migration(migrations.Migration):
    dependencies = [
        ("settlements", "0004_settlementinstruction_group_reference_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SettlementExport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("format_key", models.CharField(default="invoice-schedule-v1", max_length=48)),
                ("file", models.FileField(upload_to=core.files.private_upload_path("settlement-exports"))),
                ("sha256", models.CharField(max_length=64)),
                ("entry_count", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("instruction", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="exports", to="settlements.settlementinstruction")),
            ],
            options={"constraints": [
                models.UniqueConstraint(fields=("instruction", "format_key"), name="unique_settlement_export_format"),
            ]},
        ),
    ]

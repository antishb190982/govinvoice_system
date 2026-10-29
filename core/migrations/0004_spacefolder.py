from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0003_personal_processing_schedules"),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="SpaceFolder", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("label", models.CharField(max_length=100)),
            ("destination", models.CharField(choices=[
                ("preparation", "Batch preparation"), ("work", "Batches in progress"),
                ("pending", "Batches pending approval"), ("rejected", "Returned / rejected batches"),
                ("completed", "Completed batches"), ("all", "All my batches"),
                ("invoices", "My invoices"), ("services", "Services in my units")],
                default="work", max_length=20)),
            ("notes", models.CharField(blank=True, max_length=250)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="space_folders", to=settings.AUTH_USER_MODEL)),
            ("org_unit", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="org.orgunit")),
            ("provider", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="providers.provider")),
        ], options={"ordering": ["label", "pk"]}),
        migrations.AddConstraint(model_name="spacefolder", constraint=models.UniqueConstraint(
            fields=("owner", "label"), name="space_folder_owner_label_unique")),
    ]

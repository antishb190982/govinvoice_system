from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_productionbootstrap"), ("org", "0001_initial"),
        ("providers", "0001_initial"), migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(name="ProcessingSchedule", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=140)),
            ("frequency", models.CharField(choices=[("once", "Once"), ("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")], default="monthly", max_length=10)),
            ("next_due_date", models.DateField(db_index=True, default=django.utils.timezone.localdate)),
            ("anchor_day", models.PositiveSmallIntegerField(default=1)),
            ("is_active", models.BooleanField(default=True)),
            ("notes", models.TextField(blank=True, max_length=2000)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="processing_schedules", to=settings.AUTH_USER_MODEL)),
            ("org_unit", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="org.orgunit")),
            ("provider", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="providers.provider")),
        ], options={"ordering": ["next_due_date", "pk"]}),
        migrations.CreateModel(name="ProcessingCompletion", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name_snapshot", models.CharField(max_length=140)),
            ("due_date", models.DateField()),
            ("completed_at", models.DateTimeField(auto_now_add=True)),
            ("note", models.TextField(blank=True, max_length=2000)),
            ("completed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ("schedule", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="completions", to="core.processingschedule")),
        ], options={"ordering": ["-completed_at", "-pk"]}),
        migrations.AddConstraint(model_name="processingcompletion", constraint=models.UniqueConstraint(
            fields=("schedule", "due_date"), name="one_processing_completion_per_due")),
    ]

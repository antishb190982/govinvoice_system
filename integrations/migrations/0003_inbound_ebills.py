import core.files
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0002_notification_delivery"),
        ("providers", "0001_initial"),
        ("imports", "0001_initial"),
    ]
    operations = [
        migrations.CreateModel(
            name="EbillMailbox",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("address", models.EmailField(max_length=254, unique=True)),
                ("enabled", models.BooleanField(default=False)),
                ("auto_stage", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("relay_client", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="ebill_mailboxes", to="integrations.apiclient")),
            ],
            options={"ordering": ["name", "pk"]},
        ),
        migrations.CreateModel(
            name="EbillSender",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("address", models.EmailField(max_length=254)),
                ("enabled", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("mailbox", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    related_name="senders", to="integrations.ebillmailbox")),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    related_name="ebill_senders", to="providers.provider")),
            ],
            options={"ordering": ["address"], "constraints": [
                models.UniqueConstraint(fields=("mailbox", "address"), name="ebill_sender_mailbox_unique")]},
        ),
        migrations.CreateModel(
            name="EbillDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("delivery_key", models.CharField(max_length=64)),
                ("sender", models.EmailField(blank=True, max_length=254)),
                ("authenticated_sender", models.EmailField(blank=True, max_length=254)),
                ("subject", models.CharField(blank=True, max_length=200)),
                ("message_id", models.CharField(blank=True, max_length=300)),
                ("received_at", models.DateTimeField()),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                ("raw_message", models.FileField(upload_to=core.files.private_upload_path("ebill-mail"))),
                ("sha256", models.CharField(max_length=64)),
                ("authentication_passed", models.BooleanField(default=False)),
                ("attachments_clean", models.BooleanField(default=False)),
                ("state", models.CharField(choices=[
                    ("received", "Received"), ("rejected", "Rejected"), ("quarantined", "Quarantined"),
                    ("held", "Held for manual staging"), ("staged", "Staged for invoice review"),
                    ("failed", "Staging failed")], default="received", max_length=16)),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("mailbox", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    related_name="deliveries", to="integrations.ebillmailbox")),
                ("relay_client", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to="integrations.apiclient")),
                ("provider", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to="providers.provider")),
                ("import_batches", models.ManyToManyField(blank=True, related_name="ebill_deliveries", to="imports.importbatch")),
            ],
            options={"ordering": ["-recorded_at", "-pk"], "constraints": [
                models.UniqueConstraint(fields=("mailbox", "delivery_key"), name="ebill_delivery_mailbox_unique")]},
        ),
    ]

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("integrations", "0001_initial"), ("core", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="outboundmessage", name="notification",
            field=models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="deliveries", to="core.notification"),
        ),
    ]

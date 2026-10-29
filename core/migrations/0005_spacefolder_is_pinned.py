from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0004_spacefolder")]

    operations = [
        migrations.AddField(
            model_name="spacefolder",
            name="is_pinned",
            field=models.BooleanField(default=False),
        ),
    ]

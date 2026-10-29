import core.files
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("signatures", "0002_initial")]
    operations = [
        migrations.AddField(model_name="signingidentity", name="signature_image",
                            field=models.FileField(blank=True, upload_to=core.files.private_upload_path("signature-appearances"))),
        migrations.AddField(model_name="signingidentity", name="signature_image_sha256",
                            field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="signature", name="appearance_image",
                            field=models.FileField(blank=True, upload_to=core.files.private_upload_path("signature-appearances"))),
        migrations.AddField(model_name="signature", name="appearance_sha256",
                            field=models.CharField(blank=True, max_length=64)),
    ]

from django.db import migrations, models
import judge.models.test_formatter


class Migration(migrations.Migration):

    dependencies = [
        ("judge", "0173_fulltext"),
    ]

    operations = [
        migrations.CreateModel(
            name="TestFormatterModel",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "file",
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to=judge.models.test_formatter.test_formatter_path,
                        verbose_name="testcase file",
                    ),
                ),
            ],
        )
    ]

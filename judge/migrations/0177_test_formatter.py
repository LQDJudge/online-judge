import os
from django.db import migrations, models


def test_formatter_path(test_formatter, filename):
    """Path function for TestFormatterModel - inlined to avoid import issues."""
    tail = filename.split(".")[-1]
    head = filename.split(".")[0]
    new_filename = f"{head}.{tail}"
    return os.path.join("test_formatter", new_filename)


class Migration(migrations.Migration):

    dependencies = [
        ("judge", "0176_comment_revision_count"),
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
                        upload_to=test_formatter_path,
                        verbose_name="testcase file",
                    ),
                ),
            ],
        )
    ]

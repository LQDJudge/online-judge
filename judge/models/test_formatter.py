import os
from django.db import models
from dmoj import settings
from django.utils.translation import gettext_lazy as _

__all__ = [
    "TestFormatterModel",
]


def test_formatter_path(test_formatter, filename):
    tail = filename.split(".")[-1]
    head = filename.split(".")[0]
    if str(tail).lower() != "zip":
        raise Exception("400: Only ZIP files are supported")
    new_filename = f"{head}.{tail}"
    return os.path.join(settings.DMOJ_TEST_FORMATTER_ROOT, new_filename)


class TestFormatterModel(models.Model):
    file = models.FileField(
        verbose_name=_("testcase file"),
        null=True,
        blank=True,
        upload_to=test_formatter_path,
    )

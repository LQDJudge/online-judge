import os

from django.db import models
from django.utils.translation import gettext_lazy as _

from judge.models.problem_data import problem_data_storage

__all__ = ["ProblemAttachment"]


def problem_attachment_path(instance, filename):
    return os.path.join(
        "problem_attachments", instance.problem.code, os.path.basename(filename)
    )


class ProblemAttachment(models.Model):
    problem = models.ForeignKey(
        "Problem",
        verbose_name=_("problem"),
        related_name="attachments",
        on_delete=models.CASCADE,
    )
    file = models.FileField(
        verbose_name=_("file"),
        upload_to=problem_attachment_path,
        storage=problem_data_storage,
    )
    description = models.CharField(
        verbose_name=_("description"),
        max_length=255,
        blank=True,
    )
    order = models.PositiveIntegerField(
        verbose_name=_("display order"),
        default=0,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("problem attachment")
        verbose_name_plural = _("problem attachments")
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.problem.code}: {os.path.basename(self.file.name)}"

    @property
    def filename(self):
        return os.path.basename(self.file.name)

from django.db import models
from django.utils.translation import gettext_lazy as _


class EmailChangeRequest(models.Model):
    profile = models.ForeignKey(
        "Profile",
        verbose_name=_("user profile"),
        on_delete=models.CASCADE,
        related_name="email_change_requests",
    )
    new_email = models.EmailField(verbose_name=_("new email address"))

    class Meta:
        verbose_name = _("email change request")
        verbose_name_plural = _("email change requests")

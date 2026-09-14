from contextvars import ContextVar

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Only the transactional enrollment/conversion services may change school identity
# or student affiliation. This is not an authorization check: the services check
# the actor against fresh database state before entering this context.
school_write = ContextVar("school_write", default=False)


class OfficialSchool(models.Model):
    organization = models.OneToOneField(
        "Organization",
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="official_school",
        verbose_name=_("Organization"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Enrollment enabled"))
    verified_at = models.DateTimeField(default=timezone.now, editable=False)
    verified_by = models.ForeignKey(
        "Profile",
        on_delete=models.PROTECT,
        related_name="verified_schools",
        editable=False,
    )

    class Meta:
        verbose_name = _("Official school")
        verbose_name_plural = _("Official schools")

    def __str__(self):
        return str(self.organization)

    def save(self, *args, **kwargs):
        if not school_write.get():
            raise ValidationError(_("Use the official school management workflow."))
        return super().save(*args, **kwargs)

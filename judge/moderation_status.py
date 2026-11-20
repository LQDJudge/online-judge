from django.db import models
from django.utils.translation import gettext_lazy as _


class MuseStatus(models.TextChoices):
    NONE = "none", _("No Muse check needed")
    PENDING_OMNI = "pending_omni", _("Pending OpenAI moderation")
    SUPERSEDED = "superseded", _("Superseded by an edit")
    PENDING_VISIBLE = "pending_visible", _("Pending Muse, visible")
    PENDING_HIDDEN = "pending_hidden", _("Pending Muse, hidden")
    COMPLETED = "completed", _("Muse completed")

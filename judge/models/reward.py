from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.utils import timezone
from django.templatetags.static import static
from django.utils.translation import gettext_lazy as _

from judge.utils.files import generate_secure_filename


def frame_artwork_path(instance, filename):
    return "avatar_frames/" + generate_secure_filename(filename, "frame")


class AvatarFrame(models.Model):
    class Style(models.TextChoices):
        IRON = "iron", _("Iron")
        BRONZE = "bronze", _("Bronze")
        SILVER = "silver", _("Silver")
        GOLD = "gold", _("Gold")
        CSES = "cses", "CSES"
        SETTER = "setter", _("Problem setter")
        UNITED = "united", _("Manchester United")

    class UnlockType(models.TextChoices):
        COURSE = "course", _("Course completion")
        MANUAL = "manual", _("Awarded by an administrator")

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    style = models.CharField(max_length=16, choices=Style.choices)
    label = models.CharField(max_length=16, default="COURSE", blank=True)
    artwork = models.ImageField(
        upload_to=frame_artwork_path,
        blank=True,
        validators=[FileExtensionValidator(["png", "webp"])],
        help_text=_(
            "Transparent PNG or WebP, including the circular avatar opening. Leave empty to use the built-in frame."
        ),
    )
    avatar_center_x = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_(
            "Center of the avatar opening, as a percentage of the image width."
        ),
    )
    avatar_center_y = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_(
            "Center of the avatar opening, as a percentage of the image height."
        ),
    )
    avatar_diameter = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=52,
        validators=[MinValueValidator(10), MaxValueValidator(100)],
        help_text=_(
            "Diameter of the avatar opening, as a percentage of the image width."
        ),
    )
    unlock_type = models.CharField(
        max_length=16, choices=UnlockType.choices, default=UnlockType.COURSE
    )
    course = models.ForeignKey(
        "judge.Course",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="avatar_frames",
    )
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("order", "id")
        verbose_name = _("Avatar frame")
        verbose_name_plural = _("Avatar frames")

    def __str__(self):
        return self.name

    @property
    def artwork_url(self):
        if self.artwork:
            return self.artwork.url
        if self.style == self.Style.UNITED:
            return static("avatar-frames/manchester-united.png")
        return ""

    @property
    def artwork_scale(self):
        return round(100 / float(self.avatar_diameter or 52), 6)

    def clean(self):
        super().clean()
        if self.unlock_type == self.UnlockType.MANUAL and self.course_id:
            raise ValidationError({"course": _("Manual awards cannot have a course.")})


class ProfileFrame(models.Model):
    profile = models.ForeignKey(
        "judge.Profile", on_delete=models.CASCADE, related_name="avatar_frames"
    )
    frame = models.ForeignKey(
        AvatarFrame, on_delete=models.CASCADE, related_name="owners"
    )
    awarded_at = models.DateTimeField(default=timezone.now, editable=False)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="granted_avatar_frames",
    )
    reason = models.TextField(blank=True)
    is_equipped = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("profile", "frame"), name="unique_profile_avatar_frame"
            )
        ]
        indexes = [models.Index(fields=("profile", "is_equipped"))]
        verbose_name = _("Awarded avatar frame")
        verbose_name_plural = _("Awarded avatar frames")

    def __str__(self):
        return str(self.frame)

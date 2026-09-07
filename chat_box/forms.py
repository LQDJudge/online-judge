from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class RoomAvatarForm(forms.Form):
    avatar = forms.ImageField(required=True)

    allowed_extensions = {
        "GIF": {".gif"},
        "JPEG": {".jpeg", ".jpg"},
        "PNG": {".png"},
        "WEBP": {".webp"},
    }

    def clean_avatar(self):
        avatar = self.cleaned_data["avatar"]
        image_format = (avatar.image.format or "").upper()
        extension = Path(avatar.name).suffix.lower()
        if extension not in self.allowed_extensions.get(image_format, set()):
            raise ValidationError(_("Use a JPG, PNG, GIF, or WebP image."))
        if avatar.size > 5 * 1024 * 1024:
            raise ValidationError(
                _("File size exceeds the maximum allowed limit of 5MB.")
            )
        if avatar.image.width > 4096 or avatar.image.height > 4096:
            raise ValidationError(
                _("Image dimensions must not exceed 4096 by 4096 pixels.")
            )
        return avatar

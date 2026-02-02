"""
Direct upload widgets for S3/R2 and local storage.

These widgets enable direct client-to-storage uploads, bypassing Django
for faster upload speeds. They preserve the existing UI styling of
ImageWidget and PDFWidget while adding progress bars and status indicators.

IMPORTANT: These widgets only work with Edit forms where the object already
exists. For Create forms, use the standard ImageWidget/FileInput instead.
"""

import secrets

from django import forms
from django.core.cache import cache
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

# Cache key prefix and expiry for upload tokens
UPLOAD_TOKEN_PREFIX = "direct_upload:"
UPLOAD_TOKEN_EXPIRY = 3600  # 1 hour


def generate_upload_token(
    profile_id, model_name, object_id, field_name, max_size, upload_to, prefix
):
    """
    Generate a secure token for direct upload and store in cache.

    The token proves the user was authorized to upload to this specific
    model/object/field at form render time. Stores all upload parameters
    so they can't be tampered with.
    """
    token = secrets.token_urlsafe(32)
    cache_key = f"{UPLOAD_TOKEN_PREFIX}{token}"
    cache.set(
        cache_key,
        {
            "profile_id": profile_id,
            "model_name": model_name,
            "object_id": object_id,
            "field_name": field_name,
            "max_size": max_size,
            "upload_to": upload_to,
            "prefix": prefix,
        },
        UPLOAD_TOKEN_EXPIRY,
    )
    return token


def get_upload_token_data(token):
    """
    Get the data stored in an upload token.

    Returns:
        dict: Token data if valid, None if invalid/expired
    """
    cache_key = f"{UPLOAD_TOKEN_PREFIX}{token}"
    return cache.get(cache_key)


def verify_upload_token(token, profile_id, model_name, object_id, field_name):
    """
    Verify an upload token is valid.

    Returns True if token exists and all parameters match.
    """
    cache_key = f"{UPLOAD_TOKEN_PREFIX}{token}"
    data = cache.get(cache_key)
    if not data:
        return False
    return (
        data["profile_id"] == profile_id
        and data["model_name"] == model_name
        and data["object_id"] == object_id
        and data["field_name"] == field_name
    )


class DirectUploadWidget(forms.ClearableFileInput):
    """
    Base class for direct upload widgets.
    Works automatically with S3, R2, or local storage.

    The widget automatically saves the uploaded file to the model immediately
    after upload completes, preventing orphaned files.

    Security: A token is generated at render time (when permission is validated)
    and verified at save time.
    """

    template_name = "widgets/direct_upload.html"

    # Widget type - determines preview rendering in JS
    widget_type = "file"

    # Default UI text - override in subclasses
    choose_text = _("Choose a file")
    change_text = _("Change")
    remove_text = _("Remove")

    # Whether to show full path or just filename in preview
    show_full_path = True

    class Media:
        js = ("direct_upload.js",)

    def __init__(
        self,
        upload_to,
        prefix,
        attrs=None,
        max_size=5 * 1024 * 1024,  # 5MB default
        accept="*/*",
    ):
        self.upload_to = upload_to
        self.prefix = prefix
        self.max_size = max_size
        self.accept = accept
        # Set by mixin at form init time
        self.model_name = ""
        self.object_id = ""
        self.profile_id = None
        super().__init__(attrs)

    def set_object_info(self, profile_id, model_name, object_id):
        """
        Set the object info for immediate save after upload.

        Args:
            profile_id: ID of the profile (for token generation)
            model_name: Model in format 'app_label.ModelName' (e.g., 'judge.Profile')
            object_id: Primary key of the object
        """
        self.profile_id = profile_id
        self.model_name = model_name
        self.object_id = object_id

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)

        # Generate security token if we have all required info
        token = ""
        if self.profile_id and self.model_name and self.object_id:
            token = generate_upload_token(
                self.profile_id,
                self.model_name,
                self.object_id,
                name,
                self.max_size,
                self.upload_to,
                self.prefix,
            )

        context["widget"].update(
            {
                "upload_config_url": reverse("direct_upload_config"),
                "upload_save_url": reverse("direct_upload_save"),
                "upload_delete_url": reverse("direct_upload_delete"),
                "upload_to": self.upload_to,
                "prefix": self.prefix,
                "max_size": self.max_size,
                "accept": self.accept,
                "upload_token": token,
                # Widget type for JS preview rendering
                "widget_type": self.widget_type,
                "show_full_path": self.show_full_path,
                # UI text for JS
                "choose_text": str(self.choose_text),
                "change_text": str(self.change_text),
                "remove_text": str(self.remove_text),
            }
        )
        return context


class DirectUploadImageWidget(DirectUploadWidget):
    """
    Drop-in replacement for ImageWidget with direct upload support.
    Preserves existing image-file-widget UI with added progress bar.

    Uploads are saved to the model immediately after completion.
    Only use in Edit forms where the object already exists.
    """

    template_name = "widgets/direct_upload.html"
    widget_type = "image"
    choose_text = _("Choose an image")
    show_full_path = False  # Show just filename for images

    def __init__(
        self,
        upload_to,
        prefix,
        attrs=None,
        max_size=5 * 1024 * 1024,
        width=80,
        height=80,
    ):
        self.width = width
        self.height = height
        super().__init__(
            upload_to, prefix, attrs=attrs, max_size=max_size, accept="image/*"
        )

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["width"] = self.width
        context["widget"]["height"] = self.height
        return context


class DirectUploadPDFWidget(DirectUploadWidget):
    """
    Drop-in replacement for PDFWidget with direct upload support.
    Preserves existing pdf-file-widget UI with added progress bar.

    Only use in Edit forms where the object already exists.
    """

    template_name = "widgets/direct_upload.html"
    widget_type = "pdf"
    choose_text = _("Choose a PDF")
    show_full_path = True  # Show full path for PDFs

    def __init__(self, upload_to, prefix, attrs=None, max_size=10 * 1024 * 1024):
        super().__init__(
            upload_to, prefix, attrs=attrs, max_size=max_size, accept=".pdf"
        )


class DirectUploadFormMixin:
    """
    Mixin for ModelForms that automatically configures DirectUpload widgets.

    Automatically sets up security tokens for all DirectUploadWidget fields.
    Requires 'profile' to be passed to form __init__ via kwargs.

    Usage:
        class MyForm(DirectUploadFormMixin, ModelForm):
            class Meta:
                model = MyModel
                widgets = {
                    "image": DirectUploadImageWidget(
                        upload_to="images",
                        prefix="mymodel",
                    ),
                }

        # In view:
        form = MyForm(instance=obj, profile=request.profile)
    """

    def __init__(self, *args, **kwargs):
        self.profile = kwargs.pop("profile", None)
        assert self.profile is not None, (
            "DirectUploadFormMixin requires 'profile' to be passed to form.__init__(). "
            "Example: MyForm(instance=obj, profile=request.profile)"
        )
        super().__init__(*args, **kwargs)
        self._setup_direct_upload_widgets()

    def _setup_direct_upload_widgets(self):
        """Configure all DirectUploadWidget fields with security tokens."""
        if not self.instance or not self.instance.pk or not self.profile:
            return

        # Get model name in format 'app_label.ModelName'
        model = self._meta.model
        model_name = f"{model._meta.app_label}.{model.__name__}"

        # Set object info for all DirectUploadWidget fields
        for field_name, field in self.fields.items():
            if isinstance(field.widget, DirectUploadWidget):
                field.widget.set_object_info(
                    self.profile.id, model_name, self.instance.pk
                )

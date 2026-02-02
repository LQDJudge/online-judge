import os

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files import File
from django.core.files.storage import default_storage
from django.http import (
    HttpResponseRedirect,
    HttpResponseForbidden,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from judge.forms import ThemeBackgroundForm
from judge.models import DYNAMIC_EFFECT_CHOICES, Profile
from judge.utils.theme import (
    get_sample_backgrounds,
    invalidate_sample_backgrounds_cache,
    SAMPLE_BACKGROUNDS_PREFIX,
)
from judge.utils.storage_helpers import (
    storage_file_exists,
    storage_delete_file,
    validate_path_prefix,
)


class ThemeSettingsView(LoginRequiredMixin, TemplateView):
    template_name = "user/theme-settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = self.request.profile
        background_form = ThemeBackgroundForm(
            instance=profile, profile=self.request.profile
        )
        context.update(
            {
                "title": _("Theme Settings"),
                "profile": profile,
                "background_form": background_form,
                "sample_backgrounds": get_sample_backgrounds(),
                "dynamic_effects": DYNAMIC_EFFECT_CHOICES,
                "use_darkmode": self.request.session.get("darkmode", False),
                "is_superuser": self.request.user.is_superuser,
                "current_background_url": (
                    profile.background_image.url if profile.background_image else None
                ),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        profile = request.profile
        action = request.POST.get("action")

        if action == "select_sample":
            # Select a sample background
            sample_filename = request.POST.get("sample_filename")
            if sample_filename:
                sample_path = f"{SAMPLE_BACKGROUNDS_PREFIX}/{sample_filename}"
                if storage_file_exists(default_storage, sample_path):
                    # Delete old background files BEFORE saving the new one
                    if profile.background_image:
                        profile.background_image.delete(save=False)

                    # Copy sample to user's background using storage API
                    with default_storage.open(sample_path, "rb") as f:
                        profile.background_image.save(
                            sample_filename, File(f), save=False
                        )

                    # Save the profile using update() to bypass save() method
                    Profile.objects.filter(pk=profile.pk).update(
                        background_image=profile.background_image.name
                    )

        elif action == "update_effect":
            # Update dynamic effect
            dynamic_effect = request.POST.get("dynamic_effect", "none")
            valid_effects = [choice[0] for choice in DYNAMIC_EFFECT_CHOICES]
            if dynamic_effect in valid_effects:
                profile.dynamic_effect = dynamic_effect
                profile.save(update_fields=["dynamic_effect"])

        return HttpResponseRedirect(reverse("theme_settings"))


@login_required
def toggle_darkmode_ajax(request):
    """AJAX endpoint to set dark mode."""
    if request.method != "POST":
        return HttpResponseBadRequest()

    # Check if a specific mode is requested
    mode = request.POST.get("mode")
    if mode == "dark":
        request.session["darkmode"] = True
    elif mode == "light":
        request.session["darkmode"] = False
    else:
        # Fallback: toggle if no mode specified
        current = request.session.get("darkmode", False)
        request.session["darkmode"] = not current

    return JsonResponse({"darkmode": request.session.get("darkmode", False)})


class SampleBackgroundUploadView(LoginRequiredMixin, View):
    """View for superusers to upload sample backgrounds."""

    def post(self, request):
        if not request.user.is_superuser:
            return HttpResponseForbidden()

        uploaded_file = request.FILES.get("image")
        if not uploaded_file:
            return JsonResponse(
                {"success": False, "error": "No file provided"}, status=400
            )

        # Validate file type
        allowed_extensions = (".jpg", ".jpeg", ".png", ".webp", ".gif")
        if not uploaded_file.name.lower().endswith(allowed_extensions):
            return JsonResponse(
                {"success": False, "error": "Invalid file type"}, status=400
            )

        # Validate file size (10MB max)
        if uploaded_file.size > 10 * 1024 * 1024:
            return JsonResponse(
                {"success": False, "error": "File too large (max 10MB)"}, status=400
            )

        # Sanitize filename
        filename = uploaded_file.name.replace(" ", "_")
        filepath = f"{SAMPLE_BACKGROUNDS_PREFIX}/{filename}"

        # Handle duplicate filenames
        base, ext = os.path.splitext(filename)
        counter = 1
        while storage_file_exists(default_storage, filepath):
            filename = f"{base}_{counter}{ext}"
            filepath = f"{SAMPLE_BACKGROUNDS_PREFIX}/{filename}"
            counter += 1

        # Save file using default_storage
        default_storage.save(filepath, uploaded_file)

        # Invalidate cache
        invalidate_sample_backgrounds_cache()

        return JsonResponse(
            {
                "success": True,
                "filename": filename,
                "url": default_storage.url(filepath),
            }
        )


class SampleBackgroundDeleteView(LoginRequiredMixin, View):
    """View for superusers to delete sample backgrounds."""

    def post(self, request):
        if not request.user.is_superuser:
            return HttpResponseForbidden()

        filename = request.POST.get("filename")
        if not filename:
            return JsonResponse(
                {"success": False, "error": "No filename provided"}, status=400
            )

        filepath = f"{SAMPLE_BACKGROUNDS_PREFIX}/{filename}"

        # Security: ensure the file is within the sample backgrounds directory
        if not validate_path_prefix(filepath, SAMPLE_BACKGROUNDS_PREFIX):
            return HttpResponseForbidden()

        storage_delete_file(default_storage, filepath)
        invalidate_sample_backgrounds_cache()
        return JsonResponse({"success": True})

import os
import shutil

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.storage import default_storage
from django.http import (
    HttpResponseRedirect,
    HttpResponseForbidden,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from judge.models import DYNAMIC_EFFECT_CHOICES
from judge.utils.theme import (
    get_sample_backgrounds,
    invalidate_sample_backgrounds_cache,
    SAMPLE_BACKGROUNDS_DIR,
)


class ThemeSettingsView(LoginRequiredMixin, TemplateView):
    template_name = "user/theme-settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = self.request.profile
        context.update(
            {
                "title": _("Theme Settings"),
                "profile": profile,
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

        if action == "clear_background":
            # Clear background
            if profile.background_image:
                profile.background_image.delete(save=False)
                profile.background_image = None
            profile.save(update_fields=["background_image"])

        elif action == "select_sample":
            # Select a sample background
            sample_filename = request.POST.get("sample_filename")
            if sample_filename:
                sample_path = os.path.join(SAMPLE_BACKGROUNDS_DIR, sample_filename)
                if os.path.exists(sample_path):
                    # Copy sample to user's background
                    from judge.utils.files import (
                        generate_image_filename,
                        delete_old_image_files,
                    )

                    # Delete old background files
                    delete_old_image_files(
                        settings.DMOJ_PROFILE_IMAGE_ROOT, f"bg_user_{profile.id}"
                    )

                    # Generate new filename
                    new_filename = generate_image_filename(
                        f"bg_user_{profile.id}", sample_filename
                    )
                    new_path = os.path.join(
                        settings.MEDIA_ROOT,
                        settings.DMOJ_PROFILE_IMAGE_ROOT,
                        new_filename,
                    )

                    # Ensure directory exists
                    os.makedirs(os.path.dirname(new_path), exist_ok=True)

                    # Copy file
                    shutil.copy2(sample_path, new_path)

                    # Update profile
                    profile.background_image = os.path.join(
                        settings.DMOJ_PROFILE_IMAGE_ROOT, new_filename
                    )
                    profile.save(update_fields=["background_image"])

        elif action == "update_effect":
            # Update dynamic effect
            dynamic_effect = request.POST.get("dynamic_effect", "none")
            valid_effects = [choice[0] for choice in DYNAMIC_EFFECT_CHOICES]
            if dynamic_effect in valid_effects:
                profile.dynamic_effect = dynamic_effect
                profile.save(update_fields=["dynamic_effect"])

        elif action == "upload_custom":
            # Upload custom background
            uploaded_file = request.FILES.get("custom_background")
            if uploaded_file:
                # Validate file type
                allowed_extensions = (".jpg", ".jpeg", ".png", ".webp", ".gif")
                if not uploaded_file.name.lower().endswith(allowed_extensions):
                    # Invalid file type - just redirect
                    return HttpResponseRedirect(reverse("theme_settings"))

                # Validate file size (5MB max)
                if uploaded_file.size > 5 * 1024 * 1024:
                    return HttpResponseRedirect(reverse("theme_settings"))

                from judge.utils.files import (
                    generate_image_filename,
                    delete_old_image_files,
                )

                # Delete old background files
                delete_old_image_files(
                    settings.DMOJ_PROFILE_IMAGE_ROOT, f"bg_user_{profile.id}"
                )

                # Generate new filename
                new_filename = generate_image_filename(
                    f"bg_user_{profile.id}", uploaded_file.name
                )
                new_path = os.path.join(
                    settings.MEDIA_ROOT,
                    settings.DMOJ_PROFILE_IMAGE_ROOT,
                    new_filename,
                )

                # Ensure directory exists
                os.makedirs(os.path.dirname(new_path), exist_ok=True)

                # Save file
                with open(new_path, "wb+") as destination:
                    for chunk in uploaded_file.chunks():
                        destination.write(chunk)

                # Update profile
                profile.background_image = os.path.join(
                    settings.DMOJ_PROFILE_IMAGE_ROOT, new_filename
                )
                profile.save(update_fields=["background_image"])

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

        # Ensure directory exists
        os.makedirs(SAMPLE_BACKGROUNDS_DIR, exist_ok=True)

        # Save file
        filename = uploaded_file.name.replace(" ", "_")
        filepath = os.path.join(SAMPLE_BACKGROUNDS_DIR, filename)

        # Handle duplicate filenames
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(filepath):
            filename = f"{base}_{counter}{ext}"
            filepath = os.path.join(SAMPLE_BACKGROUNDS_DIR, filename)
            counter += 1

        with open(filepath, "wb+") as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        # Invalidate cache
        invalidate_sample_backgrounds_cache()

        return JsonResponse(
            {
                "success": True,
                "filename": filename,
                "url": f"{settings.MEDIA_URL}sample_backgrounds/{filename}",
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

        filepath = os.path.join(SAMPLE_BACKGROUNDS_DIR, filename)

        # Security: ensure the file is within the sample backgrounds directory
        filepath = os.path.abspath(filepath)
        if not filepath.startswith(os.path.abspath(SAMPLE_BACKGROUNDS_DIR)):
            return HttpResponseForbidden()

        if os.path.exists(filepath):
            os.remove(filepath)
            invalidate_sample_backgrounds_cache()
            return JsonResponse({"success": True})
        else:
            return JsonResponse(
                {"success": False, "error": "File not found"}, status=404
            )

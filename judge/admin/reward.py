from django import forms
from django.contrib import admin
from django.contrib.admin import helpers
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.utils.translation import gettext_lazy as _
from django.template.response import TemplateResponse
from PIL import Image

from judge.models import AvatarFrame, ProfileFrame
from judge.utils.rewards import grant_manual_frame


class ManualFrameGrantForm(forms.Form):
    frame = forms.ModelChoiceField(
        queryset=AvatarFrame.objects.none(), label=_("Khung thưởng")
    )
    reason = forms.CharField(
        label=_("Lý do trao tặng"),
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["frame"].queryset = AvatarFrame.objects.filter(
            unlock_type=AvatarFrame.UnlockType.MANUAL, is_active=True
        )


def award_avatar_frame_action(model_admin, request, queryset):
    if not request.user.has_perm("judge.add_profileframe"):
        raise PermissionDenied
    form = ManualFrameGrantForm(request.POST if request.POST.get("apply") else None)
    if request.POST.get("apply") and form.is_valid():
        try:
            count = grant_manual_frame(
                form.cleaned_data["frame"],
                queryset,
                granted_by=request.user,
                reason=form.cleaned_data["reason"],
            )
        except (AvatarFrame.DoesNotExist, ValidationError):
            form.add_error("frame", _("Khung này không còn khả dụng để trao tặng."))
        else:
            model_admin.message_user(
                request,
                _(
                    "Đã cập nhật bộ sưu tập cho %(count)d tài khoản. Người đã có khung được giữ nguyên."
                )
                % {"count": count},
            )
            return None
    return TemplateResponse(
        request,
        "admin/judge/profile/award_avatar_frame.html",
        {
            **model_admin.admin_site.each_context(request),
            "title": _("Trao khung avatar"),
            "opts": model_admin.model._meta,
            "form": form,
            "recipient_count": queryset.count(),
            "recipient_preview": queryset.order_by("pk").values_list(
                "user__username", flat=True
            )[:10],
            "selected_ids": request.POST.getlist(helpers.ACTION_CHECKBOX_NAME),
            "select_across": "1" if request.POST.get("select_across") == "1" else "0",
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "media": model_admin.media + form.media,
        },
    )


class AvatarFrameForm(forms.ModelForm):
    class Meta:
        model = AvatarFrame
        fields = "__all__"

    def clean_artwork(self):
        artwork = self.cleaned_data.get("artwork")
        if not isinstance(artwork, UploadedFile):
            return artwork
        if artwork.size > 5 * 1024 * 1024:
            raise forms.ValidationError(_("Frame artwork must be at most 5 MB."))
        try:
            artwork.seek(0)
            with Image.open(artwork) as image:
                if image.format not in ("PNG", "WEBP"):
                    raise forms.ValidationError(_("Upload a PNG or WebP image."))
                alpha_range = image.convert("RGBA").getchannel("A").getextrema()
            if alpha_range[0] == 255 or alpha_range[1] == 0:
                raise forms.ValidationError(
                    _(
                        "Upload a visible frame with a transparent background and avatar opening."
                    )
                )
        finally:
            artwork.seek(0)
        return artwork


class AvatarFrameAdmin(admin.ModelAdmin):
    change_list_template = "admin/judge/reward/change_list.html"
    form = AvatarFrameForm
    list_display = ("name", "style", "unlock_type", "course", "is_active", "order")
    list_filter = ("is_active", "unlock_type", "style")
    search_fields = ("name", "slug", "course__slug")
    list_select_related = ("course",)
    prepopulated_fields = {"slug": ("name",)}


class ProfileFrameForm(forms.ModelForm):
    class Meta:
        model = ProfileFrame
        fields = ("profile", "frame", "reason")

    def clean(self):
        data = super().clean()
        frame = data.get("frame")
        if not self.instance.pk and frame:
            if frame.unlock_type != AvatarFrame.UnlockType.MANUAL:
                self.add_error("frame", _("Course frames are awarded automatically."))
            elif not frame.is_active:
                self.add_error("frame", _("This frame is inactive."))
        return data


class ProfileFrameAdmin(admin.ModelAdmin):
    change_list_template = "admin/judge/reward/change_list.html"
    form = ProfileFrameForm
    list_display = ("profile", "frame", "awarded_at", "granted_by", "is_equipped")
    list_filter = ("frame", "is_equipped")
    search_fields = ("profile__user__username", "reason")
    raw_id_fields = ("profile",)
    list_select_related = ("profile__user", "frame", "granted_by")
    readonly_fields = ("awarded_at", "granted_by", "is_equipped")
    fields = ("profile", "frame", "reason", "awarded_at", "granted_by", "is_equipped")

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (("profile", "frame") if obj else ())

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "frame":
            kwargs["queryset"] = AvatarFrame.objects.filter(
                unlock_type=AvatarFrame.UnlockType.MANUAL, is_active=True
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.granted_by = request.user
        super().save_model(request, obj, form, change)

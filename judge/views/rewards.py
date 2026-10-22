from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from judge.models import AvatarFrame, Profile, ProfileFrame
from judge.utils.rewards import invalidate_equipped_frame, sync_course_rewards


class AvatarFrameCollection(LoginRequiredMixin, TemplateView):
    template_name = "user/frames.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = self.request.profile
        progress = sync_course_rewards(profile)
        ownership = {
            award.frame_id: award
            for award in ProfileFrame.objects.filter(profile=profile)
        }
        frames = []
        for frame in AvatarFrame.objects.filter(is_active=True).select_related(
            "course"
        ):
            award = ownership.get(frame.pk)
            frames.append(
                {
                    "frame": frame,
                    "owned": award,
                    "equipped": bool(award and award.is_equipped),
                    "progress": progress.get(frame.pk),
                }
            )
        context.update(
            title=_("Bộ sưu tập"),
            user=profile,
            frames=frames,
            tab="frames",
            page_type="frames",
            owned_count=sum(bool(item["owned"]) for item in frames),
        )
        return context

    def post(self, request, *args, **kwargs):
        frame_id = request.POST.get("frame")
        if frame_id is None:
            return HttpResponseBadRequest(_("Thiếu lựa chọn khung."))
        if frame_id:
            try:
                frame_id = int(frame_id)
            except (ValueError, TypeError):
                return HttpResponseBadRequest(_("Khung không hợp lệ."))
            if not 0 < frame_id <= 2147483647:
                return HttpResponseBadRequest(_("Khung không hợp lệ."))

        with transaction.atomic():
            # Lock a stable row even when the user has never equipped a frame.
            Profile.objects.select_for_update().get(pk=request.profile.pk)
            awards = ProfileFrame.objects.filter(profile_id=request.profile.pk)
            selected = None
            if frame_id:
                selected = (
                    awards.select_for_update()
                    .filter(frame_id=frame_id, frame__is_active=True)
                    .first()
                )
                if selected is None:
                    return HttpResponseForbidden(_("Bạn chưa mở khóa khung này."))
            awards.filter(is_equipped=True).update(is_equipped=False)
            if selected is not None:
                selected.is_equipped = True
                selected.save(update_fields=["is_equipped"])
            invalidate_equipped_frame(request.profile.pk)
        return redirect("user_frames")

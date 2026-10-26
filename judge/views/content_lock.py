from django.apps import apps
from django.db import transaction
from django.http import JsonResponse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST


@require_POST
def toggle_content_lock(request, kind, object_id):
    if not request.user.is_authenticated or not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": _("Permission denied.")}, status=403
        )
    models = {
        "problem": "Problem",
        "contest": "Contest",
        "quiz": "Quiz",
        "question": "QuizQuestion",
        "post": "BlogPost",
    }
    if kind not in models or request.POST.get("locked") not in ("0", "1"):
        return JsonResponse(
            {"success": False, "error": _("Invalid lock request.")}, status=400
        )
    with transaction.atomic():
        model = apps.get_model("judge", models[kind])
        obj = model.objects.select_for_update().filter(pk=object_id).first()
        if obj is None:
            return JsonResponse(
                {"success": False, "error": _("Object not found.")}, status=404
            )
        obj.is_locked = request.POST["locked"] == "1"
        obj.save(update_fields=["is_locked"])
    return JsonResponse({"success": True, "locked": obj.is_locked})

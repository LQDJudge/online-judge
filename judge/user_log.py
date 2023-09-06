from django.utils.timezone import now
from django.conf import settings

from judge.models import Profile


class LogUserAccessMiddleware(object):
    def __init__(self, get_response=None):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if (
            hasattr(request, "user")
            and request.user.is_authenticated
            and not getattr(request, "no_profile_update", False)
        ):
            updates = {"last_access": now()}
            # Decided on using REMOTE_ADDR as nginx will translate it to the external IP that hits it.
            if request.META.get(settings.META_REMOTE_ADDRESS_KEY):
                updates["ip"] = request.META.get(settings.META_REMOTE_ADDRESS_KEY)
            Profile.objects.filter(user_id=request.user.pk).update(**updates)

        return response

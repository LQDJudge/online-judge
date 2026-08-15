from types import SimpleNamespace

from django.conf import settings

from celery import shared_task
from registration.models import RegistrationProfile, send_email

from judge.utils.deferred_email import send_mail_task  # noqa: F401


@shared_task
def send_registration_activation_email_task(user_id, site_name, site_domain, protocol):
    registration_profile = RegistrationProfile.objects.select_related("user").get(
        user_id=user_id
    )
    site = SimpleNamespace(name=site_name, domain=site_domain)
    context = {
        "user": registration_profile.user,
        "activation_key": registration_profile.activation_key,
        "expiration_days": settings.ACCOUNT_ACTIVATION_DAYS,
        "site": site,
        "protocol": protocol,
        "domain": site_domain,
        "site_name": settings.SITE_NAME,
        "SITE_NAME": settings.SITE_NAME,
    }
    send_email(
        [registration_profile.user.email],
        context,
        getattr(
            settings,
            "ACTIVATION_EMAIL_SUBJECT",
            "registration/activation_email_subject.txt",
        ),
        getattr(settings, "ACTIVATION_EMAIL_BODY", "registration/activation_email.txt"),
        getattr(
            settings, "ACTIVATION_EMAIL_HTML", "registration/activation_email.html"
        ),
    )

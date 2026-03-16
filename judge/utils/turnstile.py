import requests

from django import forms
from django.conf import settings
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def is_turnstile_configured():
    return bool(
        getattr(settings, "TURNSTILE_SITE_KEY", None)
        and getattr(settings, "TURNSTILE_SECRET_KEY", None)
    )


class TurnstileWidget(forms.Widget):
    def render(self, name, value, attrs=None, renderer=None):
        site_key = settings.TURNSTILE_SITE_KEY
        return mark_safe(
            f'<div id="turnstile-container"></div>'
            f"<script>"
            f"function onloadTurnstileCallback(){{turnstile.render('#turnstile-container',{{sitekey:'{site_key}'}});}}"
            f"</script>"
            f'<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=onloadTurnstileCallback" async defer></script>'
        )

    def value_from_datadict(self, data, files, name):
        return data.get("cf-turnstile-response", "")


class TurnstileField(forms.Field):
    widget = TurnstileWidget

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label", "")
        kwargs.setdefault("required", True)
        super().__init__(*args, **kwargs)

    def clean(self, value):
        if not value:
            raise forms.ValidationError(_("Please complete the captcha."))

        try:
            response = requests.post(
                TURNSTILE_VERIFY_URL,
                data={
                    "secret": settings.TURNSTILE_SECRET_KEY,
                    "response": value,
                },
                timeout=10,
            )
            result = response.json()
        except (requests.RequestException, ValueError):
            raise forms.ValidationError(
                _("Captcha verification failed. Please try again.")
            )

        if not result.get("success"):
            raise forms.ValidationError(
                _("Captcha verification failed. Please try again.")
            )

        return value

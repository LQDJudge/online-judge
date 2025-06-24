# coding=utf-8
import re

from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import get_default_password_validators
from django.forms import ChoiceField, ModelChoiceField
from django.shortcuts import render
from django.utils.translation import gettext, gettext_lazy as _
from registration.backends.default.views import (
    ActivationView as OldActivationView,
    RegistrationView as OldRegistrationView,
)
from celery import shared_task
from registration.forms import RegistrationForm

from judge.models import Language, Profile, TIMEZONE
from judge.utils.recaptcha import ReCaptchaField, ReCaptchaWidget
from judge.widgets import Select2Widget

valid_id = re.compile(r"^\w+$")
bad_mail_regex = list(map(re.compile, settings.BAD_MAIL_PROVIDER_REGEX))


class CustomRegistrationForm(RegistrationForm):
    username = forms.RegexField(
        regex=r"^\w+$",
        max_length=30,
        label=_("Username"),
        error_messages={
            "invalid": _("A username must contain letters, " "numbers, or underscores")
        },
    )
    timezone = ChoiceField(
        label=_("Timezone"),
        choices=TIMEZONE,
        widget=Select2Widget(attrs={"style": "width:100%"}),
    )
    language = ModelChoiceField(
        queryset=Language.objects.all(),
        label=_("Preferred language"),
        empty_label=None,
        widget=Select2Widget(attrs={"style": "width:100%"}),
    )

    if ReCaptchaField is not None:
        captcha = ReCaptchaField(widget=ReCaptchaWidget())

    def clean_email(self):
        if User.objects.filter(email=self.cleaned_data["email"]).exists():
            raise forms.ValidationError(
                gettext(
                    'The email address "%s" is already taken. Only one registration '
                    "is allowed per address."
                )
                % self.cleaned_data["email"]
            )
        if "@" in self.cleaned_data["email"]:
            domain = self.cleaned_data["email"].split("@")[-1].lower()
            if domain in settings.BAD_MAIL_PROVIDERS or any(
                regex.match(domain) for regex in bad_mail_regex
            ):
                raise forms.ValidationError(
                    gettext(
                        "Your email provider is not allowed due to history of abuse. "
                        "Please use a reputable email provider."
                    )
                )
        return self.cleaned_data["email"]


class RegistrationView(OldRegistrationView):
    title = _("Registration")
    form_class = CustomRegistrationForm
    template_name = "registration/registration_form.html"

    def get_context_data(self, **kwargs):
        if "title" not in kwargs:
            kwargs["title"] = self.title
        tzmap = settings.TIMEZONE_MAP
        kwargs["TIMEZONE_MAP"] = tzmap or "http://momentjs.com/static/img/world.png"
        kwargs["TIMEZONE_BG"] = settings.TIMEZONE_BG if tzmap else "#4E7CAD"
        kwargs["password_validators"] = get_default_password_validators()
        kwargs["tos_url"] = settings.TERMS_OF_SERVICE_URL
        return super(RegistrationView, self).get_context_data(**kwargs)

    def register(self, form):
        user = super(RegistrationView, self).register(form)
        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={
                "language": Language.get_default_language(),
            },
        )

        cleaned_data = form.cleaned_data
        profile.timezone = cleaned_data["timezone"]
        profile.language = cleaned_data["language"]
        profile.save()

        self.send_activation_email_task.delay(user.id)
        return user

    def send_activation_email(self, user):
        pass

    @shared_task
    def send_activation_email_task(user_id):
        user = User.objects.get(id=user_id)
        registration_view = OldRegistrationView()
        registration_view.send_activation_email(user)

    def get_initial(self, *args, **kwargs):
        initial = super(RegistrationView, self).get_initial(*args, **kwargs)
        initial["timezone"] = settings.DEFAULT_USER_TIME_ZONE
        initial["language"] = Language.objects.get(key=settings.DEFAULT_USER_LANGUAGE)
        return initial


class ActivationView(OldActivationView):
    title = _("Registration")
    template_name = "registration/activate.html"

    def get_context_data(self, **kwargs):
        if "title" not in kwargs:
            kwargs["title"] = self.title
        return super(ActivationView, self).get_context_data(**kwargs)


def social_auth_error(request):
    return render(
        request,
        "generic-message.html",
        {
            "title": gettext("Authentication failure"),
            "message": request.GET.get("message"),
        },
    )

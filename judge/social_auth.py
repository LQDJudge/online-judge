import logging
from operator import itemgetter
from urllib.parse import quote

from django import forms
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from requests import HTTPError
from reversion import revisions
from social_core.backends.github import GithubOAuth2
from social_core.exceptions import InvalidEmail, SocialAuthBaseException
from social_core.pipeline.partial import partial
from social_django.middleware import (
    SocialAuthExceptionMiddleware as OldSocialAuthExceptionMiddleware,
)

from judge.forms import ProfileForm
from judge.models import Language, Profile
from judge.validators import (
    USERNAME_ALLOWED_MESSAGE,
    clean_username as clean_username_value,
    is_allowed_username_char,
    normalize_username,
)

logger = logging.getLogger("judge.social_auth")


class GitHubSecureEmailOAuth2(GithubOAuth2):
    name = "github-secure"

    def user_data(self, access_token, *args, **kwargs):
        data = self._user_data(access_token)
        try:
            emails = self._user_data(access_token, "/emails")
        except (HTTPError, ValueError, TypeError):
            emails = []

        emails = [
            (e.get("email"), e.get("primary"), 0)
            for e in emails
            if isinstance(e, dict) and e.get("verified")
        ]
        emails.sort(key=itemgetter(1), reverse=True)
        emails = list(map(itemgetter(0), emails))

        if emails:
            data["email"] = emails[0]
        else:
            data["email"] = None

        return data


def slugify_username(username):
    username = normalize_username(username)
    return "".join(
        char for char in username.replace("-", "_") if is_allowed_username_char(char)
    )


def verify_email(backend, details, *args, **kwargs):
    if not details["email"]:
        raise InvalidEmail(backend)


class UsernameForm(forms.Form):
    username = forms.CharField(
        max_length=30,
        label="Username",
        help_text=USERNAME_ALLOWED_MESSAGE,
    )

    def clean_username(self):
        username = clean_username_value(self.cleaned_data["username"])
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError(_("Sorry, the username is taken."))
        return username


@partial
def choose_username(backend, user, username=None, *args, **kwargs):
    if not user:
        request = backend.strategy.request
        if request.POST:
            form = UsernameForm(request.POST)
            if form.is_valid():
                return {"username": form.cleaned_data["username"]}
        else:
            form = UsernameForm(initial={"username": username})
        return render(
            request,
            "registration/username_select.html",
            {
                "title": _("Choose a username"),
                "form": form,
            },
        )


@partial
def make_profile(backend, user, response, is_new=False, *args, **kwargs):
    if is_new:
        if not hasattr(user, "profile"):
            profile = Profile(user=user)
            profile.language_id = Language.get_default_language_pk()
            logger.info("Info from %s: %s", backend.name, response)
            profile.save()
            form = ProfileForm(instance=profile, profile=profile)
        else:
            data = backend.strategy.request_data()
            logger.info(data)
            form = ProfileForm(data, instance=user.profile, profile=user.profile)
            if form.is_valid():
                with transaction.atomic(), revisions.create_revision():
                    form.save()
                    revisions.set_user(user)
                    revisions.set_comment("Updated on registration")
                    return
        return render(
            backend.strategy.request,
            "registration/profile_creation.html",
            {
                "title": _("Create your profile"),
                "form": form,
            },
        )


class SocialAuthExceptionMiddleware(OldSocialAuthExceptionMiddleware):
    def process_exception(self, request, exception):
        if isinstance(exception, SocialAuthBaseException):
            return HttpResponseRedirect(
                "%s?message=%s"
                % (
                    reverse("social_auth_error"),
                    quote(self.get_message(request, exception)),
                )
            )

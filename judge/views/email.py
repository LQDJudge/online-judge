from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.shortcuts import render, redirect
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.conf import settings
from django import forms
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.hashers import check_password

from urllib.parse import urlencode, urlunparse, urlparse

from judge.models import Profile
from judge.utils.email_render import render_email_message


class EmailChangeForm(forms.Form):
    new_email = forms.EmailField(label=_("New Email"))
    password = forms.CharField(label=_("Password"), widget=forms.PasswordInput)

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

    def clean_new_email(self):
        new_email = self.cleaned_data.get("new_email")
        if User.objects.filter(email=new_email).exists():
            raise forms.ValidationError(_("An account with this email already exists."))
        return new_email

    def clean_password(self):
        password = self.cleaned_data.get("password")
        if not self.user.check_password(password):
            raise forms.ValidationError("Invalid password")
        return password


@login_required
def email_change_view(request):
    form = EmailChangeForm(request.POST or None, user=request.user)

    if request.method == "POST" and form.is_valid():
        new_email = request.POST.get("new_email")
        user = request.user
        profile = request.profile

        # Generate a token for email verification
        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(bytes(user.pk))

        # Send the email to the user
        subject = settings.SITE_NAME + " - " + _("Email Change Request")
        email_contexts = {
            "message": _(
                "We have received a request to change your email to this email. Click the button below to change your email:"
            ),
            "title": _("Email Change"),
            "button_text": _("Change Email"),
            "url_path": reverse(
                "email_change_verify", kwargs={"uidb64": uid, "token": token}
            ),
        }
        message = render_email_message(request, email_contexts)
        send_mail(
            subject,
            message,
            settings.EMAIL_HOST_USER,
            [new_email],
            html_message=message,
        )
        profile.email_change_pending = new_email
        profile.save()
        return redirect("email_change_pending")

    return render(
        request,
        "email_change/email_change.html",
        {
            "form": form,
            "title": _("Change email"),
        },
    )


def verify_email_view(request, uidb64, token):
    try:
        uid = str(urlsafe_base64_decode(uidb64), encoding="utf-8")
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None
    if user is not None and default_token_generator.check_token(user, token):
        profile = Profile.objects.get(user=user)
        new_email = profile.email_change_pending
        if new_email and not User.objects.filter(email=new_email).exists():
            user.email = new_email
            profile.email_change_pending = None
            user.save()
            profile.save()

            return render(
                request,
                "email_change/email_change_success.html",
                {"title": _("Success"), "user": user},
            )

    return render(
        request, "email_change/email_change_failure.html", {"title": _("Invalid")}
    )


def email_change_pending_view(request):
    return render(
        request,
        "email_change/email_change_pending.html",
        {
            "title": _("Email change pending"),
        },
    )
